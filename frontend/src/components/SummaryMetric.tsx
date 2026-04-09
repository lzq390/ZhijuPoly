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
    <div className={cn("flex min-h-[144px] flex-col rounded-[20px] border border-slate-200 bg-white p-4", className)}>
      <div className="flex min-h-[20px] items-center gap-2 text-xs font-medium uppercase tracking-[0.16em] text-mutedForeground">
        {icon}
        {label}
      </div>
      <div
        className={
          mono
            ? "mt-3 font-mono text-[13px] leading-6 text-slate-700"
            : "mt-3 text-lg font-semibold text-slate-950"
        }
      >
        {value}
      </div>
      <div className="mt-2 flex-1 text-sm leading-6 text-mutedForeground">{detail}</div>
    </div>
  );
}
