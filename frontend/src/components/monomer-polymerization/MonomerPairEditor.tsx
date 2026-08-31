import {
  Atom,
  Check,
  Clipboard,
  Copy,
  Eraser,
  ImageOff,
  LoaderCircle,
  PencilRuler,
  TriangleAlert
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchStructure2D } from "../../services/api";
import { StructureSvg } from "../StructureSvg";
import { SMIPOLY_POLYIMIDE_FIXTURE } from "./config";

export type MonomerSlot = "A" | "B";

type MonomerPairEditorProps = {
  monomerA: string;
  monomerB: string;
  monomerBRequired: boolean;
  monomerBRequirementNote: string;
  monomerAError: string | null;
  monomerBError: string | null;
  onMonomerAChange: (value: string) => void;
  onMonomerBChange: (value: string) => void;
  onTouched: (slot: MonomerSlot) => void;
  getSharedSmiles: () => Promise<string>;
  onEditStructure: () => void;
};

type PreviewState = {
  smiles: string;
  svg: string | null;
  loading: boolean;
  error: string | null;
};

const EMPTY_PREVIEW: PreviewState = {
  smiles: "",
  svg: null,
  loading: false,
  error: null
};

function previewErrorMessage(error: unknown) {
  if (error instanceof TypeError) {
    return "暂时无法连接 2D 结构预览服务。";
  }
  if (error instanceof Error && error.message.trim()) return error.message;
  return "无法生成 2D 结构预览。";
}

function MonomerInputSlot({
  slot,
  value,
  required,
  requirementNote,
  error,
  placeholder,
  onValueChange,
  onTouched,
  getSharedSmiles
}: {
  slot: MonomerSlot;
  value: string;
  required: boolean;
  requirementNote: string;
  error: string | null;
  placeholder: string;
  onValueChange: (value: string) => void;
  onTouched: () => void;
  getSharedSmiles: () => Promise<string>;
}) {
  const inputId = `monomer-${slot.toLowerCase()}-smiles`;
  const hintId = `${inputId}-hint`;
  const errorId = `${inputId}-error`;
  const previewAbortRef = useRef<AbortController | null>(null);
  const previewRevisionRef = useRef(0);
  const copyTimerRef = useRef<number | null>(null);
  const [preview, setPreview] = useState<PreviewState>(EMPTY_PREVIEW);
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [copyState, setCopyState] = useState<"copied" | "failed" | null>(null);

  const loadPreview = useCallback(async (nextValue: string) => {
    previewAbortRef.current?.abort();
    const smiles = nextValue.trim();
    const revision = previewRevisionRef.current + 1;
    previewRevisionRef.current = revision;
    if (!smiles) {
      previewAbortRef.current = null;
      setPreview(EMPTY_PREVIEW);
      return;
    }

    const controller = new AbortController();
    previewAbortRef.current = controller;
    setPreview({ smiles, svg: null, loading: true, error: null });
    try {
      const result = await fetchStructure2D(smiles, controller.signal);
      if (controller.signal.aborted || previewRevisionRef.current !== revision) return;
      setPreview({ smiles, svg: result.structure_svg, loading: false, error: null });
    } catch (previewError) {
      if (controller.signal.aborted || previewRevisionRef.current !== revision) return;
      setPreview({
        smiles,
        svg: null,
        loading: false,
        error: previewErrorMessage(previewError)
      });
    } finally {
      if (previewAbortRef.current === controller) previewAbortRef.current = null;
    }
  }, []);

  useEffect(() => {
    if (value.trim()) void loadPreview(value);
    return () => {
      previewRevisionRef.current += 1;
      previewAbortRef.current?.abort();
      if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
    };
    // Only the mounted value is previewed automatically. Typed values wait for blur.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!value.trim() && (preview.smiles || preview.loading || preview.error)) {
      previewRevisionRef.current += 1;
      previewAbortRef.current?.abort();
      previewAbortRef.current = null;
      setPreview(EMPTY_PREVIEW);
    }
  }, [preview.error, preview.loading, preview.smiles, value]);

  async function importSharedStructure() {
    setImporting(true);
    setImportError(null);
    try {
      const smiles = (await getSharedSmiles()).trim();
      if (!smiles) throw new Error("共享结构为空，请先在结构工作台绘制或输入单体。");
      onValueChange(smiles);
      onTouched();
      await loadPreview(smiles);
    } catch (sharedError) {
      setImportError(
        sharedError instanceof Error && sharedError.message.trim()
          ? sharedError.message
          : "无法读取共享结构。"
      );
    } finally {
      setImporting(false);
    }
  }

  async function copySmiles() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(value.trim());
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
    if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
    copyTimerRef.current = window.setTimeout(() => setCopyState(null), 1300);
  }

  const previewIsStale = Boolean(preview.smiles && preview.smiles !== value.trim());
  const describedBy = [hintId, error ? errorId : null].filter(Boolean).join(" ");

  return (
    <article className={`np-mp-monomer${error ? " has-error" : ""}`}>
      <header className="np-mp-monomer__header">
        <div>
          <span className="np-mp-monomer__index">{slot}</span>
          <div>
            <h3>单体 {slot}</h3>
            <p>{slot === "A" ? "主单体" : "互补单体"}</p>
          </div>
        </div>
        <span className={`np-mp-required-badge${required ? " is-required" : ""}`}>
          {required ? "必填" : "可选"}
        </span>
      </header>

      <div className="np-mp-monomer__body">
        <label htmlFor={inputId}>SMILES</label>
        <textarea
          id={inputId}
          value={value}
          placeholder={placeholder}
          spellCheck={false}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
          onChange={(event) => {
            setImportError(null);
            onValueChange(event.target.value);
          }}
          onBlur={() => {
            onTouched();
            void loadPreview(value);
          }}
        />

        <div className="np-mp-monomer__actions">
          <button
            type="button"
            className="np-mp-compact-button"
            onClick={() => void importSharedStructure()}
            disabled={importing}
          >
            {importing ? <LoaderCircle className="np-sw-spin" /> : <Clipboard />}
            {importing ? "导入中" : "导入共享结构"}
          </button>
          <button
            type="button"
            className="np-mp-icon-action"
            aria-label={`复制单体 ${slot} SMILES`}
            title={copyState === "copied" ? "已复制" : copyState === "failed" ? "复制失败" : "复制 SMILES"}
            onClick={() => void copySmiles()}
            disabled={!value.trim()}
          >
            {copyState === "copied" ? <Check /> : <Copy />}
          </button>
          <button
            type="button"
            className="np-mp-icon-action"
            aria-label={`清空单体 ${slot}`}
            title="清空"
            onClick={() => {
              onValueChange("");
              onTouched();
              previewRevisionRef.current += 1;
              previewAbortRef.current?.abort();
              setPreview(EMPTY_PREVIEW);
            }}
            disabled={!value}
          >
            <Eraser />
          </button>
        </div>

        <p id={hintId} className="np-mp-monomer__hint">{requirementNote}</p>
        {error ? <p id={errorId} className="np-mp-field-error" role="alert">{error}</p> : null}
        {importError ? <p className="np-mp-field-error" role="status">{importError}</p> : null}

        <div className={`np-mp-preview${previewIsStale ? " is-stale" : ""}`}>
          <div className="np-mp-preview__label">
            <span>2D PREVIEW</span>
            {previewIsStale ? <small>对应上次输入</small> : null}
          </div>
          <div className="np-mp-preview__canvas">
            {preview.loading ? (
              <div className="np-mp-preview__state"><LoaderCircle className="np-sw-spin" /><span>正在生成预览</span></div>
            ) : preview.svg ? (
              <StructureSvg
                svg={preview.svg}
                alt={`单体 ${slot} 的 2D 结构预览`}
                className="np-mp-preview__structure"
                imageClassName="np-mp-preview__image"
                transparentBackground
              />
            ) : preview.error ? (
              <div className="np-mp-preview__state is-error"><ImageOff /><span>{preview.error}</span></div>
            ) : (
              <div className="np-mp-preview__state"><Atom /><span>输入 SMILES 后失焦即可预览</span></div>
            )}
          </div>
        </div>
      </div>
    </article>
  );
}

