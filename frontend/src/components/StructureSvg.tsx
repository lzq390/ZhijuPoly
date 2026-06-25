import { useMemo } from "react";
import { cn } from "../lib/utils";

type StructureSvgProps = {
  svg: string;
  alt?: string;
  className?: string;
  imageClassName?: string;
};

export function StructureSvg({ svg, alt = "2D structure", className, imageClassName }: StructureSvgProps) {
  const src = useMemo(() => {
    const trimmedSvg = svg.trim();
    return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(trimmedSvg)}`;
  }, [svg]);

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