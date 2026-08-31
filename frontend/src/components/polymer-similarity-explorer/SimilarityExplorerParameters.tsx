import { Check, FlaskConical, LoaderCircle, Search, SlidersHorizontal, X } from "lucide-react";
import type { FormEvent, RefObject } from "react";
import { PREDICT_PROPERTY_CATALOG } from "../../constants/predictableProperties";
import type { MatchMode, PredictableProperty } from "../../types";

type SimilarityExplorerParametersProps = {
  open: boolean;
  panelRef: RefObject<HTMLElement | null>;
  mode: MatchMode;
  similarityThreshold: number;
  topK: number;
  selectedProperty: PredictableProperty;
  submitting: boolean;
  onClose: (restoreFocus?: boolean) => void;
  onModeChange: (mode: MatchMode) => void;
  onSimilarityThresholdChange: (value: number) => void;
  onTopKChange: (value: number) => void;
  onSelectedPropertyChange: (property: PredictableProperty) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
};

const MODES: readonly {
  key: MatchMode;
  label: string;
  description: string;
  icon: typeof Search;
}[] = [
  {
    key: "structure",
    label: "结构相似",
    description: "按分子指纹匹配相近聚合物",
    icon: Search
  },
  {
    key: "property",
    label: "性能相似",
    description: "按所选预测性质寻找接近记录",
    icon: SlidersHorizontal
  }
];

export function SimilarityExplorerParameters({
  open,
  panelRef,
  mode,
  similarityThreshold,
  topK,
  selectedProperty,
  submitting,
  onClose,
  onModeChange,
  onSimilarityThresholdChange,
  onTopKChange,
  onSelectedPropertyChange,
  onSubmit
}: SimilarityExplorerParametersProps) {
  const thresholdValid = Number.isFinite(similarityThreshold) && similarityThreshold >= 0 && similarityThreshold <= 1;
  const topKValid = Number.isInteger(topK) && topK >= 1 && topK <= 100;

  return (
    <div className={`np-sw-utility-layer${open ? " is-open" : ""}`} aria-hidden={!open}>
      <button
        type="button"
        className="np-sw-utility-backdrop"
        aria-label="关闭相似性探索参数背景"
        tabIndex={open ? 0 : -1}
        onClick={() => onClose(false)}
      />
      <section
        ref={panelRef}
        id="polymer-similarity-parameters"
        className={`np-sw-popover np-sw-popover--modules np-se-parameters${open ? " is-open" : ""}`}
        role="dialog"
        aria-modal="false"
        aria-labelledby="polymer-similarity-parameters-title"
        aria-hidden={!open}
        inert={!open}
      >
        <header className="np-sw-popover__header">
          <div>
            <span className="np-sw-popover__mark"><Search aria-hidden="true" /></span>
            <span>
              <h2 id="polymer-similarity-parameters-title">相似性探索参数</h2>
              <small>选择匹配口径、阈值与搜索数量</small>
            </span>
          </div>
          <button type="button" className="np-sw-icon-button" aria-label="收起相似性探索参数" onClick={() => onClose()}>
            <X aria-hidden="true" />
          </button>
        </header>

        <form className="np-se-parameters__form" noValidate onSubmit={onSubmit}>
          <fieldset className="np-se-mode-fieldset">
            <legend>探索方式</legend>
            <div className="np-se-mode-grid" role="radiogroup" aria-label="相似性探索方式">
              {MODES.map((item) => {
                const Icon = item.icon;
                const selected = mode === item.key;
                return (
                  <button
                    key={item.key}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    className={selected ? "is-selected" : ""}
                    onClick={() => onModeChange(item.key)}
                  >
                    <span><Icon aria-hidden="true" /></span>
                    <span>
                      <strong>{item.label}</strong>
                      <small>{item.description}</small>
                    </span>
                    <Check aria-hidden="true" />
                  </button>
                );
              })}
            </div>
          </fieldset>

          <div className="np-se-parameter-grid">
            <label className="np-se-field np-se-field--threshold">
              <span>
                <strong>相似度阈值</strong>
                <small>0 表示不过滤，1 表示最严格</small>
              </span>
              <span className="np-se-threshold-control">
                <input
                  type="range"
                  min="0"
                  max="1"
                  step="0.05"
                  value={Number.isFinite(similarityThreshold) ? similarityThreshold : 0}
                  onChange={(event) => onSimilarityThresholdChange(Number(event.currentTarget.value))}
                  aria-label="相似度阈值滑块"
                />
                <input
                  type="number"
                  min="0"
                  max="1"
                  step="0.05"
                  value={similarityThreshold}
                  onChange={(event) => onSimilarityThresholdChange(Number(event.currentTarget.value))}
                  aria-label="相似度阈值"
                  aria-invalid={!thresholdValid}
                />
              </span>
            </label>

            <label className="np-se-field np-se-field--count">
              <span>
                <strong>搜索数量</strong>
                <small>1–100 条</small>
              </span>
              <input
                type="number"
                min="1"
                max="100"
                step="1"
                value={topK}
                onChange={(event) => onTopKChange(Number(event.currentTarget.value))}
                aria-label="搜索数量"
                aria-invalid={!topKValid}
              />
            </label>
          </div>

          {mode === "property" ? (
            <fieldset className="np-se-property-fieldset">
              <legend>性能指标</legend>
              <div className="np-se-property-grid" role="radiogroup" aria-label="性能相似指标">
                {PREDICT_PROPERTY_CATALOG.map((property) => {
                  const selected = selectedProperty === property.key;
                  return (
                    <label key={property.key} className={selected ? "is-selected" : ""}>
                      <input
                        type="radio"
                        name="similarity-property"
                        value={property.key}
                        checked={selected}
                        onChange={() => onSelectedPropertyChange(property.key)}
                      />
                      <span>
                        <strong>{property.shortLabel}</strong>
                        <small>{property.label}</small>
                      </span>
                    </label>
                  );
                })}
              </div>
            </fieldset>
          ) : null}

          <footer className="np-se-parameters__footer">
            <span>
              <FlaskConical aria-hidden="true" />
              {mode === "structure" ? "Morgan 指纹结构匹配" : "模型预测值与数据库记录接近度匹配"}
            </span>
            <button
              type="submit"
              className="np-sw-primary-button"
              disabled={submitting || !thresholdValid || !topKValid}
            >
              {submitting ? <LoaderCircle className="np-sw-spin" /> : <Search aria-hidden="true" />}
              {submitting ? "正在准备" : "运行探索"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}
