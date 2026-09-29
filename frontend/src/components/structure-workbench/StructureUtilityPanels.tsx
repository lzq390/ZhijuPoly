import {
  ArrowUp,
  Atom,
  BarChart3,
  Database,
  FlaskConical,
  Grid2X2,
  LoaderCircle,
  Microscope,
  Orbit,
  Plus,
  Route,
  Sparkles,
  X,
  type LucideIcon
} from "lucide-react";
import type { RefObject } from "react";
import { useMotionPresence } from "../../hooks/useMotionPresence";
import type { StructureUtilityPanel } from "./StructureCanvasSurface";

export type StructureWorkbenchModuleId =
  | "databaseQuery"
  | "homopolymerPrediction"
  | "explorer"
  | "monomerDft"
  | "monomerPolymerization"
  | "monomerRetrosynthesis"
  | "reverseDesign"
  | "conditionalGeneration"
  | "polytaoGeneration";

type ModuleRelationship = "direct" | "shared" | "optional";

type WorkbenchModule = {
  id: StructureWorkbenchModuleId;
  name: string;
  shortName: string;
  icon: LucideIcon;
  relationship: ModuleRelationship;
};

const WORKBENCH_MODULES: WorkbenchModule[] = [
  {
    id: "databaseQuery",
    name: "数据库查询",
    shortName: "数据库查询",
    icon: Database,
    relationship: "direct"
  },
  {
    id: "homopolymerPrediction",
    name: "均聚物性质预测",
    shortName: "性质预测",
    icon: BarChart3,
    relationship: "direct"
  },
  {
    id: "explorer",
    name: "聚合物相似性探索",
    shortName: "相似探索",
    icon: Atom,
    relationship: "direct"
  },
  {
    id: "monomerDft",
    name: "单体 DFT",
    shortName: "单体 DFT",
    icon: Orbit,
    relationship: "shared"
  },
  {
    id: "monomerPolymerization",
    name: "单体正向聚合",
    shortName: "正向聚合",
    icon: FlaskConical,
    relationship: "shared"
  },
  {
    id: "reverseDesign",
    name: "Tg 逆向设计",
    shortName: "Tg 逆向",
    icon: Sparkles,
    relationship: "direct"
  },
  {
    id: "conditionalGeneration",
    name: "条件聚合物生成",
    shortName: "条件生成",
    icon: Microscope,
    relationship: "shared"
  },
  {
    id: "polytaoGeneration",
    name: "聚合物生成",
    shortName: "聚合物生成",
    icon: Sparkles,
    relationship: "optional"
  },
  {
    id: "monomerRetrosynthesis",
    name: "单体逆合成反推",
    shortName: "单体反推",
    icon: Route,
    relationship: "shared"
  }
];

const RELATIONSHIP_LABEL: Record<ModuleRelationship, string> = {
  direct: "直接使用画板",
  shared: "消费共享结构",
  optional: "结构输入可选"
};

type StructureUtilityPanelsProps = {
  openPanel: StructureUtilityPanel;
  modulePanelRef: RefObject<HTMLElement | null>;
  assistantPanelRef: RefObject<HTMLElement | null>;
  openingModuleId: StructureWorkbenchModuleId | null;
  selectedModuleName: string;
  structureSmiles: string;
  assistantInput: string;
  assistantNotice: string | null;
  onClose: (restoreFocus?: boolean) => void;
  onOpenExternal: (id: StructureWorkbenchModuleId, shortName: string) => void;
  onAssistantInputChange: (value: string) => void;
  onAssistantNew: () => void;
  onAssistantSend: () => void;
};

