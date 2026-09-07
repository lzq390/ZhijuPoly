import { ChevronLeft, ChevronRight, CircleAlert, Clock3, Database, PanelRightOpen, Search } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
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
import { WorkbenchDrawerShell } from "../structure-workbench/WorkbenchDrawerShell";
import { formatNumber } from "./charts";
import { databaseAnalysisErrorMessage, databaseAnalysisSourceMessage } from "./errors";
import type { DftConvergencePresentation, DrawerRequest } from "./types";
import { dftConvergencePresentation } from "./types";

type DrawerRecord = {
  id: string;
  title: string;
  status?: DftConvergencePresentation;
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
    status: dftConvergencePresentation(row.is_converged),
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
    mono: `知识文档 #${row.knowledge_id}`,
    details: [
      { label: "配方", value: optional(row.formulation) },
      { label: "催化剂与溶剂", value: [row.catalyst, row.solvent].filter(Boolean).join(" · ") || "—" },
      { label: "温度与时间", value: [row.temperature, row.reaction_time].filter(Boolean).join(" · ") || "—" },
      { label: "来源", value: `${row.source_file}:${row.source_row_number}` }
    ]
  };
}

export type DrawerSizeProfile = {
  defaultWidth: number;
  minWidth: number;
  maxWidth: number;
  keyboardStep: number;
  overlayContainerWidth: number;
};

const STANDARD_DRAWER: DrawerSizeProfile = {
  defaultWidth: 380,
  minWidth: 320,
  maxWidth: 560,
  keyboardStep: 16,
  overlayContainerWidth: 1360
};
const TWO_K_DRAWER: DrawerSizeProfile = {
  defaultWidth: 540,
  minWidth: 480,
  maxWidth: 720,
  keyboardStep: 24,
  overlayContainerWidth: 2050
};
const TWO_K_QUERY = "(min-width: 2000px) and (min-height: 1120px)";

function mapDrawerWidth(width: number, from: DrawerSizeProfile, to: DrawerSizeProfile) {
  const ratio = (width - from.minWidth) / Math.max(1, from.maxWidth - from.minWidth);
  return Math.round(to.minWidth + Math.min(1, Math.max(0, ratio)) * (to.maxWidth - to.minWidth));
}

export function useDatabaseRecordDrawerSizing() {
  const initialProfile = typeof window !== "undefined"
    && typeof window.matchMedia === "function"
    && window.matchMedia(TWO_K_QUERY).matches
    ? TWO_K_DRAWER
    : STANDARD_DRAWER;
  const [sizing, setSizing] = useState(() => ({
    profile: initialProfile,
    width: initialProfile.defaultWidth
  }));
  const setWidth = useCallback((width: number) => {
    setSizing((current) => current.width === width ? current : { ...current, width });
  }, []);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(TWO_K_QUERY);
    const update = () => {
      const nextProfile = media.matches ? TWO_K_DRAWER : STANDARD_DRAWER;
      setSizing((current) => {
        if (current.profile === nextProfile) return current;
        return {
          profile: nextProfile,
          width: mapDrawerWidth(current.width, current.profile, nextProfile)
        };
      });
    };
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  return { width: sizing.width, setWidth, profile: sizing.profile };
}

