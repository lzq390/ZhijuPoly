import type { TextareaHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

export function Textarea({ className, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className={cn(
        "flex min-h-[120px] w-full rounded-[1.35rem] border border-white/80 bg-white/80 px-4 py-3 text-sm shadow-[inset_0_1px_0_rgba(255,255,255,0.7)] ring-offset-background transition-all placeholder:text-mutedForeground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        className
      )}
      {...props}
    />
  );
}
