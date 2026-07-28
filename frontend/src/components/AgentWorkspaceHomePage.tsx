import type { Ref } from "react";

const DEFAULT_AGENT_WORKSPACE_URL = "http://127.0.0.1:4454";

export function agentWorkspaceUrl() {
  return import.meta.env.VITE_AGENT_WORKSPACE_URL?.trim() || DEFAULT_AGENT_WORKSPACE_URL;
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
  return (
    <iframe
      key={reloadKey}
      ref={iframeRef}
      onLoad={onLoad}
      title="智聚万物智能体工作台"
      src={src}
      className="block h-full w-full border-0 bg-white"
    />
  );
}
