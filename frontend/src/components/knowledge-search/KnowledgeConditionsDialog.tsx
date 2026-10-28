import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { createPortal } from "react-dom";
import { BookOpenText, Database, RotateCcw, SlidersHorizontal, X } from "lucide-react";
import { ModalFocusSuspendedContext, useModalFocus } from "../../hooks/useModalFocus";
import type { KnowledgeDataSource, KnowledgeSourcesResponse } from "../../types";
import { WorkbenchSelect, type WorkbenchSelectOption } from "../structure-workbench/WorkbenchSelect";
import "../../styles/workbench-theme.css";
import "./knowledge-conditions.css";

export type FilterDraft = {
  year_from: string;
  year_to: string;
  has_properties: boolean;
  has_processes: boolean;
  has_original: boolean;
};

export type KnowledgeConditionSettings = {
  sources: KnowledgeDataSource[];
  pageSize: 20 | 50 | 100;
  filters: FilterDraft;
};

const EMPTY_FILTERS: FilterDraft = {
  year_from: "", year_to: "", has_properties: false, has_processes: false, has_original: false
};

export const DEFAULT_KNOWLEDGE_CONDITIONS: KnowledgeConditionSettings = {
  sources: ["legacy"], pageSize: 20, filters: { ...EMPTY_FILTERS }
};

export const KNOWLEDGE_YEAR_MIN = 1960;
export const KNOWLEDGE_YEAR_MAX = new Date().getUTCFullYear();
const YEAR_OPTIONS = Array.from({ length: KNOWLEDGE_YEAR_MAX - KNOWLEDGE_YEAR_MIN + 1 }, (_, index) => KNOWLEDGE_YEAR_MAX - index);

type KnowledgeConditionsDialogProps = {
  settings: KnowledgeConditionSettings;
  onApply: (settings: KnowledgeConditionSettings) => void;
  onClose: () => void;
  sources: KnowledgeSourcesResponse | null;
  sourceError: string | null;
};

const SOURCE_OPTIONS = [
  { value: "legacy", label: "现有专利知识库", Icon: Database },
  { value: "extracted_literature", label: "提取文献库", Icon: BookOpenText }
] as const;

function unavailable(source: KnowledgeDataSource, status: KnowledgeSourcesResponse | null) {
  const entry = status?.sources.find((item) => item.data_source === source);
  return Boolean(entry && (!entry.enabled || !entry.ready));
}

function cloneSettings(settings: KnowledgeConditionSettings, status: KnowledgeSourcesResponse | null) {
  return {
    sources: SOURCE_OPTIONS.filter(({ value }) => settings.sources.includes(value) && !unavailable(value, status)).map(({ value }) => value),
    pageSize: settings.pageSize,
    filters: { ...settings.filters }
  };
}

function selectableYear(value: string) {
  return /^\d{4}$/.test(value) && Number(value) >= KNOWLEDGE_YEAR_MIN && Number(value) <= KNOWLEDGE_YEAR_MAX;
}

function yearError(filters: FilterDraft) {
  const from = filters.year_from.trim(), to = filters.year_to.trim();
  if ([from, to].some((value) => value && !selectableYear(value))) {
    return `请选择 ${KNOWLEDGE_YEAR_MIN}–${KNOWLEDGE_YEAR_MAX} 范围内的年份，或选择不限。`;
  }
  return from && to && Number(from) > Number(to) ? "起始年份不能晚于结束年份。" : null;
}

export function KnowledgeConditionsDialog({ settings, onApply, onClose, sources, sourceError }: KnowledgeConditionsDialogProps) {
  // The recording panel can suspend background drawers. This foreground dialog
  // owns focus even when that panel was already open.
  return <ModalFocusSuspendedContext.Provider value={false}><KnowledgeConditionsDialogContent settings={settings} onApply={onApply} onClose={onClose} sources={sources} sourceError={sourceError} /></ModalFocusSuspendedContext.Provider>;
}

