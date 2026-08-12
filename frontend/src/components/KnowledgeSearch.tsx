import { Database, FileText, Globe2 } from "lucide-react";
import { useState, type KeyboardEvent } from "react";
import "../styles/knowledge-retrieval.css";
import { LocalKnowledgePanel } from "./knowledge-search/LocalKnowledgePanel";
import { OnlineKnowledgeSearchPanel } from "./online-knowledge/OnlineKnowledgeSearchPanel";
import { PdfSimilarityDemoPanel } from "./PdfSimilarityDemoPanel";

type KnowledgeSearchProps = {
  onBackHome: () => void;
  initialQuery?: string;
  initialTerms?: string[];
};

type KnowledgeMode = "local" | "online" | "pdf";

const MODES: Array<{
  id: KnowledgeMode;
  label: string;
  icon: typeof Database;
  demo?: boolean;
}> = [
  { id: "local", label: "本地知识库", icon: Database },
  { id: "online", label: "在线文献", icon: Globe2 },
  { id: "pdf", label: "PDF 相似度", icon: FileText, demo: true }
];

export function KnowledgeSearch({ initialQuery = "", initialTerms = [] }: KnowledgeSearchProps) {
  const [mode, setMode] = useState<KnowledgeMode>("local");
  const [visitedModes, setVisitedModes] = useState<Set<KnowledgeMode>>(() => new Set(["local"]));

  function activateMode(nextMode: KnowledgeMode) {
    setMode(nextMode);
    setVisitedModes((current) => {
      if (current.has(nextMode)) return current;
      const next = new Set(current);
      next.add(nextMode);
      return next;
    });
  }

  function handleTabKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const currentIndex = MODES.findIndex((item) => item.id === mode);
    let nextIndex = currentIndex;
    if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = MODES.length - 1;
    else {
      const direction = event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 1;
      nextIndex = (currentIndex + direction + MODES.length) % MODES.length;
    }
    const nextMode = MODES[nextIndex].id;
    activateMode(nextMode);
    window.requestAnimationFrame(() => document.getElementById(`knowledge-tab-${nextMode}-${nextMode}`)?.focus());
  }

  function modeNavigation(instance: KnowledgeMode) {
    return (
      <div
        className="ks-mode-tabs"
        role="tablist"
        aria-label="知识检索模式"
        onKeyDown={handleTabKeyDown}
      >
        {MODES.map((item) => {
          const Icon = item.icon;
          const active = item.id === mode;
          return (
            <button
              key={item.id}
              id={`knowledge-tab-${instance}-${item.id}`}
              type="button"
              role="tab"
              aria-selected={active}
              aria-controls={`knowledge-panel-${item.id}`}
              tabIndex={active ? 0 : -1}
              className={active ? "is-active" : ""}
              onClick={() => activateMode(item.id)}
            >
              <Icon aria-hidden="true" />
              <span>{item.label}</span>
              {item.demo ? <b>Demo</b> : null}
            </button>
          );
        })}
      </div>
    );
  }

  return (
    <div className="knowledge-retrieval-page">
      <header className="ks-page-header">
        <div className="ks-page-title">
          <h1>知识检索</h1>
        </div>
      </header>

      <div className="ks-mode-stage">
        <section
          id="knowledge-panel-local"
          className={`ks-mode-panel${mode === "local" ? " is-active" : ""}`}
          role="tabpanel"
          aria-labelledby="knowledge-tab-local-local"
          aria-hidden={mode !== "local"}
          inert={mode !== "local"}
          hidden={mode !== "local"}
        >
          <LocalKnowledgePanel
            initialQuery={initialQuery}
            initialTerms={initialTerms}
            modeNavigation={modeNavigation("local")}
          />
        </section>

        {visitedModes.has("online") ? (
          <section
            id="knowledge-panel-online"
            className={`ks-mode-panel${mode === "online" ? " is-active" : ""}`}
            role="tabpanel"
            aria-labelledby="knowledge-tab-online-online"
            aria-hidden={mode !== "online"}
            inert={mode !== "online"}
            hidden={mode !== "online"}
          >
            <OnlineKnowledgeSearchPanel initialMaterial={initialQuery} modeNavigation={modeNavigation("online")} />
          </section>
        ) : null}

        {visitedModes.has("pdf") ? (
          <section
            id="knowledge-panel-pdf"
            className={`ks-mode-panel${mode === "pdf" ? " is-active" : ""}`}
            role="tabpanel"
            aria-labelledby="knowledge-tab-pdf-pdf"
            aria-hidden={mode !== "pdf"}
            inert={mode !== "pdf"}
            hidden={mode !== "pdf"}
          >
            <PdfSimilarityDemoPanel modeNavigation={modeNavigation("pdf")} />
          </section>
        ) : null}
      </div>
    </div>
  );
}
