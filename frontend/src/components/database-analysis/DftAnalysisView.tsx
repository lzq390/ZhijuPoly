import { Atom, CircleAlert, Orbit, RotateCw } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  browseDftMolecules,
  fetchDftMolecule,
  fetchDftPcaSample
} from "../../services/api";
import type { DftMoleculeBrowserRecord, DftMoleculeDetail, DftPcaPoint } from "../../types";
import { DataTable, EmptyPanel, formatNumber, KpiStrip, Panel } from "./charts";
import { databaseAnalysisErrorMessage } from "./errors";
import type { DftAnalytics, DftTabKey, DrawerRequest } from "./types";

const D3MOL_SRC = "/vendor/3Dmol-min.js";
type AtomCoordinate = [number, number, number, number];

function loadScriptOnce(src: string, id: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const existing = document.getElementById(id) as HTMLScriptElement | null;
    if (existing) {
      if (existing.dataset.loaded === "true") resolve();
      else {
        existing.addEventListener("load", () => resolve(), { once: true });
        existing.addEventListener("error", () => reject(new Error(`Failed to load script: ${src}`)), { once: true });
      }
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

function atomSymbol(atom: number) {
  return ({ 1: "H", 5: "B", 6: "C", 7: "N", 8: "O", 9: "F", 14: "Si", 15: "P", 16: "S", 17: "Cl" } as Record<number, string>)[atom] ?? String(atom);
}

function distance(a: AtomCoordinate, b: AtomCoordinate) {
  return Math.hypot(a[1] - b[1], a[2] - b[2], a[3] - b[3]);
}

function toMolBlock(molId: string, coordinates: AtomCoordinate[]) {
  const bonds: Array<{ from: number; to: number }> = [];
  coordinates.forEach((a, from) => {
    coordinates.slice(from + 1).forEach((b, offset) => {
      const to = from + offset + 1;
      const threshold = a[0] === 1 || b[0] === 1 ? 1.22 : 1.72;
      if (distance(a, b) <= threshold) bonds.push({ from, to });
    });
  });
  const atoms = coordinates.map((coordinate) => {
    const x = coordinate[1].toFixed(4).padStart(10);
    const y = coordinate[2].toFixed(4).padStart(10);
    const z = coordinate[3].toFixed(4).padStart(10);
    return `${x}${y}${z} ${atomSymbol(coordinate[0]).padEnd(3)} 0  0  0  0  0  0  0  0  0  0  0  0`;
  });
  const bondLines = bonds.map((bond) => `${String(bond.from + 1).padStart(3)}${String(bond.to + 1).padStart(3)}  1  0  0  0  0`);
  return [
    molId,
    "  NexPoly DFT",
    "",
    `${String(coordinates.length).padStart(3)}${String(bonds.length).padStart(3)}  0  0  0  0            999 V2000`,
    ...atoms,
    ...bondLines,
    "M  END"
  ].join("\n");
}

export function DftAnalysisView({
  data,
  recordCount,
  onOpenRecords
}: {
  data: DftAnalytics;
  recordCount: number | null;
  onOpenRecords: (request: DrawerRequest, trigger?: HTMLElement) => void;
}) {
  const [tab, setTab] = useState<DftTabKey>("analysis");
  const [points, setPoints] = useState<DftPcaPoint[]>([]);
  const [pointsLoading, setPointsLoading] = useState(true);
  const [pointsError, setPointsError] = useState<string | null>(null);
  const [selectedMolId, setSelectedMolId] = useState<string | null>(null);
  const [molecule, setMolecule] = useState<DftMoleculeDetail | null>(null);
  const [moleculeLoading, setMoleculeLoading] = useState(false);
  const [moleculeError, setMoleculeError] = useState<string | null>(null);
  const [records, setRecords] = useState<DftMoleculeBrowserRecord[]>([]);
  const [recordsLoading, setRecordsLoading] = useState(false);
  const [recordsError, setRecordsError] = useState<string | null>(null);
  const tabListRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setPointsLoading(true);
    setPointsError(null);
    fetchDftPcaSample(160, controller.signal)
      .then((response) => {
        setPoints(response.results);
        setSelectedMolId((current) => current ?? response.results[0]?.mol_id ?? null);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setPointsError(databaseAnalysisErrorMessage(error, "PCA 数据加载失败"));
      })
      .finally(() => {
        if (!controller.signal.aborted) setPointsLoading(false);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!selectedMolId) {
      setMolecule(null);
      return;
    }
    const controller = new AbortController();
    setMoleculeLoading(true);
    setMoleculeError(null);
    fetchDftMolecule(selectedMolId, controller.signal)
      .then(setMolecule)
      .catch((error) => {
        if (!controller.signal.aborted) {
          setMolecule(null);
          setMoleculeError(databaseAnalysisErrorMessage(error, "构象详情加载失败"));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setMoleculeLoading(false);
      });
    return () => controller.abort();
  }, [selectedMolId]);

  useEffect(() => {
    if (tab !== "records") return;
    const controller = new AbortController();
    setRecordsLoading(true);
    setRecordsError(null);
    browseDftMolecules({ page: 1, page_size: 12 }, controller.signal)
      .then((response) => setRecords(response.results))
      .catch((error) => {
        if (!controller.signal.aborted) setRecordsError(databaseAnalysisErrorMessage(error, "DFT 记录加载失败"));
      })
      .finally(() => {
        if (!controller.signal.aborted) setRecordsLoading(false);
      });
    return () => controller.abort();
  }, [tab]);

  function selectTab(next: DftTabKey, focus = false) {
    setTab(next);
    if (focus) requestAnimationFrame(() => tabListRef.current?.querySelector<HTMLElement>(`[data-dft-tab="${next}"]`)?.focus());
  }

  return (
    <>
      <KpiStrip
        items={[
          { label: "DFT 分子", value: formatNumber(data.molCount, 0), unit: "个", note: "最终态分子构象" },
          { label: "构象记录", value: formatNumber(recordCount ?? data.rows, 0), unit: "条", note: "真实几何优化步骤" },
          { label: "中位优化步数", value: formatNumber(data.stepRange?.median, 0), unit: "步", note: `最长 ${formatNumber(data.stepRange?.max, 0)} 步` },
          { label: "中位能隙", value: formatNumber(data.gapRange?.median, 3), unit: "eV", note: "HOMO–LUMO gap" }
        ]}
      />

      <div className="dba-tabs" role="tablist" aria-label="DFT 数据视图" ref={tabListRef}>
        {([
          ["analysis", "构象分析"],
          ["records", "分子记录"],
          ["steps", "优化步骤"]
        ] as Array<[DftTabKey, string]>).map(([key, label], index, all) => (
          <button
            type="button"
            role="tab"
            id={`dba-dft-tab-${key}`}
            aria-controls="dba-dft-panel"
            aria-selected={tab === key}
            tabIndex={tab === key ? 0 : -1}
            data-dft-tab={key}
            key={key}
            onClick={() => selectTab(key)}
            onKeyDown={(event) => {
              if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
              event.preventDefault();
              let nextIndex = index;
              if (event.key === "ArrowRight") nextIndex = (index + 1) % all.length;
              if (event.key === "ArrowLeft") nextIndex = (index - 1 + all.length) % all.length;
              if (event.key === "Home") nextIndex = 0;
              if (event.key === "End") nextIndex = all.length - 1;
              selectTab(all[nextIndex][0], true);
            }}
          >
            {label}
          </button>
        ))}
      </div>

      <div id="dba-dft-panel" role="tabpanel" aria-labelledby={`dba-dft-tab-${tab}`}>
        {tab === "analysis" ? (
          <div className="dba-dashboard-grid">
            <Panel title="PCA 构象分布" subtitle="选择分子；方向键切换" meta={`${formatNumber(points.length, 0)} randoms`}>
              <PcaScatter
                points={points}
                selectedMolId={selectedMolId}
                loading={pointsLoading}
                error={pointsError}
                onSelect={setSelectedMolId}
              />
            </Panel>
            <Panel title="分子构象" subtitle={selectedMolId ?? "选择 PCA 点查看构象"} meta="3Dmol">
              <MoleculeViewer molecule={molecule} loading={moleculeLoading} error={moleculeError} />
            </Panel>
            <Panel title="优化能量轨迹" subtitle="电子能量随几何优化步骤收敛" className="dba-panel-span-2" meta={molecule ? `${molecule.trace.length} steps` : undefined}>
              <EnergyTrace molecule={molecule} loading={moleculeLoading} error={moleculeError} />
            </Panel>
          </div>
        ) : null}

        {tab === "records" ? (
          <Panel title="DFT 分子记录" subtitle="真实分子构象记录；可继续查看原始记录或优化步骤" meta={`${formatNumber(data.molCount, 0)} molecules`}>
            {recordsLoading ? <DftInlineLoading /> : null}
            {recordsError ? <div className="dba-inline-error"><CircleAlert aria-hidden="true" />{recordsError}</div> : null}
            {!recordsLoading && !recordsError ? (
              <DataTable
                caption="DFT 分子记录"
                headers={["分子 ID", "原子数", "最终步", "总能量 / Ha", "Gap / eV", "操作"]}
                rows={records.map((record) => [
                  <button
                    className="dba-table-link"
                    type="button"
                    onClick={(event) => onOpenRecords({ dataset: "dft", context: `${record.mol_id} 分子记录`, query: record.mol_id }, event.currentTarget)}
                  >
                    {record.mol_id}
                  </button>,
                  String(record.n_atoms),
                  String(record.final_step),
                  record.scf_energy === null ? "—" : formatNumber(record.scf_energy, 6),
                  record.gap_ev === null ? "—" : formatNumber(record.gap_ev, 3),
                  <button
                    className="dba-table-action"
                    type="button"
                    onClick={(event) => onOpenRecords({ dataset: "dft", mode: "dftSteps", molId: record.mol_id, context: `${record.mol_id} · 优化步骤` }, event.currentTarget)}
                  >
                    查看步骤
                  </button>
                ])}
              />
            ) : null}
          </Panel>
        ) : null}

        {tab === "steps" ? (
          <Panel title="几何优化步骤" subtitle={selectedMolId ? `${selectedMolId} · 当前构象轨迹` : "请先选择分子"} meta={molecule?.is_converged ?? undefined}>
            {moleculeLoading ? <DftInlineLoading /> : null}
            {moleculeError ? <div className="dba-inline-error"><CircleAlert aria-hidden="true" />{moleculeError}</div> : null}
            {!moleculeLoading && !moleculeError && molecule ? (
              <div className="dba-timeline">
                {molecule.trace.slice(-12).map((step) => (
                  <button
                    type="button"
                    className="dba-timeline-item"
                    key={step.step}
                    onClick={(event) => onOpenRecords({ dataset: "dft", mode: "dftSteps", molId: molecule.mol_id, context: `${molecule.mol_id} · 优化步骤` }, event.currentTarget)}
                  >
                    <span>Step {step.step}</span>
                    <span>SCF energy</span>
                    <strong>{step.scf_energy === null ? "—" : `${formatNumber(step.scf_energy, 6)} Ha`}</strong>
                  </button>
                ))}
              </div>
            ) : null}
            {!moleculeLoading && !moleculeError && !molecule ? <EmptyPanel message="选择 PCA 分子后显示真实优化轨迹。" /> : null}
          </Panel>
        ) : null}
      </div>
    </>
  );
}

function PcaScatter({
  points,
  selectedMolId,
  loading,
  error,
  onSelect
}: {
  points: DftPcaPoint[];
  selectedMolId: string | null;
  loading: boolean;
  error: string | null;
  onSelect: (molId: string) => void;
}) {
  const visible = points.slice(0, 80);
  const plotted = useMemo(() => {
    if (!visible.length) return [];
    const xs = visible.map((point) => point.x);
    const ys = visible.map((point) => point.y);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const xSpan = maxX - minX || 1;
    const ySpan = maxY - minY || 1;
    return visible.map((point) => ({
      ...point,
      left: 7 + ((point.x - minX) / xSpan) * 86,
      bottom: 8 + ((point.y - minY) / ySpan) * 84
    }));
  }, [visible]);
  const selectedIndex = Math.max(0, plotted.findIndex((point) => point.mol_id === selectedMolId));

  if (error) return <div className="dba-inline-error"><CircleAlert aria-hidden="true" />{error}</div>;
  if (!loading && !plotted.length) return <EmptyPanel />;

  return (
    <div className="dba-scatter" role="group" aria-label="DFT 构象 PCA 散点图">
      {plotted.map((point, index) => (
        <button
          type="button"
          className={`dba-scatter-point ${point.mol_id === selectedMolId ? "is-selected" : ""}`}
          style={{ left: `${point.left}%`, bottom: `${point.bottom}%` }}
          key={point.mol_id}
          aria-label={`${point.mol_id}，PCA X ${formatNumber(point.x, 2)}，PCA Y ${formatNumber(point.y, 2)}`}
          aria-pressed={point.mol_id === selectedMolId}
          tabIndex={point.mol_id === selectedMolId ? 0 : -1}
          title={`${point.mol_id} · gap ${formatNumber(point.gap_ev, 3)} eV`}
          onClick={() => onSelect(point.mol_id)}
          onKeyDown={(event) => {
            if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
            event.preventDefault();
            const direction = event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 1;
            const next = (selectedIndex + direction + plotted.length) % plotted.length;
            const parent = event.currentTarget.parentElement;
            onSelect(plotted[next].mol_id);
            requestAnimationFrame(() => parent?.querySelectorAll<HTMLButtonElement>(".dba-scatter-point")[next]?.focus());
          }}
        />
      ))}
      {loading ? <div className="dba-chart-loading">正在获取 PCA 样本…</div> : null}
      <div className="dba-scatter-axis dba-scatter-axis-x">PC1</div>
      <div className="dba-scatter-axis dba-scatter-axis-y">PC2</div>
    </div>
  );
}

function MoleculeViewer({ molecule, loading, error }: { molecule: DftMoleculeDetail | null; loading: boolean; error: string | null }) {
  const viewerRef = useRef<HTMLDivElement | null>(null);
  const [viewerError, setViewerError] = useState<string | null>(null);
  const [renderKey, setRenderKey] = useState(0);
  const molBlock = useMemo(() => molecule ? toMolBlock(molecule.mol_id, molecule.coordinates) : null, [molecule]);

  useEffect(() => {
    if (!molBlock || !viewerRef.current) return;
    let cancelled = false;
    setViewerError(null);
    loadScriptOnce(D3MOL_SRC, "3dmol-script")
      .then(() => {
        if (cancelled || !viewerRef.current || !window.$3Dmol) return;
        viewerRef.current.innerHTML = "";
        const viewer = window.$3Dmol.createViewer(viewerRef.current, { backgroundColor: "#f8fbff" });
        viewer.addModel(molBlock, "mol");
        viewer.setStyle({}, { stick: { radius: 0.16, color: "0x64748b" }, sphere: { scale: 0.31, colorscheme: "Jmol" } });
        viewer.zoomTo();
        viewer.render();
      })
      .catch((nextError) => {
        if (!cancelled) setViewerError(databaseAnalysisErrorMessage(nextError, "三维构象渲染失败"));
      });
    return () => {
      cancelled = true;
      if (viewerRef.current) viewerRef.current.innerHTML = "";
    };
  }, [molBlock, renderKey]);

  if (error || viewerError) return <div className="dba-inline-error"><CircleAlert aria-hidden="true" />{error ?? viewerError}</div>;
  if (!molecule && !loading) return <EmptyPanel message="选择 PCA 点后显示真实三维构象。" />;
  return (
    <div className="dba-molecule-stage">
      <button className="dba-icon-button dba-molecule-reset" type="button" aria-label="重置构象视图" onClick={() => setRenderKey((key) => key + 1)}>
        <RotateCw aria-hidden="true" />
      </button>
      <div ref={viewerRef} className="dba-molecule-viewer" />
      {loading ? <div className="dba-chart-loading">正在加载三维构象…</div> : null}
      {molecule ? (
        <div className="dba-molecule-caption">
          <span><Orbit aria-hidden="true" />Geometry preview</span>
          <span><Atom aria-hidden="true" />{molecule.n_atoms} atoms</span>
        </div>
      ) : null}
    </div>
  );
}

function EnergyTrace({ molecule, loading, error }: { molecule: DftMoleculeDetail | null; loading: boolean; error: string | null }) {
  const values = (molecule?.trace ?? []).filter((step) => step.scf_energy !== null);
  if (error) return <div className="dba-inline-error"><CircleAlert aria-hidden="true" />{error}</div>;
  if (!loading && !values.length) return <EmptyPanel message="当前分子没有可用的能量轨迹。" />;
  const energies = values.map((step) => step.scf_energy as number);
  const min = energies.length ? Math.min(...energies) : 0;
  const max = energies.length ? Math.max(...energies) : 1;
  const span = max - min || 1;
  const points = values.map((step, index) => ({
    ...step,
    x: 22 + (index / Math.max(1, values.length - 1)) * 328,
    y: 102 - (((step.scf_energy as number) - min) / span) * 78
  }));
  return (
    <div className="dba-energy-layout">
      <div className="dba-energy-chart">
        <svg viewBox="0 0 372 126" role="img" aria-label="构象优化能量轨迹">
          {[24, 63, 102].map((y) => <line key={y} x1="22" y1={y} x2="350" y2={y} className="dba-chart-grid" />)}
          <polyline points={points.map((point) => `${point.x},${point.y}`).join(" ")} className="dba-energy-line" />
          {points.filter((_, index) => index === 0 || index === points.length - 1 || index % Math.max(1, Math.floor(points.length / 5)) === 0).map((point) => (
            <circle key={point.step} cx={point.x} cy={point.y} r="3" className="dba-energy-point" />
          ))}
        </svg>
        <div className="dba-energy-caption"><span>Step 0</span><span>Step {molecule?.final_step ?? "—"}</span></div>
      </div>
      <div className="dba-metric-stack">
        <div><span>总能量</span><strong>{molecule?.scf_energy === null || !molecule ? "—" : `${formatNumber(molecule.scf_energy, 6)} Ha`}</strong></div>
        <div><span>HOMO–LUMO gap</span><strong>{molecule?.gap_ev === null || !molecule ? "—" : `${formatNumber(molecule.gap_ev, 3)} eV`}</strong></div>
        <div><span>原子组成</span><strong>{molecule ? `${molecule.n_atoms} atoms` : "—"}</strong></div>
        <div><span>收敛标记</span><strong>{molecule?.is_converged ?? "—"}</strong></div>
      </div>
      {loading ? <div className="dba-chart-loading">正在获取能量轨迹…</div> : null}
    </div>
  );
}

function DftInlineLoading() {
  return <div className="dba-inline-loading">正在加载真实 DFT 数据…</div>;
}
