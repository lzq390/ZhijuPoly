import { BarChart3, Check, LoaderCircle, Sparkles, X } from "lucide-react";
import type { FormEvent, RefObject } from "react";
import {
  PREDICTABLE_PROPERTIES,
  PREDICT_PROPERTY_CATALOG,
  PREDICT_PROPERTY_GROUPS
} from "../../constants/predictableProperties";
import type { PredictableProperty } from "../../types";

type HomopolymerPredictionParametersProps = {
  open: boolean;
  panelRef: RefObject<HTMLElement | null>;
  selectedProperties: readonly PredictableProperty[];
  submitting: boolean;
  onClose: (restoreFocus?: boolean) => void;
  onSelectedPropertiesChange: (properties: PredictableProperty[]) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
};

export function HomopolymerPredictionParameters({
  open,
  panelRef,
  selectedProperties,
  submitting,
  onClose,
  onSelectedPropertiesChange,
  onSubmit
}: HomopolymerPredictionParametersProps) {
  const selected = new Set(selectedProperties);

  function toggleProperty(property: PredictableProperty) {
    onSelectedPropertiesChange(
      selected.has(property)
        ? PREDICTABLE_PROPERTIES.filter((item) => item !== property && selected.has(item))
        : PREDICTABLE_PROPERTIES.filter((item) => item === property || selected.has(item))
    );
  }

  function toggleGroup(group: (typeof PREDICT_PROPERTY_GROUPS)[number]["key"]) {
    const groupProperties = PREDICT_PROPERTY_CATALOG.filter((property) => property.group === group);
    const allSelected = groupProperties.every((property) => selected.has(property.key));
    onSelectedPropertiesChange(
      PREDICTABLE_PROPERTIES.filter((property) =>
        groupProperties.some((item) => item.key === property) ? !allSelected : selected.has(property)
      )
    );
  }

  return (
    <div className={`np-sw-utility-layer${open ? " is-open" : ""}`} aria-hidden={!open}>
      <button
        type="button"
        className="np-sw-utility-backdrop"
        aria-label="关闭预测参数背景"
        tabIndex={open ? 0 : -1}
        onClick={() => onClose(false)}
      />
      <section
        ref={panelRef}
        id="homopolymer-prediction-parameters"
        className={`np-sw-popover np-sw-popover--modules np-hp-parameters${open ? " is-open" : ""}`}
        role="dialog"
        aria-modal="false"
        aria-labelledby="homopolymer-prediction-parameters-title"
        aria-hidden={!open}
        inert={!open}
      >
        <header className="np-sw-popover__header">
          <div>
            <span className="np-sw-popover__mark"><BarChart3 aria-hidden="true" /></span>
            <span>
              <h2 id="homopolymer-prediction-parameters-title">预测参数</h2>
              <small>普通 SMILES 与带 * 连接点的重复单元均可预测</small>
            </span>
          </div>
          <button type="button" className="np-sw-icon-button" aria-label="收起预测参数" onClick={() => onClose()}>
            <X aria-hidden="true" />
          </button>
        </header>

        <form className="np-hp-parameters__form" noValidate onSubmit={onSubmit}>
          <div className="np-hp-property-groups">
            {PREDICT_PROPERTY_GROUPS.map((group) => {
              const properties = PREDICT_PROPERTY_CATALOG.filter((property) => property.group === group.key);
              const groupSelected = properties.filter((property) => selected.has(property.key)).length;
              return (
                <section
                  key={group.key}
                  className={`np-hp-property-group is-${group.key}`}
                  role="group"
                  aria-labelledby={`homopolymer-property-group-${group.key}`}
                >
                  <header>
                    <span>
                      <strong id={`homopolymer-property-group-${group.key}`}>{group.label}</strong>
                    </span>
                    <button type="button" onClick={() => toggleGroup(group.key)}>
                      {groupSelected === properties.length ? "取消本组" : "选择本组"}
                    </button>
                  </header>
                  <div className="np-hp-property-grid">
                    {properties.map((property) => {
                      const checked = selected.has(property.key);
                      return (
                        <label key={property.key} className={`np-hp-property-option${checked ? " is-selected" : ""}`}>
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() => toggleProperty(property.key)}
                          />
                          <span className="np-hp-property-option__check" aria-hidden="true">
                            {checked ? <Check /> : null}
                          </span>
                          <span>
                            <strong>{property.label}</strong>
                            <small>{property.shortLabel} · {property.unit}</small>
                          </span>
                        </label>
                      );
                    })}
                  </div>
                </section>
              );
            })}
          </div>

          <footer className="np-hp-parameters__footer">
            <div>
              <strong>已选 {selectedProperties.length} / {PREDICTABLE_PROPERTIES.length} 项</strong>
              <span>
                <button
                  type="button"
                  onClick={() => onSelectedPropertiesChange([...PREDICTABLE_PROPERTIES])}
                  disabled={selectedProperties.length === PREDICTABLE_PROPERTIES.length}
                >
                  全选
                </button>
                <button
                  type="button"
                  onClick={() => onSelectedPropertiesChange([])}
                  disabled={selectedProperties.length === 0}
                >
                  清空
                </button>
              </span>
            </div>
            <button
              type="submit"
              className="np-sw-primary-button"
              disabled={selectedProperties.length === 0}
            >
              {submitting ? <LoaderCircle className="np-sw-spin" /> : <Sparkles aria-hidden="true" />}
              {submitting ? "重新预测" : "运行预测"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}
