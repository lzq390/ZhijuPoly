import {
  Atom,
  Check,
  Clipboard,
  Copy,
  Eraser,
  ImageOff,
  LoaderCircle,
  PencilRuler
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchStructure2D } from "../../services/api";
import { StructureSvg } from "../StructureSvg";

type PreviewState = {
  smiles: string;
  svg: string | null;
  loading: boolean;
  error: string | null;
};

const EMPTY_PREVIEW: PreviewState = { smiles: "", svg: null, loading: false, error: null };

function previewErrorMessage(error: unknown) {
  if (error instanceof TypeError) return "暂时无法生成 2D 预览，请稍后重试。";
  if (
    error instanceof Error &&
    error.message.trim() &&
    /[\u3400-\u9fff]/.test(error.message)
  ) {
    return error.message;
  }
  return "无法生成 2D 结构预览。";
}

type MdStructureInputProps = {
  value: string;
  error: string | null;
  onChange: (value: string) => void;
  onTouched: () => void;
  getSharedSmiles: () => Promise<string>;
  onEditStructure: () => void;
};

export function MdStructureInput({
  value,
  error,
  onChange,
  onTouched,
  getSharedSmiles,
  onEditStructure
}: MdStructureInputProps) {
  const previewAbortRef = useRef<AbortController | null>(null);
  const previewRevisionRef = useRef(0);
  const previewValueRef = useRef("");
  const copyTimerRef = useRef<number | null>(null);
  const [preview, setPreview] = useState<PreviewState>(EMPTY_PREVIEW);
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [copyState, setCopyState] = useState<"copied" | "failed" | null>(null);

  const loadPreview = useCallback(async (nextValue: string) => {
    previewAbortRef.current?.abort();
    const smiles = nextValue.trim();
    previewValueRef.current = smiles;
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
      const response = await fetchStructure2D(smiles, controller.signal);
      if (controller.signal.aborted || previewRevisionRef.current !== revision) return;
      setPreview({ smiles, svg: response.structure_svg, loading: false, error: null });
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
    const smiles = value.trim();
    if (previewValueRef.current === smiles) return;
    previewRevisionRef.current += 1;
    previewAbortRef.current?.abort();
    const timeout = window.setTimeout(() => void loadPreview(value), 420);
    return () => window.clearTimeout(timeout);
  }, [loadPreview, value]);

  useEffect(() => {
    return () => {
      previewRevisionRef.current += 1;
      previewAbortRef.current?.abort();
      if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
    };
  }, []);

  async function importSharedStructure() {
    setImporting(true);
    setImportError(null);
    try {
      const smiles = (await getSharedSmiles()).trim();
      if (!smiles) throw new Error("共享结构为空，请先在结构工作台绘制或输入结构。");
      onChange(smiles);
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
  const describedBy = ["md-smiles-hint", error ? "md-smiles-error" : null].filter(Boolean).join(" ");

  return (
    <div className={`np-md-structure-input${error ? " has-error" : ""}`}>
      <div className="np-md-structure-editor">
        <label htmlFor="md-simulation-smiles">SMILES</label>
        <textarea
          id="md-simulation-smiles"
          value={value}
          spellCheck={false}
          placeholder="请输入聚合物重复单元 SMILES"
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
          onChange={(event) => {
            setImportError(null);
            onChange(event.target.value);
          }}
          onBlur={onTouched}
        />
        <div className="np-md-input-actions">
          <button type="button" onClick={() => void importSharedStructure()} disabled={importing}>
            {importing ? <LoaderCircle className="np-sw-spin" /> : <Clipboard />}
            {importing ? "导入中" : "导入共享结构"}
          </button>
          <button
            type="button"
            className="is-icon"
            aria-label="复制 SMILES"
            title={copyState === "copied" ? "已复制" : copyState === "failed" ? "复制失败" : "复制 SMILES"}
            disabled={!value.trim()}
            onClick={() => void copySmiles()}
          >
            {copyState === "copied" ? <Check /> : <Copy />}
          </button>
          <button
            type="button"
            className="is-icon"
            aria-label="清空 SMILES"
            title="清空"
            disabled={!value}
            onClick={() => {
              previewRevisionRef.current += 1;
              previewAbortRef.current?.abort();
              previewValueRef.current = "";
              setPreview(EMPTY_PREVIEW);
              onChange("");
              onTouched();
            }}
          >
            <Eraser />
          </button>
          <button type="button" className="np-md-edit-structure" onClick={onEditStructure}>
            <PencilRuler />
            编辑共享结构
          </button>
        </div>
        <p id="md-smiles-hint" className="np-md-field-hint">
          可输入带 <code>*</code> 连接点的聚合物重复单元；开始模拟前会自动检查结构。
        </p>
        {error ? <p id="md-smiles-error" className="np-md-field-error" role="alert">{error}</p> : null}
        {importError ? <p className="np-md-field-error" role="status">{importError}</p> : null}
      </div>

      <div className={`np-md-preview${previewIsStale ? " is-stale" : ""}`}>
        <div className="np-md-preview__label">
          <span>2D 预览</span>
          {previewIsStale ? <small>对应上次输入</small> : null}
        </div>
        <div className="np-md-preview__canvas">
          {preview.loading ? (
            <div className="np-md-preview__state"><LoaderCircle className="np-sw-spin" /><span>正在生成预览</span></div>
          ) : preview.svg ? (
            <StructureSvg
              svg={preview.svg}
              alt="聚合物结构的 2D 预览"
              className="np-md-preview__structure"
              imageClassName="np-md-preview__image"
              transparentBackground
            />
          ) : preview.error ? (
            <div className="np-md-preview__state is-error"><ImageOff /><span>{preview.error}</span></div>
          ) : (
            <div className="np-md-preview__state"><Atom /><span>请先输入 SMILES</span></div>
          )}
        </div>
      </div>
    </div>
  );
}
