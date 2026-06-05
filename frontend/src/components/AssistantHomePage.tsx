import { type KeyboardEvent, type WheelEvent, useEffect, useRef, useState } from "react";
import { ArrowUp, Bot, Eraser, Loader2, MessageSquare, OctagonX, Sparkles, UserRound } from "lucide-react";
import type { AppShellModuleGroup } from "./AppShell";
import { Button } from "./ui/button";
import { Textarea } from "./ui/textarea";
import { useAssistantChat } from "../hooks/useAssistantChat";
import type { AssistantModuleContext, AssistantPredictionSkillResult, AssistantSkillCall } from "../types";

type AssistantHomePageProps = {
  activeModule: string;
  modules: AssistantModuleContext[];
  moduleGroups: AppShellModuleGroup[];
  onOpenModule: (moduleId: string) => void;
};

const starterPrompts = [
  "我有一批聚合物数据，应该先从哪个模块开始清洗和分析？",
  "想提升 Tg，同时兼顾可合成性，应该关注哪些结构因素？",
  "帮我规划一次聚合物知识检索，主题是高介电 PI 材料。",
  "给定 SMILES，如何判断它更适合做性能探索还是逆向设计？"
];

export function AssistantHomePage({ activeModule, modules, moduleGroups, onOpenModule }: AssistantHomePageProps) {
  const [draft, setDraft] = useState("");
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const { messages, isStreaming, error, sendMessage, stopStreaming, clearMessages } = useAssistantChat();
  const trimmedDraft = draft.trim();
  const moduleItems = moduleGroups.flatMap((group) => group.items);
  const streamingAssistantMessageId = isStreaming
    ? [...messages].reverse().find((message) => message.role === "assistant")?.id
    : null;

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, isStreaming]);

  async function submitMessage(text: string) {
    const value = text.trim();
    if (!value || isStreaming) {
      return;
    }
    setDraft("");
    await sendMessage(value, {
      active_module: activeModule,
      modules
    });
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) {
      return;
    }
    event.preventDefault();
    void submitMessage(draft);
  }

  return (
    <section className="relative h-full min-h-0 bg-[#f8f9fb]">
      {messages.length === 0 ? (
        <div className="h-full overflow-y-auto px-4 py-6 md:px-8 lg:px-10">
          <div className="mx-auto flex min-h-full w-full max-w-3xl flex-col justify-center pt-8">
            <div className="text-center">
              <div className="inline-flex items-center gap-2 rounded-full border border-teal-100 bg-white px-3 py-1.5 text-xs font-semibold text-teal-700 shadow-sm">
                <Sparkles className="h-3.5 w-3.5" />
                智聚万物
              </div>
              <h1 className="font-heading mt-4 text-3xl font-semibold tracking-tight text-slate-950 md:text-4xl">
                今天研究什么聚合物问题？
              </h1>
              <p className="mx-auto mt-3 max-w-2xl text-sm leading-6 text-slate-500 md:text-base">
                围绕数据采集、知识检索、性能探索和分子设计进行科研对话。
              </p>
            </div>

            {isStreaming ? <StreamingStatus /> : null}
            {error ? <ErrorNotice detail={error} /> : null}

            <div className="mt-[14vh] grid gap-3 md:mt-[18vh] md:grid-cols-2">
              {starterPrompts.map((prompt) => (
                <button
                  key={prompt}
                  type="button"
                  className="min-h-[88px] rounded-2xl border border-slate-200 bg-white p-4 text-left text-sm leading-6 text-slate-700 shadow-sm transition hover:-translate-y-0.5 hover:border-teal-200 hover:text-slate-950 hover:shadow-md"
                  onClick={() => void submitMessage(prompt)}
                >
                  <MessageSquare className="mb-3 h-4 w-4 text-teal-600" />
                  {prompt}
                </button>
              ))}
            </div>

            <ModuleShortcutRow items={moduleItems} onOpenModule={onOpenModule} />

            <ChatComposer
              className="mt-5"
              draft={draft}
              trimmedDraft={trimmedDraft}
              isStreaming={isStreaming}
              onChange={setDraft}
              onSubmit={() => void submitMessage(draft)}
              onStop={stopStreaming}
              onKeyDown={handleKeyDown}
            />
          </div>
        </div>
      ) : (
        <>
          <div className="h-full overflow-y-auto px-4 py-6 md:px-8 lg:px-10">
            <div className="mx-auto flex min-h-full w-full max-w-4xl flex-col">
              <div className="mb-4 flex shrink-0 items-center justify-between gap-3">
                <div className="inline-flex items-center gap-2 rounded-full border border-teal-100 bg-white px-3 py-1.5 text-xs font-semibold text-teal-700 shadow-sm">
                  <Sparkles className="h-3.5 w-3.5" />
                  智聚万物
                </div>
                <Button type="button" variant="outline" className="h-10 rounded-xl px-3" onClick={clearMessages}>
                  <Eraser className="mr-2 h-4 w-4" />
                  清空
                </Button>
              </div>

              <div className="flex flex-col gap-5 pb-8 pt-1">
                {messages.map((message) => (
                  <article key={message.id} className={message.role === "user" ? "flex justify-end" : "flex justify-start"}>
                    <div
                      className={[
                        "flex max-w-[88%] gap-3 rounded-3xl px-4 py-3 text-sm leading-7 shadow-sm md:max-w-[78%]",
                        message.role === "user"
                          ? "bg-slate-950 text-white"
                          : "border border-slate-200 bg-white text-slate-800"
                      ].join(" ")}
                    >
                      <span
                        className={[
                          "mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-full",
                          message.role === "user" ? "bg-white/12 text-white" : "bg-teal-50 text-teal-700"
                        ].join(" ")}
                      >
                        {message.role === "user" ? <UserRound className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
                      </span>
                      {message.role === "assistant" ? (
                        <div className="min-w-0 flex-1">
                          {message.content || message.id === streamingAssistantMessageId ? (
                            <AssistantMessageContent
                              content={message.content || "正在生成..."}
                              moduleItems={moduleItems}
                              onOpenModule={onOpenModule}
                            />
                          ) : null}
                          {message.skillCalls?.length ? <AssistantSkillPanels skillCalls={message.skillCalls} /> : null}
                        </div>
                      ) : message.content ? (
                        <div className="min-w-0 whitespace-pre-wrap break-words">{message.content}</div>
                      ) : null}
                    </div>
                  </article>
                ))}
                {error ? <ErrorNotice detail={error} /> : null}
                <div className="h-[220px] shrink-0 md:h-[240px]" aria-hidden="true" />
                <div ref={messagesEndRef} />
              </div>
            </div>
          </div>

          <div className="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-[#f8f9fb] via-[#f8f9fb] to-[#f8f9fb]/0 px-4 pb-6 pt-10 md:px-8 lg:px-10">
            <div className="pointer-events-auto mx-auto max-w-3xl">
              <ModuleShortcutRow items={moduleItems} onOpenModule={onOpenModule} compact />
              <ChatComposer
                draft={draft}
                trimmedDraft={trimmedDraft}
                isStreaming={isStreaming}
                onChange={setDraft}
                onSubmit={() => void submitMessage(draft)}
                onStop={stopStreaming}
                onKeyDown={handleKeyDown}
              />
              {isStreaming ? <StreamingStatus /> : null}
            </div>
          </div>
        </>
      )}
    </section>
  );
}
type ModuleShortcutItem = AppShellModuleGroup["items"][number];

