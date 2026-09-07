import {
  Atom,
  Check,
  Copy,
  Eraser,
  ImageOff,
  LoaderCircle,
  PencilRuler
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { userFacingMonomerDftMessage } from "../../lib/monomerDftPresentation";
import { fetchStructure2D } from "../../services/api";
import { StructureSvg } from "../StructureSvg";

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

type MonomerDftStructureInputProps = {
  value: string;
  disabled: boolean;
  error: string | null;
  onChange: (value: string) => void;
  onEditStructure: () => void;
};

function previewErrorMessage(error: unknown) {
  if (error instanceof Error && /[\u3400-\u9fff]/.test(error.message)) {
    return userFacingMonomerDftMessage(error.message);
  }
  return "无法生成 2D 结构预览，仍可由计算服务继续验证。";
}

export function MonomerDftStructureInput({
  value,
  disabled,
  error,
  onChange,
  onEditStructure
}: MonomerDftStructureInputProps) {
  const previewAbortRef = useRef<AbortController | null>(null);
  const previewRevisionRef = useRef(0);
  const copyTimerRef = useRef<number | null>(null);
  const [preview, setPreview] = useState<PreviewState>(EMPTY_PREVIEW);
  const [copyState, setCopyState] = useState<"copied" | "failed" | null>(null);

  const loadPreview = useCallback(async (rawValue: string) => {
    const revision = previewRevisionRef.current + 1;
    previewRevisionRef.current = revision;
    previewAbortRef.current?.abort();
    const smiles = rawValue.trim();
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
    previewRevisionRef.current += 1;
    previewAbortRef.current?.abort();
    previewAbortRef.current = null;
    const timer = window.setTimeout(() => void loadPreview(value), 380);
    return () => window.clearTimeout(timer);
  }, [loadPreview, value]);

  useEffect(() => () => {
    previewRevisionRef.current += 1;
    previewAbortRef.current?.abort();
    if (copyTimerRef.current != null) window.clearTimeout(copyTimerRef.current);
  }, []);

  async function copySmiles() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(value.trim());
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
    if (copyTimerRef.current != null) window.clearTimeout(copyTimerRef.current);
    copyTimerRef.current = window.setTimeout(() => setCopyState(null), 1400);
  }

  const previewIsStale = Boolean(preview.smiles && preview.smiles !== value.trim());
  const describedBy = ["monomer-dft-smiles-hint", error ? "monomer-dft-smiles-error" : null]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={`np-dft-structure-input${error ? " has-error" : ""}`}>
      <div className="np-dft-structure-editor">
        <div className="np-dft-field-heading">
          <label htmlFor="monomer-dft-smiles">SMILES / PSMILES</label>
          <span>{value.length} 字符</span>
        </div>
        <textarea
          id="monomer-dft-smiles"
          value={value}
          disabled={disabled}
          spellCheck={false}
          placeholder="请输入单体 SMILES，或包含 * 连接点的 PSMILES"
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
          onChange={(event) => onChange(event.target.value)}
        />
        <div className="np-dft-input-actions">
          <button
            type="button"
            aria-label="复制 SMILES"
            title={copyState === "copied" ? "已复制" : copyState === "failed" ? "复制失败" : "复制 SMILES"}
            disabled={!value.trim()}
            onClick={() => void copySmiles()}
          >
            {copyState === "copied" ? <Check /> : <Copy />}
            <span>{copyState === "copied" ? "已复制" : copyState === "failed" ? "复制失败" : "复制"}</span>
          </button>
          <button type="button" disabled={disabled || !value} onClick={() => onChange("")}>
            <Eraser />清空
          </button>
          <button type="button" disabled={disabled} onClick={onEditStructure}>
            <PencilRuler />前往结构工作台
          </button>
        </div>
        <p id="monomer-dft-smiles-hint" className="np-dft-field-hint">
          提交时将使用这里的结构；含连接位点时需选择连接或补全方式。
        </p>
        {error ? (
          <p id="monomer-dft-smiles-error" className="np-dft-field-error" role="alert">
            {error}
          </p>
        ) : null}
        {copyState === "failed" ? (
          <p className="np-dft-field-error" role="status">无法访问剪贴板，请手动复制。</p>
        ) : null}
      </div>

      <div className={`np-dft-preview${previewIsStale ? " is-stale" : ""}`}>
        <div className="np-dft-preview__label">
          <span>2D 结构预览</span>
          {previewIsStale ? <small>正在更新</small> : null}
        </div>
        <div className="np-dft-preview__canvas">
          {preview.loading ? (
            <div className="np-dft-preview__state" role="status">
              <LoaderCircle className="np-dft-spin" />正在生成预览
            </div>
          ) : preview.svg ? (
            <StructureSvg
              svg={preview.svg}
              alt="DFT 输入结构的 2D 预览"
              className="np-dft-preview__structure"
              imageClassName="np-dft-preview__image"
              fillContainer
              transparentBackground
            />
          ) : preview.error ? (
            <div className="np-dft-preview__state is-error" role="status">
              <ImageOff />{preview.error}
            </div>
          ) : (
            <div className="np-dft-preview__state"><Atom />输入结构后显示预览</div>
          )}
        </div>
      </div>
    </div>
  );
}
