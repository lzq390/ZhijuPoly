import { useId, useRef, useState, type KeyboardEvent, type Ref } from "react";
import { useAuth } from "../auth/AuthProvider";
import { requestServiceAccess } from "../auth/guestAccess";
import { MessageSquare, BrainCircuit, ArrowUp, ArrowUpRight, BookOpen, ChartNoAxesCombined, Atom, LockKeyhole, LogIn } from "lucide-react";
import { resolveAgentWorkspaceUrl } from "../lib/openScienceProjectBridge";
import "./agent-workspace-home.css";

const researchPrompts = [
  { title: "文献与知识", icon: BookOpen, prompt: "帮我梳理聚酰亚胺低介电材料的研究进展" },
  { title: "性质与预测", icon: ChartNoAxesCombined, prompt: "聚合物的分子结构如何影响玻璃化转变温度？" },
  { title: "结构与设计", icon: Atom, prompt: "设计耐高温聚合物时，可以从哪些结构特征入手？" }
];

function GuestConversation() {
  const [tab, setTab] = useState<"chat" | "skills">("chat");
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const id = useId();

  function handleTabKey(event: KeyboardEvent<HTMLButtonElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? "chat" : event.key === "End" ? "skills" : tab === "chat" ? "skills" : "chat";
    setTab(next);
    tabRefs.current[next === "chat" ? 0 : 1]?.focus();
  }

  return (
    <section className="np-guest-conversation" aria-label="对话工作空间">
      <header className="np-guest-conversation__header">
        <div><h1>对话</h1><p>你的材料研究工作空间</p></div>
        <div className="np-guest-conversation__tabs" role="tablist" aria-label="对话视图">
          {(["chat", "skills"] as const).map((value, index) => {
            const Icon = value === "chat" ? MessageSquare : BrainCircuit;
            return <button key={value} ref={element => { tabRefs.current[index] = element; }} type="button" role="tab"
              id={`${id}-${value}-tab`} aria-controls={`${id}-panel`} aria-selected={tab === value}
              tabIndex={tab === value ? 0 : -1} onClick={() => setTab(value)} onKeyDown={handleTabKey}>
              <Icon aria-hidden="true" />{value === "chat" ? "聊天" : "技能"}
            </button>;
          })}
        </div>
      </header>

      <div className="np-guest-conversation__panel" id={`${id}-panel`} role="tabpanel" aria-labelledby={`${id}-${tab}-tab`} tabIndex={0}>
        <div className="np-guest-conversation__body">
          <div className="np-guest-conversation__intro">
            <span className="np-guest-conversation__mark" aria-hidden="true">{tab === "chat" ? <MessageSquare /> : <BrainCircuit />}</span>
            <h2>{tab === "chat" ? "从一个研究问题开始" : "让研究工具连接你的工作"}</h2>
            <p>{tab === "chat" ? "围绕文献、性质与分子结构，展开你的材料研究。" : "登录后使用智能体技能，协助完成材料检索、分析与设计。"}</p>
          </div>

          {tab === "chat" ? (
            <div className="np-guest-conversation__suggestions" aria-label="研究问题示例">
              {researchPrompts.map(({ title, icon: Icon, prompt }) => (
                <button type="button" key={title} onClick={() => { setDraft(prompt); inputRef.current?.focus(); }}>
                  <span className="np-guest-conversation__suggestion-title"><Icon aria-hidden="true" />{title}<ArrowUpRight aria-hidden="true" /></span>
                  <span className="np-guest-conversation__suggestion-text">{prompt}</span>
                </button>
              ))}
            </div>
          ) : (
            <div className="np-guest-conversation__skills">
              <div className="np-guest-conversation__skill-list">{researchPrompts.map(({ title, icon: Icon }) => <span key={title}><Icon aria-hidden="true" />{title}</span>)}</div>
              <button type="button" className="np-guest-conversation__login" onClick={() => requestServiceAccess()}><LogIn aria-hidden="true" />登录后使用技能</button>
            </div>
          )}
        </div>

        {tab === "chat" && (
          <div className="np-guest-conversation__input-area">
            <form className="np-guest-conversation__composer" onSubmit={event => { event.preventDefault(); requestServiceAccess(); }}>
              <textarea ref={inputRef} aria-label="发送消息" placeholder="描述你的研究问题，或选择上方示例…" value={draft} onChange={event => setDraft(event.target.value)} rows={3} />
              <div className="np-guest-conversation__composer-toolbar">
                <span><LockKeyhole aria-hidden="true" />登录后即可发送消息</span>
                <button type="submit" aria-label="发送消息"><ArrowUp aria-hidden="true" /></button>
              </div>
            </form>
            <p className="np-guest-conversation__input-note">游客可浏览科研模块，使用结构工作台的本地画板。</p>
          </div>
        )}
      </div>
    </section>
  );
}

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
  if (auth?.status === "guest") {
    return <GuestConversation />;
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
