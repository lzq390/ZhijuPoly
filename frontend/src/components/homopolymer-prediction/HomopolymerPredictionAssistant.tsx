import { ArrowUp, Sparkles } from "lucide-react";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";

export type PredictionAssistantMessage = {
  id: number;
  role: "user" | "assistant";
  content: string;
};

export type PredictionAssistantContext = {
  /** Display SMILES of the structure on the canvas, or null when empty. */
  structure: string | null;
  /** Number of runs in this session. */
  runCount: number;
  /** Property count of the displayed result, or null before a result. */
  resultCount: number | null;
  /** Short label of the run used for comparison, if any. */
  comparisonLabel: string | null;
};

type HomopolymerPredictionAssistantProps = {
  context: PredictionAssistantContext;
  messages: readonly PredictionAssistantMessage[];
  attachContext: boolean;
  onAttachContextChange: (attach: boolean) => void;
  onSend: (text: string) => void;
  onClear: () => void;
};

function suggestionsFor(context: PredictionAssistantContext) {
  if (!context.structure) {
    return ["怎样画重复单元的连接点？", "这个页面能预测哪些性质？", "SMILES 里的 * 是什么意思？"];
  }
  if (context.resultCount === null) {
    return ["这个结构适合预测哪些性质？", "预测前需要检查结构的哪些地方？", "运行一次大概需要多久？"];
  }
  return context.runCount > 1
    ? ["和上次相比变化最大的是哪几项？", "解释这次的预测结果", "Tg 偏低时结构上可以怎么调整？"]
    : ["解释这次的预测结果", "这些数值在已知聚合物里算高还是低？", "Tg 偏低时结构上可以怎么调整？"];
}

/**
 * AI assistant for homopolymer prediction. Interface only: questions stay on this page and
 * the assistant answers with a fixed preview reply until a model service is connected.
 */
export function HomopolymerPredictionAssistant({
  context,
  messages,
  attachContext,
  onAttachContextChange,
  onSend,
  onClear
}: HomopolymerPredictionAssistantProps) {
  const [draft, setDraft] = useState("");
  const composingRef = useRef(false);
  const logRef = useRef<HTMLDivElement | null>(null);
  const attachId = useId();
  const suggestions = suggestionsFor(context);

  useEffect(() => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [messages.length]);

  function send(text: string) {
    const value = text.trim();
    if (!value) return;
    onSend(value);
    setDraft("");
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    send(draft);
  }

  const chips = [
    context.structure ? { label: "结构", value: context.structure, mono: true } : { label: "尚未添加结构", value: null, mono: false },
    context.resultCount !== null ? { label: `${context.resultCount} 项预测结果`, value: null, mono: false } : null,
    context.comparisonLabel ? { label: "对比", value: context.comparisonLabel, mono: true } : null
  ].filter(Boolean) as { label: string; value: string | null; mono: boolean }[];

  return (
    <div className="np-hp-assistant">
      <div className="np-hp-assistant__context" aria-label="AI 可读取的页面内容">
        {chips.map((chip) => (
          <span key={chip.label} className={attachContext ? "is-attached" : undefined} title={chip.value ?? undefined}>
            {chip.label}
            {chip.value ? <code>{chip.value}</code> : null}
          </span>
        ))}
      </div>

      <div ref={logRef} className="np-hp-assistant__log" role="log" aria-live="polite" aria-label="对话">
        <p className="np-hp-assistant__notice">界面预览：AI 助手尚未接入，问题只保留在本页面，不会发送到服务。</p>
        {messages.length === 0 ? (
          <div className="np-hp-assistant__welcome">
            <span className="np-hp-assistant__mark" aria-hidden="true"><Sparkles /></span>
            <strong>我可以帮你解读预测结果</strong>
            <p>结合当前结构和预测值，解释数值、比较两次运行，或给出结构调整的方向。</p>
            <ul aria-label="可以这样问">
              {suggestions.map((suggestion) => (
                <li key={suggestion}>
                  <button type="button" onClick={() => send(suggestion)}>{suggestion}</button>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <ol className="np-hp-assistant__messages">
            {messages.map((message) => (
              <li key={message.id} className={`is-${message.role}`}>
                {message.role === "assistant" ? <span className="np-hp-assistant__avatar" aria-hidden="true"><Sparkles /></span> : null}
                <div>
                  <span className="np-hp-assistant__sr">{message.role === "user" ? "你：" : "AI 助手："}</span>
                  {message.content}
                </div>
              </li>
            ))}
          </ol>
        )}
      </div>

      <form className="np-hp-assistant__composer" onSubmit={submit}>
        <div className="np-hp-assistant__input">
          <textarea
            rows={2}
            value={draft}
            maxLength={4000}
            placeholder="问一问这次的预测结果…"
            aria-label="发送给 AI 助手的消息"
            onChange={(event) => setDraft(event.currentTarget.value)}
            onCompositionStart={() => { composingRef.current = true; }}
            onCompositionEnd={() => { composingRef.current = false; }}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !composingRef.current && !event.nativeEvent.isComposing) {
                event.preventDefault();
                send(draft);
              }
            }}
          />
          <button type="submit" className="np-hp-assistant__send" aria-label="发送消息" disabled={!draft.trim()}>
            <ArrowUp aria-hidden="true" />
          </button>
        </div>
        <div className="np-hp-assistant__options">
          <label htmlFor={attachId}>
            <input
              id={attachId}
              type="checkbox"
              checked={attachContext}
              onChange={(event) => onAttachContextChange(event.currentTarget.checked)}
            />
            附带当前结构和结果
          </label>
          {messages.length > 0 ? (
            <button type="button" className="np-hp-link" onClick={onClear}>清空对话</button>
          ) : (
            <span>Enter 发送，Shift + Enter 换行</span>
          )}
        </div>
      </form>
    </div>
  );
}
