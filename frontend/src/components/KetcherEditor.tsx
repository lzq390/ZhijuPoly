import { Copy, Eraser, FlaskConical, ImagePlus, LoaderCircle, RefreshCcw, Sigma, Upload, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { recognizeStructureImage } from "../services/api";
import { cn } from "../lib/utils";
import { Button } from "./ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";
import { Textarea } from "./ui/textarea";

type KetcherApi = NonNullable<Window["ketcher"]>;
type LoadedStructureFormat = "molfile" | "smiles";
type RecognizedStructurePayload = {
  molfile: string;
  smiles: string;
};

type KetcherEditorProps = {
  smiles: string;
  iframeRef: React.RefObject<HTMLIFrameElement | null>;
  onReadyChange: (ready: boolean) => void;
  onChange: (value: string) => void;
  presetStructure?: {
    label: string;
    smiles: string;
  };
  smilesPlaceholder?: string;
  layout?: "stacked" | "split";
  tone?: "default" | "dark";
  className?: string;
  contentClassName?: string;
  frameClassName?: string;
  iframeClassName?: string;
  smilesPanelClassName?: string;
  smilesTextareaClassName?: string;
  showSmilesPanel?: boolean;
  showToolsBadge?: boolean;
  eyebrow?: string;
  title?: string;
};

export function KetcherEditor({
  smiles,
  iframeRef,
  onReadyChange,
  onChange,
  presetStructure,
  smilesPlaceholder = "例如：*CC*、CCO，或用于相似匹配的其他 SMILES",
  layout = "stacked",
  tone = "default",
  className,
  contentClassName,
  frameClassName,
  iframeClassName,
  smilesPanelClassName,
  smilesTextareaClassName,
  showSmilesPanel = true,
  showToolsBadge = true,
  eyebrow = "分子画布",
  title = "结构编辑器"
}: KetcherEditorProps) {
  const [isSyncing, setIsSyncing] = useState(false);
  const [isClearing, setIsClearing] = useState(false);
  const [isLoadingPreset, setIsLoadingPreset] = useState(false);
  const [isLoadingSmilesIntoEditor, setIsLoadingSmilesIntoEditor] = useState(false);
  const [copyLabel, setCopyLabel] = useState("复制");
  const [isImportingImage, setIsImportingImage] = useState(false);
  const [imagePreviewUrl, setImagePreviewUrl] = useState<string | null>(null);
  const [imageImportName, setImageImportName] = useState<string | null>(null);
  const [imageImportError, setImageImportError] = useState<string | null>(null);
  const [imageImportWarnings, setImageImportWarnings] = useState<string[]>([]);
  const [imageImportLoadedFormat, setImageImportLoadedFormat] =
    useState<LoadedStructureFormat | null>(null);
  const [lastRecognizedStructure, setLastRecognizedStructure] =
    useState<RecognizedStructurePayload | null>(null);
  const copyResetTimerRef = useRef<number | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const previewUrlRef = useRef<string | null>(null);

  useEffect(() => {
    return () => {
      if (copyResetTimerRef.current !== null) {
        window.clearTimeout(copyResetTimerRef.current);
      }
      if (previewUrlRef.current !== null) {
        URL.revokeObjectURL(previewUrlRef.current);
      }
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let attempts = 0;

    const timer = window.setInterval(() => {
      const ketcher = iframeRef.current?.contentWindow?.ketcher;
      attempts += 1;
      if (ketcher) {
        if (!cancelled) {
          onReadyChange(true);
        }
        window.clearInterval(timer);
        return;
      }

      if (attempts >= 40) {
        window.clearInterval(timer);
      }
    }, 500);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [iframeRef, onReadyChange]);

  async function writeClipboardText(value: string) {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return;
    }

    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    textarea.style.top = "0";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();

    try {
      if (!document.execCommand("copy")) {
        throw new Error("document.execCommand('copy') returned false");
      }
    } finally {
      document.body.removeChild(textarea);
    }
  }

  const setImagePreviewForFile = useCallback((file: File) => {
    if (previewUrlRef.current !== null) {
      URL.revokeObjectURL(previewUrlRef.current);
    }

    const previewUrl = URL.createObjectURL(file);
    previewUrlRef.current = previewUrl;
    setImagePreviewUrl(previewUrl);
    setImageImportName(file.name || "粘贴的图片");
  }, []);

  const clearImageImportFeedback = useCallback(() => {
    if (previewUrlRef.current !== null) {
      URL.revokeObjectURL(previewUrlRef.current);
      previewUrlRef.current = null;
    }
    setImagePreviewUrl(null);
    setImageImportName(null);
    setImageImportError(null);
    setImageImportWarnings([]);
    setImageImportLoadedFormat(null);
    setLastRecognizedStructure(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }, []);

  const refreshKetcherFrame = useCallback(() => {
    const frameWindow = iframeRef.current?.contentWindow;
    if (!frameWindow) {
      return;
    }
    const FrameEvent = (frameWindow as Window & typeof globalThis).Event;
    frameWindow.dispatchEvent(new FrameEvent("resize"));
    frameWindow.scrollTo(0, 0);
  }, [iframeRef]);

  const waitForKetcherCommit = useCallback(async () => {
    await new Promise((resolve) => window.setTimeout(resolve, 80));
    refreshKetcherFrame();
    await new Promise((resolve) => window.setTimeout(resolve, 80));
  }, [refreshKetcherFrame]);

  const writeStructureToEditor = useCallback(
    async (ketcher: KetcherApi, structure: string, fallbackSmiles: string) => {
      if (typeof ketcher.setMolecule !== "function") {
        throw new Error("结构编辑器无法加载分子。");
      }

      await ketcher.setMolecule(structure);
      await waitForKetcherCommit();

      if (typeof ketcher.getSmiles !== "function") {
        return fallbackSmiles;
      }

      return (await ketcher.getSmiles()).trim();
    },
    [waitForKetcherCommit],
  );

  const hydrateEmptyEditorFromSmiles = useCallback(
    async (ketcher: KetcherApi, sourceSmiles: string, shouldApply: () => boolean = () => true) => {
      if (typeof ketcher.getSmiles !== "function" || typeof ketcher.setMolecule !== "function") {
        return;
      }

      const editorSmiles = (await ketcher.getSmiles()).trim();
      if (editorSmiles) {
        return;
      }

      const loadedSmiles = await writeStructureToEditor(ketcher, sourceSmiles, sourceSmiles);
      if (!shouldApply()) {
        return;
      }
      onReadyChange(true);
      if (loadedSmiles && loadedSmiles !== sourceSmiles) {
        onChange(loadedSmiles);
      }
    },
    [onChange, onReadyChange, writeStructureToEditor],
  );

  useEffect(() => {
    const sourceSmiles = smiles.trim();
    if (!sourceSmiles) {
      return;
    }

    let cancelled = false;
    let attempts = 0;
    let isChecking = false;

    const timer = window.setInterval(() => {
      if (isChecking) {
        return;
      }

      const ketcher = iframeRef.current?.contentWindow?.ketcher;
      attempts += 1;
      if (!ketcher) {
        if (attempts >= 40) {
          window.clearInterval(timer);
        }
        return;
      }

      isChecking = true;
      void hydrateEmptyEditorFromSmiles(ketcher, sourceSmiles, () => !cancelled)
        .then(() => {
          window.clearInterval(timer);
        })
        .catch((error) => {
          console.error("Failed to hydrate empty Ketcher editor from shared SMILES", error);
          if (attempts >= 40) {
            window.clearInterval(timer);
            return;
          }
          isChecking = false;
        });
    }, 500);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [hydrateEmptyEditorFromSmiles, iframeRef, smiles]);

  const loadRecognizedStructureIntoEditor = useCallback(
    async (
      payload: RecognizedStructurePayload,
      warnings: string[] = [],
    ): Promise<{ nextSmiles: string; loadedFormat: LoadedStructureFormat; warnings: string[] }> => {
      const ketcher = iframeRef.current?.contentWindow?.ketcher;
      if (!ketcher || typeof ketcher.setMolecule !== "function") {
        onReadyChange(false);
        throw new Error("结构编辑器尚未就绪。");
      }

      const normalizedSmiles = payload.smiles.trim();
      const nextWarnings = [...warnings];

      if (payload.molfile.trim()) {
        try {
          const editorSmiles = await writeStructureToEditor(ketcher, payload.molfile, normalizedSmiles);
          if (editorSmiles) {
            return { nextSmiles: editorSmiles, loadedFormat: "molfile", warnings: nextWarnings };
          }
          nextWarnings.push(
            "Molfile 已发送到 Ketcher，但编辑器未返回结构；已改用 SMILES。",
          );
        } catch (error) {
          console.error("Failed to load recognized molfile into Ketcher", error);
          if (!normalizedSmiles) {
            throw error;
          }
          nextWarnings.push("Molfile 无法加载；已改用 SMILES。");
        }
      }

      if (!normalizedSmiles) {
        throw new Error("识别结果未返回结构。");
      }

      const editorSmiles = await writeStructureToEditor(ketcher, normalizedSmiles, normalizedSmiles);
      if (!editorSmiles && typeof ketcher.getSmiles === "function") {
        throw new Error("Ketcher 未接受识别出的结构。");
      }

      return {
        nextSmiles: editorSmiles || normalizedSmiles,
        loadedFormat: "smiles",
        warnings: nextWarnings,
      };
    },
    [iframeRef, onReadyChange, writeStructureToEditor],
  );

  const importImageFile = useCallback(
    async (file: File) => {
      setImageImportError(null);
      setImageImportWarnings([]);
      setImageImportLoadedFormat(null);
      setLastRecognizedStructure(null);

      if (file.type && !file.type.startsWith("image/")) {
        if (previewUrlRef.current !== null) {
          URL.revokeObjectURL(previewUrlRef.current);
          previewUrlRef.current = null;
        }
        setImagePreviewUrl(null);
        setImageImportName(file.name || "已选择文件");
        setImageImportError("只能导入图片文件。");
        return;
      }

      setImagePreviewForFile(file);

      const ketcher = iframeRef.current?.contentWindow?.ketcher;
      if (!ketcher || typeof ketcher.setMolecule !== "function") {
        onReadyChange(false);
        setImageImportError("结构编辑器尚未就绪。");
        return;
      }

      setIsImportingImage(true);
      try {
        const result = await recognizeStructureImage(file);
        const molfile = result.molfile && result.molfile.trim() ? result.molfile : "";
        const recognizedSmiles = result.smiles.trim();
        const payload = { molfile, smiles: recognizedSmiles };

        if (!payload.molfile.trim() && !payload.smiles) {
          throw new Error("识别结果未返回结构。");
        }

        setLastRecognizedStructure(payload);
        const { nextSmiles, loadedFormat, warnings } = await loadRecognizedStructureIntoEditor(
          payload,
          result.warnings,
        );
        onChange(nextSmiles || recognizedSmiles);
        onReadyChange(true);
        setImageImportLoadedFormat(loadedFormat);
        setImageImportWarnings(warnings);
      } catch (error) {
        console.error("Failed to import structure image", error);
        setImageImportError(error instanceof Error ? error.message : "图片导入失败。");
      } finally {
        setIsImportingImage(false);
        if (fileInputRef.current) {
          fileInputRef.current.value = "";
        }
      }
    },
    [iframeRef, loadRecognizedStructureIntoEditor, onChange, onReadyChange, setImagePreviewForFile],
  );

  const reloadLastImageImport = useCallback(async () => {
    if (!lastRecognizedStructure) {
      return;
    }

    setIsImportingImage(true);
    setImageImportError(null);
    setImageImportLoadedFormat(null);
    try {
      const { nextSmiles, loadedFormat, warnings } =
        await loadRecognizedStructureIntoEditor(lastRecognizedStructure, imageImportWarnings);
      onChange(nextSmiles || lastRecognizedStructure.smiles);
      onReadyChange(true);
      setImageImportLoadedFormat(loadedFormat);
      setImageImportWarnings(warnings);
    } catch (error) {
      console.error("Failed to load recognized structure into Ketcher", error);
      setImageImportError(error instanceof Error ? error.message : "无法将结构加载到编辑器。");
    } finally {
      setIsImportingImage(false);
    }
  }, [
    imageImportWarnings,
    lastRecognizedStructure,
    loadRecognizedStructureIntoEditor,
    onChange,
    onReadyChange,
  ]);

  useEffect(() => {
    function findImageFile(event: ClipboardEvent): File | null {
      const files = Array.from(event.clipboardData?.files ?? []);
      const directFile = files.find((file) => file.type.startsWith("image/"));
      if (directFile) {
        return directFile;
      }

      const items = Array.from(event.clipboardData?.items ?? []);
      const imageItem = items.find((item) => item.type.startsWith("image/"));
      return imageItem?.getAsFile() ?? null;
    }

    function handlePaste(event: ClipboardEvent) {
      const target = event.target;
      const isEditorPasteTarget =
        document.activeElement === iframeRef.current ||
        (target instanceof Element && target.closest('[data-ketcher-editor-root="true"]') !== null);
      if (!isEditorPasteTarget) {
        return;
      }

      const imageFile = findImageFile(event);
      if (!imageFile) {
        return;
      }

      event.preventDefault();
      void importImageFile(imageFile);
    }

    window.addEventListener("paste", handlePaste);
    return () => window.removeEventListener("paste", handlePaste);
  }, [iframeRef, importImageFile]);

  async function syncSmilesFromKetcher() {
    const ketcher = iframeRef.current?.contentWindow?.ketcher;
    if (!ketcher) {
      onReadyChange(false);
      return;
    }

    setIsSyncing(true);
    try {
      const nextSmiles = await ketcher.getSmiles();
      onChange(nextSmiles);
      onReadyChange(true);
    } catch (error) {
      console.error("Failed to read SMILES from Ketcher", error);
    } finally {
      setIsSyncing(false);
    }
  }

  async function loadSmilesIntoKetcher() {
    const nextStructure = smiles.trim();
    if (!nextStructure) {
      return;
    }

    const ketcher = iframeRef.current?.contentWindow?.ketcher;
    if (!ketcher || typeof ketcher.setMolecule !== "function") {
      onReadyChange(false);
      return;
    }

    setIsLoadingSmilesIntoEditor(true);
    try {
      const nextSmiles = await writeStructureToEditor(ketcher, nextStructure, nextStructure);
      onChange(nextSmiles || nextStructure);
      onReadyChange(true);
    } catch (error) {
      console.error("Failed to load SMILES into Ketcher", error);
    } finally {
      setIsLoadingSmilesIntoEditor(false);
    }
  }

  async function clearKetcherCanvas() {
    const ketcher = iframeRef.current?.contentWindow?.ketcher;
    if (!ketcher) {
      onReadyChange(false);
      return;
    }

    setIsClearing(true);
    try {
      if (typeof ketcher.clear === "function") {
        await ketcher.clear();
      } else if (typeof ketcher.setMolecule === "function") {
        await ketcher.setMolecule("");
      }

      onChange("");
      onReadyChange(true);
    } catch (error) {
      console.error("Failed to clear Ketcher canvas", error);
    } finally {
      setIsClearing(false);
    }
  }

  async function loadPresetStructure() {
    if (!presetStructure) {
      return;
    }

    const ketcher = iframeRef.current?.contentWindow?.ketcher;
    if (!ketcher || typeof ketcher.setMolecule !== "function") {
      onReadyChange(false);
      return;
    }

    setIsLoadingPreset(true);
    try {
      await ketcher.setMolecule(presetStructure.smiles);
      const nextSmiles =
        typeof ketcher.getSmiles === "function"
          ? await ketcher.getSmiles()
          : presetStructure.smiles;
      onChange(nextSmiles || presetStructure.smiles);
      onReadyChange(true);
    } catch (error) {
      console.error("Failed to load preset structure into Ketcher", error);
    } finally {
      setIsLoadingPreset(false);
    }
  }

  async function copySmiles() {
    const value = smiles.trim();
    if (!value) {
      return;
    }

    try {
      await writeClipboardText(value);
      setCopyLabel("已复制");
      scheduleCopyLabelReset();
    } catch (error) {
      console.error("Failed to copy SMILES", error);
      setCopyLabel("失败");
      scheduleCopyLabelReset();
    }
  }

  function scheduleCopyLabelReset() {
    if (copyResetTimerRef.current !== null) {
      window.clearTimeout(copyResetTimerRef.current);
    }

    copyResetTimerRef.current = window.setTimeout(() => {
      setCopyLabel("复制");
      copyResetTimerRef.current = null;
    }, 1400);
  }

  const isSplitLayout = layout === "split";
  const isDarkTone = tone === "dark";

  const imageImportFeedback = imagePreviewUrl || imageImportError || imageImportWarnings.length > 0 ? (
    <div
      className={cn(
        "flex flex-col gap-4 rounded-[20px] border p-4 shadow-sm sm:flex-row sm:items-start",
        isDarkTone ? "border-cyan-200/10 bg-slate-950/70" : "border-teal-100 bg-white/85"
      )}
    >
      {imagePreviewUrl ? (
        <img
          src={imagePreviewUrl}
          alt={imageImportName ?? "导入的结构"}
          className="h-24 w-32 flex-none rounded-2xl border border-slate-200 bg-white object-contain"
        />
      ) : (
        <div
          className={cn(
            "flex h-24 w-32 flex-none items-center justify-center rounded-2xl border",
            isDarkTone ? "border-slate-700 bg-slate-900" : "border-slate-200 bg-white"
          )}
        >
          <ImagePlus className="h-6 w-6 text-slate-400" />
        </div>
      )}
      <div className="min-w-0 flex-1 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <div className={cn("text-sm font-semibold", isDarkTone ? "text-slate-100" : "text-slate-950")}>
            {isImportingImage
              ? "正在识别图片"
              : imageImportError
                ? "图片导入失败"
                : imageImportLoadedFormat
                  ? "已加载到编辑器"
                  : "图片导入就绪"}
          </div>
          {imageImportName ? (
            <div className="max-w-full truncate rounded-full bg-teal-50 px-2.5 py-1 text-xs font-medium text-teal-700">
              {imageImportName}
            </div>
          ) : null}
        </div>
        {imageImportError ? <p className="text-sm text-rose-700">{imageImportError}</p> : null}
        {!imageImportError && imageImportLoadedFormat ? (
          <p className={cn("text-sm", isDarkTone ? "text-teal-300" : "text-teal-700")}>
            结构已通过 {imageImportLoadedFormat === "molfile" ? "molfile 坐标" : "SMILES"} 加载。
          </p>
        ) : null}
        {imageImportWarnings.length > 0 ? (
          <div className="space-y-1 text-sm text-amber-700">
            {imageImportWarnings.map((warning) => (
              <p key={warning}>{warning}</p>
            ))}
          </div>
        ) : null}
        {lastRecognizedStructure && !isImportingImage ? (
          <Button
            type="button"
            variant="outline"
            onClick={() => void reloadLastImageImport()}
            className="min-h-[36px] min-w-[138px] px-3"
          >
            <Upload className="mr-2 h-3.5 w-3.5" />
            加载到画布
          </Button>
        ) : null}
      </div>
      <Button
        type="button"
        variant="outline"
        aria-label="关闭图片导入状态"
        onClick={clearImageImportFeedback}
        className="h-9 w-9 flex-none rounded-full p-0"
      >
        <X className="h-4 w-4" />
      </Button>
    </div>
  ) : null;

  const smilesPanel = (
    <div
      className={cn(
        "rounded-[24px] border p-4",
        isDarkTone
          ? "border-cyan-200/10 bg-slate-950"
          : "border-slate-200 bg-white",
        isSplitLayout ? "flex min-h-full flex-col" : "",
        smilesPanelClassName
      )}
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div className={cn("flex items-center gap-2 text-sm font-medium", isDarkTone ? "text-slate-100" : "text-slate-900")}>
          <Sigma className={cn("h-4 w-4", isDarkTone ? "text-cyan-300" : "text-teal-600")} />
          SMILES 输入
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={loadSmilesIntoKetcher}
            disabled={!smiles.trim() || isLoadingSmilesIntoEditor}
            className={cn("min-h-[38px] min-w-[142px] px-3", isDarkTone ? "border-white/10 bg-white/[0.08] text-white hover:bg-white/[0.14] hover:text-white" : "")}
          >
            {isLoadingSmilesIntoEditor ? (
              <LoaderCircle className="mr-2 h-3.5 w-3.5 animate-spin" />
            ) : (
              <Upload className="mr-2 h-3.5 w-3.5" />
            )}
            加载到编辑器
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={copySmiles}
            disabled={!smiles.trim()}
            className={cn("min-h-[38px] min-w-[96px] px-3", isDarkTone ? "border-white/10 bg-white/[0.08] text-white hover:bg-white/[0.14] hover:text-white" : "")}
          >
            <Copy className="mr-2 h-3.5 w-3.5" />
            {copyLabel}
          </Button>
        </div>
      </div>
      <Textarea
        value={smiles}
        onChange={(event) => onChange(event.target.value)}
        placeholder={smilesPlaceholder}
        className={cn(
          "min-h-[128px] rounded-[18px] px-4 py-3",
          isDarkTone
            ? "border-slate-700 bg-slate-950/70 text-emerald-100 placeholder:text-slate-600"
            : "border-slate-200 bg-white",
          isSplitLayout ? "min-h-[280px] flex-1 font-mono-ui text-sm leading-6" : "",
          smilesTextareaClassName
        )}
      />
    </div>
  );

  const hiddenFileInput = (
    <input
      ref={fileInputRef}
      type="file"
      accept="image/png,image/jpeg,image/gif,image/webp"
      className="hidden"
      onChange={(event) => {
        const file = event.currentTarget.files?.[0];
        if (file) {
          void importImageFile(file);
        }
      }}
    />
  );

  const canvasActionButtonClass = isDarkTone
    ? "border-white/10 bg-white/[0.08] text-white hover:bg-white/[0.14] hover:text-white"
    : "border-sky-100 bg-white text-slate-800 shadow-[0_12px_28px_rgba(37,99,235,0.08)] hover:border-blue-200 hover:bg-blue-50 hover:text-blue-700";

  const canvasActions = (
    <div
      className={cn(
        isSplitLayout
          ? cn(
              "mt-auto grid gap-2 p-3 sm:grid-cols-2 2xl:grid-cols-4",
              isDarkTone ? "bg-slate-950/55" : "bg-white"
            )
          : "flex flex-wrap justify-end gap-3"
      )}
    >
      <Button
        type="button"
        variant="outline"
        onClick={() => fileInputRef.current?.click()}
        disabled={isImportingImage}
        className={cn(isSplitLayout ? "min-h-[40px] min-w-0 px-3" : "min-w-[164px]", canvasActionButtonClass)}
      >
        {isImportingImage ? (
          <LoaderCircle className="mr-2 h-4 w-4 animate-spin" />
        ) : (
          <ImagePlus className="mr-2 h-4 w-4" />
        )}
        {isImportingImage ? "导入中..." : "导入图片"}
      </Button>
      {presetStructure ? (
        <Button
          type="button"
          variant="outline"
          onClick={loadPresetStructure}
          disabled={isLoadingPreset}
          className={cn(isSplitLayout ? "min-h-[40px] min-w-0 px-3" : "min-w-[190px]", canvasActionButtonClass)}
        >
          {isLoadingPreset ? (
            <LoaderCircle className="mr-2 h-4 w-4 animate-spin" />
          ) : (
            <FlaskConical className="mr-2 h-4 w-4" />
          )}
          {presetStructure.label}
        </Button>
      ) : null}
      <Button
        type="button"
        variant="outline"
        onClick={clearKetcherCanvas}
        disabled={isClearing}
        className={cn(isSplitLayout ? "min-h-[40px] min-w-0 px-3" : "min-w-[152px]", canvasActionButtonClass)}
      >
        {isClearing ? (
          <LoaderCircle className="mr-2 h-4 w-4 animate-spin" />
        ) : (
          <Eraser className="mr-2 h-4 w-4" />
        )}
        清空画布
      </Button>
      <Button
        type="button"
        variant="outline"
        onClick={syncSmilesFromKetcher}
        disabled={isSyncing}
        className={cn(isSplitLayout ? "min-h-[40px] min-w-0 px-3" : "min-w-[212px]", canvasActionButtonClass)}
      >
        {isSyncing ? (
          <LoaderCircle className="mr-2 h-4 w-4 animate-spin" />
        ) : (
          <RefreshCcw className="mr-2 h-4 w-4" />
        )}
        从编辑器同步
      </Button>
    </div>
  );

  if (isSplitLayout) {
    const canvasPanel = (
      <section
        className={cn(
          "relative flex h-full flex-col overflow-hidden rounded-[24px] border",
          isDarkTone
            ? "border-cyan-200/10 bg-slate-950 text-slate-100"
            : "border-sky-100 bg-white text-slate-900 shadow-[0_22px_58px_rgba(37,99,235,0.12),0_6px_18px_rgba(15,23,42,0.05)] ring-1 ring-white/80",
          frameClassName
        )}
      >
        {hiddenFileInput}
        <div
          className={cn(
            "flex items-center justify-between gap-4 px-4 py-3",
            isDarkTone ? "bg-slate-900/55" : "bg-white"
          )}
        >
          <div className="min-w-0">
            {eyebrow ? (
              <div className={cn("truncate text-[11px] font-semibold uppercase tracking-[0.18em]", isDarkTone ? "text-cyan-300/85" : "text-teal-700/80")}>
                {eyebrow}
              </div>
            ) : null}
            <div className={cn("font-heading truncate text-base font-semibold", eyebrow ? "mt-1" : "", isDarkTone ? "text-white" : "text-slate-950")}>
              {title}
            </div>
          </div>
          {showToolsBadge ? (
            <div
              className={cn(
                "rounded-full border px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.14em]",
                isDarkTone ? "border-white/10 bg-white/[0.05] text-slate-400" : "border-blue-100 bg-blue-50 text-blue-600"
              )}
            >
              编辑工具
            </div>
          ) : null}
        </div>

        <div className={isSplitLayout ? "p-0" : "p-3"}>
          <div
            className={cn(
              "overflow-hidden rounded-[18px] border bg-white shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]",
              isSplitLayout ? "rounded-none border-x-0" : "",
              isDarkTone ? "border-cyan-200/10 bg-slate-900/80 shadow-none" : "border-sky-100 shadow-[inset_0_1px_0_rgba(255,255,255,0.9),0_10px_26px_rgba(37,99,235,0.06)]"
            )}
          >
            <iframe
              key="polyprop-ketcher-frame"
              title="Ketcher 编辑器"
              src="/ketcher/index.html"
              ref={iframeRef}
              tabIndex={-1}
              className={cn("h-[420px] w-full border-0", iframeClassName)}
            />
          </div>
        </div>

        {imageImportFeedback ? <div className="px-3 pb-3">{imageImportFeedback}</div> : null}
        {canvasActions}
      </section>
    );

    if (!showSmilesPanel) {
      return <div className={className} data-ketcher-editor-root="true">{canvasPanel}</div>;
    }

    return (
      <div
        className={cn("grid gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(340px,0.9fr)] xl:items-stretch", className)}
        data-ketcher-editor-root="true"
      >
        {canvasPanel}
        {smilesPanel}
      </div>
    );
  }

  return (
    <Card
      className={cn(
        "overflow-hidden rounded-[30px]",
        isDarkTone ? "border-cyan-200/10 bg-slate-950/70 text-slate-100 shadow-none" : "border-white/70",
        className
      )}
      data-ketcher-editor-root="true"
    >
      <CardHeader
        className={cn(
          "border-b py-5",
          isDarkTone ? "border-cyan-200/10 bg-slate-900/70" : "border-slate-200 bg-white"
        )}
      >
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="space-y-1.5">
            {eyebrow ? (
              <div className={cn("text-[11px] font-medium uppercase tracking-[0.22em]", isDarkTone ? "text-cyan-300/85" : "text-teal-700/80")}>
                {eyebrow}
              </div>
            ) : null}
            <CardTitle className={cn("text-[1.4rem] tracking-tight", isDarkTone ? "text-white" : "")}>
              {title}
            </CardTitle>
          </div>
          <div className="flex flex-wrap justify-end gap-3 self-start lg:ml-auto">
            {hiddenFileInput}
            {canvasActions}
          </div>
        </div>
      </CardHeader>

      <CardContent className={cn("space-y-5 pt-6", isSplitLayout ? "pt-4" : "", contentClassName)}>
        <div className={cn(isSplitLayout ? "grid gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(340px,0.9fr)] xl:items-stretch" : "space-y-5")}>
          <div
            className={cn(
              "overflow-hidden rounded-[24px] border bg-white/90 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]",
              isDarkTone ? "border-cyan-200/10 bg-slate-900/80 shadow-none" : "border-white/80",
              frameClassName
            )}
          >
            <iframe
              key="polyprop-ketcher-frame"
              title="Ketcher 编辑器"
              src="/ketcher/index.html"
              ref={iframeRef}
              tabIndex={-1}
              className={cn("h-[560px] w-full border-0", isSplitLayout ? "h-[420px]" : "", iframeClassName)}
            />
          </div>
          {smilesPanel}
        </div>

        {imageImportFeedback}
      </CardContent>
    </Card>
  );
}
