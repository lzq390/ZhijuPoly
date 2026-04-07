import type { TextareaHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

export function Textarea({ className, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className={cn(
        "flex min-h-[120px] w-full rounded-lg border border-input bg-card px-3 py-2 text-sm ring-offset-background placeholder:text-mutedForeground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        className
      )}
      {...props}
    />
  );
}
