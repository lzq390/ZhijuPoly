import { useEffect, useRef, useState } from "react";
import { recognizeStructureImage, standardizeSmiles } from "../services/api";
import type { StructureWorkspaceContext } from "../types";

type KetcherApi = NonNullable<Window["ketcher"]>;

type KetcherSnapshot = {
  smiles: string;
  molfile: string;
};

type SyncOptions = {
  preserveExisting?: boolean;
  quiet?: boolean;
};

type UseTgStructureCanvasOptions = {
  structure: StructureWorkspaceContext;
  onStructureChanged: () => void;
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
  const [isEditorReady, setIsEditorReady] = useState(false);
  const [isFlipped, setIsFlipped] = useState(false);
  const [isFlipping, setIsFlipping] = useState(false);
  const [isImportingImage, setIsImportingImage] = useState(false);
  const [isClearing, setIsClearing] = useState(false);
  const [isSyncing, setIsSyncing] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");

  useEffect(() => {
    smilesRef.current = structure.smiles;
  }, [structure.smiles]);

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
      onStructureChanged();
    }
    return normalized;
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
  }, [isEditorReady, structure.iframeRef, structure.setIsReady]);

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

  async function clearCanvas() {
    const ketcher = getKetcher();
    if (!ketcher) {
      setFeedback("结构编辑器尚未就绪。");
      return false;
    }
    setIsClearing(true);
    try {
      await clearEditor(ketcher);
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
        const nextSmiles = await syncSmilesFromCanvas({ preserveExisting: true });
        if (!nextSmiles) {
          setFeedback("请先绘制或导入聚合物结构。");
          return false;
        }
      }
      setIsFlipped((current) => !current);
      return true;
    } finally {
      window.setTimeout(() => setIsFlipping(false), 620);
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
      setFeedback("SMILES 标准化失败，已使用当前可靠结构继续搜索。");
      return synchronized;
    }
  }

  async function copySmiles() {
    const value = smilesRef.current.trim();
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
      window.setTimeout(() => setCopyState("idle"), 1200);
    }
  }

  const isBusy = isImportingImage || isClearing || isSyncing || isFlipping;

  return {
    fileInputRef,
    handleEditorLoad,
    isEditorReady,
    isFlipped,
    isFlipping,
    isImportingImage,
    isClearing,
    isSyncing,
    isBusy,
    feedback,
    setFeedback,
    copyState,
    clearCanvas,
    importImageFile,
    syncSmilesFromCanvas,
    toggle3D,
    resolveSmilesForSearch,
    copySmiles
  };
}
