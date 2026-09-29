import { useState, type Ref } from "react";
import { useAuth } from "../auth/AuthProvider";
import { requestServiceAccess } from "../auth/guestAccess";
import { MessageSquare, BrainCircuit, ArrowUp } from "lucide-react";
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
  const auth = useAuth();
  const [tab, setTab] = useState("chat");
  if (auth?.status === "guest") {
    return <section className="np-guest-conversation" aria-label="对话工作空间">
      <header><h1>对话</h1><div role="tablist" aria-label="对话视图"><button type="button" role="tab" aria-selected={tab === "chat"} onClick={() => setTab("chat")}><MessageSquare />聊天</button><button type="button" role="tab" aria-selected={tab === "skills"} onClick={() => setTab("skills")}><BrainCircuit />技能</button></div></header>
      <div className="np-guest-conversation__body"><p>请登录账号。</p><button type="button" onClick={() => requestServiceAccess()}>登录后开始{tab === "chat" ? "对话" : "使用技能"}</button></div>
      {tab === "chat" && <form className="np-guest-conversation__composer" onSubmit={event => { event.preventDefault(); requestServiceAccess(); }}><textarea aria-label="发送消息" placeholder="向智能体提问…" /><button type="submit" aria-label="发送消息"><ArrowUp /></button></form>}
    </section>;
  }
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
