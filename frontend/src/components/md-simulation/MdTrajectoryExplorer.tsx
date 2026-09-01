import {
  Atom,
  CircleDot,
  LoaderCircle,
  MousePointer2,
  Play,
  RotateCcw,
  Ruler,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type {
  MdDemoAtomDistanceResponse,
  MdDemoAtomSelection,
  MdDemoTrajectoryPoint
} from "../../types";
import { MdSeriesChart } from "./MdSeriesChart";

export type MdAtomSelectionSlots = [MdDemoAtomSelection | null, MdDemoAtomSelection | null];

type TrajectoryView = { rotationX: number; rotationY: number; zoom: number };
type CanvasSize = { width: number; height: number };
type DragState = {
  pointerId: number;
  startX: number;
  startY: number;
  moved: boolean;
  view: TrajectoryView;
};
type TrajectoryCloud = {
  points: MdDemoTrajectoryPoint[];
  center: { x: number; y: number; z: number };
  radius: number;
};
type ProjectedTrajectoryPoint = {
  point: MdDemoTrajectoryPoint;
  x: number;
  y: number;
  z: number;
  depth: number;
};

const INITIAL_VIEW: TrajectoryView = { rotationX: -0.45, rotationY: 0.62, zoom: 1 };

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function selectionFromPoint(point: MdDemoTrajectoryPoint): MdDemoAtomSelection {
  return { atom_id: point.atom_id, chain_id: point.chain_id, atom_type: point.atom_type };
}

export function nextMdAtomSelection(
  current: MdAtomSelectionSlots,
  atom: MdDemoAtomSelection
): MdAtomSelectionSlots {
  if (current.some((item) => item?.atom_id === atom.atom_id)) return current;
  if (!current[0]) return [atom, current[1]];
  if (!current[1]) return [current[0], atom];
  return [current[1], atom];
}

function atomTypeToColor(atomType: string) {
  const colors: Record<string, string> = {
    "1": "#64748b",
    "2": "#22c55e",
    "3": "#ef4444",
    "4": "#2563eb",
    "5": "#a16207",
    "6": "#0891b2",
    "7": "#16a34a",
    "8": "#7c3aed"
  };
  return colors[atomType] ?? "#475569";
}

function buildTrajectoryCloud(points: MdDemoTrajectoryPoint[]): TrajectoryCloud {
  if (!points.length) return { points, center: { x: 0, y: 0, z: 0 }, radius: 1 };
  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let minZ = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  let maxZ = Number.NEGATIVE_INFINITY;
  points.forEach((point) => {
    minX = Math.min(minX, point.x);
    minY = Math.min(minY, point.y);
    minZ = Math.min(minZ, point.z);
    maxX = Math.max(maxX, point.x);
    maxY = Math.max(maxY, point.y);
    maxZ = Math.max(maxZ, point.z);
  });
  return {
    points,
    center: { x: (minX + maxX) / 2, y: (minY + maxY) / 2, z: (minZ + maxZ) / 2 },
    radius: Math.max(maxX - minX, maxY - minY, maxZ - minZ, 1) / 2
  };
}

function projectTrajectoryPoints(
  cloud: TrajectoryCloud,
  view: TrajectoryView,
  size: CanvasSize
): ProjectedTrajectoryPoint[] {
  const scale = (Math.min(size.width, size.height) * 0.42 * view.zoom) / cloud.radius;
  const cosX = Math.cos(view.rotationX);
  const sinX = Math.sin(view.rotationX);
  const cosY = Math.cos(view.rotationY);
  const sinY = Math.sin(view.rotationY);
  return cloud.points.map((point) => {
    const x = point.x - cloud.center.x;
    const y = point.y - cloud.center.y;
    const z = point.z - cloud.center.z;
    const rotatedX = x * cosY + z * sinY;
    const zAfterY = -x * sinY + z * cosY;
    const rotatedY = y * cosX - zAfterY * sinX;
    const rotatedZ = y * sinX + zAfterY * cosX;
    return {
      point,
      x: size.width / 2 + rotatedX * scale,
      y: size.height / 2 - rotatedY * scale,
      z: rotatedZ,
      depth: clamp((rotatedZ / cloud.radius + 1) / 2, 0, 1)
    };
  });
}

function drawTrajectoryCloud(
  canvas: HTMLCanvasElement | null,
  cloud: TrajectoryCloud,
  selectedAtomIds: Set<number>,
  view: TrajectoryView,
  size: CanvasSize
) {
  if (!canvas || size.width <= 0 || size.height <= 0) return;
  const context = canvas.getContext("2d");
  if (!context) return;
  const pixelRatio = window.devicePixelRatio || 1;
  canvas.width = Math.round(size.width * pixelRatio);
  canvas.height = Math.round(size.height * pixelRatio);
  canvas.style.width = `${size.width}px`;
  canvas.style.height = `${size.height}px`;
  context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  context.clearRect(0, 0, size.width, size.height);
  context.fillStyle = "#f8fbff";
  context.fillRect(0, 0, size.width, size.height);
  context.strokeStyle = "rgba(37, 99, 235, 0.055)";
  context.lineWidth = 1;
  for (let x = 24; x < size.width; x += 24) {
    context.beginPath();
    context.moveTo(x, 0);
    context.lineTo(x, size.height);
    context.stroke();
  }
  for (let y = 24; y < size.height; y += 24) {
    context.beginPath();
    context.moveTo(0, y);
    context.lineTo(size.width, y);
    context.stroke();
  }
  const projected = projectTrajectoryPoints(cloud, view, size).sort((left, right) => left.z - right.z);
  projected.forEach((item) => {
    const selected = selectedAtomIds.has(item.point.atom_id);
    const radius = selected ? 5.2 : 1.35 + item.depth * 0.8;
    context.beginPath();
    context.arc(item.x, item.y, radius, 0, Math.PI * 2);
    context.fillStyle = selected ? "#f59e0b" : atomTypeToColor(item.point.atom_type);
    context.globalAlpha = selected ? 1 : 0.62 + item.depth * 0.32;
    context.fill();
    if (selected) {
      context.globalAlpha = 0.85;
      context.lineWidth = 2;
      context.strokeStyle = "#92400e";
      context.stroke();
    }
  });
  context.globalAlpha = 1;
}

function pickTrajectoryPoint(
  cloud: TrajectoryCloud,
  view: TrajectoryView,
  size: CanvasSize,
  x: number,
  y: number
) {
  const projected = projectTrajectoryPoints(cloud, view, size);
  let bestPoint: MdDemoTrajectoryPoint | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  let bestZ = Number.NEGATIVE_INFINITY;
  projected.forEach((item) => {
    const distance = Math.hypot(item.x - x, item.y - y);
    const closer = distance < bestDistance - 0.3;
    const sameSpot = Math.abs(distance - bestDistance) <= 0.3 && item.z > bestZ;
    if (distance <= 20 && (closer || sameSpot)) {
      bestPoint = item.point;
      bestDistance = distance;
      bestZ = item.z;
    }
  });
  return bestPoint;
}

function TrajectoryCanvas({
  points,
  selections,
  onAtomSelect
}: {
  points: MdDemoTrajectoryPoint[];
  selections: MdAtomSelectionSlots;
  onAtomSelect: (atom: MdDemoAtomSelection) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const dragStateRef = useRef<DragState | null>(null);
  const [view, setView] = useState<TrajectoryView>(INITIAL_VIEW);
  const [canvasSize, setCanvasSize] = useState<CanvasSize>({ width: 0, height: 0 });
  const cloud = useMemo(() => buildTrajectoryCloud(points), [points]);
  const selectedIds = useMemo(
    () => new Set(selections.flatMap((selection) => selection ? [selection.atom_id] : [])),
    [selections]
  );

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const resize = () => {
      const rect = canvas.parentElement?.getBoundingClientRect();
      if (rect) setCanvasSize({ width: Math.max(1, rect.width), height: Math.max(1, rect.height) });
    };
    resize();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(resize);
    if (canvas.parentElement) observer.observe(canvas.parentElement);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    drawTrajectoryCloud(canvasRef.current, cloud, selectedIds, view, canvasSize);
  }, [canvasSize, cloud, selectedIds, view]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const handleWheel = (event: WheelEvent) => {
      event.preventDefault();
      event.stopPropagation();
      setView((current) => ({
        ...current,
        zoom: clamp(current.zoom * (event.deltaY > 0 ? 0.9 : 1.1), 0.55, 2.7)
      }));
    };
    canvas.addEventListener("wheel", handleWheel, { passive: false });
    return () => canvas.removeEventListener("wheel", handleWheel);
  }, []);

  return (
    <div className="np-md-trajectory-view">
      <div className="np-md-trajectory-view__toolbar">
        <span id="md-trajectory-help">
          <MousePointer2 />拖拽旋转，滚轮缩放，点击选择原子
        </span>
        <button type="button" onClick={() => setView(INITIAL_VIEW)}>
          <RotateCcw />重置视角
        </button>
      </div>
      <div className="np-md-trajectory-canvas">
        <div className="np-md-canvas-hud" aria-hidden="true">
          <span>
            <i /> 交互视图
          </span>
          <b>EQ3 · 最终帧</b>
        </div>
        <canvas
          ref={canvasRef}
          tabIndex={0}
          role="img"
          aria-label="最终帧原子分布，可拖拽旋转并点击选择原子"
          aria-describedby="md-trajectory-help"
          onPointerDown={(event) => {
            dragStateRef.current = {
              pointerId: event.pointerId,
              startX: event.clientX,
              startY: event.clientY,
              moved: false,
              view
            };
            event.currentTarget.setPointerCapture(event.pointerId);
          }}
          onPointerMove={(event) => {
            const drag = dragStateRef.current;
            if (!drag || drag.pointerId !== event.pointerId) return;
            const deltaX = event.clientX - drag.startX;
            const deltaY = event.clientY - drag.startY;
            if (Math.abs(deltaX) > 3 || Math.abs(deltaY) > 3) drag.moved = true;
            setView({
              ...drag.view,
              rotationX: clamp(drag.view.rotationX + deltaY * 0.008, -Math.PI / 2, Math.PI / 2),
              rotationY: drag.view.rotationY + deltaX * 0.008
            });
          }}
          onPointerUp={(event) => {
            const drag = dragStateRef.current;
            if (!drag || drag.pointerId !== event.pointerId) return;
            dragStateRef.current = null;
            event.currentTarget.releasePointerCapture(event.pointerId);
            if (drag.moved) return;
            const rect = event.currentTarget.getBoundingClientRect();
            const point = pickTrajectoryPoint(
              cloud,
              view,
              { width: rect.width, height: rect.height },
              event.clientX - rect.left,
              event.clientY - rect.top
            );
            if (point) onAtomSelect(selectionFromPoint(point));
          }}
          onPointerCancel={() => { dragStateRef.current = null; }}
        />
        <div className="np-md-canvas-corners" aria-hidden="true">
          <i />
          <i />
          <i />
          <i />
        </div>
        <div className="np-md-canvas-axis" aria-hidden="true">
          <span className="is-x">X</span>
          <span className="is-y">Y</span>
          <span className="is-z">Z</span>
        </div>
        {!points.length ? <div className="np-md-trajectory-empty">暂无轨迹坐标可显示。</div> : null}
      </div>
    </div>
  );
}

