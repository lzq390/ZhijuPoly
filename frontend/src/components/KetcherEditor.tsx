import { LoaderCircle, RefreshCcw, Sigma } from "lucide-react";
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

  return (
    <Card className="overflow-hidden rounded-[28px] border-slate-200/90">
      <CardHeader className="min-h-[124px] gap-4 border-b border-slate-200/80 bg-[linear-gradient(180deg,#f9fbfd_0%,#f4f8fc_100%)]">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="space-y-2">
            <CardTitle className="text-xl">Structure Editor</CardTitle>
            <CardDescription>使用 Ketcher 编辑当前目标结构，并作为查询主输入。</CardDescription>
          </div>
          <Button
            type="button"
            variant="outline"
            onClick={syncSmilesFromKetcher}
            disabled={isSyncing}
            className="min-w-[212px] self-start"
          >
            {isSyncing ? (
              <LoaderCircle className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <RefreshCcw className="mr-2 h-4 w-4" />
            )}
            Pull SMILES From Ketcher
          </Button>
        </div>
      </CardHeader>

      <CardContent className="space-y-5 pt-6">
        <div className="overflow-hidden rounded-[22px] border border-slate-200 bg-white shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]">
          <iframe
            key="polyprop-ketcher-frame"
            title="Ketcher Editor"
            src="/ketcher/index.html"
            ref={iframeRef}
            className="h-[560px] w-full border-0"
          />
        </div>

        <div className="rounded-[22px] border border-slate-200 bg-slate-50/70 p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium text-slate-900">
            <Sigma className="h-4 w-4 text-blue-600" />
            SMILES fallback
          </div>
          <Textarea
            value={smiles}
            onChange={(event) => onChange(event.target.value)}
            placeholder="例如: *CC*、CCO 或其他用于匹配查询的 SMILES"
            className="min-h-[128px] rounded-[18px] border-slate-200 bg-white px-4 py-3"
          />
        </div>
      </CardContent>
    </Card>
  );
}