export function DatabaseRecordDrawer({
  open,
  request,
  onClose,
  onOpen,
  width,
  onWidthChange,
  profile,
  restoreFocusTarget
}: {
  open: boolean;
  request: DrawerRequest | null;
  onClose: () => void;
  onOpen: (trigger?: HTMLElement) => void;
  width: number;
  onWidthChange: (width: number) => void;
  profile: DrawerSizeProfile;
  restoreFocusTarget?: HTMLElement | null;
}) {
  const [queryDraft, setQueryDraft] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [payload, setPayload] = useState<DrawerPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retryVersion, setRetryVersion] = useState(0);
  const [initializedRequest, setInitializedRequest] = useState<string | null>(null);
  const pageSize = 10;
  const requestIdentity = request
    ? [request.dataset, request.mode ?? "records", request.molId ?? "", request.context, request.query ?? ""].join("|")
    : null;

  useEffect(() => {
    if (!request || !requestIdentity) {
      setInitializedRequest(null);
      setPayload(null);
      setError(null);
      return;
    }
    if (initializedRequest === requestIdentity) return;
    const initialQuery = request.query ?? "";
    setQueryDraft(initialQuery);
    setQuery(initialQuery);
    setPage(1);
    setPayload(null);
    setError(null);
    setInitializedRequest(requestIdentity);
  }, [initializedRequest, request, requestIdentity]);

  useEffect(() => {
    if (!request) return;
    const timer = window.setTimeout(() => {
      const next = queryDraft.trim();
      if (query === next) return;
      setPage(1);
      setQuery(next);
    }, 240);
    return () => window.clearTimeout(timer);
  }, [query, queryDraft, request]);

  useEffect(() => {
    if (!request || initializedRequest !== requestIdentity) return;
    const currentRequest = request;
    const controller = new AbortController();
    setLoading(true);
    setError(null);

    async function load() {
      try {
        if (currentRequest.dataset === "process") {
          const response = await browseExperimentalProcessRecords({ q: query, page, page_size: pageSize }, controller.signal);
          const nextPayload = {
            total: response.matched_records,
            sourceStatus: response.source_status,
            sourceMessage: response.source_message,
            queryTimeMs: response.query_time_ms,
            records: response.results.map(processRecord)
          };
          setPayload(nextPayload);
          setPage((current) => Math.min(current, Math.max(1, Math.ceil(nextPayload.total / pageSize))));
        } else if (currentRequest.dataset === "property") {
          const response = await browseExperimentalPropertyRecords({ q: query, page, page_size: pageSize }, controller.signal);
          const nextPayload = {
            total: response.matched_records,
            sourceStatus: response.source_status,
            sourceMessage: response.source_message,
            queryTimeMs: response.query_time_ms,
            records: response.results.map(propertyRecord)
          };
          setPayload(nextPayload);
          setPage((current) => Math.min(current, Math.max(1, Math.ceil(nextPayload.total / pageSize))));
        } else if (currentRequest.dataset === "structureEffect") {
          const response = await browseStructurePropertyRecords({ q: query, page, page_size: pageSize }, controller.signal);
          const nextPayload = {
            total: response.matched_records,
            sourceStatus: response.source_status,
            sourceMessage: response.source_message,
            queryTimeMs: response.query_time_ms,
            records: response.results.map(structureRecord)
          };
          setPayload(nextPayload);
          setPage((current) => Math.min(current, Math.max(1, Math.ceil(nextPayload.total / pageSize))));
        } else if (currentRequest.dataset === "formulation") {
          const response = await browseFormulationRecords({ q: query, page, page_size: pageSize }, controller.signal);
          const nextPayload = {
            total: response.matched_records,
            sourceStatus: response.source_status,
            sourceMessage: response.source_message,
            queryTimeMs: response.query_time_ms,
            records: response.results.map(formulationRecord)
          };
          setPayload(nextPayload);
          setPage((current) => Math.min(current, Math.max(1, Math.ceil(nextPayload.total / pageSize))));
        } else if (currentRequest.mode === "dftSteps") {
          const response = await browseDftEnergySteps(
            { q: query, mol_id: currentRequest.molId, page, page_size: pageSize },
            controller.signal
          );
          const nextPayload = {
            total: response.matched_records,
            sourceStatus: response.source_status,
            sourceMessage: response.source_message,
            queryTimeMs: response.query_time_ms,
            records: response.results.map(dftStepRecord)
          };
          setPayload(nextPayload);
          setPage((current) => Math.min(current, Math.max(1, Math.ceil(nextPayload.total / pageSize))));
        } else {
          const response = await browseDftMolecules({ q: query, page, page_size: pageSize }, controller.signal);
          const nextPayload = {
            total: response.matched_records,
            sourceStatus: response.source_status,
            sourceMessage: response.source_message,
            queryTimeMs: response.query_time_ms,
            records: response.results.map(dftMoleculeRecord)
          };
          setPayload(nextPayload);
          setPage((current) => Math.min(current, Math.max(1, Math.ceil(nextPayload.total / pageSize))));
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
  }, [initializedRequest, page, query, request, requestIdentity, retryVersion]);

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

  return (
    <WorkbenchDrawerShell
      open={open}
      hasRun={Boolean(request)}
      width={width}
      minWidth={profile.minWidth}
      maxWidth={profile.maxWidth}
      keyboardStep={profile.keyboardStep}
      overlayContainerWidth={profile.overlayContainerWidth + Math.max(0, width - profile.defaultWidth)}
      restoreFocusTarget={restoreFocusTarget}
      title={title}
      status={payload ? `${context} · ${formatNumber(payload.total, 0)} 条` : context}
      headerIcon={<Database aria-hidden="true" />}
      reopenIcon={<PanelRightOpen aria-hidden="true" />}
      reopenLabel="重新打开记录"
      reopenVariant="side-handle"
      closeLabel="关闭记录抽屉"
      resizeLabel="调整记录抽屉宽度"
      onWidthChange={onWidthChange}
      onClose={onClose}
      onOpen={onOpen}
    >
      <div className="dba-record-drawer-content">
        <div className="dba-drawer-tools">
          <label className="dba-search-box">
            <span className="dba-sr-only">搜索当前数据集记录</span>
            <Search aria-hidden="true" />
            <input
              type="search"
              value={queryDraft}
              disabled={state === "reserved"}
              placeholder="按材料、指标或来源搜索"
              onChange={(event) => setQueryDraft(event.target.value)}
            />
          </label>
        </div>

        <div className="dba-drawer-body" aria-busy={loading}>
          {state === "loading" ? <DrawerSkeleton /> : null}
          {state === "error" ? (
            <DrawerState tone="error" title="记录加载失败" message={error ?? "请稍后重试。"}>
              <button type="button" onClick={() => setRetryVersion((version) => version + 1)}>重试</button>
            </DrawerState>
          ) : null}
          {state === "empty" ? (
            <DrawerState tone="empty" title="没有匹配记录" message="当前搜索范围内没有原始记录。">
              {queryDraft ? <button type="button" onClick={() => setQueryDraft("")}>清空搜索</button> : null}
            </DrawerState>
          ) : null}
          {state === "reserved" ? (
            <DrawerState tone="reserved" title="数据源暂不可用" message={databaseAnalysisSourceMessage(payload?.sourceMessage)} />
          ) : null}
          {state === "ready" && payload ? (
            <div className="dba-drawer-records">
              {payload.records.map((record) => (
                <article className="dba-record-card" key={record.id}>
                  <div className="dba-record-top">
                    <span>{record.id}</span>
                    {record.status ? <em className={`is-${record.status.tone}`}>{record.status.label}</em> : null}
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
      </div>
    </WorkbenchDrawerShell>
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

function DrawerState({
  tone,
  title,
  message,
  children
}: {
  tone: "error" | "empty" | "reserved";
  title: string;
  message: string;
  children?: ReactNode;
}) {
  const icon = tone === "error"
    ? <CircleAlert aria-hidden="true" />
    : tone === "reserved"
      ? <Clock3 aria-hidden="true" />
      : <Search aria-hidden="true" />;
  return (
    <div className={`dba-drawer-state is-${tone}`}>
      <div className="dba-drawer-state-icon">{icon}</div>
      <h3>{title}</h3>
      <p>{message}</p>
      {children}
    </div>
  );
}
