import { Copy, Eraser, FlaskConical, LoaderCircle, RefreshCcw, Sigma } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "./ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";
import { Textarea } from "./ui/textarea";

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
};

export function KetcherEditor({
  smiles,
  iframeRef,
  onReadyChange,
  onChange,
  presetStructure,
  smilesPlaceholder = "For example: *CC*, CCO, or another SMILES for similarity matching"
}: KetcherEditorProps) {
  const [isSyncing, setIsSyncing] = useState(false);
  const [isClearing, setIsClearing] = useState(false);
  const [isLoadingPreset, setIsLoadingPreset] = useState(false);
  const [copyLabel, setCopyLabel] = useState("Copy");
  const copyResetTimerRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (copyResetTimerRef.current !== null) {
        window.clearTimeout(copyResetTimerRef.current);
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

  return (
    <Card className="overflow-hidden rounded-[30px] border-white/70">
      <CardHeader className="mesh-surface border-b border-white/70 py-5">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="space-y-1.5">
            <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-teal-700/80">
              Molecular Canvas
            </div>
            <CardTitle className="text-[1.4rem] tracking-tight">Structure Editor</CardTitle>
          </div>
          <div className="flex flex-wrap justify-end gap-3 self-start lg:ml-auto">
            {presetStructure ? (
              <Button
                type="button"
                variant="outline"
                onClick={loadPresetStructure}
                disabled={isLoadingPreset}
                className="min-w-[190px]"
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
              className="min-w-[152px]"
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
              className="min-w-[212px]"
            >
              {isSyncing ? (
                <LoaderCircle className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <RefreshCcw className="mr-2 h-4 w-4" />
              )}
              Sync from Editor
            </Button>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-5 pt-6">
        <div className="overflow-hidden rounded-[24px] border border-white/80 bg-white/90 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]">
          <iframe
            key="polyprop-ketcher-frame"
            title="Ketcher Editor"
            src="/ketcher/index.html"
            ref={iframeRef}
            tabIndex={-1}
            className="h-[560px] w-full border-0"
          />
        </div>

        <div className="rounded-[24px] border border-white/70 bg-[linear-gradient(180deg,rgba(248,251,252,0.98)_0%,rgba(239,246,247,0.88)_100%)] p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2 text-sm font-medium text-slate-900">
              <Sigma className="h-4 w-4 text-teal-600" />
              SMILES Input
            </div>
            <Button
              type="button"
              variant="outline"
              onClick={copySmiles}
              disabled={!smiles.trim()}
              className="min-h-[38px] min-w-[96px] px-3"
            >
              <Copy className="mr-2 h-3.5 w-3.5" />
              {copyLabel}
            </Button>
          </div>
          <Textarea
            value={smiles}
            onChange={(event) => onChange(event.target.value)}
            placeholder={smilesPlaceholder}
            className="min-h-[128px] rounded-[18px] border-slate-200 bg-white px-4 py-3"
          />
        </div>
      </CardContent>
    </Card>
  );
}
