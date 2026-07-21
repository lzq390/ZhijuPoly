const DEFAULT_AGENT_WORKSPACE_URL = "http://127.0.0.1:4454";

function agentWorkspaceUrl() {
  return import.meta.env.VITE_AGENT_WORKSPACE_URL?.trim() || DEFAULT_AGENT_WORKSPACE_URL;
}

export function AgentWorkspaceHomePage() {
  return (
    <iframe
      title="智聚万物智能体工作台"
      src={agentWorkspaceUrl()}
      className="block h-full w-full border-0 bg-white"
    />
  );
}
