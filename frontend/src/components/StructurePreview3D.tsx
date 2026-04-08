import { useEffect, useRef, useState } from "react";
import { fetchStructure3D } from "../services/api";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";

const D3MOL_SRC = "/vendor/3Dmol-min.js";
const structureCache = new Map<string, { molblock: string; capped_smiles: string }>();

function loadScriptOnce(src: string, id: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const existing = document.getElementById(id) as HTMLScriptElement | null;
    if (existing) {
      if ((existing as HTMLScriptElement & { dataset: { loaded?: string } }).dataset.loaded === "true") {
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
      setError("No structure");
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
          structureCache.set(source, payload);
        }

        if (cancelled || !viewerRef.current) {
          return;
        }

        viewerRef.current.innerHTML = "";
        const viewer = window.$3Dmol.createViewer(viewerRef.current, {
          backgroundColor: "#ffffff"
        });
        viewer.addModel(payload.molblock, "mol");
        viewer.setStyle(
          {},
          {
            stick: { radius: 0.2, color: "0x4b5563" },
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
    <Card className="overflow-hidden border-white/70 bg-white">
      <CardHeader className="pb-4">
        <CardTitle>3D Structure</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="relative h-[292px] overflow-hidden rounded-2xl border border-slate-200 bg-white">
          <div ref={viewerRef} className="absolute inset-0" />
          {isLoading ? (
            <div className="absolute inset-0 flex items-center justify-center text-sm font-medium text-slate-700">
              Loading 3D structure...
            </div>
          ) : null}
          {error ? (
            <div className="absolute inset-0 flex items-center justify-center p-6">
              <div className="max-w-[85%] rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-center text-sm font-medium leading-6 text-amber-950 shadow-sm">
                {error}
              </div>
            </div>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}
