import { useMotionPresence } from "../../hooks/useMotionPresence";
import { Check, Database, FlaskConical, LoaderCircle, Search, X } from "lucide-react";
import type { FormEvent, RefObject } from "react";
import type { SmilesLookupTable } from "../../types";
import { DATABASE_QUERY_TABLES } from "./config";

type DatabaseQueryParametersProps = {
  open: boolean;
  panelRef: RefObject<HTMLElement | null>;
  selectedTable: SmilesLookupTable;
  submitting: boolean;
  onClose: (restoreFocus?: boolean) => void;
  onTableChange: (table: SmilesLookupTable) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
};

export function DatabaseQueryParameters({
  open,
  panelRef,
  selectedTable,
  submitting,
  onClose,
  onTableChange,
  onSubmit
}: DatabaseQueryParametersProps) {
  const presence = useMotionPresence(open, { elementRef: panelRef });
  return (
    <div className={`np-sw-utility-layer${presence.present ? " is-open" : ""}`} aria-hidden={!open}>
      <button
        type="button"
        className="np-sw-utility-backdrop"
        aria-label="关闭数据库查询参数背景"
        tabIndex={open ? 0 : -1}
        onClick={() => onClose(false)}
      />
      <section
        ref={panelRef}
        {...presence.motionProps}
        id="database-query-parameters"
        className={`np-sw-popover np-sw-popover--modules np-dq-parameters${open ? " is-open" : ""}`}
        role="dialog"
        aria-modal="false"
        aria-labelledby="database-query-parameters-title"
        aria-hidden={!open}
        inert={!open}
      >
        <header className="np-sw-popover__header">
          <div>
            <span className="np-sw-popover__mark"><Database aria-hidden="true" /></span>
            <span>
              <h2 id="database-query-parameters-title">数据库查询参数</h2>
              <small>选择目标数据表，按规范化 SMILES 进行精确匹配</small>
            </span>
          </div>
          <button type="button" className="np-sw-icon-button" aria-label="收起数据库查询参数" onClick={() => onClose()}>
            <X aria-hidden="true" />
          </button>
        </header>

        <form className="np-dq-parameters__form" noValidate onSubmit={onSubmit}>
          <fieldset>
            <legend>目标数据表</legend>
            <div className="np-dq-table-grid" role="radiogroup" aria-label="数据库查询目标表">
              {DATABASE_QUERY_TABLES.map((option) => {
                const selected = selectedTable === option.value;
                return (
                  <button
                    key={option.value}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    className={selected ? "is-selected" : ""}
                    onClick={() => onTableChange(option.value)}
                  >
                    <span><Database aria-hidden="true" /></span>
                    <span>
                      <strong>{option.label}</strong>
                      <small>{option.description}</small>
                      <code>{option.fields}</code>
                    </span>
                    <Check aria-hidden="true" />
                  </button>
                );
              })}
            </div>
          </fieldset>

          <footer className="np-dq-parameters__footer">
            <span><FlaskConical aria-hidden="true" /> Canonical SMILES 精确匹配</span>
            <button type="submit" className="np-sw-primary-button" disabled={submitting} aria-busy={submitting}>
              {submitting ? <LoaderCircle className="np-sw-spin" /> : <Search aria-hidden="true" />}
              {submitting ? "正在准备" : "运行查询"}
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}