type MdTrajectoryExplorerProps = {
  points: MdDemoTrajectoryPoint[];
  timePs: number;
  selections: MdAtomSelectionSlots;
  distance: MdDemoAtomDistanceResponse | null;
  distanceLoading: boolean;
  distanceError: string | null;
  onSelectionChange: (selection: MdAtomSelectionSlots) => void;
  onCalculate: () => void;
  onClear: () => void;
};

export function MdTrajectoryExplorer({
  points,
  timePs,
  selections,
  distance,
  distanceLoading,
  distanceError,
  onSelectionChange,
  onCalculate,
  onClear
}: MdTrajectoryExplorerProps) {
  const atomLookup = useMemo(() => new Map(points.map((point) => [point.atom_id, selectionFromPoint(point)])), [points]);
  const [idValues, setIdValues] = useState<[string, string]>(["", ""]);
  const [selectionError, setSelectionError] = useState<string | null>(null);

  useEffect(() => {
    setIdValues([selections[0]?.atom_id.toString() ?? "", selections[1]?.atom_id.toString() ?? ""]);
  }, [selections]);

  function applyId(index: 0 | 1) {
    const raw = idValues[index].trim();
    if (!raw) {
      const next: MdAtomSelectionSlots = [...selections] as MdAtomSelectionSlots;
      next[index] = null;
      setSelectionError(null);
      onSelectionChange(next);
      return;
    }
    const atomId = Number(raw);
    const atom = Number.isInteger(atomId) ? atomLookup.get(atomId) : null;
    if (!atom) {
      setSelectionError(`轨迹中没有编号为 ${raw} 的原子，请重新输入。`);
      return;
    }
    const other = selections[index === 0 ? 1 : 0];
    if (other?.atom_id === atom.atom_id) {
      setSelectionError("请选择两个不同的原子。");
      return;
    }
    const next: MdAtomSelectionSlots = [...selections] as MdAtomSelectionSlots;
    next[index] = atom;
    setSelectionError(null);
    onSelectionChange(next);
  }

  return (
    <div className="np-md-trajectory-explorer">
      <section className="np-md-result-card">
        <header>
          <div className="np-md-card-heading">
            <span className="np-md-card-mark is-cyan">
              <Atom aria-hidden="true" />
            </span>
            <div>
              <h3>最终帧原子分布</h3>
              <p>拖拽与缩放浏览空间结构，点击可选择原子。</p>
            </div>
          </div>
          <span className="np-md-trajectory-meta">
            <i /> EQ3 · {points.length} 个原子 · {timePs.toFixed(2)} ps
          </span>
        </header>
        <TrajectoryCanvas
          points={points}
          selections={selections}
          onAtomSelect={(atom) => {
            setSelectionError(null);
            onSelectionChange(nextMdAtomSelection(selections, atom));
          }}
        />
      </section>

      <section className="np-md-result-card np-md-distance-panel">
        <header>
          <div className="np-md-card-heading">
            <span className="np-md-card-mark is-violet">
              <Ruler aria-hidden="true" />
            </span>
            <div>
              <h3>原子对距离</h3>
              <p>选择两个原子，分析随时间变化的三维距离。</p>
            </div>
          </div>
          <span className="np-md-pbc-badge">
            <ShieldCheck aria-hidden="true" /> PBC 已启用
          </span>
        </header>
        <div className="np-md-atom-pair-rail" aria-hidden="true">
          <span>A</span>
          <i />
          <CircleDot />
          <i />
          <span>B</span>
        </div>
        <div className="np-md-atom-fields">
          {([0, 1] as const).map((index) => (
            <label key={index} className={index === 0 ? "is-a" : "is-b"}>
              <span>
                <b>{index === 0 ? "A" : "B"}</b>
                原子 {index + 1} 编号
              </span>
              <input
                type="number"
                min="1"
                inputMode="numeric"
                value={idValues[index]}
                placeholder="输入原子编号"
                onChange={(event) => {
                  const next: [string, string] = [...idValues] as [string, string];
                  next[index] = event.target.value;
                  setIdValues(next);
                  setSelectionError(null);
                }}
                onBlur={() => applyId(index)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    applyId(index);
                  }
                }}
              />
              <small>
                {selections[index]
                  ? `链 ${selections[index]?.chain_id} · 类型 ${selections[index]?.atom_type}`
                  : "可点击图中原子或直接输入编号"}
              </small>
            </label>
          ))}
        </div>
        {selectionError ? <p className="np-md-inline-error" role="alert">{selectionError}</p> : null}
        {distanceError ? <p className="np-md-inline-error" role="alert">{distanceError}</p> : null}
        <div className="np-md-distance-actions">
          <button
            type="button"
            className="is-primary"
            disabled={distanceLoading || !selections[0] || !selections[1]}
            onClick={onCalculate}
          >
            {distanceLoading ? <LoaderCircle className="np-sw-spin" /> : <Play />}
            {distanceLoading ? "计算中" : "计算距离"}
          </button>
          <button
            type="button"
            disabled={distanceLoading || (!selections[0] && !selections[1])}
            onClick={onClear}
          >
            <Trash2 />清空选择
          </button>
        </div>
        <div className="np-md-distance-output" aria-live="polite">
          {distanceLoading ? (
            <div className="np-md-distance-state is-loading">
              <span className="np-md-distance-state__icon">
                <LoaderCircle className="np-sw-spin" />
              </span>
              <div>
                <strong>正在分析原子对距离</strong>
                <span>正在读取轨迹中的采样帧</span>
              </div>
            </div>
          ) : distance ? (
            <div className="np-md-distance-chart">
              <MdSeriesChart
                series={distance.series}
                label="原子对距离"
                color="#7c3aed"
              />
              <div className="np-md-distance-stats">
                <span><i />{distance.stats.n_frames} 个采样帧</span>
                <span><i />源轨迹 {distance.stats.source_n_frames} 帧</span>
                <span><i />PBC 已启用</span>
              </div>
            </div>
          ) : (
            <div className="np-md-distance-state">
              <span className="np-md-distance-state__icon">
                <Ruler aria-hidden="true" />
              </span>
              <div>
                <strong>等待选择原子对</strong>
                <span>可点击左侧轨迹或输入原子编号。</span>
              </div>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
