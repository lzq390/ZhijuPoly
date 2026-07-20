import { Atom, ChevronLeft, ChevronRight, Loader2, Orbit, TriangleAlert } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { cn } from "../lib/utils";
import type { MonomerDftAtom } from "../types";
import { Button } from "./ui/button";

const D3MOL_SRC = "/vendor/3Dmol-min.js";
type ThreeDMolViewer = ReturnType<NonNullable<Window["$3Dmol"]>["createViewer"]>;
const ELEMENT_BY_ATOMIC_NUMBER: Record<number, string> = {
  1: "H", 5: "B", 6: "C", 7: "N", 8: "O", 9: "F", 14: "Si", 15: "P", 16: "S",
  17: "Cl", 33: "As", 34: "Se", 35: "Br", 46: "Pd", 53: "I"
};

export type MoleculeCoordinateFrame = {
  id: string;
  label: string;
  kind: "initial" | "trajectory" | "final";
  atoms: MonomerDftAtom[];
  step?: number;
  energyEv?: number | null;
};

type MoleculeCoordinates3DProps = {
  frames: MoleculeCoordinateFrame[];
  className?: string;
};

function load3Dmol(): Promise<void> {
  return new Promise((resolve, reject) => {
    if (window.$3Dmol) {
      resolve();
      return;
    }
    const existing = document.getElementById("3dmol-script") as HTMLScriptElement | null;
    if (existing) {
      if (existing.dataset.loaded === "true") {
        resolve();
        return;
      }
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener("error", () => {
        existing.remove();
        reject(new Error("3Dmol 加载失败。"));
      }, { once: true });
      return;
    }
    const script = document.createElement("script");
    script.id = "3dmol-script";
    script.src = D3MOL_SRC;
    script.async = true;
    script.onload = () => {
      script.dataset.loaded = "true";
      resolve();
    };
    script.onerror = () => {
      script.remove();
      reject(new Error("3Dmol 加载失败。"));
    };
    document.head.appendChild(script);
  });
}

function atomElement(atom: MonomerDftAtom): string {
  return atom.element || ELEMENT_BY_ATOMIC_NUMBER[atom.atomic_number] || "X";
}

export function atomsToXyz(atoms: MonomerDftAtom[], label = "AIMNet2 geometry"): string {
  return [
    String(atoms.length),
    label,
    ...atoms.map((atom) => {
      const [x, y, z] = atom.position_angstrom;
      return `${atomElement(atom)} ${x.toFixed(10)} ${y.toFixed(10)} ${z.toFixed(10)}`;
    })
  ].join("\n");
}

function vectorNorm(vector: [number, number, number] | null | undefined): number {
  if (!vector) {
    return 0;
  }
  return Math.hypot(vector[0], vector[1], vector[2]);
}

function chargeColor(charge: number): string {
  const magnitude = Math.min(1, Math.abs(charge) / 0.6);
  const channel = Math.round(245 - magnitude * 150).toString(16).padStart(2, "0");
  if (charge > 0.02) {
    return `#ef${channel}${channel}`;
  }
  if (charge < -0.02) {
    return `#${channel}${channel}ef`;
  }
  return "#94a3b8";
}

