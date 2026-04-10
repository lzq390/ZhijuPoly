import type { InputHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={cn(
        "flex h-11 w-full rounded-2xl border border-white/80 bg-white/80 px-4 py-2 text-sm shadow-[inset_0_1px_0_rgba(255,255,255,0.7)] ring-offset-background transition-all placeholder:text-mutedForeground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        className
      )}
      {...props}
    />
  );
}
