import type { PropertyItem as PropertyItemData } from "../types";
import { PropertyItem } from "./PropertyItem";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";

type PropertyGroupCardProps = {
  title: string;
  items: PropertyItemData[];
};

export function PropertyGroupCard({ title, items }: PropertyGroupCardProps) {
  return (
    <Card className="flex h-full min-h-[280px] flex-col rounded-[24px] border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.88)_0%,rgba(245,249,250,0.82)_100%)] shadow-none">
      <CardHeader className="min-h-[88px] border-b border-white/70 pb-4">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="text-base tracking-tight">{title}</CardTitle>
          <div className="rounded-full border border-white bg-white/80 px-2.5 py-1 text-xs font-medium text-mutedForeground shadow-sm">
            {items.length}
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col space-y-2 pt-5">
        {items.length === 0 ? (
          <div className="flex flex-1 items-center rounded-[18px] border border-dashed border-slate-200/80 bg-white/85 px-4 py-5 text-sm leading-6 text-mutedForeground">
            当前结果在该属性分组下没有可用记录，可继续查看其他分组或切换查询条件。
          </div>
        ) : (
          items.map((item, index) => (
            <PropertyItem key={`${item.property_name}-${index}`} item={item} />
          ))
        )}
      </CardContent>
    </Card>
  );
}
