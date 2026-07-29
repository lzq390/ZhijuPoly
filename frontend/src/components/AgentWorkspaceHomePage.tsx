import type { Ref } from "react";
import { resolveAgentWorkspaceUrl } from "../lib/openScienceProjectBridge";

export function agentWorkspaceUrl() {
  return resolveAgentWorkspaceUrl(import.meta.env.VITE_AGENT_WORKSPACE_URL ?? "") ?? "";
}

type AgentWorkspaceHomePageProps = {
  iframeRef?: Ref<HTMLIFrameElement>;
  onLoad?: () => void;
  src?: string;
  reloadKey?: number;
};

export function AgentWorkspaceHomePage({
  iframeRef,
  onLoad,
  src = agentWorkspaceUrl(),
  reloadKey = 0
}: AgentWorkspaceHomePageProps) {
  const workspaceUrl = resolveAgentWorkspaceUrl(src);
  if (!workspaceUrl) {
    return (
      <div
        role="status"
        aria-live="polite"
        className="flex h-full w-full items-center justify-center bg-white text-sm text-slate-500"
      >
        正在同步
      </div>
    );
  }

  return (
    <iframe
      key={reloadKey}
      ref={iframeRef}
      onLoad={onLoad}
      title="智聚万物智能体工作台"
      src={workspaceUrl}
      className="block h-full w-full border-0 bg-white"
    />
  );
}
