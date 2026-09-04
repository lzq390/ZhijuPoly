import { Atom, ChevronLeft, ChevronRight, Loader2, Orbit, TriangleAlert } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { MonomerDftAtom, MonomerDftCalculationType } from "../types";

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
  calculationType: MonomerDftCalculationType;
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
        reject(new Error("三维预览组件加载失败。"));
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
      reject(new Error("三维预览组件加载失败。"));
    };
    document.head.appendChild(script);
  });
}

function atomElement(atom: MonomerDftAtom): string {
  return atom.element || ELEMENT_BY_ATOMIC_NUMBER[atom.atomic_number] || "X";
}

export function atomsToXyz(atoms: MonomerDftAtom[], label = "calculated geometry"): string {
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

export function MoleculeCoordinates3D({
  frames,
  calculationType,
  className
}: MoleculeCoordinates3DProps) {
  const viewerRef = useRef<HTMLDivElement | null>(null);
  const viewerInstanceRef = useRef<ThreeDMolViewer | null>(null);
  const [frameIndex, setFrameIndex] = useState(Math.max(0, frames.length - 1));
  const [showCharges, setShowCharges] = useState(false);
  const [showForces, setShowForces] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [renderRevision, setRenderRevision] = useState(0);

  useEffect(() => {
    setFrameIndex(Math.max(0, frames.length - 1));
  }, [frames]);

  const frame = frames[frameIndex] ?? null;
  const hasTrajectory = frames.some((item) => item.kind === "trajectory");
  const frameKinds: MoleculeCoordinateFrame["kind"][] = calculationType === "single_point"
    ? ["initial", "final"]
    : ["initial", "trajectory", "final"];
  const hasCharges = Boolean(frame?.atoms.some((atom) => atom.charge_e != null));
  const hasForces = Boolean(frame?.atoms.some((atom) => vectorNorm(atom.force_ev_per_angstrom) > 0));

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
          setError(nextError instanceof Error ? nextError.message : "三维结构渲染失败。");
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
  }, [frame, renderRevision, showCharges, showForces]);

  useEffect(() => {
    const target = viewerRef.current;
    if (!target) return;
    let animationFrame: number | null = null;
    const resizeViewer = () => {
      if (animationFrame != null) window.cancelAnimationFrame(animationFrame);
      animationFrame = window.requestAnimationFrame(() => {
        animationFrame = null;
        viewerInstanceRef.current?.resize?.();
        viewerInstanceRef.current?.render();
      });
    };
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", resizeViewer);
      return () => {
        window.removeEventListener("resize", resizeViewer);
        if (animationFrame != null) window.cancelAnimationFrame(animationFrame);
      };
    }
    const observer = new ResizeObserver(resizeViewer);
    observer.observe(target);
    return () => {
      observer.disconnect();
      if (animationFrame != null) window.cancelAnimationFrame(animationFrame);
    };
  }, []);

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
    <section className={`np-dft-coordinate-viewer${className ? ` ${className}` : ""}`}>
      <div className="np-dft-coordinate-viewer__header">
        <div>
          <div><Orbit />显式坐标 3D</div>
          <p>{calculationType === "single_point"
            ? "单点计算只评估固定结构，不更新坐标，因此不生成优化轨迹。"
            : "使用计算返回的原子坐标；轨迹帧展示几何优化过程。"}</p>
        </div>
        <div className="np-dft-frame-kinds" role="group" aria-label="结构帧类型">
          {frameKinds.map((kind) => {
            const available = frames.some((item) => item.kind === kind);
            const label = kind === "initial" ? "初始" : kind === "trajectory" ? "轨迹" : "最终";
            const selected = frame?.kind === kind;
            return (
              <button
                key={kind}
                type="button"
                disabled={!available}
                aria-pressed={selected}
                className={selected ? "is-active" : ""}
                title={!available && kind === "trajectory" ? "当前结果没有可用的中间轨迹帧" : undefined}
                onClick={() => selectKind(kind)}
              >
                {label}
              </button>
            );
          })}
        </div>
      </div>
      <div className="np-dft-coordinate-viewer__canvas">
        <div ref={viewerRef} aria-label="计算结构三维预览" />
        {!frame ? <div className="np-dft-viewer-state"><Atom />计算完成后显示显式原子坐标</div> : null}
        {isLoading ? <div className="np-dft-viewer-state is-loading"><Loader2 className="np-dft-spin" /><span>正在渲染三维结构</span></div> : null}
        {error ? (
          <div className="np-dft-viewer-state is-error" role="alert">
            <TriangleAlert />
            <div><strong>三维结构加载失败</strong><span>{error}</span></div>
            <button type="button" onClick={() => setRenderRevision((value) => value + 1)}>重新加载</button>
          </div>
        ) : null}
      </div>
      <div className="np-dft-coordinate-viewer__controls">
        <div className="np-dft-frame-controls">
          <div>
            <button type="button" disabled={frameIndex <= 0} onClick={() => setFrameIndex((value) => Math.max(0, value - 1))} aria-label="上一帧"><ChevronLeft /></button>
            <div><strong>{frame?.label ?? "暂无帧"}</strong>{frame?.energyEv != null ? <span>{frame.energyEv.toFixed(6)} eV</span> : null}</div>
            <button type="button" disabled={frameIndex >= frames.length - 1} onClick={() => setFrameIndex((value) => Math.min(frames.length - 1, value + 1))} aria-label="下一帧"><ChevronRight /></button>
          </div>
          <div className="np-dft-view-options">
            <label title={hasCharges ? undefined : "当前帧没有电荷数据"}>
              <input
                type="checkbox"
                checked={showCharges && hasCharges}
                disabled={!hasCharges}
                onChange={(event) => setShowCharges(event.target.checked)}
              />电荷着色
            </label>
            <label title={hasForces ? undefined : "当前帧没有可显示的原子力"}>
              <input
                type="checkbox"
                checked={showForces && hasForces}
                disabled={!hasForces}
                onChange={(event) => setShowForces(event.target.checked)}
              />力箭头
            </label>
          </div>
        </div>
        {hasTrajectory ? (
          <input
            type="range"
            min={0}
            max={frames.length - 1}
            value={frameIndex}
            onChange={(event) => setFrameIndex(Number(event.target.value))}
            aria-label="优化轨迹帧"
            aria-valuetext={`${frame?.label ?? "暂无帧"}，第 ${frameIndex + 1} / ${frames.length} 帧`}
          />
        ) : null}
      </div>
    </section>
  );
}
