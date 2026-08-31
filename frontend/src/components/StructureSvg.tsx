import { useMemo } from "react";
import { cn } from "../lib/utils";

type StructureSvgProps = {
  svg: string;
  alt?: string;
  className?: string;
  imageClassName?: string;
  transparentBackground?: boolean;
};

function parseSvgLength(value: string | null) {
  if (value == null) return null;
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function isWhiteSvgFill(value: string) {
  const normalized = value.toLowerCase().replace(/\s+/g, "");
  return ["white", "#fff", "#ffffff", "rgb(255,255,255)", "rgba(255,255,255,1)"].includes(normalized);
}

function makeStructureSvgBackgroundTransparent(svg: string) {
  if (typeof DOMParser === "undefined" || typeof XMLSerializer === "undefined") return svg;

  const document = new DOMParser().parseFromString(svg, "image/svg+xml");
  const root = document.documentElement;
  if (root.localName !== "svg" || document.querySelector("parsererror")) return svg;

  const background = Array.from(root.children).find(
    (child) => !["defs", "metadata", "title"].includes(child.localName)
  );
  if (!background || background.localName !== "rect") return svg;

  const inlineStyle = background.getAttribute("style") ?? "";
  const styleFill = inlineStyle.match(/(?:^|;)\s*fill\s*:\s*([^;]+)/i)?.[1] ?? "";
  const fill = background.getAttribute("fill") ?? styleFill;
  if (!isWhiteSvgFill(fill)) return svg;

  const viewBox = (root.getAttribute("viewBox") ?? "")
    .trim()
    .split(/[\s,]+/)
    .map(Number)
    .filter(Number.isFinite);
  const canvasX = viewBox.length === 4 ? viewBox[0] : 0;
  const canvasY = viewBox.length === 4 ? viewBox[1] : 0;
  const canvasWidth = viewBox.length === 4 ? viewBox[2] : parseSvgLength(root.getAttribute("width"));
  const canvasHeight = viewBox.length === 4 ? viewBox[3] : parseSvgLength(root.getAttribute("height"));
  const rectX = parseSvgLength(background.getAttribute("x")) ?? 0;
  const rectY = parseSvgLength(background.getAttribute("y")) ?? 0;
  const rectWidth = parseSvgLength(background.getAttribute("width"));
  const rectHeight = parseSvgLength(background.getAttribute("height"));
  const nearlyEqual = (left: number, right: number) => Math.abs(left - right) < 0.01;

  if (
    canvasWidth == null ||
    canvasHeight == null ||
    rectWidth == null ||
    rectHeight == null ||
    !nearlyEqual(rectX, canvasX) ||
    !nearlyEqual(rectY, canvasY) ||
    !nearlyEqual(rectWidth, canvasWidth) ||
    !nearlyEqual(rectHeight, canvasHeight)
  ) {
    return svg;
  }

  background.remove();
  return new XMLSerializer().serializeToString(root);
}

export function StructureSvg({
  svg,
  alt = "2D structure",
  className,
  imageClassName,
  transparentBackground = false
}: StructureSvgProps) {
  const src = useMemo(() => {
    const trimmedSvg = svg.trim();
    const renderedSvg = transparentBackground
      ? makeStructureSvgBackgroundTransparent(trimmedSvg)
      : trimmedSvg;
    return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(renderedSvg)}`;
  }, [svg, transparentBackground]);

  return (
    <div className={className}>
      <img
        src={src}
        alt={alt}
        className={cn("mx-auto block h-auto max-h-full w-full max-w-full object-contain", imageClassName)}
        decoding="async"
        draggable={false}
        loading="lazy"
        referrerPolicy="no-referrer"
      />
    </div>
  );
}