type AssistantMessageSegment =
  | { type: "text"; text: string }
  | { type: "module"; item: ModuleShortcutItem };

type ModuleMatch = {
  index: number;
  length: number;
  item: ModuleShortcutItem;
};

type AssistantMessageContentProps = {
  content: string;
  moduleItems: ModuleShortcutItem[];
  onOpenModule: (moduleId: string) => void;
};

function AssistantMessageContent({ content, moduleItems, onOpenModule }: AssistantMessageContentProps) {
  const segments = buildAssistantMessageSegments(content, moduleItems);

  return (
    <div className="min-w-0 whitespace-pre-wrap break-words">
      {segments.map((segment, index) => {
        if (segment.type === "text") {
          return <span key={"text-" + index}>{segment.text}</span>;
        }

        return (
          <ModuleInlineButton
            key={"module-" + segment.item.id + "-" + index}
            item={segment.item}
            onClick={() => onOpenModule(segment.item.id)}
          />
        );
      })}
    </div>
  );
}

function ModuleInlineButton({ item, onClick }: { item: ModuleShortcutItem; onClick: () => void }) {
  return (
    <button
      type="button"
      className="mx-1 inline-flex translate-y-[2px] items-center gap-1.5 rounded-full border border-teal-200 bg-teal-50 px-2.5 py-1 text-xs font-semibold leading-none text-teal-800 shadow-sm transition hover:border-teal-300 hover:bg-teal-100"
      onClick={onClick}
    >
      <span className="text-[11px] font-bold">@</span>
      <span className="flex h-3.5 w-3.5 items-center justify-center">{item.icon}</span>
      <span>{item.label}</span>
    </button>
  );
}

