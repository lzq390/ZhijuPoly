import { useId, type CSSProperties, type ReactNode } from "react";
import "./resource-inspection.css";

export interface InspectionMetric {
  label: string;
  value: string;
  amount: number | null;
  max?: number;
  description?: string;
  /** @deprecated Threshold colors are shared by all inspection metrics. */
  color?: string;
}

export interface ResourceInspectionCardProps {
  title: string;
  subtitle: string;
  icon: ReactNode;
  metrics: InspectionMetric[];
  selected: boolean;
  onSelect: () => void;
  status: string;
  warning?: boolean;
  note?: string;
  kind: "server" | "gpu";
}

const SEGMENT_COUNT = 16;

export function ResourceInspectionCard({
  title, subtitle, icon, metrics, selected, onSelect, status, warning = false, note, kind,
}: ResourceInspectionCardProps) {
  const descriptionPrefix = useId();

  return <button
    type="button"
    className={`sm-inspection-card${kind === "gpu" ? " sm-gpu" : " sm-inspection-server"}${selected ? " is-selected" : ""}`}
    aria-pressed={selected}
    aria-label={`查看${title}详情`}
    onClick={onSelect}
  >
    <span className="sm-inspection-heading">
      <span className="sm-inspection-icon" aria-hidden="true">{icon}</span>
      <span className="sm-inspection-identity">
        <strong className="sm-inspection-title">{title}</strong>
        <span className="sm-inspection-subtitle" title={subtitle}>{subtitle}</span>
      </span>
      <span className="sm-inspection-selection" aria-hidden="true">
        <svg viewBox="0 0 16 16" fill="none"><path d="m4 8 2.5 2.5L12 5" /></svg>
      </span>
    </span>

    <span className="sm-inspection-metrics">
      {metrics.map((metric, index) => {
        const maximum = Number.isFinite(metric.max) && (metric.max ?? 0) > 0 ? metric.max! : 100;
        const known = metric.amount !== null && Number.isFinite(metric.amount);
        const amount = known ? Math.min(maximum, Math.max(0, metric.amount!)) : null;
        const fraction = amount === null ? 0 : amount / maximum;
        const level = !known ? "unknown" : fraction >= 0.85 ? "high" : fraction >= 0.6 ? "medium" : "low";
        const levelDescription = {
          unknown: "数据不可用",
          low: "刻度占比低，低于 60%",
          medium: "刻度占比中，60% 至低于 85%",
          high: "刻度占比高，85% 及以上",
        }[level];
        const descriptionId = `${descriptionPrefix}-metric-${index}`;

        return <span className="sm-inspection-metric" key={`${metric.label}-${index}`} title={`${levelDescription}${metric.description ? `；${metric.description}` : ""}`}>
          <span className="sm-inspection-metric-heading">
            <span className="sm-inspection-label">{metric.label}</span>
            <strong className="sm-inspection-value">{metric.value}</strong>
          </span>
          <span
            className={`sm-inspection-bar is-${level}`}
            role="progressbar"
            aria-label={`${title} ${metric.label}`}
            aria-valuemin={0}
            aria-valuemax={maximum}
            aria-valuenow={amount ?? undefined}
            aria-valuetext={`${metric.value}，${levelDescription}`}
            aria-describedby={metric.description ? descriptionId : undefined}
          >
            {Array.from({ length: SEGMENT_COUNT }, (_, segment) => <span
              key={segment}
              aria-hidden="true"
              style={{ "--sm-segment-fill": `${Math.min(100, Math.max(0, fraction * SEGMENT_COUNT - segment) * 100)}%` } as CSSProperties}
            />)}
          </span>
          {metric.description && <span className="sm-inspection-description" id={descriptionId}>
            {metric.description}
          </span>}
        </span>;
      })}
    </span>

    <span className="sm-inspection-footer">
      <span className={`sm-inspection-status${warning ? " is-warning" : ""}`}>
        <span className="sm-inspection-status-dot" aria-hidden="true" />{status}
      </span>
      {note && <span className="sm-inspection-note" title={note}>{note}</span>}
    </span>
  </button>;
}
