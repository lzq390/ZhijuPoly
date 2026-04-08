import type { PropertyItem as PropertyItemData } from "../types";

type PropertyItemProps = {
  item: PropertyItemData;
};

export function PropertyItem({ item }: PropertyItemProps) {
  const sourceLabel =
    item.label_source?.toLowerCase() === "exp" ? "Experimental" : item.label_source;

  return (
    <div className="grid min-h-[78px] grid-cols-[minmax(0,1fr)_auto] items-start gap-4 rounded-xl border border-slate-200/80 bg-white px-3 py-3">
      <div className="min-w-0 space-y-1">
        <div className="text-sm font-semibold text-slate-900">{item.property_name}</div>
        {sourceLabel ? (
          <div className="text-[11px] uppercase tracking-[0.18em] text-mutedForeground">
            {sourceLabel}
          </div>
        ) : null}
      </div>
      <div className="text-right text-sm text-slate-700">
        <div className="font-medium text-slate-900">
          {item.property_value}
          {item.property_unit ? ` ${item.property_unit}` : ""}
        </div>
      </div>
    </div>
  );
}
