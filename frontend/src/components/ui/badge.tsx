import type { HTMLAttributes } from "react";
import { cn } from "../../lib/utils";

export function Badge({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "inline-flex items-center rounded-full bg-primary/10 px-3 py-1 text-xs font-semibold text-primary",
        className
      )}
      {...props}
    />
  );
}
