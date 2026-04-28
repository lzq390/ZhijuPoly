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
      setError("No structure available for preview");
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
    <Card className="overflow-hidden rounded-[30px] border-white/70">
      <CardHeader className="min-h-[112px] gap-3 border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.96)_0%,rgba(244,248,249,0.86)_100%)]">
        <div className="flex items-center justify-between gap-4">
          <div className="space-y-2">
            <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-sky-700/80">
              Spatial Preview
            </div>
            <CardTitle className="text-[1.35rem] tracking-tight">3D Structure</CardTitle>
            <CardDescription>
              Review the generated 3D conformation before running similarity matching.
            </CardDescription>
          </div>
          <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-slate-950 text-white shadow-[0_12px_30px_rgba(8,17,31,0.18)]">
            <Orbit className="h-4 w-4" />
          </div>
        </div>
      </CardHeader>
      <CardContent className="pt-4">
        <div className="relative h-[280px] overflow-hidden rounded-[24px] border border-white/80 bg-[radial-gradient(circle_at_top,rgba(56,189,248,0.2),transparent_42%),radial-gradient(circle_at_80%_20%,rgba(20,184,166,0.12),transparent_26%),linear-gradient(180deg,#fbfdff_0%,#edf5f8_100%)]">
          <div className="pointer-events-none absolute left-1/2 top-6 h-24 w-24 -translate-x-1/2 rounded-full bg-white/60 blur-2xl" />
          <div ref={viewerRef} className="absolute inset-0" />
          {isLoading ? (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="inline-flex items-center gap-2 rounded-full border border-white/80 bg-white/90 px-4 py-2 text-sm font-medium text-slate-700 shadow-sm backdrop-blur">
                <Sparkles className="h-4 w-4 text-teal-600" />
                Generating 3D structure...
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
