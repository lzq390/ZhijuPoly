import type { ComponentPropsWithoutRef } from "react";
import "../styles/module-page.css";

/** A module's identity stays outside its scrolling work surface. */
export function ModulePageHeader({ className, ...props }: ComponentPropsWithoutRef<"h1">) {
  return (
    <header className="np-module-page-header">
      <h1 {...props} className={["np-module-page-title", className].filter(Boolean).join(" ")} />
    </header>
  );
}
