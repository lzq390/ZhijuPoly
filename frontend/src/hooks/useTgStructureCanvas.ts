import { useEffect, useRef, useState } from "react";
import { recognizeStructureImage, standardizeSmiles } from "../services/api";
import type { StructureWorkspaceContext } from "../types";

type KetcherApi = NonNullable<Window["ketcher"]>;

type KetcherSnapshot = {
  smiles: string;
  molfile: string;
};

type CanvasImageCache = {
  sourceKey: string;
  blob: Blob;
};

const TG_CANVAS_IMAGE_MAX_DIMENSION = 1600;
const TG_CANVAS_IMAGE_MAX_BYTES = 5 * 1024 * 1024;
const TG_CANVAS_RENDER_TIMEOUT_MS = 8000;
const TG_CANVAS_KET_STABILITY_TIMEOUT_MS = 640;
const TG_CANVAS_KET_RETRY_INTERVAL_MS = 80;

type SyncOptions = {
  preserveExisting?: boolean;
  quiet?: boolean;
};

type CanvasMutationOptions = {
  isCurrent?: () => boolean;
};

type UseTgStructureCanvasOptions = {
  structure: StructureWorkspaceContext;
  onStructureChanged: () => void;
};

export type TgCanvasPeekState = {
  smiles: string;
  canvasDirty: boolean;
  editorReady: boolean;
  viewMode: "2d" | "3d";
  busy: boolean;
  revisionKey: string;
};

export function wildcardCount(value: string) {
  return value.match(/\*/g)?.length ?? 0;
}

export function shouldAdoptEditorSmiles(sourceSmiles: string, editorSmiles: string) {
  const normalizedEditor = editorSmiles.trim();
  if (!normalizedEditor) {
    return false;
  }
  const sourceWildcardCount = wildcardCount(sourceSmiles);
  return sourceWildcardCount === 0 || wildcardCount(normalizedEditor) >= sourceWildcardCount;
}

export function isProtectedCanvasConsistent(sourceSmiles: string, editorSmiles: string) {
  const source = sourceSmiles.trim();
  const editor = editorSmiles.trim();
  if (source === editor) return true;
  if (!source || !editor || wildcardCount(source) === 0) return false;
  return source.replace(/\*/g, "").trim() === editor;
}

export function stripKetcherSelectedFields(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stripKetcherSelectedFields);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .filter(([key]) => key !== "selected")
      .map(([key, nested]) => [key, stripKetcherSelectedFields(nested)])
  );
}

export function isEmptyKetcherDocument(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const root = (value as Record<string, unknown>).root;
  if (!root || typeof root !== "object" || Array.isArray(root)) return false;
  const record = root as Record<string, unknown>;
  const collections = [record.nodes, record.connections];
  return collections.every((collection) => !Array.isArray(collection) || collection.length === 0);
}

function parseKetcherDocument(value: string): unknown | null {
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function molfileHasAtoms(value: string) {
  if (!value) return false;
  const v3000Count = value.match(/M\s+V30\s+COUNTS\s+(\d+)/i);
  if (v3000Count) return Number.parseInt(v3000Count[1], 10) > 0;
  const v2000CountsLine = value
    .replace(/\r/g, "")
    .split("\n")
    .find((line) => /\bV2000\b/i.test(line));
  if (!v2000CountsLine) return false;
  const atomCount = Number.parseInt(v2000CountsLine.slice(0, 3).trim(), 10);
  return Number.isFinite(atomCount) && atomCount > 0;
}

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a] as const;

export async function adoptKetcherPng(value: unknown): Promise<Blob> {
  if (!value || typeof value !== "object") {
    throw new Error("Ketcher 未生成有效的画板图片。");
  }
  const candidate = value as {
    size?: unknown;
    type?: unknown;
    arrayBuffer?: unknown;
  };
  if (
    typeof candidate.size !== "number" ||
    !Number.isFinite(candidate.size) ||
    candidate.size <= 0 ||
    typeof candidate.arrayBuffer !== "function"
  ) {
    throw new Error("Ketcher 未生成有效的画板图片。");
  }
  if (candidate.type !== undefined && candidate.type !== "" && candidate.type !== "image/png") {
    throw new Error("Ketcher 返回了非 PNG 画板图片。");
  }
  const buffer = await (candidate.arrayBuffer as () => Promise<ArrayBuffer>).call(value);
  const bytes = new Uint8Array(buffer);
  if (
    bytes.byteLength !== candidate.size ||
    PNG_SIGNATURE.some((expected, index) => bytes[index] !== expected)
  ) {
    throw new Error("Ketcher 返回的画板 PNG 已损坏。");
  }
  // Ketcher lives in an iframe, so its Blob belongs to a different JavaScript
  // realm and fails `instanceof window.Blob` in the React parent page. Rebuild
  // it here to give downstream browser APIs a parent-realm Blob.
  return new Blob([bytes], { type: "image/png" });
}

