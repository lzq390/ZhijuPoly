import {
  ArrowLeft,
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
import type { FormEvent, RefObject } from "react";
import type {
  MonomerRetrosynthesisResponse,
  MonomerRetrosynthesisTargetRole
} from "../../types";
import type { StructureUtilityPanel } from "./StructureCanvasSurface";

export type StructureWorkbenchModuleId =
  | "databaseQuery"
  | "homopolymerPrediction"
  | "explorer"
  | "monomerDft"
  | "monomerPolymerization"
  | "reverseDesign"
  | "conditionalGeneration"
  | "polytaoGeneration";

export type StructureModulePanelView = "grid" | "retrosynthesis";
type ModuleRelationship = "direct" | "shared" | "optional" | "local";

type WorkbenchModule = {
  id: StructureWorkbenchModuleId | "retrosynthesis";
  name: string;
  shortName: string;
  icon: LucideIcon;
  relationship: ModuleRelationship;
  builtIn?: boolean;
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
    name: "单体 DFT（AIMNet2）",
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
    id: "retrosynthesis",
    name: "单体逆合成反推",
    shortName: "单体反推",
    icon: Route,
    relationship: "local",
    builtIn: true
  }
];

const RELATIONSHIP_LABEL: Record<ModuleRelationship, string> = {
  direct: "直接使用画板",
  shared: "消费共享结构",
  optional: "结构输入可选",
  local: "工作台内置"
};

const TARGET_ROLE_OPTIONS: { value: MonomerRetrosynthesisTargetRole; label: string }[] = [
  { value: "auto", label: "自动识别" },
  { value: "other", label: "通用单体" },
  { value: "diamine", label: "二胺提示" },
  { value: "dianhydride", label: "二酐提示" }
];

type StructureUtilityPanelsProps = {
  openPanel: StructureUtilityPanel;
  modulePanelView: StructureModulePanelView;
  modulePanelRef: RefObject<HTMLElement | null>;
  assistantPanelRef: RefObject<HTMLElement | null>;
  openingModuleId: StructureWorkbenchModuleId | null;
  selectedModuleName: string;
  structureSmiles: string;
  assistantInput: string;
  assistantNotice: string | null;
  retroSmiles: string;
  retroTargetRole: MonomerRetrosynthesisTargetRole;
  retroReturnCount: string;
  showRetroValidation: boolean;
  retroTargetValidation: string | null;
  retroCountValidation: string | null;
  isRetrosynthesizing: boolean;
  retroError: string | null;
  retroData: MonomerRetrosynthesisResponse | null;
  operationBusy: boolean;
  onClose: (restoreFocus?: boolean) => void;
  onShowGrid: () => void;
  onShowRetrosynthesis: () => void;
  onOpenExternal: (id: StructureWorkbenchModuleId, shortName: string) => void;
  onUseCurrentStructure: () => void;
  onSubmitRetrosynthesis: (event: FormEvent<HTMLFormElement>) => void;
  onRetroSmilesChange: (value: string) => void;
  onRetroTargetRoleChange: (value: MonomerRetrosynthesisTargetRole) => void;
  onRetroReturnCountChange: (value: string) => void;
  onAssistantInputChange: (value: string) => void;
  onAssistantNew: () => void;
  onAssistantSend: () => void;
};

