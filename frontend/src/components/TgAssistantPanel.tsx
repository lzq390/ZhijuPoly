import {
  ArrowUp,
  BookOpen,
  Check,
  ChevronDown,
  Image,
  LoaderCircle,
  Plus,
  RefreshCcw,
  Sparkles,
  Square,
  Trash2,
  X
} from "lucide-react";
import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type {
  TgAssistantActionItem,
  TgAssistantMessageItem,
  TgAssistantProcessStage,
  TgAssistantSession
} from "../hooks/useTgAssistant";

type TgAssistantPanelProps = {
  assistant: TgAssistantSession;
  onClose: () => void;
  contextLabels: string[];
  localDiagnostic: string;
  contextualSuggestions: string[];
};

function inlineMarkdown(text: string): ReactNode[] {
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g).filter(Boolean);
  return parts.map((part, index) => {
    if (part.startsWith("`") && part.endsWith("`")) {
      return <code key={index}>{part.slice(1, -1)}</code>;
    }
    if (part.startsWith("**") && part.endsWith("**")) {
      return <strong key={index}>{part.slice(2, -2)}</strong>;
    }
    return <Fragment key={index}>{part}</Fragment>;
  });
}

function SafeMarkdown({ content }: { content: string }) {
  const lines = content.split("\n");
  const nodes: ReactNode[] = [];
  let code: string[] | null = null;
  lines.forEach((line, index) => {
    if (line.trim().startsWith("```")) {
      if (code) {
        nodes.push(<pre key={`code-${index}`}><code>{code.join("\n")}</code></pre>);
        code = null;
      } else {
        code = [];
      }
      return;
    }
    if (code) {
      code.push(line);
      return;
    }
    const heading = /^(#{1,3})\s+(.+)$/.exec(line);
    if (heading) {
      nodes.push(<strong className="tg-assistant-md-heading" key={index}>{inlineMarkdown(heading[2])}</strong>);
      return;
    }
    const bullet = /^\s*[-*]\s+(.+)$/.exec(line);
    const numbered = /^\s*(\d+)\.\s+(.+)$/.exec(line);
    if (bullet) {
      nodes.push(<div className="tg-assistant-md-list" key={index}>• <span>{inlineMarkdown(bullet[1])}</span></div>);
    } else if (numbered) {
      nodes.push(<div className="tg-assistant-md-list" key={index}>{numbered[1]}. <span>{inlineMarkdown(numbered[2])}</span></div>);
    } else if (line.trim()) {
      nodes.push(<p key={index}>{inlineMarkdown(line)}</p>);
    } else {
      nodes.push(<span className="tg-assistant-md-space" key={index} />);
    }
  });
  if (code !== null) {
    const remainingCode = code as string[];
    nodes.push(<pre key="code-final"><code>{remainingCode.join("\n")}</code></pre>);
  }
  return <div className="tg-assistant-markdown">{nodes}</div>;
}

function ActionCard({
  item,
  assistant,
  onRegenerate
}: {
  item: TgAssistantActionItem;
  assistant: TgAssistantSession;
  onRegenerate: (text: string) => void;
}) {
  const hasSearch = item.operations.some((operation) => operation.type === "run_search");
  const patch = item.operations.find((operation) => operation.type === "set_parameters");
  const structure = item.operations.find((operation) => operation.type === "set_structure");
  const parameters = patch?.type === "set_parameters" ? patch.parameters : null;
  const parameterValue = (
    key: "target_tg" | "similarity_threshold" | "candidate_size",
    next: number,
    unit = ""
  ) => {
    const previous = item.previousParameters?.[key];
    return `${previous === null || previous === undefined ? "未设置" : previous}${unit} → ${next}${unit}`;
  };
  return (
    <article className={`tg-assistant-action-card is-${item.status}`}>
      <strong>{structure ? "页面操作 · 替换画板结构" : hasSearch ? "页面操作 · 应用并搜索" : "页面操作 · 应用参数"}</strong>
      {structure?.type === "set_structure" ? (
        <dl className="tg-assistant-structure-change">
          <dt>当前 SMILES</dt><dd>{item.previousStructure || "空画板"}</dd>
          <dt>建议 SMILES</dt><dd>{structure.smiles}</dd>
        </dl>
      ) : null}
      {parameters ? (
        <dl>
          {parameters.target_tg !== undefined && parameters.target_tg !== null ? <><dt>目标 Tg</dt><dd>{parameterValue("target_tg", parameters.target_tg, " °C")}</dd></> : null}
          {parameters.similarity_threshold !== undefined ? <><dt>相似度阈值</dt><dd>{parameterValue("similarity_threshold", parameters.similarity_threshold)}</dd></> : null}
          {parameters.candidate_size !== undefined ? <><dt>候选数量</dt><dd>{parameterValue("candidate_size", parameters.candidate_size)}</dd></> : null}
        </dl>
      ) : null}
      {hasSearch ? <p>确认后将使用当前可靠画布结构运行搜索。</p> : null}
      {structure ? <p>确认后将替换当前画板；加载失败时会恢复原结构。</p> : null}
      {item.detail ? <small>{item.detail}</small> : null}
      {item.status === "pending" ? (
        <div>
          <button type="button" onClick={() => void assistant.applyAction(item.id)}>
            <Check />{structure ? "替换画板" : hasSearch ? "应用并搜索" : "应用参数"}
          </button>
          <button type="button" onClick={() => assistant.rejectAction(item.id)}>忽略</button>
        </div>
      ) : item.status === "applying" ? (
        <span className="tg-assistant-action-status"><LoaderCircle className="animate-spin" />正在应用…</span>
      ) : item.status === "expired" ? (
        <button type="button" onClick={() => onRegenerate(item.sourceText)}><RefreshCcw />重新生成</button>
      ) : (
        <span className="tg-assistant-action-status">
          {item.status === "applied" ? "已应用" : item.status === "rejected" ? "已忽略" : "执行失败"}
        </span>
      )}
    </article>
  );
}

const PROCESS_STAGE_LABELS: Record<TgAssistantProcessStage, string> = {
  capturing_canvas: "正在截取当前画板",
  routing_request: "正在识别请求",
  validating_decision: "正在校验操作",
  analyzing_images: "正在分析图片",
  composing_answer: "正在组织回答",
  writing_answer: "正在生成回答",
  transport_fallback: "兼容模式继续处理"
};

function ProcessDetails({ item }: { item: TgAssistantMessageItem }) {
  const active = item.status === "understanding" || item.status === "streaming";
  const [open, setOpen] = useState(active);
  const wasActiveRef = useRef(active);
  useEffect(() => {
    if (active) setOpen(true);
    else if (wasActiveRef.current) setOpen(false);
    wasActiveRef.current = active;
  }, [active]);
  const process = item.processing;
  if (!process) return null;
  const hasSummary = Boolean(process.intentSummary || process.answerSummary);
  return (
    <details
      className="tg-assistant-process"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span>处理过程</span>
        {process.currentStage ? <small>{PROCESS_STAGE_LABELS[process.currentStage]}</small> : null}
        <ChevronDown />
      </summary>
      <div>
        <ol>
          {process.stages.map((stage) => (
            <li key={stage} className={stage === process.currentStage ? "is-active" : ""}>
              {stage === process.currentStage && active ? <LoaderCircle className="animate-spin" /> : <Check />}
              {PROCESS_STAGE_LABELS[stage]}
            </li>
          ))}
        </ol>
        {hasSummary ? (
          <section className="tg-assistant-reasoning-summary">
            <strong>思考过程（摘要）</strong>
            {process.intentSummary ? (
              <div><small>请求理解</small><SafeMarkdown content={process.intentSummary} /></div>
            ) : null}
            {process.answerSummary ? (
              <div><small>回答组织</small><SafeMarkdown content={process.answerSummary} /></div>
            ) : null}
          </section>
        ) : null}
        {process.warning ? <p className="tg-assistant-process-warning">{process.warning}</p> : null}
      </div>
    </details>
  );
}

function MessageBubble({
  item,
  attachContext,
  assistant
}: {
  item: TgAssistantMessageItem;
  attachContext: boolean;
  assistant: TgAssistantSession;
}) {
  const imagePreviewUrl = item.image ? assistant.getImagePreviewUrl(item.id) : null;
  const imagePreviewRestoring = item.image ? assistant.isImagePreviewRestoring(item.id) : false;
  const [imagePreviewFailed, setImagePreviewFailed] = useState(false);
  useEffect(() => setImagePreviewFailed(false), [imagePreviewUrl]);
  const showImagePreview = Boolean(imagePreviewUrl) && !imagePreviewFailed;
  const unavailableLabel = imagePreviewRestoring ? "正在恢复图片预览" : "图片预览已失效";
  return (
    <article className={`tg-assistant-message is-${item.role}`}>
      <span>{item.role === "user" ? "你" : "AI"}</span>
      <div>
        {item.image ? (
          <figure className="tg-assistant-message-image">
            {showImagePreview ? (
              <img
                src={imagePreviewUrl ?? undefined}
                alt={`上传的图片：${item.image.name}`}
                onError={() => setImagePreviewFailed(true)}
              />
            ) : (
              <span className="tg-assistant-message-image-unavailable" role="img" aria-label={unavailableLabel}>
                {imagePreviewRestoring ? <LoaderCircle className="animate-spin" /> : <Image />}
                <em>{unavailableLabel}</em>
              </span>
            )}
            <figcaption title={item.image.name}>{item.image.name}</figcaption>
          </figure>
        ) : null}
        {item.content ? <SafeMarkdown content={item.content} /> : null}
        {item.status === "understanding" ? (
          <small className="tg-assistant-thinking"><LoaderCircle className="animate-spin" />正在思考中</small>
        ) : null}
        {item.status === "streaming" && !item.content ? <small>正在生成…</small> : null}
        {item.role === "assistant" ? <ProcessDetails item={item} /> : null}
        {item.status === "stopped" ? <small>已停止生成，已保留当前内容。</small> : null}
        {item.error ? (
          <div className="tg-assistant-message-error">
            <span>{item.error.message}</span>
            {item.error.retryable ? (
              <button type="button" onClick={() => void assistant.retry(item.id, attachContext)}>
                <RefreshCcw />重试
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </article>
  );
}

export function TgAssistantPanel({
  assistant,
  onClose,
  contextLabels,
  localDiagnostic,
  contextualSuggestions
}: TgAssistantPanelProps) {
  const [input, setInput] = useState("");
  const [attachContext, setAttachContext] = useState(assistant.consent === "granted");
  const [consentOpen, setConsentOpen] = useState(false);
  const [pendingSend, setPendingSend] = useState(false);
  const [guideOpen, setGuideOpen] = useState(true);
  const [navigationNotice, setNavigationNotice] = useState<string | null>(null);
  const [selectedImage, setSelectedImage] = useState<{ file: File; url: string } | null>(null);
  const [imageError, setImageError] = useState<string | null>(null);
  const composingRef = useRef(false);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const imageInputRef = useRef<HTMLInputElement | null>(null);
  const nearBottomRef = useRef(true);
  const available = assistant.status?.enabled === true && assistant.status.configured === true;
  const hasConversationContent = assistant.items.some((item) => item.kind !== "divider");

  useEffect(() => {
    void assistant.loadMetadata();
  }, [assistant.loadMetadata]);

  useEffect(() => {
    if (assistant.consent !== "granted") setAttachContext(false);
  }, [assistant.consent]);

  useEffect(() => {
    const body = bodyRef.current;
    if (body && nearBottomRef.current) body.scrollTop = body.scrollHeight;
  }, [assistant.items]);

  useEffect(() => {
    const url = selectedImage?.url;
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
  }, [selectedImage?.url]);

  const suggestions = useMemo(() => {
    if (!available) return [];
    if (!attachContext) {
      return [
        "怎样从目标 Tg 反推聚合物骨架设计？",
        "如何权衡 Tg 接近度与结构相似度？",
        "候选结果应按哪些指标排序验证？"
      ];
    }
    return contextualSuggestions.slice(0, 3);
  }, [attachContext, available, contextualSuggestions]);

  function dispatchSend(withContext: boolean, restoreContext = assistant.consent === "granted") {
    const normalized = input.trim() || (selectedImage ? "请分析这张图片" : "");
    if (!normalized || normalized.length > 8000 || assistant.isStreaming || !available) return;
    if (selectedImage) void assistant.send(normalized, withContext, selectedImage.file);
    else void assistant.send(normalized, withContext);
    setInput("");
    setSelectedImage(null);
    setImageError(null);
    if (imageInputRef.current) imageInputRef.current.value = "";
    setAttachContext(restoreContext);
    window.requestAnimationFrame(() => inputRef.current?.focus());
  }

  function send(withContext = attachContext) {
    const normalized = input.trim() || (selectedImage ? "请分析这张图片" : "");
    if (!normalized || normalized.length > 8000 || assistant.isStreaming || !available) return;
    if (withContext && assistant.consent !== "granted") {
      setPendingSend(true);
      setConsentOpen(true);
      return;
    }
    dispatchSend(withContext);
  }

  function selectImage(file: File | undefined) {
    if (!file) return;
    const capability = assistant.status?.image;
    const acceptedTypes = capability?.accepted_mime_types ?? ["image/png", "image/jpeg", "image/webp"];
    const maxBytes = capability?.max_bytes ?? 5 * 1024 * 1024;
    if (!acceptedTypes.includes(file.type as "image/png" | "image/jpeg" | "image/webp")) {
      setImageError("仅支持 PNG、JPEG 或 WebP 图片。");
      return;
    }
    if (file.size > maxBytes) {
      setImageError(`图片不能超过 ${Math.round(maxBytes / 1024 / 1024)} MiB。`);
      return;
    }
    try {
      setSelectedImage({ file, url: URL.createObjectURL(file) });
      setImageError(null);
    } catch {
      setImageError("图片预览创建失败，请重新选择。");
    }
  }

  function decideConsent(granted: boolean) {
    assistant.setConsent(granted ? "granted" : "denied");
    assistant.addDivider(granted ? "页面上下文已开启" : "本次未附带页面上下文");
    setAttachContext(granted);
    setConsentOpen(false);
    if (pendingSend) {
      setPendingSend(false);
      dispatchSend(granted, granted);
    }
  }

  return (
    <>
      <header className="tg-assistant-header">
        <div>
          <span className="tg-assistant-mark"><Sparkles /></span>
          <span>
            <h2 id="tg-assistant-title">Tg AI 助手</h2>
            <small>{available ? "AI 服务已就绪" : "使用指南与本地帮助"}</small>
          </span>
        </div>
        <span className="tg-assistant-header-actions">
          <button
            type="button"
            aria-label="清空当前对话（不可恢复）"
            title="清空当前对话（不可恢复）"
            onClick={() => {
              assistant.newConversation();
              setInput("");
              setSelectedImage(null);
              setImageError(null);
              if (imageInputRef.current) imageInputRef.current.value = "";
            }}
          >
            <Trash2 />
          </button>
          <button type="button" aria-label="收起 AI 助手" onClick={onClose}><X /></button>
        </span>
      </header>

      <div
        ref={bodyRef}
        className="tg-assistant-body"
        role="log"
        aria-live="polite"
        onScroll={(event) => {
          const target = event.currentTarget;
          nearBottomRef.current = target.scrollHeight - target.scrollTop - target.clientHeight < 80;
        }}
      >
        <div className="tg-assistant-context" aria-label="当前 AI 上下文">
          {contextLabels.map((label, index) => <span key={label} className={index === 0 ? "is-ready" : ""}><i />{label}</span>)}
        </div>

        {!available && assistant.items.length > 0 ? (
          <p className="tg-assistant-local-diagnostic">{localDiagnostic}</p>
        ) : null}

        <section className="tg-assistant-guide">
          <button type="button" onClick={() => setGuideOpen((current) => !current)} aria-expanded={guideOpen}>
            <BookOpen />Tg 逆向设计使用说明<ChevronDown />
          </button>
          {guideOpen ? (
            <div>
              {assistant.metadataLoading ? <small>正在加载指南…</small> : null}
              {assistant.guide?.sections.map((section) => (
                <details key={section.id} open={section.id === "workflow"}>
                  <summary>{section.title}</summary>
                  <ul>{section.content.map((text) => <li key={text}>{text}</li>)}</ul>
                </details>
              ))}
              {!assistant.guide && assistant.metadataError ? <p>{assistant.metadataError}</p> : null}
            </div>
          ) : null}
        </section>

        {!hasConversationContent ? (
          <div className="tg-assistant-welcome">
            <span className="tg-assistant-orb"><Sparkles /></span>
            <h3><em>你好，</em><br />今天想一起研究什么？</h3>
            <p>{localDiagnostic}</p>
            <div className="tg-assistant-suggestions">
              {suggestions.map((suggestion) => (
                <button key={suggestion} type="button" onClick={() => {
                  setInput(suggestion);
                  window.requestAnimationFrame(() => inputRef.current?.focus());
                }}>
                  <Sparkles />{suggestion}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="tg-assistant-thread">
            {assistant.items.map((item) => {
              if (item.kind === "message") return <MessageBubble key={item.id} item={item} attachContext={attachContext} assistant={assistant} />;
              if (item.kind === "divider") return <div className="tg-assistant-divider" key={item.id}>{item.text}</div>;
              if (item.kind === "action") return <ActionCard key={item.id} item={item} assistant={assistant} onRegenerate={(text) => {
                setInput(text);
                window.requestAnimationFrame(() => inputRef.current?.focus());
              }} />;
              return (
                <button className="tg-assistant-navigation" key={item.id} type="button" onClick={() => {
                  if (!assistant.runNavigation(item)) setNavigationNotice("页面状态已变化，请重新提出打开面板的请求。");
                }}>
                  {item.target === "parameters" ? "打开搜索参数" : "打开候选结果"}
                </button>
              );
            })}
            {navigationNotice ? <small className="tg-assistant-inline-notice">{navigationNotice}</small> : null}
          </div>
        )}
      </div>

      <footer className="tg-assistant-composer">
        {assistant.storageWarning ? <p className="tg-assistant-storage-warning">{assistant.storageWarning}</p> : null}
        {!available && assistant.status ? (
          <p className="tg-assistant-unavailable">AI 助手暂不可用，请稍后重试。</p>
        ) : null}
        {!available && assistant.metadataError ? (
          <p className="tg-assistant-unavailable">{assistant.metadataError}</p>
        ) : null}
        <div className="tg-assistant-context-toggle">
          <label>
            <input
              type="checkbox"
              checked={attachContext}
              disabled={!available}
              onChange={(event) => {
                const checked = event.currentTarget.checked;
                if (checked && assistant.consent !== "granted") {
                  setConsentOpen(true);
                } else {
                  setAttachContext(checked);
                  assistant.addDivider(checked ? "页面上下文已开启" : "本轮不附带新的页面上下文");
                }
              }}
            />
            附带当前页面
          </label>
          {assistant.consent === "granted" ? (
            <button type="button" onClick={() => {
              assistant.setConsent("unknown");
              assistant.addDivider("页面上下文授权已清除");
              setAttachContext(false);
            }}>清除授权</button>
          ) : null}
        </div>
        <div className="tg-assistant-input-shell">
          <input
            ref={imageInputRef}
            className="tg-visually-hidden"
            type="file"
            accept="image/png,image/jpeg,image/webp"
            aria-label="选择供 AI 分析的图片"
            onClick={(event) => { event.currentTarget.value = ""; }}
            onChange={(event) => selectImage(event.currentTarget.files?.[0])}
          />
          {selectedImage ? (
            <div className="tg-assistant-image-preview">
              <img src={selectedImage.url} alt="待发送图片预览" />
              <span title={selectedImage.file.name}>{selectedImage.file.name}</span>
              <button
                type="button"
                aria-label="删除待发送图片"
                onClick={() => {
                  setSelectedImage(null);
                  if (imageInputRef.current) imageInputRef.current.value = "";
                }}
              ><X /></button>
            </div>
          ) : null}
          <textarea
            ref={inputRef}
            rows={2}
            value={input}
            maxLength={8000}
            disabled={!available}
            onCompositionStart={() => { composingRef.current = true; }}
            onCompositionEnd={() => { composingRef.current = false; }}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !composingRef.current) {
                event.preventDefault();
                send();
              }
            }}
            onChange={(event) => setInput(event.currentTarget.value)}
            placeholder="向 AI 助手提问，或描述新的结构约束…"
            aria-label="发送给 AI 助手的消息"
          />
          <div className="tg-assistant-input-actions">
            <button
              type="button"
              className="tg-assistant-add-image"
              aria-label="上传图片给 AI 分析"
              title="上传图片给 AI 分析"
              disabled={!available || assistant.isStreaming}
              onClick={() => imageInputRef.current?.click()}
            ><Plus /></button>
            {assistant.isStreaming ? (
              <button type="button" aria-label="停止生成" onClick={assistant.stop}><Square /></button>
            ) : (
              <button type="button" aria-label="发送消息" onClick={() => send()} disabled={(!input.trim() && !selectedImage) || !available}>
                <ArrowUp />
              </button>
            )}
          </div>
        </div>
        {imageError ? <p className="tg-assistant-image-error" role="alert">{imageError}</p> : null}
      </footer>

      {consentOpen ? (
        <div className="tg-assistant-consent" role="dialog" aria-modal="true" aria-labelledby="tg-consent-title">
          <div>
            <strong id="tg-consent-title">是否附带当前页面？</strong>
            <p>允许 AI 读取发送时的当前结构、搜索参数、进度和当前页候选，以提供针对性帮助。每次发送时只读取一次。</p>
            <button type="button" onClick={() => decideConsent(true)}>同意并附带</button>
            <button type="button" onClick={() => decideConsent(false)}>仅发送问题</button>
          </div>
        </div>
      ) : null}
    </>
  );
}