function KnowledgeConditionsDialogContent({ settings, onApply, onClose, sources, sourceError }: KnowledgeConditionsDialogProps) {
  const [draft, setDraft] = useState<KnowledgeConditionSettings>(() => cloneSettings(settings, sources));
  const id = useId();
  const layerRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLFormElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const previousFocus = useRef(typeof document !== "undefined" && document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const literatureSelected = draft.sources.includes("extracted_literature");
  const validationError = draft.sources.length === 0 ? "请至少选择一个知识库。" : literatureSelected ? yearError(draft.filters) : null;

  useModalFocus({ active: true, open: true, scopeRef: layerRef, panelRef, initialFocusRef: closeRef, onClose });

  useEffect(() => {
    setDraft((value) => {
      const available = value.sources.filter((source) => !unavailable(source, sources));
      return available.length === value.sources.length ? value : { ...value, sources: available };
    });
  }, [sources]);

  useEffect(() => {
    const panel = panelRef.current;
    const target = previousFocus.current;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = overflow;
      // Let useModalFocus release the background's inert state first. A Strict
      // Mode effect replay keeps the panel connected and must not steal focus.
      queueMicrotask(() => {
        if (!panel?.isConnected && target?.isConnected && !target.closest('[inert], [hidden], [aria-hidden="true"]')) target.focus({ preventScroll: true });
      });
    };
  }, []);

  function toggleSource(source: KnowledgeDataSource, checked: boolean) {
    setDraft((value) => ({
      ...value,
      sources: SOURCE_OPTIONS.filter(({ value: candidate }) => candidate === source ? checked : value.sources.includes(candidate)).map(({ value: candidate }) => candidate)
    }));
  }

  function changeYear(key: "year_from" | "year_to", selected: string) {
    if (selected && !selectableYear(selected)) return;
    setDraft((value) => {
      const filters = { ...value.filters, [key]: selected };
      const from = filters.year_from.trim(), to = filters.year_to.trim();
      if (from && to && selectableYear(from) && selectableYear(to) && Number(from) > Number(to)) return value;
      return { ...value, filters };
    });
  }

  function yearOptions(key: "year_from" | "year_to"): WorkbenchSelectOption<string>[] {
    const current = draft.filters[key].trim();
    const opposite = draft.filters[key === "year_from" ? "year_to" : "year_from"].trim();
    const limit = selectableYear(opposite) ? Number(opposite) : null;
    return [
      { value: "", label: "不限" },
      ...(current && !selectableYear(current) ? [{ value: current, label: `${current}（超出可选范围）`, disabled: true }] : []),
      ...YEAR_OPTIONS.map((year) => ({ value: String(year), label: `${year}年`, disabled: limit !== null && (key === "year_from" ? year > limit : year < limit) }))
    ];
  }

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (validationError || draft.sources.some((source) => unavailable(source, sources))) return;
    onApply({ ...draft, sources: [...draft.sources], filters: { ...draft.filters, year_from: draft.filters.year_from.trim(), year_to: draft.filters.year_to.trim() } });
  }

  if (typeof document === "undefined") return null;
  return createPortal(
    <div ref={layerRef} className="ks-conditions-layer np-workbench-theme" onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <form ref={panelRef} className="ks-conditions-dialog" role="dialog" aria-modal="true" aria-labelledby={`${id}-title`} aria-describedby={`${id}-description`} noValidate onSubmit={apply} tabIndex={-1}>
        <header className="ks-conditions-header">
          <div className="ks-conditions-heading"><span className="ks-conditions-heading-icon"><SlidersHorizontal aria-hidden="true" /></span><div><h2 id={`${id}-title`}>条件设置</h2><p id={`${id}-description`}>设置来源、年份与结果数量。</p></div></div>
          <button ref={closeRef} type="button" className="ks-conditions-close" aria-label="关闭条件设置" onClick={onClose}><X aria-hidden="true" /></button>
        </header>

        <div className="ks-conditions-body">
          <fieldset className="ks-conditions-section">
            <legend>检索来源</legend>
            <p className="ks-conditions-note">可多选，结果按知识库分别展示。</p>
            <div className="ks-conditions-sources">
              {SOURCE_OPTIONS.map(({ value, label, Icon }) => {
                const entry = sources?.sources.find((item) => item.data_source === value);
                const disabled = unavailable(value, sources);
                const state = !entry ? (sourceError ? "状态未确认" : "正在检查可用性…") : !entry.enabled ? "未启用" : !entry.ready ? "暂不可用" : `可用 · ${entry.total.toLocaleString("zh-CN")} 条`;
                return <label className={`ks-conditions-source${disabled ? " is-unavailable" : ""}`} key={value}>
                  <input type="checkbox" aria-label={label} aria-describedby={`${id}-${value}-status`} checked={draft.sources.includes(value)} disabled={disabled} onChange={(event) => toggleSource(value, event.target.checked)} />
                  <span className="ks-conditions-source-icon"><Icon aria-hidden="true" /></span><span className="ks-conditions-source-copy"><strong>{label}</strong><small id={`${id}-${value}-status`} className={entry?.enabled && entry.ready ? "is-ready" : ""}>{state}</small></span>
                </label>;
              })}
            </div>
            {sourceError ? <p className="ks-conditions-source-error" role="alert">{sourceError}</p> : null}
          </fieldset>

          <section className="ks-conditions-section" aria-labelledby={`${id}-literature-title`}>
            <div className="ks-conditions-section-heading"><h3 id={`${id}-literature-title`}>文献筛选</h3><button type="button" className="ks-conditions-link" onClick={() => setDraft((value) => ({ ...value, filters: { ...EMPTY_FILTERS } }))}><RotateCcw aria-hidden="true" />清空文献筛选</button></div>
            <p className="ks-conditions-note" id={`${id}-literature-note`}>仅对提取文献库生效，未勾选项不限。</p>
            <fieldset className="ks-conditions-literature" disabled={!literatureSelected} aria-label="文献筛选条件" aria-describedby={`${id}-literature-note`}>
              <div className="ks-conditions-years">
                <div className="ks-conditions-field"><label htmlFor={`${id}-year-from`}>起始年份</label><WorkbenchSelect id={`${id}-year-from`} ariaLabel="文献起始年份" ariaDescribedBy={`${id}-year-note`} ariaInvalid={Boolean(literatureSelected && yearError(draft.filters))} value={draft.filters.year_from.trim()} options={yearOptions("year_from")} disabled={!literatureSelected} onChange={(value) => changeYear("year_from", value)} constrainToScrollParent /></div>
                <span className="ks-conditions-year-separator" aria-hidden="true">—</span>
                <div className="ks-conditions-field"><label htmlFor={`${id}-year-to`}>结束年份</label><WorkbenchSelect id={`${id}-year-to`} ariaLabel="文献结束年份" ariaDescribedBy={`${id}-year-note`} ariaInvalid={Boolean(literatureSelected && yearError(draft.filters))} value={draft.filters.year_to.trim()} options={yearOptions("year_to")} disabled={!literatureSelected} onChange={(value) => changeYear("year_to", value)} constrainToScrollParent /></div>
              </div>
              <p className="ks-conditions-note ks-conditions-year-note" id={`${id}-year-note`}>{KNOWLEDGE_YEAR_MIN}–{KNOWLEDGE_YEAR_MAX} 年，起始不晚于结束。均不限时包含未标年份文献。</p>
              <div className="ks-conditions-checks">
                {([['has_properties', '有性质信息'], ['has_processes', '有工艺信息'], ['has_original', '有可用原文']] as const).map(([key, label]) => <label key={key}><input type="checkbox" checked={draft.filters[key]} disabled={!literatureSelected} onChange={(event) => setDraft((value) => ({ ...value, filters: { ...value.filters, [key]: event.target.checked } }))} />{label}</label>)}
              </div>
            </fieldset>
          </section>

          <section className="ks-conditions-section ks-conditions-page-size">
            <div className="ks-conditions-field"><label htmlFor={`${id}-page-size`}>每库每页数量</label><WorkbenchSelect id={`${id}-page-size`} ariaLabel="每库每页数量" value={String(draft.pageSize)} options={([20, 50, 100] as const).map((count) => ({ value: String(count), label: `${count} 条` }))} onChange={(count) => setDraft((value) => ({ ...value, pageSize: Number(count) as KnowledgeConditionSettings['pageSize'] }))} constrainToScrollParent /></div>
            <p className="ks-conditions-note">两个知识库各自分页。</p>
          </section>
          {validationError ? <p className="ks-conditions-error" role="alert" id={`${id}-error`}>{validationError}</p> : null}
        </div>

        <footer className="ks-conditions-footer"><button type="button" className="ks-conditions-button" onClick={onClose}>取消</button><button type="submit" className="ks-conditions-button is-primary" disabled={Boolean(validationError)} aria-describedby={validationError ? `${id}-error` : undefined}>应用条件</button></footer>
      </form>
    </div>, document.body
  );
}
