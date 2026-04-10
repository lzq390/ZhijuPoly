import type { HTMLAttributes } from "react";
import { cn } from "../../lib/utils";

export function Badge({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "inline-flex items-center rounded-full border border-white/80 bg-white/75 px-3 py-1 text-xs font-semibold tracking-[0.08em] text-primary shadow-sm backdrop-blur",
        className
      )}
      {...props}
    />
  );
}
