import { requestServiceAccess } from "../auth/guestAccess";
import { BrowsingRecordingControls } from "./browsing-recording/BrowsingRecording";
import { ModulePageHeader } from "./ModulePageHeader";
import { BookOpen, Hexagon, Sparkles } from "lucide-react";
import {
  forwardRef,
  useCallback,
  useEffect,
  useId,
  useImperativeHandle,
  useRef,
  useState,
  type FormEvent,
  type MouseEvent
} from "react";
import { PREDICTABLE_PROPERTIES } from "../constants/predictableProperties";
import { usePredict } from "../hooks/usePredict";
import { useTgStructureCanvas } from "../hooks/useTgStructureCanvas";
import type { PredictableProperty, StructureWorkspaceContext } from "../types";
import "../styles/structure-workbench.css";
import "../styles/homopolymer-property-prediction.css";
import type { StructureCanvasOwnerHandle } from "./StructureWorkbenchPage";
import { runCanvasMutation, StructureCanvasSurface } from "./structure-workbench/StructureCanvasSurface";
import {
  HomopolymerPredictionPanel,
  type PredictionRelayModule
} from "./homopolymer-prediction/HomopolymerPredictionPanel";
import { HomopolymerPredictionHelp } from "./homopolymer-prediction/HomopolymerPredictionHelp";
import { HOMOPOLYMER_EXAMPLES } from "./homopolymer-prediction/examples";
import { displaySmiles, formatRunTime, type PredictionRun } from "./homopolymer-prediction/predictionFormat";
import {
  HomopolymerPredictionAssistant,
  type PredictionAssistantMessage
} from "./homopolymer-prediction/HomopolymerPredictionAssistant";
import { StructureFactsStrip } from "./homopolymer-prediction/StructureFactsStrip";
import { useStructureDescriptors } from "./homopolymer-prediction/useStructureDescriptors";
import { ModuleDock } from "./module-frame/ModuleDock";
import { ModuleTools } from "./module-frame/ModuleTools";

type HomopolymerPropertyPredictionPageProps = {
  structure: StructureWorkspaceContext;
  onOpenModule?: (module: PredictionRelayModule) => void;
};

const HOMOPOLYMER_EXAMPLE_SMILES = "*CC*";
const STRUCTURE_FACTS_HELP_ID = "np-hp-help-structure-facts";
const STRUCTURE_EXAMPLES = HOMOPOLYMER_EXAMPLES.map((example) => ({
  id: example.abbr,
  label: example.abbr,
  description: example.name,
  smiles: example.smiles,
  image: example.image
}));
const RUN_HISTORY_LIMIT = 10;

function sameProperties(left: readonly PredictableProperty[], right: readonly PredictableProperty[]) {
  return left.length === right.length && left.every((property, index) => property === right[index]);
}

export const HomopolymerPropertyPredictionPage = forwardRef<
  StructureCanvasOwnerHandle,
  HomopolymerPropertyPredictionPageProps
