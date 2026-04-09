import { Orbit, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { fetchStructure3D } from "../services/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";

const D3MOL_SRC = "/vendor/3Dmol-min.js";
const structureCache = new Map<string, { molblock: string; capped_smiles: string }>();
const CACHE_MAX = 50;

function setCacheEntry(key: string, value: { molblock: string; capped_smiles: string }) {
  if (structureCache.size >= CACHE_MAX) {
    structureCache.delete(structureCache.keys().next().value!);
  }
  structureCache.set(key, value);
}

function loadScriptOnce(src: string, id: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const existing = document.getElementById(id) as HTMLScriptElement | null;
    if (existing) {
      if (
        (existing as HTMLScriptElement & { dataset: { loaded?: string } }).dataset.loaded ===
        "true"
      ) {
        resolve();
        return;
      }
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener("error", () => reject(new Error(`Failed to load script: ${src}`)), {
        once: true
      });
      return;
    }

    const script = document.createElement("script");
    script.id = id;
    script.src = src;
    script.async = true;
    script.onload = () => {
      script.dataset.loaded = "true";
      resolve();
    };
    script.onerror = () => reject(new Error(`Failed to load script: ${src}`));
    document.head.appendChild(script);
  });
}

type StructurePreview3DProps = {
  smiles: string;
};

export function StructurePreview3D({ smiles }: StructurePreview3DProps) {
  const viewerRef = useRef<HTMLDivElement | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    const source = smiles.trim();
    if (!source) {
      setIsLoading(false);
      setError("暂无可预览结构");
      if (viewerRef.current) {
        viewerRef.current.innerHTML = "";
      }
      return;
    }

    let cancelled = false;

    async function renderStructure() {
      setIsLoading(true);
      setError(null);

      try {
        await loadScriptOnce(D3MOL_SRC, "3dmol-script");
        if (!window.$3Dmol) {
          throw new Error("3Dmol not available");
        }

        let payload = structureCache.get(source);
        if (!payload) {
          payload = await fetchStructure3D(source);
          setCacheEntry(source, payload);
        }

        if (cancelled || !viewerRef.current) {
          return;
        }

        viewerRef.current.innerHTML = "";
        const viewer = window.$3Dmol.createViewer(viewerRef.current, {
          backgroundColor: "#f8fbff"
        });
        viewer.addModel(payload.molblock, "mol");
        viewer.setStyle(
          {},
          {
            stick: { radius: 0.2, color: "0x475569" },
            sphere: { scale: 0.34, colorscheme: "Jmol" }
          }
        );
        viewer.zoomTo();
        viewer.render();
      } catch (nextError) {
        if (!cancelled) {
          setError(nextError instanceof Error ? nextError.message : "3D render failed");
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    }

    void renderStructure();

    return () => {
      cancelled = true;
    };
  }, [smiles]);

  return (
    <Card className="overflow-hidden rounded-[28px] border-slate-200/90">
      <CardHeader className="min-h-[96px] gap-3 border-b border-slate-200/80 bg-[linear-gradient(180deg,#ffffff_0%,#f8fafc_100%)]">
        <div className="flex items-center justify-between gap-4">
          <div className="space-y-2">
            <CardTitle className="text-xl">3D Structure</CardTitle>
            <CardDescription>
              在运行查询前快速核对结构构型，作为视觉辅助检查面板。
            </CardDescription>
          </div>
          <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-slate-950 text-white">
            <Orbit className="h-4 w-4" />
          </div>
        </div>
      </CardHeader>
      <CardContent className="pt-4">
        <div className="relative h-[262px] overflow-hidden rounded-[22px] border border-slate-200 bg-[radial-gradient(circle_at_top,rgba(96,165,250,0.16),transparent_42%),linear-gradient(180deg,#fbfdff_0%,#f2f7fc_100%)]">
          <div ref={viewerRef} className="absolute inset-0" />
          {isLoading ? (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 shadow-sm">
                <Sparkles className="h-4 w-4 text-blue-600" />
                正在生成 3D 结构...
              </div>
            </div>
          ) : null}
          {error ? (
            <div className="absolute inset-0 flex items-center justify-center p-6">
              <div className="max-w-[85%] rounded-2xl border border-slate-200 bg-white px-5 py-4 text-center text-sm font-medium leading-6 text-slate-700 shadow-sm">
                {error}
              </div>
            </div>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}
