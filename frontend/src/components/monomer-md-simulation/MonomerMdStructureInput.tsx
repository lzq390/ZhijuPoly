import {
  Atom,
  Check,
  ClipboardPaste,
  Copy,
  Eraser,
  ImageOff,
  LoaderCircle,
  PencilRuler,
  Sparkles
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchStructure2D } from "../../services/api";
import { StructureSvg } from "../StructureSvg";

const DEMO_EXAMPLE_SMILES = "CCO";

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

type MonomerMdStructureInputProps = {
  value: string;
  error: string | null;
  onChange: (value: string) => void;
  getSharedSmiles: () => Promise<string>;
  onEditStructure: () => void;
};

function previewErrorMessage(error: unknown) {
  if (error instanceof Error && /[\u3400-\u9fff]/.test(error.message)) {
    return error.message;
  }
  return "无法生成 2D 结构预览，仍可由服务端继续验证。";
}

export function MonomerMdStructureInput({
  value,
  error,
  onChange,
  getSharedSmiles,
  onEditStructure
}: MonomerMdStructureInputProps) {
  const previewAbortRef = useRef<AbortController | null>(null);
  const previewRevisionRef = useRef(0);
  const copyTimerRef = useRef<number | null>(null);
  const [preview, setPreview] = useState<PreviewState>(EMPTY_PREVIEW);
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const loadPreview = useCallback(async (rawValue: string) => {
    previewRevisionRef.current += 1;
    const revision = previewRevisionRef.current;
    previewAbortRef.current?.abort();
    const smiles = rawValue.trim();
    if (!smiles || smiles.includes("*") || smiles.length > 1000) {
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
    const timer = window.setTimeout(() => void loadPreview(value), 380);
    return () => window.clearTimeout(timer);
  }, [loadPreview, value]);

  useEffect(() => () => {
    previewRevisionRef.current += 1;
    previewAbortRef.current?.abort();
    if (copyTimerRef.current != null) window.clearTimeout(copyTimerRef.current);
  }, []);

  async function importSharedStructure() {
    setImporting(true);
    setImportError(null);
    try {
      const shared = (await getSharedSmiles()).trim();
      if (!shared) throw new Error("共享结构为空，请先在结构工作台绘制或输入结构。 ");
      onChange(shared);
    } catch (sharedError) {
      setImportError(
        sharedError instanceof Error && sharedError.message.trim()
          ? sharedError.message.trim()
          : "无法读取共享结构。"
      );
    } finally {
      setImporting(false);
    }
  }

  async function copySmiles() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(value.trim());
      setCopied(true);
      if (copyTimerRef.current != null) window.clearTimeout(copyTimerRef.current);
      copyTimerRef.current = window.setTimeout(() => setCopied(false), 1300);
    } catch {
      setImportError("无法访问剪贴板，请手动复制。 ");
    }
  }

  return (
    <div className="np-mmd-structure-input">
      <div className="np-mmd-structure-editor">
        <div className="np-mmd-field-heading">
          <label htmlFor="monomer-md-demo-smiles">单体 SMILES</label>
          <span>{value.length} / 1000</span>
        </div>
        <textarea
          id="monomer-md-demo-smiles"
          value={value}
          maxLength={1000}
          spellCheck={false}
          placeholder="请输入普通单分子 SMILES"
          aria-invalid={error ? true : undefined}
          aria-describedby={`monomer-md-demo-hint${error ? " monomer-md-demo-error" : ""}`}
          onChange={(event) => {
            setImportError(null);
            onChange(event.target.value);
          }}
        />
        <div className="np-mmd-input-actions">
          <button type="button" onClick={() => onChange(DEMO_EXAMPLE_SMILES)}>
            <Sparkles />载入示例
          </button>
          <button type="button" onClick={() => void importSharedStructure()} disabled={importing}>
            {importing ? <LoaderCircle className="np-mmd-spin" /> : <ClipboardPaste />}
            {importing ? "导入中" : "导入共享结构"}
          </button>
          <button
            type="button"
            aria-label="复制 SMILES"
            title={copied ? "已复制" : "复制 SMILES"}
            disabled={!value.trim()}
            onClick={() => void copySmiles()}
          >
            {copied ? <Check /> : <Copy />}
          </button>
          <button
            type="button"
            aria-label="清空 SMILES"
            title="清空 SMILES"
            disabled={!value}
            onClick={() => onChange("")}
          >
            <Eraser />
          </button>
          <button type="button" onClick={onEditStructure}>
            <PencilRuler />前往结构工作台编辑
          </button>
        </div>
        <p id="monomer-md-demo-hint" className="np-mmd-field-hint">
          只接受不含 <code>*</code> 连接点的普通单分子结构；最终化学有效性由后端验证。
        </p>
        {error ? <p id="monomer-md-demo-error" className="np-mmd-field-error" role="alert">{error}</p> : null}
        {importError ? <p className="np-mmd-field-error" role="status">{importError}</p> : null}
      </div>

      <div className="np-mmd-preview">
        <div className="np-mmd-preview__label">
          <span>安全 2D 预览</span>
          {preview.smiles && preview.smiles !== value.trim() ? <small>正在更新</small> : null}
        </div>
        <div className="np-mmd-preview__canvas">
          {preview.loading ? (
            <div className="np-mmd-preview__state"><LoaderCircle className="np-mmd-spin" />正在生成预览</div>
          ) : preview.svg ? (
            <StructureSvg
              svg={preview.svg}
              alt="单体结构的 2D 预览"
              className="np-mmd-preview__structure"
              imageClassName="np-mmd-preview__image"
              transparentBackground
            />
          ) : preview.error ? (
            <div className="np-mmd-preview__state is-error"><ImageOff />{preview.error}</div>
          ) : (
            <div className="np-mmd-preview__state"><Atom />输入 SMILES 后显示预览</div>
          )}
        </div>
      </div>
    </div>
  );
}
