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
  smilesPlaceholder = "For example: *CC*, CCO, or another SMILES for similarity matching",
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
  eyebrow = "Molecular Canvas",
  title = "Structure Editor"
}: KetcherEditorProps) {
  const [isSyncing, setIsSyncing] = useState(false);
  const [isClearing, setIsClearing] = useState(false);
  const [isLoadingPreset, setIsLoadingPreset] = useState(false);
  const [isLoadingSmilesIntoEditor, setIsLoadingSmilesIntoEditor] = useState(false);
  const [copyLabel, setCopyLabel] = useState("Copy");
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
    setImageImportName(file.name || "Pasted image");
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
        throw new Error("Structure editor cannot load molecules.");
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

  const loadRecognizedStructureIntoEditor = useCallback(
    async (
      payload: RecognizedStructurePayload,
      warnings: string[] = [],
    ): Promise<{ nextSmiles: string; loadedFormat: LoadedStructureFormat; warnings: string[] }> => {
      const ketcher = iframeRef.current?.contentWindow?.ketcher;
      if (!ketcher || typeof ketcher.setMolecule !== "function") {
        onReadyChange(false);
        throw new Error("Structure editor is not ready.");
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
            "Molfile was sent to Ketcher, but the editor returned no structure; SMILES fallback was used.",
          );
        } catch (error) {
          console.error("Failed to load recognized molfile into Ketcher", error);
          if (!normalizedSmiles) {
            throw error;
          }
          nextWarnings.push("Molfile could not be loaded; SMILES fallback was used.");
        }
      }

      if (!normalizedSmiles) {
        throw new Error("Recognition did not return a structure.");
      }

      const editorSmiles = await writeStructureToEditor(ketcher, normalizedSmiles, normalizedSmiles);
      if (!editorSmiles && typeof ketcher.getSmiles === "function") {
        throw new Error("Ketcher did not accept the recognized structure.");
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
        setImageImportName(file.name || "Selected file");
        setImageImportError("Only image files can be imported.");
        return;
      }

      setImagePreviewForFile(file);

      const ketcher = iframeRef.current?.contentWindow?.ketcher;
      if (!ketcher || typeof ketcher.setMolecule !== "function") {
        onReadyChange(false);
        setImageImportError("Structure editor is not ready.");
        return;
      }

      setIsImportingImage(true);
      try {
        const result = await recognizeStructureImage(file);
        const molfile = result.molfile && result.molfile.trim() ? result.molfile : "";
        const recognizedSmiles = result.smiles.trim();
        const payload = { molfile, smiles: recognizedSmiles };

        if (!payload.molfile.trim() && !payload.smiles) {
          throw new Error("Recognition did not return a structure.");
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
        setImageImportError(error instanceof Error ? error.message : "Image import failed.");
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
      setImageImportError(error instanceof Error ? error.message : "Failed to load structure into editor.");
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
      const imageFile = findImageFile(event);
      if (!imageFile) {
        return;
      }

      event.preventDefault();
      void importImageFile(imageFile);
    }

    window.addEventListener("paste", handlePaste);
    return () => window.removeEventListener("paste", handlePaste);
  }, [importImageFile]);

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
      setCopyLabel("Copied");
      scheduleCopyLabelReset();
    } catch (error) {
      console.error("Failed to copy SMILES", error);
      setCopyLabel("Failed");
      scheduleCopyLabelReset();
    }
  }

  function scheduleCopyLabelReset() {
    if (copyResetTimerRef.current !== null) {
      window.clearTimeout(copyResetTimerRef.current);
    }

    copyResetTimerRef.current = window.setTimeout(() => {
      setCopyLabel("Copy");
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
          alt={imageImportName ?? "Imported structure"}
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
              ? "Recognizing image"
              : imageImportError
                ? "Image import failed"
                : imageImportLoadedFormat
                  ? "Loaded to editor"
                  : "Image import ready"}
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
            Structure loaded by {imageImportLoadedFormat === "molfile" ? "molfile coordinates" : "SMILES"}.
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
            Load to Canvas
          </Button>
        ) : null}
      </div>
      <Button
        type="button"
        variant="outline"
        aria-label="Dismiss image import status"
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
          SMILES Input
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
            Load to Editor
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
        {isImportingImage ? "Importing..." : "Import Image"}
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
        Clear Canvas
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
        Sync from Editor
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
              Streaming Tools
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
              title="Ketcher Editor"
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
      return <div className={className}>{canvasPanel}</div>;
    }

    return (
      <div className={cn("grid gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(340px,0.9fr)] xl:items-stretch", className)}>
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
              title="Ketcher Editor"
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
