import type { SelectHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

export function Select({ className, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cn(
        "flex h-11 w-full rounded-2xl border border-white/80 bg-white/80 px-4 py-2 text-sm shadow-[inset_0_1px_0_rgba(255,255,255,0.7)] ring-offset-background transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        className
      )}
      {...props}
    />
  );
}
