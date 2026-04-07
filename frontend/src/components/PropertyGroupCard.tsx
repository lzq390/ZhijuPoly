import type { PropertyItem as PropertyItemData } from "../types";
import { PropertyItem } from "./PropertyItem";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";

type PropertyGroupCardProps = {
  title: string;
  items: PropertyItemData[];
};

export function PropertyGroupCard({ title, items }: PropertyGroupCardProps) {
  return (
    <Card className="h-full">
      <CardHeader className="pb-4">
        <CardTitle className="text-lg">{title}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {items.length === 0 ? (
          <div className="text-sm text-mutedForeground">No properties</div>
        ) : (
          items.map((item, index) => (
            <PropertyItem key={`${item.property_name}-${index}`} item={item} />
          ))
        )}
      </CardContent>
    </Card>
  );
}