export function MonomerPairEditor({
  monomerA,
  monomerB,
  monomerBRequired,
  monomerBRequirementNote,
  monomerAError,
  monomerBError,
  onMonomerAChange,
  onMonomerBChange,
  onTouched,
  getSharedSmiles,
  onEditStructure
}: MonomerPairEditorProps) {
  return (
    <div>
      <div className="np-mp-monomer-toolbar">
        <div className="np-mp-ordinary-hint">
          <TriangleAlert aria-hidden="true" />
          <span>请输入普通单体 SMILES，不使用 <code>*</code> 连接点；化学有效性以后端校验为准。</span>
        </div>
        <button type="button" className="np-sw-secondary-button" onClick={onEditStructure}>
          <PencilRuler aria-hidden="true" />
          编辑共享结构
        </button>
      </div>
      <div className="np-mp-monomer-grid">
        <MonomerInputSlot
          slot="A"
          value={monomerA}
          required
          requirementNote="聚合检索的主单体。"
          error={monomerAError}
          placeholder={`示例：${SMIPOLY_POLYIMIDE_FIXTURE.monomerA}`}
          onValueChange={onMonomerAChange}
          onTouched={() => onTouched("A")}
          getSharedSmiles={getSharedSmiles}
        />
        <MonomerInputSlot
          slot="B"
          value={monomerB}
          required={monomerBRequired}
          requirementNote={monomerBRequirementNote}
          error={monomerBError}
          placeholder={`示例：${SMIPOLY_POLYIMIDE_FIXTURE.monomerB}`}
          onValueChange={onMonomerBChange}
          onTouched={() => onTouched("B")}
          getSharedSmiles={getSharedSmiles}
        />
      </div>
    </div>
  );
}
