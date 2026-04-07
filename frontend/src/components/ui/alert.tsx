import type { HTMLAttributes } from "react";
import { cn } from "../../lib/utils";

type AlertProps = HTMLAttributes<HTMLDivElement> & {
  variant?: "default" | "destructive";
};

export function Alert({ className, variant = "default", ...props }: AlertProps) {
  return (
    <div
      className={cn(
        "rounded-lg border px-4 py-3 text-sm",
        variant === "destructive"
          ? "border-destructive/20 bg-destructiveForeground text-destructive"
          : "border-input bg-accent text-foreground",
        className
      )}
      {...props}
    />
  );
}
