import { Atom, Eraser, ImageOff, LoaderCircle, LockKeyhole, Network, PencilRuler, Play, RotateCcw, Route } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent, type RefObject } from "react";
import { getSessionEpoch } from "../../auth/session";
import { fetchStructure2D } from "../../services/api";
import { StructureSvg } from "../StructureSvg";
import { WorkbenchSelect } from "../structure-workbench/WorkbenchSelect";
import { RETROSYNTHESIS_EXAMPLE, TARGET_ROLE_OPTIONS, type MonomerRetrosynthesisDraft } from "./session";

type Props = {
  form: MonomerRetrosynthesisDraft;
  guest: boolean;
  loading: boolean;
  importing: boolean;
  targetError: string | null;
  countError: string | null;
  notice: string | null;
  targetRef: RefObject<HTMLTextAreaElement | null>;
  onChange: (patch: Partial<MonomerRetrosynthesisDraft>) => void;
  onImport: () => Promise<string | null>;
  onEdit: () => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
};
type Preview = { smiles: string; svg?: string; loading?: boolean; error?: string };

export function MonomerRetrosynthesisForm({ form, guest, loading, importing, targetError, countError,
  notice, targetRef, onChange, onImport, onEdit, onSubmit }: Props) {
  const identityEpoch = useRef(getSessionEpoch()).current;
  const [preview, setPreview] = useState<Preview>({ smiles: "" });
  const [previewRequest, setPreviewRequest] = useState({ smiles: form.smiles, revision: 0 });
  const updatePreview = (smiles: string) => setPreviewRequest(previous => ({ smiles, revision: previous.revision + 1 }));

  useEffect(() => {
    const smiles = previewRequest.smiles.trim();
    if (!smiles || guest || smiles !== form.smiles.trim() || identityEpoch !== getSessionEpoch()) return;
    const controller = new AbortController();
    const current = () => !controller.signal.aborted && identityEpoch === getSessionEpoch();
    setPreview({ smiles, loading: true });
    void fetchStructure2D(smiles, controller.signal).then(result => {
      if (current()) setPreview({ smiles, svg: result.structure_svg });
    }).catch(error => {
      if (current()) setPreview({ smiles, error: error instanceof Error ? error.message : "暂时无法生成 2D 预览。" });
    });
    return () => controller.abort();
  }, [previewRequest, form.smiles, guest, identityEpoch]);

  const matches = preview.smiles === form.smiles.trim() && Boolean(form.smiles.trim());
  return <form className="np-mr-form" noValidate onSubmit={onSubmit}>
    <div className="np-mr-module-toolbar" aria-label="单体逆合成反推工具栏">
      <span className="np-mr-scope"><Network aria-hidden="true" />单步前体生成</span>
      <button type="button" className="np-sw-secondary-button" onClick={() => {
        onChange({ smiles: RETROSYNTHESIS_EXAMPLE }); updatePreview(RETROSYNTHESIS_EXAMPLE);
      }}><RotateCcw aria-hidden="true" />加载示例</button>
    </div>
    <section className="np-mr-surface np-sw-accented-surface" aria-labelledby="np-mr-form-title">
      <header className="np-mr-surface-header">
        <span className="np-mr-mark"><Route aria-hidden="true" /></span>
        <div><h2 id="np-mr-form-title">单体反推设置</h2><p>输入目标单体，生成可能的单步前体组合。</p></div>
      </header>
      <section className="np-mr-section" aria-labelledby="np-mr-target-title">
        <header className="np-mr-section-header">
          <span aria-hidden="true">01</span><h3 id="np-mr-target-title">目标结构</h3>
        </header>
        <div className="np-mr-target-grid">
          <div className={`np-mr-input${targetError ? " has-error" : ""}`}>
            <label className="np-sw-field" htmlFor="np-mr-target"><span>目标单体 SMILES <small>必填</small></span>
              <textarea id="np-mr-target" aria-label="目标单体 SMILES" ref={targetRef} value={form.smiles} rows={4} spellCheck={false}
                placeholder={`例如：${RETROSYNTHESIS_EXAMPLE}`} aria-invalid={Boolean(targetError)}
                aria-describedby="np-mr-target-error" onChange={event => onChange({ smiles: event.currentTarget.value })}
                onBlur={() => updatePreview(form.smiles)} />
            </label>
            <small id="np-mr-target-error" className="np-mr-error" role="status">{targetError}</small>
            <div className="np-mr-input-actions">
              <button type="button" className="np-sw-secondary-button" disabled={importing} onClick={async () => {
                const smiles = await onImport();
                if (smiles) updatePreview(smiles);
              }}>{importing ? <LoaderCircle className="np-sw-spin" aria-hidden="true" /> : <Atom aria-hidden="true" />}使用当前结构</button>
              {form.smiles ? <button type="button" className="np-sw-icon-button" aria-label="清空目标 SMILES"
                onClick={() => { onChange({ smiles: "" }); setPreview({ smiles: "" }); }}><Eraser aria-hidden="true" /></button> : null}
              <button type="button" className="np-sw-secondary-button np-mr-edit" onClick={onEdit}><PencilRuler aria-hidden="true" />编辑共享结构</button>
            </div>
            <p className="np-mr-notice" role="status">{notice}</p>
          </div>
          <div className="np-mr-preview" aria-label="目标单体 2D 预览">
            <div className="np-mr-preview-label"><span>2D 结构预览</span><small>只读</small></div>
            <div className="np-mr-preview-canvas">
              {guest ? <div className="np-mr-preview-state"><LockKeyhole aria-hidden="true" /><p>登录后生成 2D 预览</p></div>
                : matches && preview.loading ? <div className="np-mr-preview-state" role="status"><LoaderCircle className="np-sw-spin" aria-hidden="true" /><p>正在生成结构预览</p></div>
                : matches && preview.error ? <div className="np-mr-preview-state is-error" role="status"><ImageOff aria-hidden="true" /><p>{preview.error}</p><button type="button" className="np-sw-secondary-button"
                  onClick={() => updatePreview(form.smiles)}>重试预览</button></div>
                : matches && preview.svg ? <StructureSvg svg={preview.svg} alt="目标单体结构" className="np-mr-preview-media" fillContainer />
                : <div className="np-mr-preview-state"><Atom aria-hidden="true" /><p>{form.smiles.trim() ? "完成输入后显示结构预览" : "输入或导入目标单体"}</p></div>}
            </div>
          </div>
        </div>
      </section>
      <section className="np-mr-section" aria-labelledby="np-mr-parameters-title">
        <header className="np-mr-section-header">
          <span aria-hidden="true">02</span><h3 id="np-mr-parameters-title">反推参数</h3>
        </header>
        <div className="np-mr-parameters">
          <div className="np-sw-field"><span id="np-mr-role-label">结构类型</span>
            <WorkbenchSelect id="np-mr-role" value={form.targetRole} options={TARGET_ROLE_OPTIONS} ariaLabel="反推结构类型"
              onChange={value => onChange({ targetRole: value })} />
            <small>用于类型标注，不影响模型生成。</small>
          </div>
          <label className="np-sw-field"><span>候选数 <small>1–10</small></span>
            <input type="number" min={1} max={10} step={1} inputMode="numeric" aria-label="反推候选数"
              value={form.returnCount} aria-invalid={Boolean(countError)} aria-describedby="np-mr-count-error"
              onChange={event => onChange({ returnCount: event.currentTarget.value })} />
            <small id="np-mr-count-error" className="np-mr-error" role="status">{countError}</small>
          </label>
          <button className="np-sw-primary-button np-mr-run" type="submit" disabled={importing}>
            {loading ? <LoaderCircle className="np-sw-spin" aria-hidden="true" /> : <Play aria-hidden="true" />}运行反推
          </button>
        </div>
      </section>
    </section>
  </form>;
}
