import { LoaderCircle, Search, SlidersHorizontal, Sparkles, X } from "lucide-react";
import type { RefObject } from "react";
import { useMotionPresence } from "../../hooks/useMotionPresence";
import type { TgAssistantSession } from "../../hooks/useTgAssistant";
import type { ReverseDesignTgRequest } from "../../types";
import { TgAssistantPanel } from "../TgAssistantPanel";

export type ReverseDesignOpenPanel = "parameters" | "assistant" | null;

type ReverseDesignUtilityPanelsProps = {
  openPanel: ReverseDesignOpenPanel;
  parameterPanelRef: RefObject<HTMLElement | null>;
  assistantPanelRef: RefObject<HTMLElement | null>;
  request: ReverseDesignTgRequest;
  parameterStatus: string;
  parameterHasError: boolean;
  searching: boolean;
  canSearch: boolean;
  assistant: TgAssistantSession;
  assistantContextLabels: string[];
  assistantLocalDiagnostic: string;
  assistantSuggestions: string[];
  onClose: (restoreFocus?: boolean) => void;
  onRequestChange: (partial: Partial<ReverseDesignTgRequest>) => void;
  onSearch: () => void;
};

function numberValue(value: number) {
  return Number.isNaN(value) ? "" : value;
}

function parseNumber(value: string) {
  return value === "" ? Number.NaN : Number(value);
}

export function ReverseDesignUtilityPanels({
  openPanel,
  parameterPanelRef,
  assistantPanelRef,
  request,
  parameterStatus,
  parameterHasError,
  searching,
  canSearch,
  assistant,
  assistantContextLabels,
  assistantLocalDiagnostic,
  assistantSuggestions,
  onClose,
  onRequestChange,
  onSearch
}: ReverseDesignUtilityPanelsProps) {
  const parameterPresence = useMotionPresence(openPanel === "parameters", { elementRef: parameterPanelRef });
  const assistantPresence = useMotionPresence(openPanel === "assistant", { elementRef: assistantPanelRef });
  return (
    <div className={`np-sw-utility-layer${parameterPresence.present || assistantPresence.present ? " is-open" : ""}`} aria-hidden={!openPanel}>
      <button
        type="button"
        className="np-sw-utility-backdrop"
        aria-label="关闭 Tg 逆向设计浮层"
        tabIndex={openPanel ? 0 : -1}
        onClick={() => onClose(false)}
      />

      <section
        ref={parameterPanelRef}
        {...parameterPresence.motionProps}
        id="tg-parameter-panel"
        className={`np-sw-popover np-sw-popover--modules np-tg-parameters${openPanel === "parameters" ? " is-open" : ""}`}
        role="dialog"
        aria-modal="false"
        aria-labelledby="tg-parameter-title"
        aria-hidden={openPanel !== "parameters"}
        inert={openPanel !== "parameters"}
      >
        <header className="np-sw-popover__header">
          <div>
            <span className="np-sw-popover__mark"><SlidersHorizontal aria-hidden="true" /></span>
            <span>
              <h2 id="tg-parameter-title">Tg 搜索参数</h2>
              <small>按目标温度与结构相似度检索 PI 候选</small>
            </span>
          </div>
          <button
            type="button"
            className="np-sw-icon-button"
            aria-label="收起搜索参数"
            onClick={() => onClose()}
          >
            <X aria-hidden="true" />
          </button>
        </header>

        <form
          className="np-tg-parameters__form"
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            onSearch();
          }}
        >
          <div className="np-tg-parameter-grid">
            <label className="np-tg-field">
              <span>目标 Tg <small>核心条件</small></span>
              <span className="np-tg-input-shell">
                <input
                  type="number"
                  step="0.1"
                  value={request.target_tg ?? ""}
                  onChange={(event) => onRequestChange({
                    target_tg: event.currentTarget.value === "" ? null : Number(event.currentTarget.value)
                  })}
                />
                <small>°C</small>
              </span>
            </label>

            <label className="np-tg-field">
              <span>相似度阈值 <small>0–1</small></span>
              <span className="np-tg-input-shell">
                <input
                  type="number"
                  min="0"
                  max="1"
                  step="0.01"
                  value={numberValue(request.similarity_threshold)}
                  onChange={(event) => onRequestChange({
                    similarity_threshold: parseNumber(event.currentTarget.value)
                  })}
                />
              </span>
            </label>

            <label className="np-tg-field">
              <span>候选数量 <small>1–200</small></span>
              <span className="np-tg-input-shell">
                <input
                  type="number"
                  min="1"
                  max="200"
                  step="1"
                  value={numberValue(request.candidate_size)}
                  onChange={(event) => onRequestChange({
                    candidate_size: parseNumber(event.currentTarget.value)
                  })}
                />
                <small>个</small>
              </span>
            </label>
          </div>

          <div
            className={`np-tg-parameter-status${parameterHasError ? " is-error" : ""}`}
            role="status"
            aria-live="polite"
          >
            {parameterStatus}
          </div>

          <footer className="np-tg-parameters__footer">
            <span>搜索会使用当前已同步的画板结构</span>
            <button type="submit" className="np-sw-primary-button" disabled={!canSearch} aria-busy={searching}>
              {searching ? <LoaderCircle className="np-sw-spin" aria-hidden="true" /> : <Search aria-hidden="true" />}
              {searching ? "搜索中" : "运行搜索"}
            </button>
          </footer>
        </form>
      </section>

      <section
        ref={assistantPanelRef}
        {...assistantPresence.motionProps}
        id="tg-assistant-panel"
        className={`np-sw-popover np-sw-popover--assistant np-tg-assistant-panel${openPanel === "assistant" ? " is-open" : ""}`}
        role="dialog"
        aria-modal="false"
        aria-labelledby="tg-assistant-title"
        aria-hidden={openPanel !== "assistant"}
        inert={openPanel !== "assistant"}
      >
        <TgAssistantPanel
          assistant={assistant}
          onClose={() => onClose()}
          contextLabels={assistantContextLabels}
          localDiagnostic={assistantLocalDiagnostic}
          contextualSuggestions={assistantSuggestions}
        />
      </section>
    </div>
  );
}