export function StructureUtilityPanels({
  openPanel,
  modulePanelRef,
  assistantPanelRef,
  openingModuleId,
  selectedModuleName,
  structureSmiles,
  assistantInput,
  assistantNotice,
  onClose,
  onOpenExternal,
  onAssistantInputChange,
  onAssistantNew,
  onAssistantSend
}: StructureUtilityPanelsProps) {
  const modules = useMotionPresence(openPanel === "modules", { elementRef: modulePanelRef });
  const assistant = useMotionPresence(openPanel === "assistant", { elementRef: assistantPanelRef });

  return (
    <div className={`np-sw-utility-layer${modules.present || assistant.present ? " is-open" : ""}`} aria-hidden={!openPanel}>
      <button
        type="button"
        className="np-sw-utility-backdrop"
        aria-label="关闭工作台浮层"
        tabIndex={openPanel ? 0 : -1}
        onClick={() => onClose(false)}
      />

      <section
        ref={modulePanelRef}
        {...modules.motionProps}
        id="structure-module-panel"
        className={`np-sw-popover np-sw-popover--modules${openPanel === "modules" ? " is-open" : ""}`}
        role="dialog"
        aria-modal="false"
        aria-labelledby="structure-module-panel-title"
        aria-hidden={openPanel !== "modules"}
        inert={openPanel !== "modules"}
      >
        <header className="np-sw-popover__header">
          <div>
            <span className="np-sw-popover__mark"><Grid2X2 aria-hidden="true" /></span>
            <span>
              <h2 id="structure-module-panel-title">选择功能</h2>
              <small>同步当前结构后进入下一项科研任务</small>
            </span>
          </div>
          <button type="button" className="np-sw-icon-button" aria-label="收起功能参数" onClick={() => onClose()}>
            <X aria-hidden="true" />
          </button>
        </header>

        <div className="np-sw-module-view">
          <div className="np-sw-module-count"><span>{WORKBENCH_MODULES.length} 项功能</span></div>
          <div className="np-sw-module-grid" aria-label="使用共享结构的功能模块">
            {WORKBENCH_MODULES.map((module) => {
              const Icon = module.icon;
              const isOpening = openingModuleId === module.id;
              return (
                <button
                  key={module.id}
                  type="button"
                  className={`np-sw-module-tile is-${module.relationship}`}
                  aria-label={`打开${module.name}`}
                  title={module.id === "monomerRetrosynthesis" ? "使用当前结构反推" : module.name}
                  disabled={Boolean(openingModuleId)}
                  onClick={() => onOpenExternal(module.id, module.shortName)}
                >
                  <span className="np-sw-module-tile__icon">
                    {isOpening ? <LoaderCircle className="np-sw-spin" /> : <Icon />}
                  </span>
                  <strong>{module.shortName}</strong>
                </button>
              );
            })}
          </div>
          <div className="np-sw-module-legend" aria-label="模块与画板关系图例">
            {(Object.entries(RELATIONSHIP_LABEL) as [ModuleRelationship, string][]).map(
              ([relationship, label]) => (
                <span key={relationship} className={`is-${relationship}`}>{label}</span>
              )
            )}
          </div>
        </div>

      </section>

      <section
        ref={assistantPanelRef}
        {...assistant.motionProps}
        id="structure-assistant-panel"
        className={`np-sw-popover np-sw-popover--assistant${openPanel === "assistant" ? " is-open" : ""}`}
        role="dialog"
        aria-modal="false"
        aria-labelledby="structure-assistant-title"
        aria-hidden={openPanel !== "assistant"}
        inert={openPanel !== "assistant"}
      >
        <header className="np-sw-popover__header">
          <div>
            <span className="np-sw-popover__mark"><Sparkles aria-hidden="true" /></span>
            <span>
              <h2 id="structure-assistant-title">结构 AI 助手</h2>
              <small>科研上下文预览</small>
            </span>
          </div>
          <span className="np-sw-popover__actions">
            <button type="button" className="np-sw-icon-button" aria-label="新建对话" title="新建对话" onClick={onAssistantNew}>
              <Plus aria-hidden="true" />
            </button>
            <button type="button" className="np-sw-icon-button" aria-label="收起 AI 助手" onClick={() => onClose()}>
              <X aria-hidden="true" />
            </button>
          </span>
        </header>
        <div className="np-sw-assistant-body">
          <div className="np-sw-assistant-context" aria-label="当前 AI 上下文">
            <span className={structureSmiles.trim() ? "is-ready" : ""}>{structureSmiles.trim() ? "共享结构已同步" : "暂无共享结构"}</span>
            <span>{selectedModuleName}</span>
          </div>
          <div className="np-sw-assistant-welcome">
            <span><Sparkles aria-hidden="true" /></span>
            <h3>你好，今天想一起研究什么？</h3>
            <p>我会结合当前共享结构与所选功能辅助分析。</p>
          </div>
          <div className="np-sw-assistant-suggestions">
            {[
              "解释当前结构中的主要官能团",
              "推荐适合当前结构的下一步模块",
              "说明当前结构的后续研究方向"
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