export function MoleculeCoordinates3D({ frames, className }: MoleculeCoordinates3DProps) {
  const viewerRef = useRef<HTMLDivElement | null>(null);
  const viewerInstanceRef = useRef<ThreeDMolViewer | null>(null);
  const [frameIndex, setFrameIndex] = useState(Math.max(0, frames.length - 1));
  const [showCharges, setShowCharges] = useState(false);
  const [showForces, setShowForces] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setFrameIndex(Math.max(0, frames.length - 1));
  }, [frames.length]);

  const frame = frames[frameIndex] ?? null;
  const hasCharges = useMemo(() => frames.some((item) => item.atoms.some((atom) => atom.charge_e != null)), [frames]);
  const hasForces = useMemo(() => frames.some((item) => item.atoms.some((atom) => vectorNorm(atom.force_ev_per_angstrom) > 0)), [frames]);

  useEffect(() => {
    if (!frame || frame.atoms.length === 0) {
      viewerInstanceRef.current?.clear();
      viewerInstanceRef.current?.render();
      return;
    }
    let cancelled = false;
    setIsLoading(true);
    setError(null);

    async function renderFrame() {
      try {
        await load3Dmol();
        if (cancelled || !viewerRef.current || !window.$3Dmol) {
          return;
        }
        let viewer = viewerInstanceRef.current;
        if (viewer == null) {
          viewer = window.$3Dmol.createViewer(viewerRef.current, { backgroundColor: "#f8fafc" });
          viewerInstanceRef.current = viewer;
        } else {
          viewer.clear();
          viewer.removeAllShapes?.();
          viewer.setBackgroundColor("#f8fafc");
        }
        viewer.addModel(atomsToXyz(frame.atoms, frame.label), "xyz");
        viewer.setStyle({}, {
          stick: { radius: 0.15, color: "#64748b" },
          sphere: { scale: 0.32, colorscheme: "Jmol" }
        });
        if (showCharges) {
          frame.atoms.forEach((atom, index) => {
            if (atom.charge_e == null) {
              return;
            }
            const style = {
              stick: { radius: 0.15, color: "#64748b" },
              sphere: { scale: 0.36, color: chargeColor(atom.charge_e) }
            };
            if (viewer.addStyle) {
              viewer.addStyle({ index }, style);
            } else {
              viewer.setStyle({ index }, style);
            }
          });
        }
        if (showForces && viewer.addArrow) {
          const maxForce = Math.max(...frame.atoms.map((atom) => vectorNorm(atom.force_ev_per_angstrom)), 0);
          if (maxForce > 0) {
            frame.atoms.forEach((atom) => {
              const force = atom.force_ev_per_angstrom;
              if (!force || vectorNorm(force) === 0) {
                return;
              }
              const [x, y, z] = atom.position_angstrom;
              const scale = 1.25 / maxForce;
              viewer.addArrow?.({
                start: { x, y, z },
                end: { x: x + force[0] * scale, y: y + force[1] * scale, z: z + force[2] * scale },
                radius: 0.045,
                radiusRatio: 1.8,
                mid: 0.72,
                color: "#f97316"
              });
            });
          }
        }
        viewer.zoomTo();
        viewer.render();
      } catch (nextError) {
        if (!cancelled) {
          setError(nextError instanceof Error ? nextError.message : "显式坐标 3D 渲染失败。");
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    }
    void renderFrame();
    return () => {
      cancelled = true;
    };
  }, [frame, showCharges, showForces]);

  useEffect(() => () => {
    viewerInstanceRef.current?.clear();
    viewerInstanceRef.current = null;
    if (viewerRef.current) viewerRef.current.innerHTML = "";
  }, []);

  function selectKind(kind: MoleculeCoordinateFrame["kind"]): void {
    const index = kind === "final"
      ? frames.map((item) => item.kind).lastIndexOf(kind)
      : frames.findIndex((item) => item.kind === kind);
    if (index >= 0) {
      setFrameIndex(index);
    }
  }

  return (
    <section className={cn("overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm", className)}>
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-4 py-3">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold text-slate-950"><Orbit className="h-4 w-4 text-sky-600" />显式坐标 3D</div>
          <div className="mt-1 text-xs text-slate-500">直接渲染 Worker 返回坐标，不从 SMILES 重建几何。</div>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {(["initial", "trajectory", "final"] as const).map((kind) => {
            const available = frames.some((item) => item.kind === kind);
            const label = kind === "initial" ? "初始" : kind === "trajectory" ? "轨迹" : "最终";
            return <button key={kind} type="button" disabled={!available} onClick={() => selectKind(kind)} className="rounded-md border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:opacity-35">{label}</button>;
          })}
        </div>
      </div>
      <div className="relative h-[360px] bg-slate-50">
        <div ref={viewerRef} className="absolute inset-0" aria-label="AIMNet2 计算结构三维预览" />
        {!frame ? <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-sm text-slate-500"><Atom className="h-7 w-7 text-slate-300" />计算完成后显示显式原子坐标</div> : null}
        {isLoading ? <div className="absolute inset-0 flex items-center justify-center bg-white/60"><Loader2 className="h-5 w-5 animate-spin text-sky-600" /></div> : null}
        {error ? <div className="absolute inset-0 flex items-center justify-center p-6"><div className="flex max-w-md items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"><TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />{error}</div></div> : null}
      </div>
      <div className="border-t border-slate-100 px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Button type="button" variant="outline" className="h-8 w-8 rounded-md p-0" disabled={frameIndex <= 0} onClick={() => setFrameIndex((value) => Math.max(0, value - 1))} aria-label="上一帧"><ChevronLeft className="h-4 w-4" /></Button>
            <div className="min-w-[160px] text-center text-xs text-slate-600"><span className="font-semibold text-slate-900">{frame?.label ?? "暂无帧"}</span>{frame?.energyEv != null ? ` · ${frame.energyEv.toFixed(6)} eV` : ""}</div>
            <Button type="button" variant="outline" className="h-8 w-8 rounded-md p-0" disabled={frameIndex >= frames.length - 1} onClick={() => setFrameIndex((value) => Math.min(frames.length - 1, value + 1))} aria-label="下一帧"><ChevronRight className="h-4 w-4" /></Button>
          </div>
          <div className="flex items-center gap-3 text-xs text-slate-600">
            <label className="inline-flex items-center gap-1.5"><input type="checkbox" checked={showCharges} disabled={!hasCharges} onChange={(event) => setShowCharges(event.target.checked)} />电荷着色</label>
            <label className="inline-flex items-center gap-1.5"><input type="checkbox" checked={showForces} disabled={!hasForces} onChange={(event) => setShowForces(event.target.checked)} />力箭头</label>
          </div>
        </div>
        {frames.length > 1 ? <input className="mt-3 w-full accent-sky-600" type="range" min={0} max={frames.length - 1} value={frameIndex} onChange={(event) => setFrameIndex(Number(event.target.value))} aria-label="优化轨迹帧" /> : null}
      </div>
    </section>
  );
}
