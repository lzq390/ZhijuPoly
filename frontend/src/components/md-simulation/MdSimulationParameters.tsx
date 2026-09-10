import { useMotionPresence } from "../../hooks/useMotionPresence";
import {
  Boxes,
  LoaderCircle,
  Play,
  RotateCcw,
  SlidersHorizontal,
  Thermometer,
  X,
} from "lucide-react";
import type { FormEvent, RefObject } from "react";
import type { MdDemoRunRequest } from "../../types";
import type { MdSimulationField, MdSimulationFormErrors } from "./config";
import { ForcefieldPicker } from "./ForcefieldPicker";

type NumberFieldProps = {
  id: string;
  label: string;
  value: number;
  unit: string;
  min: number;
  max: number;
  integer?: boolean;
  error?: string;
  onChange: (value: number) => void;
  onTouched: () => void;
};

function NumberField({
  id,
  label,
  value,
  unit,
  min,
  max,
  integer = false,
  error,
  onChange,
  onTouched,
}: NumberFieldProps) {
  const errorId = `${id}-error`;
  return (
    <label className={`np-md-field${error ? " has-error" : ""}`} htmlFor={id}>
      <span>{label}</span>
      <div className="np-md-number-input">
        <input
          id={id}
          type="number"
          value={Number.isFinite(value) ? value : ""}
          min={min}
          max={max}
          step={integer ? 1 : "any"}
          inputMode="decimal"
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : undefined}
          onChange={(event) =>
            onChange(
              event.target.value === ""
                ? Number.NaN
                : Number(event.target.value),
            )
          }
          onBlur={onTouched}
        />
        <small>{unit}</small>
      </div>
      {error ? (
        <em id={errorId} role="alert">
          {error}
        </em>
      ) : null}
    </label>
  );
}

type MdSimulationParametersProps = {
  open: boolean;
  panelRef: RefObject<HTMLElement | null>;
  request: MdDemoRunRequest;
  errors: MdSimulationFormErrors;
  canRun: boolean;
  submitting: boolean;
  statusMessage: string;
  onChange: (update: Partial<MdDemoRunRequest>) => void;
  onTouched: (field: MdSimulationField) => void;
  onClose: (restoreFocus?: boolean) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onReset: () => void;
};

export function MdSimulationParameters({
  open,
  panelRef,
  request,
  errors,
  canRun,
  submitting,
  statusMessage,
  onChange,
  onTouched,
  onClose,
  onSubmit,
  onReset,
}: MdSimulationParametersProps) {
  const presence = useMotionPresence(open, { elementRef: panelRef });
  const forcefieldError = errors.forcefield;
  return (
    <div
      className={`np-sw-utility-layer${presence.present ? " is-open" : ""}`}
      aria-hidden={!open}
    >
      <button
        type="button"
        className="np-sw-utility-backdrop"
        aria-label="关闭 MD 模拟参数"
        tabIndex={open ? 0 : -1}
        onClick={() => onClose(false)}
      />
      <section
        ref={panelRef}
        {...presence.motionProps}
        id="md-simulation-parameters"
        className={`np-sw-popover np-sw-popover--modules np-md-parameters${open ? " is-open" : ""}`}
        role="dialog"
        aria-modal="false"
        aria-labelledby="md-simulation-parameters-title"
        aria-hidden={!open}
        inert={!open}
      >
        <header className="np-sw-popover__header">
          <div>
            <span className="np-sw-popover__mark">
              <SlidersHorizontal aria-hidden="true" />
            </span>
            <span>
              <h2 id="md-simulation-parameters-title">MD 模拟参数</h2>
              <small>设置热力学条件、目标体系规模与力场</small>
            </span>
          </div>
          <button
            type="button"
            className="np-sw-icon-button"
            aria-label="收起 MD 模拟参数"
            onClick={() => onClose()}
          >
            <X aria-hidden="true" />
          </button>
        </header>

        <form className="np-md-parameters__form" noValidate onSubmit={onSubmit}>
          <div className="np-md-parameter-groups">
            <section
              className="np-md-parameter-group"
              aria-labelledby="md-thermo-parameters-title"
            >
              <header>
                <span>
                  <Thermometer aria-hidden="true" />
                </span>
                <div>
                  <strong id="md-thermo-parameters-title">热力学条件</strong>
                  <small>设置模拟的目标温度与压力</small>
                </div>
              </header>
              <div className="np-md-parameter-grid is-thermo">
                <NumberField
                  id="md-temperature"
                  label="目标温度"
                  value={request.temperature}
                  unit="K"
                  min={0}
                  max={5000}
                  error={errors.temperature}
                  onChange={(temperature) => onChange({ temperature })}
                  onTouched={() => onTouched("temperature")}
                />
                <NumberField
                  id="md-pressure"
                  label="目标压力"
                  value={request.pressure}
                  unit="atm"
                  min={0}
                  max={100000}
                  error={errors.pressure}
                  onChange={(pressure) => onChange({ pressure })}
                  onTouched={() => onTouched("pressure")}
                />
              </div>
            </section>

            <section
              className="np-md-parameter-group"
              aria-labelledby="md-scale-parameters-title"
            >
              <header>
                <span>
                  <Boxes aria-hidden="true" />
                </span>
                <div>
                  <strong id="md-scale-parameters-title">体系规模与力场</strong>
                  <small>设置体系规模与所用力场</small>
                </div>
              </header>
              <div className="np-md-parameter-grid is-scale">
                <NumberField
                  id="md-atom-count"
                  label="目标原子数"
                  value={request.n_atom}
                  unit="个原子"
                  min={100}
                  max={500000}
                  integer
                  error={errors.n_atom}
                  onChange={(n_atom) => onChange({ n_atom })}
                  onTouched={() => onTouched("n_atom")}
                />
                <NumberField
                  id="md-chain-count"
                  label="链数"
                  value={request.n_chain}
                  unit="条链"
                  min={1}
                  max={10000}
                  integer
                  error={errors.n_chain}
                  onChange={(n_chain) => onChange({ n_chain })}
                  onTouched={() => onTouched("n_chain")}
                />
                <ForcefieldPicker
                  value={request.forcefield}
                  error={forcefieldError}
                  onChange={(forcefield) => onChange({ forcefield })}
                  onTouched={() => onTouched("forcefield")}
                />
              </div>
            </section>
          </div>

          <footer className="np-md-parameters__footer">
            <div>
              <strong>{statusMessage}</strong>
              <small>将展示示例结构的真实 MD 计算结果。</small>
            </div>
            <span>
              <button
                type="button"
                className="np-sw-secondary-button"
                onClick={onReset}
              >
                <RotateCcw aria-hidden="true" />
                重置
              </button>
              <button
                type="submit"
                aria-busy={submitting}
                className="np-sw-primary-button"
                disabled={!canRun}
              >
                {submitting ? (
                  <LoaderCircle className="np-sw-spin" />
                ) : (
                  <Play aria-hidden="true" />
                )}
                {submitting ? "模拟中" : "开始模拟"}
              </button>
            </span>
          </footer>
        </form>
      </section>
    </div>
  );
}
