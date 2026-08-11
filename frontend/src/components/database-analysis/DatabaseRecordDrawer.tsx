import { ChevronLeft, ChevronRight, Search, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  browseDftEnergySteps,
  browseDftMolecules,
  browseExperimentalProcessRecords,
  browseExperimentalPropertyRecords,
  browseFormulationRecords,
  browseStructurePropertyRecords
} from "../../services/api";
import type {
  DftEnergyStepRecord,
  DftMoleculeBrowserRecord,
  ExperimentalProcessRecord,
  ExperimentalPropertyRecord,
  FormulationRecord,
  StructurePropertyRecord
} from "../../types";
import { formatNumber } from "./charts";
import { databaseAnalysisErrorMessage } from "./errors";
import type { DrawerRequest } from "./types";

type DrawerRecord = {
  id: string;
  title: string;
  status?: string;
  mono?: string;
  details: Array<{ label: string; value: string }>;
};

type DrawerPayload = {
  total: number;
  sourceStatus: string;
  sourceMessage: string | null;
  queryTimeMs: number;
  records: DrawerRecord[];
};

function optional(value: string | number | null | undefined, fallback = "—") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function processRecord(row: ExperimentalProcessRecord): DrawerRecord {
  return {
    id: `${row.source_file}:${row.source_row_number}`,
    title: row.product_name || row.polymer_name || row.polymer_id || "实验过程记录",
    mono: row.polymer_id || undefined,
    details: [
      { label: "聚合物", value: optional(row.polymer_name || row.polymer_id) },
      { label: "材料实体", value: optional(row.material_original_text) },
      { label: "过程描述", value: optional(row.process_flow_original_text) },
      { label: "来源", value: `${row.source_file}:${row.source_row_number}` }
    ]
  };
}

function propertyRecord(row: ExperimentalPropertyRecord): DrawerRecord {
  return {
    id: `${row.source_file}:${row.source_row_number}`,
    title: row.property_name_en || "实验性能记录",
    mono: row.polymer_id || undefined,
    details: [
      { label: "聚合物", value: optional(row.polymer_name || row.polymer_id) },
      { label: "属性类别", value: optional(row.property_category) },
      { label: "测量值", value: optional(row.value) },
      { label: "来源", value: `${row.source_file}:${row.source_row_number}` }
    ]
  };
}

function structureRecord(row: StructurePropertyRecord): DrawerRecord {
  const measurement = [row.property_value, row.property_unit].filter(Boolean).join(" ");
  return {
    id: `PROP-${row.property_id}`,
    title: row.property_name,
    mono: row.canonical_smiles || row.smiles,
    details: [
      { label: "测量值", value: optional(measurement) },
      { label: "类别", value: optional(row.property_category) },
      { label: "聚合物 ID", value: String(row.polymer_id) },
      { label: "来源", value: optional(row.label_source) }
    ]
  };
}

function dftMoleculeRecord(row: DftMoleculeBrowserRecord): DrawerRecord {
  return {
    id: row.mol_id,
    title: `DFT 构象 ${row.mol_id}`,
    status: row.is_converged || undefined,
    mono: row.range_group,
    details: [
      { label: "原子数", value: String(row.n_atoms) },
      { label: "最终步数", value: String(row.final_step) },
      { label: "SCF 能量", value: row.scf_energy === null ? "—" : `${formatNumber(row.scf_energy, 6)} Ha` },
      { label: "HOMO–LUMO gap", value: row.gap_ev === null ? "—" : `${formatNumber(row.gap_ev, 3)} eV` }
    ]
  };
}

function dftStepRecord(row: DftEnergyStepRecord): DrawerRecord {
  return {
    id: `${row.mol_id}-STEP-${row.step}`,
    title: `${row.mol_id} · Step ${row.step}`,
    mono: row.mol_id,
    details: [
      { label: "SCF 能量", value: row.scf_energy === null ? "—" : `${formatNumber(row.scf_energy, 6)} Ha` },
      { label: "HOMO", value: row.homo_ev === null ? "—" : `${formatNumber(row.homo_ev, 3)} eV` },
      { label: "LUMO", value: row.lumo_ev === null ? "—" : `${formatNumber(row.lumo_ev, 3)} eV` },
      { label: "Gap", value: row.gap_ev === null ? "—" : `${formatNumber(row.gap_ev, 3)} eV` }
    ]
  };
}