function AssistantSkillPanels({ skillCalls }: { skillCalls: AssistantSkillCall[] }) {
  return (
    <div className="mt-3 space-y-3">
      {skillCalls.map((skillCall) => (
        <AssistantSkillPanel key={skillCall.skill_call_id} skillCall={skillCall} />
      ))}
    </div>
  );
}

function AssistantSkillPanel({ skillCall }: { skillCall: AssistantSkillCall }) {
  if (skillCall.status === "running") {
    return (
      <div className="flex items-center gap-2 rounded-2xl border border-teal-100 bg-teal-50/80 px-3 py-2 text-xs font-semibold text-teal-800">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        正在执行{skillCall.display_name || skillCall.skill_name}...
      </div>
    );
  }

  if (skillCall.status === "error") {
    return (
      <div className="rounded-2xl border border-rose-100 bg-rose-50 px-3 py-2 text-xs leading-5 text-rose-800">
        <div className="font-semibold">{skillCall.display_name || skillCall.skill_name}执行失败</div>
        <div className="mt-1">{skillCall.error || "未知错误"}</div>
      </div>
    );
  }

  if (isPredictionSkillResult(skillCall.result)) {
    return <PredictionSkillResultPanel result={skillCall.result} />;
  }

  return (
    <div className="rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 text-xs leading-5 text-slate-700">
      {skillCall.display_name || skillCall.skill_name}已完成。
    </div>
  );
}

