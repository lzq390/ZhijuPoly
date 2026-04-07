import type { PropertyItem as PropertyItemData } from "../types";

type PropertyItemProps = {
  item: PropertyItemData;
};

export function PropertyItem({ item }: PropertyItemProps) {
  const sourceLabel =
    item.label_source?.toLowerCase() === "exp" ? "Experimental" : item.label_source;

  return (
    <div className="flex items-center justify-between gap-4 border-b border-dashed py-2">
      <div className="space-y-1">
        <div className="font-semibold">{item.property_name}</div>
        {sourceLabel ? (
          <div className="text-xs uppercase tracking-[0.2em] text-mutedForeground">
            {sourceLabel}
          </div>
        ) : null}
      </div>
      <div className="text-right text-sm text-mutedForeground">
        <div>
          {item.property_value}
          {item.property_unit ? ` ${item.property_unit}` : ""}
        </div>
      </div>
    </div>
  );
}