function abortError() {
  return new DOMException("画板图片捕获已取消。", "AbortError");
}

async function withTimeout<T>(
  promise: Promise<T>,
  timeoutMs: number,
  signal?: AbortSignal
): Promise<T> {
  if (signal?.aborted) throw abortError();
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const cleanup = () => {
      window.clearTimeout(timeout);
      signal?.removeEventListener("abort", handleAbort);
    };
    const resolveOnce = (value: T) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(value);
    };
    const rejectOnce = (error: unknown) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(error);
    };
    const timeout = window.setTimeout(
      () => rejectOnce(new Error("画板图片生成超时。")),
      timeoutMs
    );
    const handleAbort = () => rejectOnce(abortError());
    signal?.addEventListener("abort", handleAbort, { once: true });
    promise.then(resolveOnce, rejectOnce);
  });
}

async function decodeCanvasImage(blob: Blob) {
  if (typeof createImageBitmap === "function") {
    try {
      const bitmap = await createImageBitmap(blob);
      return {
        source: bitmap as CanvasImageSource,
        width: bitmap.width,
        height: bitmap.height,
        dispose: () => bitmap.close()
      };
    } catch {
      // Older WebViews may expose createImageBitmap but reject Blob decoding.
      // The object-URL path below provides the same bounded rasterization.
    }
  }
  const url = URL.createObjectURL(blob);
  const image = new Image();
  try {
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve();
      image.onerror = () => reject(new Error("画板图片解码失败。"));
      image.src = url;
    });
    return {
      source: image as CanvasImageSource,
      width: image.naturalWidth,
      height: image.naturalHeight,
      dispose: () => URL.revokeObjectURL(url)
    };
  } catch (error) {
    URL.revokeObjectURL(url);
    throw error;
  }
}

function canvasToPngBlob(canvas: HTMLCanvasElement) {
  return new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("浏览器无法编码画板图片。"));
    }, "image/png");
  });
}

export async function normalizeTgCanvasImage(
  sourceBlob: Blob,
  maxDimension = TG_CANVAS_IMAGE_MAX_DIMENSION,
  maxBytes = TG_CANVAS_IMAGE_MAX_BYTES
) {
  const decoded = await decodeCanvasImage(sourceBlob);
  try {
    if (!decoded.width || !decoded.height) throw new Error("画板图片尺寸无效。");
    let scale = Math.min(1, (maxDimension - 128) / Math.max(decoded.width, decoded.height));
    let latest: Blob | null = null;
    for (let attempt = 0; attempt < 8; attempt += 1) {
      const contentWidth = Math.max(1, Math.round(decoded.width * scale));
      const contentHeight = Math.max(1, Math.round(decoded.height * scale));
      const padding = Math.min(64, Math.max(24, Math.round(Math.max(contentWidth, contentHeight) * 0.05)));
      const canvas = document.createElement("canvas");
      canvas.width = Math.min(maxDimension, contentWidth + padding * 2);
      canvas.height = Math.min(maxDimension, contentHeight + padding * 2);
      const context = canvas.getContext("2d");
      if (!context) throw new Error("浏览器无法规范化画板图片。");
      context.fillStyle = "#ffffff";
      context.fillRect(0, 0, canvas.width, canvas.height);
      const drawWidth = Math.min(contentWidth, canvas.width - padding * 2);
      const drawHeight = Math.min(contentHeight, canvas.height - padding * 2);
      context.drawImage(
        decoded.source,
        Math.round((canvas.width - drawWidth) / 2),
        Math.round((canvas.height - drawHeight) / 2),
        drawWidth,
        drawHeight
      );
      latest = await canvasToPngBlob(canvas);
      if (latest.size <= maxBytes) return latest;
      scale *= 0.8;
    }
    throw new Error(`画板图片超过 ${Math.round(maxBytes / 1024 / 1024)} MiB。`);
  } finally {
    decoded.dispose();
  }
}

function getEditorLoadCandidates(sourceSmiles: string) {
  const normalized = sourceSmiles.trim();
  const stripped = normalized.replace(/\*/g, "").trim();
  return Array.from(new Set([normalized, stripped])).filter(Boolean);
}