function PredictionSkillResultPanel({ result }: { result: AssistantPredictionSkillResult }) {
  return (
    <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white text-slate-800 shadow-sm">
      <div className="flex flex-col gap-2 border-b border-slate-100 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="text-xs font-semibold text-slate-950">性质预测结果</div>
          <div className="mt-1 break-all font-mono text-[11px] text-slate-500">{result.smiles}</div>
        </div>
        <div className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] font-semibold text-slate-600">
          {result.properties.length} 项 · {result.query_time_ms.toFixed(1)} ms
        </div>
      </div>
      <div className="max-h-[320px] overflow-y-auto">
        {result.properties.map((property) => (
          <div
            key={property.name}
            className="grid gap-2 border-b border-slate-100 px-4 py-3 last:border-b-0 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center"
          >
            <div className="min-w-0">
              <div className="text-sm font-semibold text-slate-950">{property.label_zh}</div>
              <div className="mt-1 break-words text-[11px] uppercase tracking-[0.08em] text-slate-400">
                {property.name}
              </div>
            </div>
            <div className="flex items-baseline gap-2 sm:justify-end">
              <span className="font-heading text-lg font-semibold text-slate-950">
                {formatPredictionValue(property.value)}
              </span>
              <span className="text-xs font-semibold text-slate-500">{property.unit}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function isPredictionSkillResult(result: AssistantSkillCall["result"]): result is AssistantPredictionSkillResult {
  return result?.type === "predict_polymer_properties";
}

function formatPredictionValue(value: number) {
  if (!Number.isFinite(value)) {
    return "--";
  }
  return Math.abs(value) >= 1000 ? value.toExponential(2) : value.toFixed(2);
}

function buildAssistantMessageSegments(content: string, moduleItems: ModuleShortcutItem[]): AssistantMessageSegment[] {
  const cleanContent = sanitizeAssistantContent(content);
  const segments: AssistantMessageSegment[] = [];
  let cursor = 0;

  while (cursor < cleanContent.length) {
    const match = findNextModuleMatch(cleanContent, cursor, moduleItems);
    if (!match) {
      segments.push({ type: "text", text: cleanContent.slice(cursor) });
      break;
    }

    if (match.index > cursor) {
      segments.push({ type: "text", text: cleanContent.slice(cursor, match.index) });
    }
    segments.push({ type: "module", item: match.item });
    cursor = match.index + match.length;
  }

  return segments.length > 0 ? segments : [{ type: "text", text: cleanContent }];
}

function findNextModuleMatch(content: string, start: number, moduleItems: ModuleShortcutItem[]): ModuleMatch | null {
  let best: ModuleMatch | null = null;
  const markerPattern = /\[\[module:([A-Za-z0-9_-]+)\|([^\]]+)\]\]/g;
  markerPattern.lastIndex = start;
  const markerMatch = markerPattern.exec(content);

  if (markerMatch) {
    const markerItem = findModuleItem(moduleItems, markerMatch[1], markerMatch[2]);
    if (markerItem) {
      best = { index: markerMatch.index, length: markerMatch[0].length, item: markerItem };
    }
  }

  for (const item of moduleItems) {
    for (const pattern of [item.label, "**" + item.label + "**"]) {
      const index = content.indexOf(pattern, start);
      if (index === -1) {
        continue;
      }
      if (!best || index < best.index || (index === best.index && pattern.length > best.length)) {
        best = { index, length: pattern.length, item };
      }
    }
  }

  return best;
}

function sanitizeAssistantContent(content: string) {
  return content
    .split(/\r?\n/)
    .filter((line) => !/^\s*[-*]?\s*(?:路由|路径|route|Route)\s*[:：]/.test(line))
    .join("\n")
    .replace(/[（(]\s*(?:路由|路径|route|Route)\s*[:：]\s*?\/[A-Za-z0-9/_-]+?\s*[）)]/g, "")
    .replace(/\s*(?:路由|路径|route|Route)\s*[:：]\s*?\/[A-Za-z0-9/_-]+?/g, "")
    .replace(/\/[A-Za-z0-9/_-]+/g, "")
    .replace(/(^|\s)\/[A-Za-z][A-Za-z0-9/_-]*/g, (_match, prefix: string) => prefix)
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function findModuleItem(moduleItems: ModuleShortcutItem[], id?: string, title?: string) {
  if (id) {
    const byId = moduleItems.find((item) => item.id === id);
    if (byId) {
      return byId;
    }
  }

  if (title) {
    return moduleItems.find((item) => item.label === title.replace(/^@/, ""));
  }

  return undefined;
}
type ChatComposerProps = {
  className?: string;
  draft: string;
  trimmedDraft: string;
  isStreaming: boolean;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onStop: () => void;
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
};

function ChatComposer({ className, draft, trimmedDraft, isStreaming, onChange, onSubmit, onStop, onKeyDown }: ChatComposerProps) {
  return (
    <div
      data-assistant-composer="true"
      className={["rounded-[1.6rem] border border-slate-200 bg-white p-2 shadow-[0_18px_60px_rgba(15,23,42,0.10)]", className]
        .filter(Boolean)
        .join(" ")}
    >
      <Textarea
        value={draft}
        rows={2}
        className="max-h-40 min-h-[72px] resize-none border-0 bg-transparent px-3 py-3 shadow-none focus-visible:ring-0"
        placeholder="向智聚万物提问：聚合物数据、性能预测、知识检索、逆向设计..."
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="flex items-center justify-between gap-3 px-2 pb-1">
        <div className="text-xs text-slate-400">智聚万物</div>
        {isStreaming ? (
          <Button type="button" variant="outline" className="h-10 rounded-xl px-3" onClick={onStop}>
            <OctagonX className="mr-2 h-4 w-4" />
            停止生成
          </Button>
        ) : (
          <Button type="button" className="h-10 w-10 rounded-xl p-0" disabled={!trimmedDraft} onClick={onSubmit}>
            <ArrowUp className="h-4 w-4" />
          </Button>
        )}
      </div>
    </div>
  );
}

type ModuleShortcutRowProps = {
  items: AppShellModuleGroup["items"];
  onOpenModule: (moduleId: string) => void;
  compact?: boolean;
};

function ModuleShortcutRow({ items, onOpenModule, compact = false }: ModuleShortcutRowProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [scrollState, setScrollState] = useState({ canScrollLeft: false, canScrollRight: false });

  function updateScrollState() {
    const target = scrollRef.current;
    if (!target) {
      return;
    }

    const maxScrollLeft = Math.max(0, target.scrollWidth - target.clientWidth);
    setScrollState({
      canScrollLeft: target.scrollLeft > 1,
      canScrollRight: target.scrollLeft < maxScrollLeft - 1,
    });
  }

  useEffect(() => {
    updateScrollState();
    window.addEventListener("resize", updateScrollState);
    return () => window.removeEventListener("resize", updateScrollState);
  }, [items.length]);

  function handleWheel(event: WheelEvent<HTMLDivElement>) {
    const target = event.currentTarget;
    if (target.scrollWidth <= target.clientWidth) {
      return;
    }

    const scrollDelta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
    if (!scrollDelta) {
      return;
    }

    const previousScrollLeft = target.scrollLeft;
    target.scrollLeft += scrollDelta;
    updateScrollState();
    if (target.scrollLeft !== previousScrollLeft) {
      event.preventDefault();
    }
  }

  return (
    <div className={["relative", compact ? "mb-3" : "mt-5"].join(" ")}>
      <div
        ref={scrollRef}
        className="flex gap-2 overflow-x-auto scroll-smooth pb-1 pl-0 pr-12 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        aria-label="模块快捷入口"
        onScroll={updateScrollState}
        onWheel={handleWheel}
      >
        {items.map((item) => (
          <button
            key={item.id}
            type="button"
            className="inline-flex shrink-0 items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 shadow-sm transition hover:border-teal-200 hover:text-teal-700"
            onClick={() => onOpenModule(item.id)}
          >
            {item.icon}
            {item.label}
          </button>
        ))}
      </div>
      {scrollState.canScrollLeft ? (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 left-0 z-10 w-12 bg-gradient-to-r from-[#f8f9fb] via-[#f8f9fb]/95 to-transparent"
        />
      ) : null}
      {scrollState.canScrollRight ? (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 right-0 z-10 w-12 bg-gradient-to-l from-[#f8f9fb] via-[#f8f9fb]/95 to-transparent"
        />
      ) : null}
    </div>
  );
}

function StreamingStatus() {
  return (
    <div className="mt-2 flex items-center gap-2 px-2 text-xs text-slate-500">
      <Loader2 className="h-3.5 w-3.5 animate-spin" />
      正在生成回复
    </div>
  );
}

function ErrorNotice({ detail }: { detail: string }) {
  return <div className="mt-3 rounded-2xl border border-red-100 bg-red-50 px-4 py-3 text-sm text-red-700">{detail}</div>;
}
