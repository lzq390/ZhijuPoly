import type { ComponentPropsWithoutRef, ReactNode } from "react";
import "../styles/module-page.css";

/** A module's identity stays outside its scrolling work surface. */
export function ModulePageHeader({ className, actions, ...props }: ComponentPropsWithoutRef<"h1"> & { actions?: ReactNode }) {
  return (
    <header className="np-module-page-header">
      <h1 {...props} className={["np-module-page-title", className].filter(Boolean).join(" ")} />
      {actions ? <div className="np-module-page-actions">{actions}</div> : null}
    </header>
  );
}