function delay(ms: number) {
  return new Promise<void>((resolve) => window.setTimeout(resolve, ms));
}

export function useTgStructureCanvas({
  structure,
  onStructureChanged
}: UseTgStructureCanvasOptions) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const smilesRef = useRef(structure.smiles);
  const importAbortRef = useRef<AbortController | null>(null);
  const canvasImageCacheRef = useRef<CanvasImageCache | null>(null);
  const canonicalSmilesCacheRef = useRef(new Map<string, Promise<string | null>>());
  const flipTimerRef = useRef<number | null>(null);
  const copyTimerRef = useRef<number | null>(null);
  const [isEditorReady, setIsEditorReady] = useState(false);
  const [editorLoadRevision, setEditorLoadRevision] = useState(0);
  const [isFlipped, setIsFlipped] = useState(false);
  const [isFlipping, setIsFlipping] = useState(false);
  const [isImportingImage, setIsImportingImage] = useState(false);
  const [isLoadingStructure, setIsLoadingStructure] = useState(false);
  const [isClearing, setIsClearing] = useState(false);
  const [isSyncing, setIsSyncing] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");

  useEffect(() => {
    smilesRef.current = structure.smiles;
  }, [structure.smiles]);

  useEffect(() => () => {
    canvasImageCacheRef.current = null;
    canonicalSmilesCacheRef.current.clear();
    if (flipTimerRef.current !== null) window.clearTimeout(flipTimerRef.current);
    if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
  }, []);

  function getKetcher() {
    return structure.iframeRef.current?.contentWindow?.ketcher;
  }

  function refreshKetcherFrame() {
    const frameWindow = structure.iframeRef.current?.contentWindow;
    if (!frameWindow) {
      return;
    }
    const FrameEvent = (frameWindow as Window & typeof globalThis).Event;
    frameWindow.dispatchEvent(new FrameEvent("resize"));
    frameWindow.scrollTo(0, 0);
  }

  function handleEditorLoad() {
    setIsEditorReady(false);
    structure.setIsReady(false);
    setEditorLoadRevision((current) => current + 1);
  }

  async function waitForKetcherCommit() {
    await delay(80);
    refreshKetcherFrame();
    await delay(80);
  }

  async function waitForKetcher(timeoutMs = 4000): Promise<KetcherApi | null> {
    const startedAt = Date.now();
    while (Date.now() - startedAt < timeoutMs) {
      const ketcher = getKetcher();
      if (ketcher && typeof ketcher.getSmiles === "function") {
        return ketcher;
      }
      await delay(120);
    }
    return null;
  }

  async function readEditorSmiles(ketcher: KetcherApi) {
    const editorSmiles = (await ketcher.getSmiles()).trim();
    return editorSmiles && editorSmiles !== "{}" ? editorSmiles : "";
  }

  async function readEditorMolfile(ketcher: KetcherApi) {
    if (typeof ketcher.getMolfile !== "function") {
      return "";
    }
    return (await ketcher.getMolfile()).trim();
  }

  async function waitForEditorSmilesState(
    ketcher: KetcherApi,
    matches: (value: string) => boolean,
    timeoutMs = 1200
  ) {
    const startedAt = Date.now();
    let latest = await readEditorSmiles(ketcher);
    while (Date.now() - startedAt < timeoutMs) {
      if (matches(latest)) {
        return latest;
      }
      await delay(80);
      latest = await readEditorSmiles(ketcher);
    }
    return latest;
  }

  function applySmiles(nextValue: string, notify = true) {
    const normalized = nextValue.trim();
    const changed = normalized !== smilesRef.current.trim();
    smilesRef.current = normalized;
    structure.setSmiles(normalized);
    if (changed && notify) {
      canvasImageCacheRef.current = null;
      onStructureChanged();
    }
    return normalized;
  }

  function canonicalizeSmiles(value: string): Promise<string | null> {
    const normalized = value.trim();
    if (!normalized) return Promise.resolve("");
    const cached = canonicalSmilesCacheRef.current.get(normalized);
    if (cached) return cached;
    const pending = standardizeSmiles({ smiles: normalized })
      .then((result) => result.standardized_smiles.trim())
      .catch(() => null);
    canonicalSmilesCacheRef.current.set(normalized, pending);
    if (canonicalSmilesCacheRef.current.size > 24) {
      const oldest = canonicalSmilesCacheRef.current.keys().next().value;
      if (typeof oldest === "string") canonicalSmilesCacheRef.current.delete(oldest);
    }
    return pending;
  }

  async function captureEditorSnapshot(ketcher: KetcherApi): Promise<KetcherSnapshot> {
    const fallbackSmiles = smilesRef.current.trim();
    let editorSmiles = fallbackSmiles;
    let molfile = "";
    try {
      editorSmiles = (await readEditorSmiles(ketcher)) || fallbackSmiles;
    } catch {
      editorSmiles = fallbackSmiles;
    }
    try {
      molfile = await readEditorMolfile(ketcher);
    } catch {
      molfile = "";
    }
    return { smiles: editorSmiles, molfile };
  }

  async function clearEditor(ketcher: KetcherApi) {
    if (typeof ketcher.clear === "function") {
      await ketcher.clear();
    } else if (typeof ketcher.setMolecule === "function") {
      await ketcher.setMolecule("");
    } else {
      throw new Error("结构编辑器无法清空画布。");
    }
    await waitForKetcherCommit();
  }

  async function writeImageStructure(ketcher: KetcherApi, source: string) {
    if (typeof ketcher.setMolecule !== "function") {
      throw new Error("结构编辑器无法加载识别结果。");
    }
    await clearEditor(ketcher);
    await ketcher.setMolecule(source);
    await waitForKetcherCommit();
    const editorSmiles = await waitForEditorSmilesState(ketcher, Boolean, 1800);
    if (!editorSmiles) {
      throw new Error("Ketcher 未接受识别出的结构。");
    }
    return editorSmiles;
  }

  async function loadRecognizedStructure(
    ketcher: KetcherApi,
    molfile: string,
    recognizedSmiles: string
  ) {
    if (molfile) {
      try {
        const editorSmiles = await writeImageStructure(ketcher, molfile);
        return shouldAdoptEditorSmiles(recognizedSmiles, editorSmiles)
          ? editorSmiles
          : recognizedSmiles || editorSmiles;
      } catch (error) {
        if (!recognizedSmiles) {
          throw error;
        }
      }
    }

    let lastError: unknown = null;
    for (const candidate of getEditorLoadCandidates(recognizedSmiles)) {
      try {
        const editorSmiles = await writeImageStructure(ketcher, candidate);
        return shouldAdoptEditorSmiles(recognizedSmiles, editorSmiles)
          ? editorSmiles
          : recognizedSmiles || editorSmiles;
      } catch (error) {
        lastError = error;
      }
    }
    throw lastError instanceof Error ? lastError : new Error("Ketcher 未接受识别出的结构。");
  }

  async function restoreEditorSnapshot(ketcher: KetcherApi, snapshot: KetcherSnapshot) {
    const source = snapshot.molfile || snapshot.smiles;
    if (!source) {
      await clearEditor(ketcher);
      applySmiles("", false);
      return;
    }
    if (typeof ketcher.setMolecule !== "function") {
      throw new Error("结构编辑器无法恢复原画布。");
    }
    await ketcher.setMolecule(source);
    await waitForKetcherCommit();
    let restoredSmiles = snapshot.smiles;
    try {
      const editorSmiles = await waitForEditorSmilesState(ketcher, Boolean);
      if (!snapshot.smiles || shouldAdoptEditorSmiles(snapshot.smiles, editorSmiles)) {
        restoredSmiles = editorSmiles || snapshot.smiles;
      }
    } catch {
      restoredSmiles = snapshot.smiles;
    }
    applySmiles(restoredSmiles, false);
  }

  useEffect(() => {
    let cancelled = false;
    let attempts = 0;
    let checking = false;

    const checkEditor = async () => {
      if (checking || cancelled) {
        return;
      }
      checking = true;
      attempts += 1;
      try {
        const ketcher = getKetcher();
        if (!ketcher || typeof ketcher.getSmiles !== "function") {
          return;
        }

        const sourceSmiles = smilesRef.current.trim();
        const editorSmiles = await readEditorSmiles(ketcher);
        if (!editorSmiles && sourceSmiles && typeof ketcher.setMolecule === "function") {
          let loaded = false;
          for (const candidate of getEditorLoadCandidates(sourceSmiles)) {
            try {
              await ketcher.setMolecule(candidate);
              await waitForKetcherCommit();
              loaded = true;
              break;
            } catch {
              // Try the next Ketcher-compatible representation.
            }
          }
          if (!loaded) {
            setFeedback("共享结构暂时无法恢复到 2D 画布。");
          }
        }
        if (!cancelled) {
          setIsEditorReady(true);
          structure.setIsReady(true);
        }
      } catch {
        if (!cancelled && attempts >= 50) {
          structure.setIsReady(false);
        }
      } finally {
        checking = false;
      }
    };

    void checkEditor();
    const timer = window.setInterval(() => {
      if (attempts >= 50 || isEditorReady) {
        window.clearInterval(timer);
        return;
      }
      void checkEditor();
    }, 300);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [editorLoadRevision, isEditorReady, structure.iframeRef, structure.setIsReady]);

  useEffect(() => {
    return () => {
      importAbortRef.current?.abort();
    };
  }, []);

  async function syncSmilesFromCanvas(options: SyncOptions = {}) {
    const ketcher = getKetcher();
    if (!ketcher) {
      structure.setIsReady(false);
      if (!options.quiet) {
        setFeedback("结构编辑器尚未就绪。");
      }
      return options.preserveExisting ? smilesRef.current.trim() : "";
    }

    setIsSyncing(true);
    try {
      const fallbackSmiles = smilesRef.current.trim();
      const editorSmiles = await readEditorSmiles(ketcher);
      if (editorSmiles) {
        const nextSmiles = shouldAdoptEditorSmiles(fallbackSmiles, editorSmiles)
          ? editorSmiles
          : fallbackSmiles || editorSmiles;
        applySmiles(nextSmiles);
        structure.setIsReady(true);
        if (!options.quiet) {
          setFeedback(
            nextSmiles === editorSmiles
              ? "SMILES 已从 2D 画布同步。"
              : "已保留含聚合物端基的共享 SMILES。"
          );
        }
        return nextSmiles;
      }
      if (options.preserveExisting && fallbackSmiles) {
        if (!options.quiet) {
          setFeedback("2D 画布为空，继续使用当前共享 SMILES。");
        }
        return fallbackSmiles;
      }
      applySmiles("");
      if (!options.quiet) {
        setFeedback("2D 画布暂无可同步结构。");
      }
      return "";
    } catch (error) {
      console.error("Failed to synchronize Tg Ketcher canvas", error);
      if (!options.quiet) {
        setFeedback("SMILES 同步失败，请检查结构画布。");
      }
      return options.preserveExisting ? smilesRef.current.trim() : "";
    } finally {
      setIsSyncing(false);
    }
  }

  async function clearCanvas(options: CanvasMutationOptions = {}) {
    const ketcher = getKetcher();
    if (!ketcher) {
      if (!structure.iframeRef.current) {
        applySmiles("");
        setIsFlipped(false);
        setFeedback("共享 SMILES 已清空。");
        return true;
      }
      setFeedback("结构编辑器尚未就绪。");
      return false;
    }
    if (options.isCurrent && !options.isCurrent()) return false;
    const previousFlipped = isFlipped;
    const previousSnapshot = await captureEditorSnapshot(ketcher);
    setIsClearing(true);
    try {
      await clearEditor(ketcher);
      if (options.isCurrent && !options.isCurrent()) {
        await restoreEditorSnapshot(ketcher, previousSnapshot);
        setIsFlipped(previousFlipped);
        return false;
      }
      applySmiles("");
      setIsFlipped(false);
      structure.setIsReady(true);
      setFeedback("画布与共享 SMILES 已清空。");
      return true;
    } catch (error) {
      console.error("Failed to clear Tg Ketcher canvas", error);
      setFeedback(error instanceof Error ? error.message : "画布清空失败。");
      return false;
    } finally {
      setIsClearing(false);
    }
  }

  async function loadStructure(sourceSmiles: string, options: CanvasMutationOptions = {}) {
    const normalizedSource = sourceSmiles.trim();
    if (!normalizedSource) {
      setFeedback("没有可加载的结构。");
      return false;
    }
    if (options.isCurrent && !options.isCurrent()) return false;

    const ketcher = await waitForKetcher();
    if (!ketcher) {
      structure.setIsReady(false);
      setFeedback("结构编辑器尚未就绪，请稍后重试。");
      return false;
    }

    const previousFlipped = isFlipped;
    const previousSnapshot = await captureEditorSnapshot(ketcher);
    setIsLoadingStructure(true);
    setFeedback("正在加载结构...");

    try {
      let editorSmiles = "";
      let lastError: unknown = null;
      for (const candidate of getEditorLoadCandidates(normalizedSource)) {
        if (options.isCurrent && !options.isCurrent()) break;
        try {
          editorSmiles = await writeImageStructure(ketcher, candidate);
          break;
        } catch (error) {
          lastError = error;
        }
      }
      if (!editorSmiles) {
        throw lastError instanceof Error
          ? lastError
          : new Error("Ketcher 未接受待加载结构。");
      }
      if (options.isCurrent && !options.isCurrent()) {
        await restoreEditorSnapshot(ketcher, previousSnapshot);
        setIsFlipped(previousFlipped);
        return false;
      }

      const nextSmiles = shouldAdoptEditorSmiles(normalizedSource, editorSmiles)
        ? editorSmiles
        : normalizedSource;
      setIsFlipped(false);
      applySmiles(nextSmiles);
      structure.setIsReady(true);
      setFeedback(
        nextSmiles === editorSmiles
          ? "结构已加载到 2D 画布。"
          : "结构已加载，并保留共享 SMILES 中的聚合物端基。"
      );
      return true;
    } catch (error) {
      console.error("Failed to load structure into Tg Ketcher canvas", error);
      const message = error instanceof Error ? error.message : "结构加载失败。";
      try {
        await restoreEditorSnapshot(ketcher, previousSnapshot);
        setIsFlipped(previousFlipped);
        structure.setIsReady(true);
        setFeedback(`${message} 已恢复原画布。`);
      } catch (restoreError) {
        console.error("Failed to restore Tg Ketcher canvas", restoreError);
        setFeedback(`${message} 原画布恢复失败，请手动检查。`);
      }
      return false;
    } finally {
      setIsLoadingStructure(false);
    }
  }

  async function applyTextStructure(sourceSmiles: string) {
    const normalizedSource = sourceSmiles.trim();
    if (!normalizedSource) {
      throw new Error("请输入要应用的 SMILES。");
    }

    const result = await standardizeSmiles({ smiles: normalizedSource });
    const standardized = result.standardized_smiles.trim();
    const reliableSmiles = shouldAdoptEditorSmiles(normalizedSource, standardized)
      ? standardized
      : normalizedSource;

    if (!structure.iframeRef.current) {
      setIsFlipped(false);
      applySmiles(reliableSmiles);
      setFeedback(
        reliableSmiles === standardized
          ? "结构已标准化并应用。"
          : "结构已应用，并保留聚合物端基。"
      );
      return { applied: true, smiles: reliableSmiles };
    }

    const applied = await loadStructure(reliableSmiles);
    return {
      applied,
      smiles: smilesRef.current.trim()
    };
  }

  async function importImageFile(file: File) {
    if (!file.type.startsWith("image/")) {
      setFeedback("请选择图片文件。");
      return false;
    }

    const ketcher = await waitForKetcher();
    if (!ketcher) {
      structure.setIsReady(false);
      setFeedback("结构编辑器尚未就绪，请稍后重试。");
      return false;
    }

    const previousFlipped = isFlipped;
    const previousSnapshot = await captureEditorSnapshot(ketcher);
    const controller = new AbortController();
    importAbortRef.current?.abort();
    importAbortRef.current = controller;
    setIsImportingImage(true);
    setFeedback("正在识别结构图片...");

    try {
      const result = await recognizeStructureImage(file, controller.signal);
      const molfile = result.molfile?.trim() ?? "";
      const recognizedSmiles = result.smiles.trim();
      if (!molfile && !recognizedSmiles) {
        throw new Error("识别结果未返回结构。");
      }
      setIsFlipped(false);
      setFeedback("正在写入 2D 画布...");
      const nextSmiles = await loadRecognizedStructure(ketcher, molfile, recognizedSmiles);
      applySmiles(nextSmiles);
      structure.setIsReady(true);
      setFeedback(
        result.warnings.length > 0
          ? `图片结构已导入：${result.warnings[0]}`
          : "图片结构已导入。"
      );
      return true;
    } catch (error) {
      if (controller.signal.aborted) {
        return false;
      }
      console.error("Failed to import structure image for Tg reverse design", error);
      const message = error instanceof Error ? error.message : "图片导入失败。";
      try {
        await restoreEditorSnapshot(ketcher, previousSnapshot);
        setIsFlipped(previousFlipped);
        structure.setIsReady(true);
        setFeedback(`${message} 已恢复原画布。`);
      } catch (restoreError) {
        console.error("Failed to restore Tg Ketcher canvas", restoreError);
        setFeedback(`${message} 原画布恢复失败，请手动检查。`);
      }
      return false;
    } finally {
      if (importAbortRef.current === controller) {
        importAbortRef.current = null;
        setIsImportingImage(false);
        if (fileInputRef.current) {
          fileInputRef.current.value = "";
        }
      }
    }
  }

  async function toggle3D() {
    if (isFlipping || isImportingImage || isClearing || isSyncing) {
      return false;
    }
    setIsFlipping(true);
    try {
      if (!isFlipped) {
        const nextSmiles = getKetcher()
          ? await syncSmilesFromCanvas({ preserveExisting: true })
          : smilesRef.current.trim();
        if (!nextSmiles) {
          setFeedback("请先绘制或导入聚合物结构。");
          return false;
        }
      }
      setIsFlipped((current) => !current);
      return true;
    } finally {
      if (flipTimerRef.current !== null) window.clearTimeout(flipTimerRef.current);
      flipTimerRef.current = window.setTimeout(() => {
        flipTimerRef.current = null;
        setIsFlipping(false);
      }, 180);
    }
  }

  async function resolveSmilesForSearch() {
    const synchronized = await syncSmilesFromCanvas({
      preserveExisting: true,
      quiet: true
    });
    if (!synchronized) {
      setFeedback("请先绘制或导入聚合物结构。");
      return "";
    }
    try {
      const result = await standardizeSmiles({ smiles: synchronized });
      const standardized = result.standardized_smiles.trim();
      const reliableSmiles = shouldAdoptEditorSmiles(synchronized, standardized)
        ? standardized
        : synchronized;
      applySmiles(reliableSmiles);
      return reliableSmiles;
    } catch (error) {
      console.error("Failed to standardize Tg search SMILES", error);
      setFeedback("SMILES 标准化失败，搜索未提交。请检查结构有效性后重试。");
      return "";
    }
  }

  async function captureCanvasImage(signal?: AbortSignal): Promise<Blob | null> {
    const ketcher = getKetcher();
    if (!ketcher || typeof ketcher.getKet !== "function" || typeof ketcher.generateImage !== "function") {
      throw new Error("画板图片接口尚未就绪，AI 将使用 SMILES 作为兜底。请稍后可重试。");
    }
    try {
      let sourceKet = await withTimeout(ketcher.getKet(), TG_CANVAS_RENDER_TIMEOUT_MS, signal);
      if (signal?.aborted) throw abortError();
      let parsed = parseKetcherDocument(sourceKet);
      let renderSource = sourceKet;
      let sourceKey = `ket:${sourceKet}`;
      if (parsed && isEmptyKetcherDocument(parsed)) {
        let editorSmiles = "";
        try {
          editorSmiles = await withTimeout(
            readEditorSmiles(ketcher),
            TG_CANVAS_RENDER_TIMEOUT_MS,
            signal
          );
        } catch (error) {
          if (error instanceof DOMException && error.name === "AbortError") throw error;
        }
        const sharedSmiles = smilesRef.current.trim();
        if (!editorSmiles && !sharedSmiles) {
          canvasImageCacheRef.current = null;
          return null;
        }

        // Ketcher may expose the new SMILES a few frames before getKet() has
        // committed its nodes. Retry briefly so an immediate AI request does
        // not mistake a populated editor for an empty canvas.
        const retryDeadline = Date.now() + TG_CANVAS_KET_STABILITY_TIMEOUT_MS;
        while (Date.now() < retryDeadline && parsed && isEmptyKetcherDocument(parsed)) {
          const remaining = retryDeadline - Date.now();
          await withTimeout(
            delay(Math.min(TG_CANVAS_KET_RETRY_INTERVAL_MS, remaining)),
            Math.min(TG_CANVAS_KET_RETRY_INTERVAL_MS, remaining) + 100,
            signal
          );
          try {
            sourceKet = await withTimeout(
              ketcher.getKet(),
              Math.max(1, retryDeadline - Date.now()),
              signal
            );
            parsed = parseKetcherDocument(sourceKet);
          } catch (error) {
            if (error instanceof DOMException && error.name === "AbortError") throw error;
            break;
          }
        }

        if (parsed && isEmptyKetcherDocument(parsed)) {
          let molfile = "";
          try {
            molfile = await withTimeout(
              readEditorMolfile(ketcher),
              TG_CANVAS_RENDER_TIMEOUT_MS,
              signal
            );
          } catch (error) {
            if (error instanceof DOMException && error.name === "AbortError") throw error;
          }
          if (!molfileHasAtoms(molfile)) {
            throw new Error("画板结构尚未完成图像提交，请稍后重试。");
          }
          // Molfile remains local and is only handed back to Ketcher's image
          // renderer. The multipart request still receives PNG bytes only.
          renderSource = molfile;
          sourceKey = `molfile:${molfile}`;
          parsed = null;
        }
      }
      const cleanedKet = parsed
        ? JSON.stringify(stripKetcherSelectedFields(parsed))
        : renderSource;
      if (parsed) sourceKey = `ket:${cleanedKet}`;
      const cached = canvasImageCacheRef.current;
      if (cached?.sourceKey === sourceKey) return cached.blob;
      const rendered = await withTimeout(
        ketcher.generateImage(cleanedKet, {
          outputFormat: "png",
          backgroundColor: "255, 255, 255",
          "image-resolution": 144
        }),
        TG_CANVAS_RENDER_TIMEOUT_MS,
        signal
      );
      const parentRealmPng = await withTimeout(
        adoptKetcherPng(rendered),
        TG_CANVAS_RENDER_TIMEOUT_MS,
        signal
      );
      const normalized = await withTimeout(
        normalizeTgCanvasImage(parentRealmPng),
        TG_CANVAS_RENDER_TIMEOUT_MS,
        signal
      );
      canvasImageCacheRef.current = { sourceKey, blob: normalized };
      return normalized;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      console.error("Failed to capture Tg canvas image", error);
      setFeedback("画板快照生成失败，本轮 AI 将使用 SMILES 兜底；可稍后重试。");
      throw error instanceof Error ? error : new Error("画板快照生成失败。");
    }
  }

  async function peekCanvasState(): Promise<TgCanvasPeekState> {
    const sharedSmiles = smilesRef.current.trim();
    const ketcher = getKetcher();
    if (!ketcher) {
      return {
        smiles: sharedSmiles,
        canvasDirty: false,
        editorReady: false,
        viewMode: isFlipped ? "3d" : "2d",
        busy: isBusy,
        revisionKey: JSON.stringify([sharedSmiles, null, false, isFlipped, isBusy])
      };
    }
    try {
      const editorSmiles = await readEditorSmiles(ketcher);
      let canvasConsistent = isProtectedCanvasConsistent(sharedSmiles, editorSmiles);
      let sharedIdentity = sharedSmiles;
      let editorIdentity = editorSmiles;
      if (!canvasConsistent && sharedSmiles && editorSmiles) {
        const [canonicalShared, canonicalEditor] = await Promise.all([
          canonicalizeSmiles(sharedSmiles),
          canonicalizeSmiles(editorSmiles)
        ]);
        if (canonicalShared !== null) sharedIdentity = canonicalShared;
        if (canonicalEditor !== null) editorIdentity = canonicalEditor;
        canvasConsistent = canonicalShared !== null && canonicalShared === canonicalEditor;
      }
      const reliableSmiles = editorSmiles
        ? shouldAdoptEditorSmiles(sharedSmiles, editorSmiles)
          ? editorSmiles
          : sharedSmiles || editorSmiles
        : sharedSmiles;
      return {
        smiles: reliableSmiles,
        canvasDirty: !canvasConsistent,
        editorReady: isEditorReady,
        viewMode: isFlipped ? "3d" : "2d",
        busy: isBusy,
        revisionKey: JSON.stringify([sharedIdentity, editorIdentity, isEditorReady, isFlipped, isBusy])
      };
    } catch {
      return {
        smiles: sharedSmiles,
        // Fail closed: when Ketcher cannot be read we cannot prove that its
        // canvas still matches the shared SMILES used to ground an AI action.
        canvasDirty: true,
        editorReady: isEditorReady,
        viewMode: isFlipped ? "3d" : "2d",
        busy: isBusy,
        revisionKey: JSON.stringify([sharedSmiles, "read-error", isEditorReady, isFlipped, isBusy])
      };
    }
  }

  async function copySmiles(sourceValue?: string) {
    const value = (sourceValue ?? smilesRef.current).trim();
    if (!value) {
      return;
    }
    try {
      await navigator.clipboard.writeText(value);
      setCopyState("copied");
      setFeedback("SMILES 已复制。");
    } catch {
      setCopyState("failed");
      setFeedback("复制失败，请手动选择 SMILES。");
    } finally {
      if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
      copyTimerRef.current = window.setTimeout(() => {
        copyTimerRef.current = null;
        setCopyState("idle");
      }, 1200);
    }
  }

  const isBusy =
    isImportingImage ||
    isLoadingStructure ||
    isClearing ||
    isSyncing ||
    isFlipping;

  return {
    fileInputRef,
    handleEditorLoad,
    isEditorReady,
    isFlipped,
    isFlipping,
    isImportingImage,
    isLoadingStructure,
    isClearing,
    isSyncing,
    isBusy,
    feedback,
    setFeedback,
    copyState,
    loadStructure,
    applyTextStructure,
    clearCanvas,
    importImageFile,
    syncSmilesFromCanvas,
    toggle3D,
    peekCanvasState,
    captureCanvasImage,
    resolveSmilesForSearch,
    copySmiles
  };
}
