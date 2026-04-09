import type { ReactNode } from "react";
import { Card } from "./ui/card";

type OverviewCardProps = {
  title: string;
  value: string;
  detail: string;
  icon?: ReactNode;
  dark?: boolean;
  tags?: string[];
};

export function OverviewCard({ title, value, detail, icon, dark = false, tags = [] }: OverviewCardProps) {
  const theme = dark
    ? {
        card: "flex min-h-[140px] flex-col rounded-[24px] border-slate-200/80 bg-[linear-gradient(180deg,#0f172a_0%,#132238_100%)] p-5 text-slate-50 shadow-soft",
        label: "text-xs font-medium uppercase tracking-[0.16em] text-slate-400",
        value: "mt-4 text-3xl font-semibold",
        tag: "rounded-full bg-white/10 px-3 py-1 text-slate-200",
        detail: "mt-2 flex-1 text-sm leading-6 text-slate-300"
      }
    : {
        card: "flex min-h-[140px] flex-col rounded-[24px] border-slate-200/80 bg-white p-5",
        label: "text-xs font-medium uppercase tracking-[0.16em] text-mutedForeground",
        value: "mt-4 text-3xl font-semibold text-slate-950",
        tag: "rounded-full bg-slate-100 px-3 py-1 text-mutedForeground",
        detail: "mt-2 flex-1 text-sm leading-6 text-mutedForeground"
      };

  return (
    <Card className={theme.card}>
      <div className="flex items-center justify-between gap-3">
        <div className={theme.label}>{title}</div>
        {icon}
      </div>
      <div className={theme.value}>{value}</div>
      {tags.length > 0 ? (
        <div className="mt-3 flex flex-wrap gap-2 text-xs">
          {tags.map((tag) => (
            <span key={tag} className={theme.tag}>
              {tag}
            </span>
          ))}
        </div>
      ) : null}
      <div className={theme.detail}>{detail}</div>
    </Card>
  );
}
