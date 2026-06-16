import type { PropertyItem as PropertyItemData } from "../types";

type PropertyItemProps = {
  item: PropertyItemData;
};

export function PropertyItem({ item }: PropertyItemProps) {
  const normalizedSource = item.label_source?.trim().toLowerCase();
  const sourceLabel =
    normalizedSource === "exp" || normalizedSource === "experimental"
      ? "实验"
      : normalizedSource === "sim" || normalizedSource === "simulated"
        ? "模拟"
        : normalizedSource === "n/a" || normalizedSource === "na"
          ? "未标注"
          : item.label_source;

  return (
    <div className="grid min-h-[82px] grid-cols-[minmax(0,1fr)_auto] items-start gap-4 rounded-[18px] border border-white/80 bg-white/90 px-3.5 py-3 shadow-sm">
      <div className="min-w-0 space-y-1">
        <div className="text-sm font-semibold text-slate-900">{item.property_name}</div>
        {sourceLabel ? (
          <div className="text-[11px] uppercase tracking-[0.2em] text-mutedForeground">
            {sourceLabel}
          </div>
        ) : null}
      </div>
      <div className="text-right text-sm text-slate-700">
        <div className="font-mono-ui font-medium text-slate-900">
          {item.property_value}
          {item.property_unit ? ` ${item.property_unit}` : ""}
        </div>
      </div>
    </div>
  );
}