>(function HomopolymerPropertyPredictionPage({ structure, onOpenModule }, forwardedRef) {
  const [selectedProperties, setSelectedProperties] = useState<PredictableProperty[]>(() => [
    ...PREDICTABLE_PROPERTIES
  ]);
  // The choice is the first decision on an empty page; it folds away after the first result.
  const [selectionOpen, setSelectionOpen] = useState(true);
  const [hasActivated3D, setHasActivated3D] = useState(false);
  const [hasAttempt, setHasAttempt] = useState(false);
  const [structureMissing, setStructureMissing] = useState(false);
  const [runs, setRuns] = useState<PredictionRun[]>([]);
  const [viewedRunId, setViewedRunId] = useState<number | null>(null);
  // "latest" follows the newest other run until the user picks another.
  const [comparisonChoice, setComparisonChoice] = useState<number | null | "latest">("latest");
  const [dockOpen, setDockOpen] = useState(false);
  const [dockTab, setDockTab] = useState<"assistant" | "help">("assistant");
  const [assistantMessages, setAssistantMessages] = useState<PredictionAssistantMessage[]>([]);
  const [assistantAttachContext, setAssistantAttachContext] = useState(false);
  const [helpTarget, setHelpTarget] = useState<string | null>(null);
  const assistantMessageIdRef = useRef(0);
  const dockTriggerRef = useRef<HTMLElement | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const formRef = useRef<HTMLFormElement | null>(null);
  const helpButtonRef = useRef<HTMLButtonElement | null>(null);
  const assistantButtonRef = useRef<HTMLButtonElement | null>(null);
  const requestRef = useRef(0);
  const runIdRef = useRef(0);
  const dockId = useId();
  const predict = usePredict();
  const canvas = useTgStructureCanvas({
    structure,
    onStructureChanged: useCallback(() => undefined, [])
  });

  useImperativeHandle(
    forwardedRef,
    () => ({
      syncBeforeLeave: canvas.syncBeforeLeave
    }),
    [canvas]
  );

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey) || event.defaultPrevented) return;
      const target = event.target;
      if (target instanceof Node && target !== document.body && !rootRef.current?.contains(target)) return;
      event.preventDefault();
      formRef.current?.requestSubmit();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, []);

  useEffect(() => {
    if (structure.smiles.trim()) setStructureMissing(false);
  }, [structure.smiles]);

  // Facts follow the committed structure; a SMILES draft that failed to parse already explains itself.
  const descriptorState = useStructureDescriptors(canvas.smilesDraftState === "error" ? "" : structure.smiles);

  useEffect(() => {
    if (!helpTarget || !dockOpen || dockTab !== "help") return;
    const frame = window.requestAnimationFrame(() => {
      document.getElementById(helpTarget)?.scrollIntoView({ block: "start" });
      setHelpTarget(null);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [helpTarget, dockOpen, dockTab]);

  async function toggle3D() {
    const activating = !canvas.isFlipped;
    const changed = await canvas.toggle3D();
    if (changed && activating) setHasActivated3D(true);
  }

  async function submitPrediction(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!requestServiceAccess()) return;
    if (selectedProperties.length === 0) return;
    const smiles = await canvas.resolveSmilesForSearch();
    if (!smiles) {
      setStructureMissing(true);
      return;
    }
    setStructureMissing(false);
    const properties = PREDICTABLE_PROPERTIES.filter((property) => selectedProperties.includes(property));
    const request = ++requestRef.current;
    setHasAttempt(true);
    try {
      const data = await predict.submit({ smiles, properties });
      if (request !== requestRef.current) return;
      const run: PredictionRun = {
        id: ++runIdRef.current,
        smiles,
        properties,
        predictions: data.predictions,
        queryTimeMs: data.query_time_ms,
        finishedAt: new Date()
      };
      setRuns((current) => [...current, run].slice(-RUN_HISTORY_LIMIT));
      setViewedRunId(null);
      setComparisonChoice("latest");
      setSelectionOpen(false);
    } catch {
      // usePredict owns the visible error and ignores cancelled or stale responses.
    }
  }

  function restoreRun(run: PredictionRun) {
    if (!requestServiceAccess()) return;
    setViewedRunId(run.id);
    setComparisonChoice("latest");
    setSelectedProperties([...run.properties]);
    void runCanvasMutation(structure, canvas, () => canvas.loadStructure(run.smiles));
  }

  const viewedRun = (viewedRunId === null ? null : runs.find((run) => run.id === viewedRunId)) ?? runs.at(-1) ?? null;
  // Run ids count every run of the session, so the label stays right after older runs drop off the list.
  const runNumber = viewedRun ? viewedRun.id : 0;
  const comparisonOptions = runs.filter((run) => run !== viewedRun).reverse();
  const comparison =
    comparisonChoice === "latest"
      ? comparisonOptions[0] ?? null
      : comparisonChoice === null
        ? null
        : comparisonOptions.find((run) => run.id === comparisonChoice) ?? null;
  const stale = Boolean(
    viewedRun &&
      (structure.smiles.trim() !== viewedRun.smiles ||
        !sameProperties(selectedProperties, viewedRun.properties))
  );
  const operationBusy = canvas.isBusy || predict.isLoading;
  const hasStructure = Boolean(structure.smiles.trim() || canvas.smilesDraft.trim());
  const canvasSmiles = structure.smiles.trim() || canvas.smilesDraft.trim();
  const assistantContext = {
    structure: canvasSmiles ? displaySmiles(canvasSmiles) : null,
    runCount: runs.length,
    resultCount: viewedRun && !predict.error
      ? viewedRun.properties.filter((property) => Number.isFinite(viewedRun.predictions[property])).length
      : null,
    comparisonLabel: comparison ? `${formatRunTime(comparison.finishedAt)} ${displaySmiles(comparison.smiles)}` : null
  };

  // One dock, one tab at a time: the same header button closes it, the other switches tabs.
  function toggleDock(tab: "assistant" | "help", trigger: HTMLElement | null) {
    dockTriggerRef.current = trigger;
    if (dockOpen && dockTab === tab) {
      setDockOpen(false);
      return;
    }
    setDockTab(tab);
    setDockOpen(true);
  }

  function skipTo(event: MouseEvent<HTMLAnchorElement>, selector: string) {
    event.preventDefault();
    rootRef.current?.querySelector<HTMLElement>(selector)?.focus();
  }

  function openStructureFactsHelp(trigger: HTMLElement | null) {
    dockTriggerRef.current = trigger;
    setDockTab("help");
    setDockOpen(true);
    setHelpTarget(STRUCTURE_FACTS_HELP_ID);
  }

  // Not connected to a model yet: the question stays on the page and gets a fixed, explicit reply.
  function sendAssistantMessage(text: string) {
    const reply = "AI 助手暂未开放，还不能回答问题。";
    setAssistantMessages((current) => [
      ...current,
      { id: ++assistantMessageIdRef.current, role: "user", content: text },
      { id: ++assistantMessageIdRef.current, role: "assistant", content: reply }
    ]);
  }

  return (
    <div
      ref={rootRef}
      className="np-module-page np-structure-workbench np-homopolymer-prediction"
      data-module="homopolymer-property-prediction"
    >
      <ModulePageHeader
        meta={<span>从重复单元结构预测热学、力学与气体渗透性质，共 9 项</span>}
        actions={
          <ModuleTools
            tools={[{
              id: "assistant",
              label: "AI 助手",
              icon: <Sparkles aria-hidden="true" />,
              status: "未开放",
              pressed: dockOpen && dockTab === "assistant",
              controls: dockId,
              buttonRef: assistantButtonRef,
              onClick: () => toggleDock("assistant", assistantButtonRef.current)
            }]}
            recording={<BrowsingRecordingControls module="homopolymerPrediction" />}
            help={{
              id: "help",
              label: "使用说明",
              icon: <BookOpen aria-hidden="true" />,
              pressed: dockOpen && dockTab === "help",
              controls: dockId,
              buttonRef: helpButtonRef,
              onClick: () => toggleDock("help", helpButtonRef.current)
            }}
          />
        }
      >
        均聚物性质预测
      </ModulePageHeader>
      <div className="np-sw-page np-module-page-body np-hp-page">
        <div className="np-module-frame np-hp-frame">
          <div className="np-hp-workbench np-module-frame__main">
            <main className="np-sw-workspace np-hp-structure">
              {/* Ketcher's toolbars hold ~45 Tab stops; keyboard users can jump past them. */}
              <nav className="np-hp-skip" aria-label="跳过画板工具">
                <a href="#np-sw-smiles-label" onClick={(event) => skipTo(event, ".np-sw-smiles textarea")}>跳到 SMILES 输入</a>
                <a href="#np-hp-task" onClick={(event) => skipTo(event, ".np-hp-task button, .np-hp-task input")}>跳到预测设置</a>
              </nav>
              <StructureCanvasSurface
                structure={structure}
                canvas={canvas}
                hasActivated3D={hasActivated3D}
                operationBusy={operationBusy}
                editorTitle="均聚物性质预测结构编辑器"
                commandbarLabel={<><span className="np-hp-glyph" aria-hidden="true"><Hexagon /></span>结构</>}
                clearLast
                viewSwitch="segmented"
                examples={STRUCTURE_EXAMPLES}
                onLoadExample={() => canvas.loadStructure(HOMOPOLYMER_EXAMPLE_SMILES)}
                onImportFile={(file) => canvas.importImageFile(file)}
                onClear={() => canvas.clearCanvas()}
                onSync={() => canvas.syncSmilesFromCanvas()}
                onToggle3D={toggle3D}
                footer={
                  <StructureFactsStrip
                    state={descriptorState}
                    onOpenHelp={() => openStructureFactsHelp(
                      document.activeElement instanceof HTMLElement ? document.activeElement : null
                    )}
                  />
                }
                emptyState={(load) => (
                  <div className="np-hp-start">
                    <strong>在这里画出重复单元</strong>
                    <p>用 * 标出两个连接点，或在下方粘贴 SMILES；也可以从示例开始：</p>
                    <ul aria-label="示例结构">
                      {HOMOPOLYMER_EXAMPLES.map((example) => (
                        <li key={example.abbr}>
                          <button type="button" onClick={() => load(example.smiles)} title={`${example.name}：${example.smiles}`}>
                            <img src={example.image} alt="" loading="lazy" />
                            <span>
                              <b>{example.abbr}</b>
                              <small>{example.name}</small>
                            </span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              />
            </main>

            <HomopolymerPredictionPanel
              formRef={formRef}
              selectedProperties={selectedProperties}
              onSelectedPropertiesChange={setSelectedProperties}
              selectionOpen={selectionOpen}
              onSelectionOpenChange={setSelectionOpen}
              loading={predict.isLoading}
              error={predict.error}
              hasAttempt={hasAttempt}
              structureMissing={structureMissing}
              latestRun={predict.error ? null : viewedRun}
              runNumber={runNumber}
              runs={runs}
              stale={stale}
              comparison={comparison}
              comparisonOptions={comparisonOptions}
              onComparisonChange={setComparisonChoice}
              onSubmit={submitPrediction}
              onRetry={() => formRef.current?.requestSubmit()}
              onRestoreRun={restoreRun}
              showRelay={hasStructure}
              onOpenModule={onOpenModule}
            />
          </div>
          <ModuleDock
            id={dockId}
            open={dockOpen}
            tabs={[
              {
                id: "assistant",
                label: "AI 助手",
                icon: <Sparkles aria-hidden="true" />,
                fill: true,
                content: (
                  <HomopolymerPredictionAssistant
                    context={assistantContext}
                    messages={assistantMessages}
                    attachContext={assistantAttachContext}
                    onAttachContextChange={setAssistantAttachContext}
                    onSend={sendAssistantMessage}
                    onClear={() => setAssistantMessages([])}
                  />
                )
              },
              {
                id: "help",
                label: "使用说明",
                icon: <BookOpen aria-hidden="true" />,
                content: <HomopolymerPredictionHelp structureFactsId={STRUCTURE_FACTS_HELP_ID} />
              }
            ]}
            activeTab={dockTab}
            onTabChange={(tab) => setDockTab(tab === "help" ? "help" : "assistant")}
            onClose={() => setDockOpen(false)}
            returnFocusRef={dockTriggerRef}
            initialFocusSelector={dockTab === "assistant" ? 'textarea[aria-label="发送给 AI 助手的消息"]' : undefined}
          />
        </div>
      </div>
    </div>
  );
});
