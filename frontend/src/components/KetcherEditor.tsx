import { Eraser, LoaderCircle, RefreshCcw, Sigma } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "./ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";
import { Textarea } from "./ui/textarea";

type KetcherEditorProps = {
  smiles: string;
  iframeRef: React.RefObject<HTMLIFrameElement | null>;
  onReadyChange: (ready: boolean) => void;
  onChange: (value: string) => void;
};

export function KetcherEditor({
  smiles,
  iframeRef,
  onReadyChange,
  onChange
}: KetcherEditorProps) {
  const [isSyncing, setIsSyncing] = useState(false);
  const [isClearing, setIsClearing] = useState(false);

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

  return (
    <Card className="overflow-hidden rounded-[30px] border-white/70">
      <CardHeader className="mesh-surface min-h-[136px] gap-4 border-b border-white/70">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="space-y-2">
            <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-teal-700/80">
              Molecular Canvas
            </div>
            <CardTitle className="text-[1.4rem] tracking-tight">Structure Editor</CardTitle>
            <CardDescription>将结构编辑器作为主舞台，SMILES 同步和文本回退作为旁路输入。</CardDescription>
          </div>
          <div className="flex flex-wrap justify-end gap-3 self-start lg:ml-auto">
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
              清空画板
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
              Pull SMILES From Ketcher
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
            className="h-[560px] w-full border-0"
          />
        </div>

        <div className="rounded-[24px] border border-white/70 bg-[linear-gradient(180deg,rgba(248,251,252,0.98)_0%,rgba(239,246,247,0.88)_100%)] p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium text-slate-900">
            <Sigma className="h-4 w-4 text-teal-600" />
            SMILES fallback
          </div>
          <Textarea
            value={smiles}
            onChange={(event) => onChange(event.target.value)}
            placeholder="例如: *CC*、CCO 或其他用于相似匹配的 SMILES"
            className="min-h-[128px] rounded-[18px] border-slate-200 bg-white px-4 py-3"
          />
        </div>
      </CardContent>
    </Card>
  );
}