export function StructureUtilityPanels({
  openPanel,
  modulePanelView,
  modulePanelRef,
  assistantPanelRef,
  openingModuleId,
  selectedModuleName,
  structureSmiles,
  assistantInput,
  assistantNotice,
  retroSmiles,
  retroTargetRole,
  retroReturnCount,
  showRetroValidation,
  retroTargetValidation,
  retroCountValidation,
  isRetrosynthesizing,
  retroError,
  retroData,
  operationBusy,
  onClose,
  onShowGrid,
  onShowRetrosynthesis,
  onOpenExternal,
  onUseCurrentStructure,
  onSubmitRetrosynthesis,
  onRetroSmilesChange,
  onRetroTargetRoleChange,
  onRetroReturnCountChange,
  onAssistantInputChange,
  onAssistantNew,
  onAssistantSend
}: StructureUtilityPanelsProps) {
  const assistantTaskStatus = isRetrosynthesizing
    ? "反推运行中"
    : retroError
      ? "反推需检查"
      : retroData
        ? `反推 ${retroData.total} 个候选`
        : "反推待运行";

  return (
    <div className={`np-sw-utility-layer${openPanel ? " is-open" : ""}`} aria-hidden={!openPanel}>
      <button
        type="button"
        className="np-sw-utility-backdrop"
        aria-label="关闭工作台浮层"
        tabIndex={openPanel ? 0 : -1}
        onClick={() => onClose(false)}
      />

      <section
        ref={modulePanelRef}
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
            {modulePanelView === "retrosynthesis" ? (
              <button
                type="button"
                className="np-sw-icon-button np-sw-module-back"
                aria-label="返回功能列表"
                onClick={onShowGrid}
              >
                <ArrowLeft aria-hidden="true" />
              </button>
            ) : (
              <span className="np-sw-popover__mark"><Grid2X2 aria-hidden="true" /></span>
            )}
            <span>
              <h2 id="structure-module-panel-title">
                {modulePanelView === "retrosynthesis" ? "单体逆合成反推" : "选择功能"}
              </h2>
              <small>
                {modulePanelView === "retrosynthesis"
                  ? "工作台内置任务"
                  : "同步当前结构后进入下一项科研任务"}
              </small>
            </span>
          </div>
          <button type="button" className="np-sw-icon-button" aria-label="收起功能参数" onClick={() => onClose()}>
            <X aria-hidden="true" />
          </button>
        </header>

        {modulePanelView === "grid" ? (
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
                    aria-label={module.builtIn ? `设置${module.name}参数` : `打开${module.name}`}
                    disabled={Boolean(openingModuleId)}
                    onClick={() => {
                      if (module.id === "retrosynthesis") {
                        onShowRetrosynthesis();
                      } else {
                        onOpenExternal(module.id, module.shortName);
                      }
                    }}
                  >
                    {module.builtIn ? <i className="np-sw-local-dot" aria-hidden="true" /> : null}
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
        ) : (
          <div className="np-sw-retro-parameters">
            <div className="np-sw-retro-intro">
              <span><Route aria-hidden="true" /></span>
              <div>
                <strong>从目标单体反推前体组合</strong>
                <p>候选与反应提示会在独立结果面板中展示。</p>
              </div>
            </div>
            <form noValidate onSubmit={onSubmitRetrosynthesis}>
              <label className="np-sw-field">
                <span>Target monomer SMILES <small>必填</small></span>
                <textarea
                  rows={3}
                  value={retroSmiles}
                  onChange={(event) => onRetroSmilesChange(event.currentTarget.value)}
                  placeholder="例如：C=C(C)C(=O)OC"
                  spellCheck={false}
                  aria-label="目标单体 SMILES"
                  aria-invalid={Boolean(showRetroValidation && retroTargetValidation)}
                  aria-describedby="np-sw-retro-target-error"
                />
                <small id="np-sw-retro-target-error" className="np-sw-field__error" role="status">
                  {showRetroValidation ? retroTargetValidation : null}
                </small>
              </label>

              <div className="np-sw-field-row">
                <label className="np-sw-field">
                  <span>结构提示</span>
                  <select
                    value={retroTargetRole}
                    onChange={(event) => onRetroTargetRoleChange(event.currentTarget.value as MonomerRetrosynthesisTargetRole)}
                    aria-label="反推结构提示"
                  >
                    {TARGET_ROLE_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                </label>
                <label className="np-sw-field">
                  <span>候选数 <small>1–10</small></span>
                  <input
                    type="number"
                    min={1}
                    max={10}
                    step={1}
                    inputMode="numeric"
                    value={retroReturnCount}
                    onChange={(event) => onRetroReturnCountChange(event.currentTarget.value)}
                    aria-label="反推候选数"
                    aria-invalid={Boolean(showRetroValidation && retroCountValidation)}
                    aria-describedby="np-sw-retro-count-error"
                  />
                </label>
              </div>
              <small id="np-sw-retro-count-error" className="np-sw-field__error" role="status">
                {showRetroValidation ? retroCountValidation : null}
              </small>
              <div className="np-sw-retro-actions">
                <button type="button" className="np-sw-secondary-button" onClick={onUseCurrentStructure} disabled={operationBusy}>
                  <Atom aria-hidden="true" /> 使用当前结构
                </button>
                <button type="submit" className="np-sw-primary-button" disabled={operationBusy}>
                  {isRetrosynthesizing ? <LoaderCircle className="np-sw-spin" /> : <Sparkles aria-hidden="true" />}
                  运行反推
                </button>
              </div>
            </form>
          </div>
        )}
      </section>

      <section
        ref={assistantPanelRef}
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
            <span>{assistantTaskStatus}</span>
          </div>
          <div className="np-sw-assistant-welcome">
            <span><Sparkles aria-hidden="true" /></span>
            <h3>你好，今天想一起研究什么？</h3>
            <p>我会结合当前共享结构、所选功能与单体反推状态辅助分析。</p>
          </div>
          <div className="np-sw-assistant-suggestions">
            {[
              "解释当前结构中的主要官能团",
              "推荐适合当前结构的下一步模块",
              "梳理单体反推结果中的前体差异"
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
