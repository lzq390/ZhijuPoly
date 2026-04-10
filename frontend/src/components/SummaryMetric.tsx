import type { ReactNode } from "react";
import { cn } from "../lib/utils";

export function SummaryMetric({
  icon,
  label,
  value,
  detail,
  mono = false,
  className
}: {
  icon?: ReactNode;
  label: string;
  value: string;
  detail?: string;
  mono?: boolean;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex min-h-[148px] flex-col rounded-[22px] border border-white/80 bg-[linear-gradient(180deg,rgba(255,255,255,0.9)_0%,rgba(246,250,251,0.82)_100%)] p-4 shadow-sm",
        className
      )}
    >
      <div className="flex min-h-[20px] items-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-mutedForeground">
        {icon}
        {label}
      </div>
      <div
        className={
          mono
            ? "font-mono-ui mt-4 break-all text-[13px] leading-6 text-slate-700"
            : "font-heading mt-4 text-lg font-semibold tracking-tight text-slate-950"
        }
      >
        {value}
      </div>
      <div className="mt-2.5 flex-1 text-sm leading-6 text-mutedForeground">{detail}</div>
    </div>
  );
}
