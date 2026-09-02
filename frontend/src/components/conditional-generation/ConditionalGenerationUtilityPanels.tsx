import {
  ArrowUp,
  ChevronDown,
  LoaderCircle,
  Plus,
  RefreshCcw,
  Search,
  SlidersHorizontal,
  Sparkles,
  X
} from "lucide-react";
import type { RefObject } from "react";
import type { ConditionalGenerationTgRequest } from "../../types";

export type ConditionalGenerationOpenPanel = "parameters" | "assistant" | null;

type ConditionalGenerationUtilityPanelsProps = {
  openPanel: ConditionalGenerationOpenPanel;
  parameterPanelRef: RefObject<HTMLElement | null>;
  assistantPanelRef: RefObject<HTMLElement | null>;
  request: ConditionalGenerationTgRequest;
  advancedOpen: boolean;
  parameterStatus: string;
  parameterHasError: boolean;
  serviceNeedsRefresh: boolean;
  isStatusLoading: boolean;
  submitting: boolean;
  canSubmit: boolean;
  structureSmiles: string;
  resultStatus: string;
  assistantInput: string;
  assistantNotice: string | null;
  onClose: (restoreFocus?: boolean) => void;
  onAdvancedOpenChange: (open: boolean) => void;
  onRequestChange: (partial: Partial<ConditionalGenerationTgRequest>) => void;
  onRefreshStatus: () => void;
  onSubmit: () => void;
  onAssistantInputChange: (value: string) => void;
  onAssistantNew: () => void;
  onAssistantSend: () => void;
};

function numberValue(value: number) {
  return Number.isNaN(value) ? "" : value;
}

function parseNumber(value: string) {
  return value === "" ? Number.NaN : Number(value);
}

