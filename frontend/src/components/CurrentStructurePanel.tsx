import { Atom } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "../lib/utils";
import type { StructureWorkspaceContext } from "../types";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";

export function WorkbenchPanel({
  children,
  className,
  id
}: {
  children: ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <section
      id={id}
      className={cn(
        "overflow-hidden rounded-[24px] border border-sky-100 bg-white shadow-[0_22px_58px_rgba(37,99,235,0.12),0_6px_18px_rgba(15,23,42,0.05)] ring-1 ring-white/80",
        className
      )}
    >
      {children}
    </section>
  );
}

export function CurrentStructurePanel({
  structure,
  onEditStructure,
  className,
  compact = false
}: {
  structure: StructureWorkspaceContext;
  onEditStructure: () => void;
  className?: string;
  compact?: boolean;
}) {
  const hasStructure = structure.smiles.trim().length > 0;

  return (
    <WorkbenchPanel className={className}>
      <div
        className={cn(
          "flex flex-col gap-4 p-4 md:flex-row md:items-center md:justify-between",
          compact ? "md:p-4" : "md:p-5"
        )}
      >
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              className={
                hasStructure
                  ? "border border-cyan-200 bg-cyan-50 text-cyan-800"
                  : "bg-slate-100 text-slate-700"
              }
            >
              {hasStructure ? "结构已就绪" : "未设置结构"}
            </Badge>
            <Badge className="border border-violet-200 bg-violet-50 text-violet-800">
              共享结构
            </Badge>
          </div>
          <div className="mt-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">
            Current SMILES
          </div>
          <div className="mt-1 max-w-full break-all font-mono-ui text-sm leading-6 text-slate-950">
            {hasStructure ? structure.smiles : "请先在结构工作台绘制、导入或输入结构。"}
          </div>
        </div>
        <Button
          type="button"
          variant="outline"
          onClick={onEditStructure}
          className="min-h-[44px] min-w-[168px] border-sky-100 bg-white text-slate-700 shadow-[0_12px_28px_rgba(37,99,235,0.08)] hover:border-blue-200 hover:bg-blue-50"
        >
          <Atom className="mr-2 h-4 w-4" />
          编辑结构
        </Button>
      </div>
    </WorkbenchPanel>
  );
}

export function MissingStructurePanel({
  title,
  description,
  onEditStructure
}: {
  title: string;
  description: string;
  onEditStructure: () => void;
}) {
  return (
    <WorkbenchPanel>
      <div className="flex min-h-[340px] flex-col items-center justify-center px-6 py-12 text-center">
        <div className="flex h-16 w-16 items-center justify-center rounded-[22px] border border-sky-100 bg-sky-50 text-blue-600 shadow-[0_16px_36px_rgba(37,99,235,0.12)]">
          <Atom className="h-6 w-6" />
        </div>
        <h2 className="font-heading mt-6 text-2xl font-semibold text-slate-950">{title}</h2>
        <p className="mt-3 max-w-2xl text-sm leading-6 text-slate-600">{description}</p>
        <Button
          type="button"
          onClick={onEditStructure}
          className="mt-7 min-h-[46px] min-w-[190px] rounded-[16px] bg-blue-600 text-white shadow-[0_18px_46px_rgba(37,99,235,0.32)] hover:bg-blue-500"
        >
          <Atom className="mr-2 h-4 w-4" />
          前往结构工作台
        </Button>
      </div>
    </WorkbenchPanel>
  );
}
