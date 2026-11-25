import { LoaderCircle } from "lucide-react";
import type { ReactNode, RefObject } from "react";
import "./module-frame.css";

export type ModuleTool = {
  id: string;
  label: string;
  icon: ReactNode;
  pressed?: boolean;
  /** Shows progress in place of the icon, e.g. while the assistant is answering. */
  busy?: boolean;
  /** A short tag after the label for an entry that opens but is not in service yet, e.g. "未开放".
      The mark drops the action colour so the entry does not read as the primary tool. */
  status?: string;
  controls?: string;
  buttonRef?: RefObject<HTMLButtonElement | null>;
  onClick: () => void;
};

type ModuleToolsProps = {
  /** Module-level tools, in the platform order: history, then AI assistant. */
  tools?: readonly ModuleTool[];
  /** The app-level browsing recording entry for this module. */
  recording?: ReactNode;
  /** Usage help is always last and separated from the action tools. */
  help?: ModuleTool;
};

function ToolButton({ tool }: { tool: ModuleTool }) {
  return (
    <button
      ref={tool.buttonRef}
      type="button"
      className={`np-module-tools__button${tool.pressed ? " is-pressed" : ""}`}
      data-module-tool={tool.id}
      data-status={tool.status ? "" : undefined}
      aria-pressed={tool.pressed}
      aria-controls={tool.controls}
      aria-busy={tool.busy || undefined}
      onClick={tool.onClick}
    >
      {tool.busy ? <LoaderCircle className="np-module-tools__spin" aria-hidden="true" /> : tool.icon}
      <span>{tool.label}</span>
      {/* The hidden comma makes the accessible name read "AI 助手，未开放" rather than run together. */}
      {tool.status ? <span className="np-module-tools__status"><span className="np-module-tools__sr">，</span>{tool.status}</span> : null}
    </button>
  );
}

/** One header tool group per module, so auxiliary entries keep the same place and order everywhere. */
export function ModuleTools({ tools = [], recording, help }: ModuleToolsProps) {
  return (
    <div className="np-module-tools" role="group" aria-label="模块工具">
      {tools.map((tool) => <ToolButton key={tool.id} tool={tool} />)}
      {recording ? (
        <>
          {tools.length > 0 ? <span className="np-module-tools__separator" aria-hidden="true" /> : null}
          <span className="np-module-tools__recording">{recording}</span>
        </>
      ) : null}
      {help ? (
        <>
          <span className="np-module-tools__separator" aria-hidden="true" />
          <ToolButton tool={help} />
        </>
      ) : null}
    </div>
  );
}