export function ConditionalGenerationUtilityPanels({
  openPanel,
  parameterPanelRef,
  assistantPanelRef,
  request,
  advancedOpen,
  parameterStatus,
  parameterHasError,
  serviceNeedsRefresh,
  isStatusLoading,
  submitting,
  canSubmit,
  structureSmiles,
  resultStatus,
  assistantInput,
  assistantNotice,
  onClose,
  onAdvancedOpenChange,
  onRequestChange,
  onRefreshStatus,
  onSubmit,
  onAssistantInputChange,
  onAssistantNew,
  onAssistantSend
}: ConditionalGenerationUtilityPanelsProps) {
  return (
    <div className={`np-sw-utility-layer${openPanel ? " is-open" : ""}`} aria-hidden={!openPanel}>
      <button
        type="button"
        className="np-sw-utility-backdrop"
        aria-label="关闭条件生成浮层"
        tabIndex={openPanel ? 0 : -1}
        onClick={() => onClose(false)}
      />

      <section
        ref={parameterPanelRef}
        id="cg-parameter-panel"
        className={`np-sw-popover np-sw-popover--modules np-cg-parameters${openPanel === "parameters" ? " is-open" : ""}`}
        role="dialog"
        aria-modal="false"
        aria-labelledby="cg-parameter-title"
        aria-hidden={openPanel !== "parameters"}
        inert={openPanel !== "parameters"}
      >
        <header className="np-sw-popover__header">
          <div>
            <span className="np-sw-popover__mark"><SlidersHorizontal aria-hidden="true" /></span>
            <span>
              <h2 id="cg-parameter-title">条件生成参数</h2>
              <small>以当前种子结构为基准设置相对 Tg 条件</small>
            </span>
          </div>
          <button
            type="button"
            className="np-sw-icon-button"
            aria-label="收起生成参数"
            onClick={() => onClose()}
          >
            <X aria-hidden="true" />
          </button>
        </header>

        <form
          className="np-cg-parameters__form"
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            onSubmit();
          }}
        >
          <div className="np-cg-parameter-grid">
            <label className="np-cg-field">
              <span>相对 Tg 变化 <small>核心条件</small></span>
              <span className="np-cg-input-shell">
                <input
                  type="number"
                  step="0.1"
                  value={numberValue(request.delta_tg)}
                  onChange={(event) => onRequestChange({ delta_tg: parseNumber(event.currentTarget.value) })}
                />
                <small>°C</small>
              </span>
            </label>
            <label className="np-cg-field">
              <span>候选数量 <small>1–50</small></span>
              <span className="np-cg-input-shell">
                <input
                  type="number"
                  min="1"
                  max="50"
                  step="1"
                  value={numberValue(request.candidate_count)}
                  onChange={(event) => onRequestChange({ candidate_count: parseNumber(event.currentTarget.value) })}
                />
                <small>个</small>
              </span>
            </label>

            <div className="np-cg-advanced">
              <button
                type="button"
                className="np-cg-advanced__toggle"
                aria-label="高级采样"
                aria-expanded={advancedOpen}
                aria-controls="cg-advanced-fields"
                onClick={() => onAdvancedOpenChange(!advancedOpen)}
              >
                <span>
                  <strong>高级采样</strong>
                  <small>控制候选池宽度与采样随机性</small>
                </span>
                <ChevronDown aria-hidden="true" />
              </button>
              <div id="cg-advanced-fields" className="np-cg-advanced__fields" hidden={!advancedOpen}>
                <label className="np-cg-field">
                  <span>Top-K <small>1–20</small></span>
                  <span className="np-cg-input-shell">
                    <input
                      type="number"
                      min="1"
                      max="20"
                      step="1"
                      value={numberValue(request.top_k)}
                      onChange={(event) => onRequestChange({ top_k: parseNumber(event.currentTarget.value) })}
                    />
                  </span>
                </label>
                <label className="np-cg-field">
                  <span>Temperature <small>0.1–2.0</small></span>
                  <span className="np-cg-input-shell">
                    <input
                      type="number"
                      min="0.1"
                      max="2"
                      step="0.05"
                      value={numberValue(request.temperature)}
                      onChange={(event) => onRequestChange({ temperature: parseNumber(event.currentTarget.value) })}
                    />
                  </span>
                </label>
              </div>
            </div>
          </div>

          <div
            className={`np-cg-parameter-status${parameterHasError ? " is-error" : ""}`}
            role="status"
            aria-live="polite"
          >
            {parameterStatus}
          </div>

          <footer className="np-cg-parameters__footer">
            {serviceNeedsRefresh ? (
              <button
                type="button"
                className="np-sw-secondary-button"
                onClick={onRefreshStatus}
                disabled={isStatusLoading}
              >
                <RefreshCcw className={isStatusLoading ? "np-sw-spin" : ""} aria-hidden="true" />
                重新检查服务
              </button>
            ) : <span />}
            <button type="submit" className="np-sw-primary-button" disabled={!canSubmit}>
              {submitting ? <LoaderCircle className="np-sw-spin" /> : <Search aria-hidden="true" />}
              {submitting ? "生成中" : "运行生成"}
            </button>
          </footer>
        </form>
      </section>

      <section
        ref={assistantPanelRef}
        id="cg-assistant-panel"
        className={`np-sw-popover np-sw-popover--assistant${openPanel === "assistant" ? " is-open" : ""}`}
        role="dialog"
        aria-modal="false"
        aria-labelledby="cg-assistant-title"
        aria-hidden={openPanel !== "assistant"}
        inert={openPanel !== "assistant"}
      >
        <header className="np-sw-popover__header">
          <div>
            <span className="np-sw-popover__mark"><Sparkles aria-hidden="true" /></span>
            <span>
              <h2 id="cg-assistant-title">条件生成 AI 助手</h2>
              <small>科研上下文预览</small>
            </span>
          </div>
          <span className="np-sw-popover__actions">
            <button
              type="button"
              className="np-sw-icon-button"
              aria-label="新建对话"
              title="新建对话"
              onClick={onAssistantNew}
            >
              <Plus aria-hidden="true" />
            </button>
            <button
              type="button"
              className="np-sw-icon-button"
              aria-label="收起 AI 助手"
              onClick={() => onClose()}
            >
              <X aria-hidden="true" />
            </button>
          </span>
        </header>

        <div className="np-sw-assistant-body">
          <div className="np-sw-assistant-context" aria-label="当前 AI 上下文">
            <span className={structureSmiles.trim() ? "is-ready" : ""}>
              {structureSmiles.trim() ? "共享结构已同步" : "暂无共享结构"}
            </span>
            <span>{`ΔTg ${Number.isFinite(request.delta_tg) ? request.delta_tg : "—"} °C`}</span>
            <span>{resultStatus}</span>
          </div>
          <div className="np-sw-assistant-welcome">
            <span><Sparkles aria-hidden="true" /></span>
            <h3>你好，今天想一起研究什么？</h3>
            <p>我会结合当前种子结构、相对 Tg 条件和候选结构辅助分析。</p>
          </div>
          <div className="np-sw-assistant-suggestions">
            {[
              "解释当前种子结构的生成空间",
              "建议更合适的采样参数",
              "比较候选结构的关键差异"
            ].map((suggestion) => (
              <button key={suggestion} type="button" onClick={() => onAssistantInputChange(suggestion)}>
                <Sparkles aria-hidden="true" /> {suggestion}
              </button>
            ))}
          </div>
        </div>

        <footer className="np-sw-assistant-composer">
          <textarea
            rows={3}
            value={assistantInput}
            onChange={(event) => onAssistantInputChange(event.currentTarget.value)}
            placeholder="向 AI 助手提问，或描述新的结构约束…"
            aria-label="发送给 AI 助手的消息"
          />
          <div>
            <small role="status">{assistantNotice || "界面设计预留 · 当前不会向 AI 模型发送数据"}</small>
            <button type="button" aria-label="发送消息" onClick={onAssistantSend} disabled={!assistantInput.trim()}>
              <ArrowUp aria-hidden="true" />
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}
