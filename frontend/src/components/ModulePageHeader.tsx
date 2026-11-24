import type { ComponentPropsWithoutRef, ReactNode } from "react";
import "../styles/module-page.css";

type ModulePageHeaderProps = ComponentPropsWithoutRef<"h1"> & {
  actions?: ReactNode;
  /** Optional one-line description beside the title; the title's own geometry never changes. */
  meta?: ReactNode;
};

/** A module's identity stays outside its scrolling work surface. */
export function ModulePageHeader({ className, actions, meta, ...props }: ModulePageHeaderProps) {
  return (
    <header className="np-module-page-header">
      <h1 {...props} className={["np-module-page-title", className].filter(Boolean).join(" ")} />
      {meta ? <p className="np-module-page-meta">{meta}</p> : null}
      {actions ? <div className="np-module-page-actions">{actions}</div> : null}
    </header>
  );
}
