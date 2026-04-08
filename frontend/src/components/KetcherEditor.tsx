import { LoaderCircle, RefreshCcw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Button } from "./ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";
import { Textarea } from "./ui/textarea";

type KetcherEditorProps = {
  smiles: string;
  onChange: (value: string) => void;
  iframeRef: React.RefObject<HTMLIFrameElement | null>;
  onReadyChange: (ready: boolean) => void;
};

export function KetcherEditor({
  smiles,
  onChange,
  iframeRef,
  onReadyChange
}: KetcherEditorProps) {
  const iframeKey = useMemo(() => "polyprop-ketcher-frame", []);
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
    <Card className="bg-card">
      <CardHeader>
        <CardTitle>Structure Editor</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="overflow-hidden rounded-lg border border-input bg-white">
          <iframe
            key={iframeKey}
            title="Ketcher Editor"
            src="/ketcher/index.html"
            ref={iframeRef}
            className="h-[520px] w-full border-0"
          />
        </div>
        <div className="flex flex-wrap gap-3">
          <Button type="button" onClick={syncSmilesFromKetcher} disabled={isSyncing}>
            {isSyncing ? <LoaderCircle className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCcw className="mr-2 h-4 w-4" />}
            Pull SMILES From Ketcher
          </Button>
        </div>
        <div className="space-y-2">
          <div className="text-sm font-medium">SMILES Fallback</div>
          <Textarea
            value={smiles}
            onChange={(event) => onChange(event.target.value)}
            placeholder="当前用作 Ketcher 的回退输入与调试入口，例如: CCO 或 *CC*"
            className="min-h-[140px]"
          />
        </div>
      </CardContent>
    </Card>
  );
}
