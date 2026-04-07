import type { InputHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={cn(
        "flex h-10 w-full rounded-lg border border-input bg-card px-3 py-2 text-sm ring-offset-background placeholder:text-mutedForeground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        className
      )}
      {...props}
    />
  );
}