function formulationRecord(row: FormulationRecord): DrawerRecord {
  return {
    id: `FRM-${row.formulation_id}`,
    title: row.polymer_iupac || `配方记录 ${row.formulation_id}`,
    mono: `knowledge #${row.knowledge_id}`,
    details: [
      { label: "配方", value: optional(row.formulation) },
      { label: "催化剂 / 溶剂", value: [row.catalyst, row.solvent].filter(Boolean).join(" · ") || "—" },
      { label: "温度 / 时间", value: [row.temperature, row.reaction_time].filter(Boolean).join(" · ") || "—" },
      { label: "来源", value: `${row.source_file}:${row.source_row_number}` }
    ]
  };
}

function useMobileDrawer() {
  const [mobile, setMobile] = useState(
    () => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(max-width: 899px)").matches
  );
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(max-width: 899px)");
    const update = () => setMobile(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return mobile;
}

export function DatabaseRecordDrawer({
  open,
  request,
  onClose
}: {
  open: boolean;
  request: DrawerRequest | null;
  onClose: () => void;
}) {
  const [queryDraft, setQueryDraft] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [width, setWidth] = useState(380);
  const [payload, setPayload] = useState<DrawerPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retryVersion, setRetryVersion] = useState(0);
  const [initializedRequest, setInitializedRequest] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const drawerRef = useRef<HTMLElement | null>(null);
  const resizeRef = useRef<HTMLDivElement | null>(null);
  const mobile = useMobileDrawer();
  const pageSize = 10;
  const requestIdentity = request
    ? [request.dataset, request.mode ?? "records", request.molId ?? "", request.context, request.query ?? ""].join("|")
    : null;

  useEffect(() => {
    if (!open || !request) return;
    const initialQuery = request.query ?? "";
    setQueryDraft(initialQuery);
    setQuery(initialQuery);
    setPage(1);
    setPayload(null);
    setError(null);
    setInitializedRequest(requestIdentity);
    requestAnimationFrame(() => searchRef.current?.focus());
  }, [open, request, requestIdentity]);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => {
      setQuery((current) => {
        const next = queryDraft.trim();
        if (current !== next) setPage(1);
        return next;
      });
    }, 240);
    return () => window.clearTimeout(timer);
  }, [open, queryDraft]);

  useEffect(() => {
    if (!open || !request || initializedRequest !== requestIdentity) return;
    const currentRequest = request;
    const controller = new AbortController();
    setLoading(true);
    setError(null);

    async function load() {
      try {
        if (currentRequest.dataset === "process") {
          const response = await browseExperimentalProcessRecords({ q: query, page, page_size: pageSize }, controller.signal);
          setPayload({
            total: response.matched_records,
            sourceStatus: response.source_status,
            sourceMessage: response.source_message,
            queryTimeMs: response.query_time_ms,
            records: response.results.map(processRecord)
          });
        } else if (currentRequest.dataset === "property") {
          const response = await browseExperimentalPropertyRecords({ q: query, page, page_size: pageSize }, controller.signal);
          setPayload({
            total: response.matched_records,
            sourceStatus: response.source_status,
            sourceMessage: response.source_message,
            queryTimeMs: response.query_time_ms,
            records: response.results.map(propertyRecord)
          });
        } else if (currentRequest.dataset === "structureEffect") {
          const response = await browseStructurePropertyRecords({ q: query, page, page_size: pageSize }, controller.signal);
          setPayload({
            total: response.matched_records,
            sourceStatus: response.source_status,
            sourceMessage: response.source_message,
            queryTimeMs: response.query_time_ms,
            records: response.results.map(structureRecord)
          });
        } else if (currentRequest.dataset === "formulation") {
          const response = await browseFormulationRecords({ q: query, page, page_size: pageSize }, controller.signal);
          setPayload({
            total: response.matched_records,
            sourceStatus: response.source_status,
            sourceMessage: response.source_message,
            queryTimeMs: response.query_time_ms,
            records: response.results.map(formulationRecord)
          });
        } else if (currentRequest.mode === "dftSteps") {
          const response = await browseDftEnergySteps(
            { q: query, mol_id: currentRequest.molId, page, page_size: pageSize },
            controller.signal
          );
          setPayload({
            total: response.matched_records,
            sourceStatus: response.source_status,
            sourceMessage: response.source_message,
            queryTimeMs: response.query_time_ms,
            records: response.results.map(dftStepRecord)
          });
        } else {
          const response = await browseDftMolecules({ q: query, page, page_size: pageSize }, controller.signal);
          setPayload({
            total: response.matched_records,
            sourceStatus: response.source_status,
            sourceMessage: response.source_message,
            queryTimeMs: response.query_time_ms,
            records: response.results.map(dftMoleculeRecord)
          });
        }
      } catch (nextError) {
        if (controller.signal.aborted) return;
        setError(databaseAnalysisErrorMessage(nextError, "记录加载失败"));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }

    void load();
    return () => controller.abort();
  }, [initializedRequest, open, page, query, request, requestIdentity, retryVersion]);

  useEffect(() => {
    if (!open) return;
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !mobile || !drawerRef.current) return;
      const focusable = Array.from(
        drawerRef.current.querySelectorAll<HTMLElement>(
          "button:not(:disabled), input:not(:disabled), [tabindex='0']"
        )
      ).filter((element) => element.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [mobile, onClose, open]);

  const totalPages = Math.max(1, Math.ceil((payload?.total ?? 0) / pageSize));
  const title = request?.mode === "dftSteps" ? "DFT 优化步骤" : "原始记录";
  const context = request?.context ?? "当前数据集";
  const state = useMemo(() => {
    if (error) return "error";
    if (loading && !payload) return "loading";
    if (payload && payload.sourceStatus !== "ready" && !payload.records.length) return "reserved";
    if (payload && !payload.records.length) return "empty";
    return "ready";
  }, [error, loading, payload]);

  function resizeBy(delta: number) {
    setWidth((current) => Math.max(320, Math.min(560, current + delta)));
  }

  return (
    <>
      <button
        type="button"
        className={`dba-drawer-backdrop ${open && mobile ? "is-open" : ""}`}
        aria-label="关闭记录抽屉"
        aria-hidden={!open || !mobile}
        tabIndex={open && mobile ? 0 : -1}
        onClick={onClose}
      />
      <aside
        ref={drawerRef}
        className={`dba-drawer ${open ? "is-open" : ""}`}
        style={{ "--dba-drawer-width": `${width}px` } as React.CSSProperties}
        role="dialog"
        aria-modal={mobile ? "true" : "false"}
        aria-labelledby="dba-drawer-title"
        aria-hidden={!open}
        inert={!open}
      >
        <div
          ref={resizeRef}
          className="dba-resize-handle"
          role="separator"
          tabIndex={open && !mobile ? 0 : -1}
          aria-orientation="vertical"
          aria-label="调整记录抽屉宽度"
          aria-valuemin={320}
          aria-valuemax={560}
          aria-valuenow={width}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft") {
              event.preventDefault();
              resizeBy(16);
            } else if (event.key === "ArrowRight") {
              event.preventDefault();
              resizeBy(-16);
            }
          }}
          onPointerDown={(event) => {
            if (mobile) return;
            event.preventDefault();
            const handle = event.currentTarget;
            handle.setPointerCapture(event.pointerId);
            const move = (moveEvent: PointerEvent) => {
              setWidth(Math.max(320, Math.min(560, window.innerWidth - moveEvent.clientX)));
            };
            const end = (endEvent: PointerEvent) => {
              if (handle.hasPointerCapture(endEvent.pointerId)) handle.releasePointerCapture(endEvent.pointerId);
              handle.removeEventListener("pointermove", move);
              handle.removeEventListener("pointerup", end);
              handle.removeEventListener("pointercancel", end);
            };
            handle.addEventListener("pointermove", move);
            handle.addEventListener("pointerup", end);
            handle.addEventListener("pointercancel", end);
          }}
        />

        <header className="dba-drawer-head">
          <div>
            <h2 id="dba-drawer-title">{title}</h2>
            <p>{context}</p>
          </div>
          <button className="dba-icon-button" type="button" aria-label="关闭记录抽屉" onClick={onClose}>
            <X aria-hidden="true" />
          </button>
        </header>

        <div className="dba-drawer-tools">
          <label className="dba-search-box">
            <span className="dba-sr-only">搜索当前数据集记录</span>
            <Search aria-hidden="true" />
            <input
              ref={searchRef}
              type="search"
              value={queryDraft}
              disabled={!open || state === "reserved"}
              placeholder="按材料、指标或来源搜索"
              onChange={(event) => setQueryDraft(event.target.value)}
            />
          </label>
        </div>

        <div className="dba-drawer-body" aria-busy={loading}>
          {state === "loading" ? <DrawerSkeleton /> : null}
          {state === "error" ? (
            <DrawerState title="记录加载失败" message={error ?? "请稍后重试。"}>
              <button type="button" onClick={() => setRetryVersion((version) => version + 1)}>重试</button>
            </DrawerState>
          ) : null}
          {state === "empty" ? (
            <DrawerState title="没有匹配记录" message="当前搜索范围内没有原始记录。">
              {queryDraft ? <button type="button" onClick={() => setQueryDraft("")}>清空搜索</button> : null}
            </DrawerState>
          ) : null}
          {state === "reserved" ? (
            <DrawerState title="数据源暂不可用" message={payload?.sourceMessage ?? "该数据源尚未导入或正在准备。"} />
          ) : null}
          {state === "ready" && payload ? (
            <div className="dba-drawer-records">
              {payload.records.map((record) => (
                <article className="dba-record-card" key={record.id}>
                  <div className="dba-record-top">
                    <span>{record.id}</span>
                    {record.status ? <em>{record.status}</em> : null}
                  </div>
                  <h3>{record.title}</h3>
                  {record.mono ? <code title={record.mono}>{record.mono}</code> : null}
                  <div className="dba-record-details">
                    {record.details.map((detail) => (
                      <div key={`${record.id}-${detail.label}`} title={detail.value}>
                        <span>{detail.label}</span>
                        <strong>{detail.value}</strong>
                      </div>
                    ))}
                  </div>
                </article>
              ))}
              {loading ? <div className="dba-drawer-loading">正在更新记录…</div> : null}
            </div>
          ) : null}
        </div>

        <footer className="dba-drawer-foot">
          <span>
            {payload ? `第 ${Math.min(page, totalPages)} / ${totalPages} 页 · 共 ${formatNumber(payload.total, 0)} 条` : "正在获取记录…"}
          </span>
          <div>
            <button
              className="dba-icon-button"
              type="button"
              aria-label="上一页"
              disabled={page <= 1 || loading}
              onClick={() => setPage((current) => Math.max(1, current - 1))}
            >
              <ChevronLeft aria-hidden="true" />
            </button>
            <button
              className="dba-icon-button"
              type="button"
              aria-label="下一页"
              disabled={page >= totalPages || loading}
              onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
            >
              <ChevronRight aria-hidden="true" />
            </button>
          </div>
        </footer>
      </aside>
    </>
  );
}

function DrawerSkeleton() {
  return (
    <div className="dba-drawer-records" role="status" aria-label="正在加载记录">
      {[0, 1, 2, 3].map((item) => (
        <div className="dba-record-card dba-record-skeleton" key={item}>
          <span /><strong /><div><i /><i /></div>
        </div>
      ))}
    </div>
  );
}

function DrawerState({ title, message, children }: { title: string; message: string; children?: React.ReactNode }) {
  return (
    <div className="dba-drawer-state">
      <div className="dba-drawer-state-icon"><Search aria-hidden="true" /></div>
      <h3>{title}</h3>
      <p>{message}</p>
      {children}
    </div>
  );
}
