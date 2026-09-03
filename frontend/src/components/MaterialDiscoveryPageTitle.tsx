import type { ComponentPropsWithoutRef } from "react";
import "../styles/material-discovery-page-title.css";

export function MaterialDiscoveryPageTitle({
  className,
  ...props
}: ComponentPropsWithoutRef<"h1">) {
  const classes = ["np-material-discovery-page-title", className].filter(Boolean).join(" ");

  return <h1 {...props} className={classes} />;
}
