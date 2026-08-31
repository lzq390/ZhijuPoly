import { SlidersHorizontal } from "lucide-react";
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent
} from "react";
import { DEFAULT_PREDICTABLE_PROPERTY } from "../constants/predictableProperties";
import { useTgStructureCanvas } from "../hooks/useTgStructureCanvas";
import type {
  MatchMode,
  PredictableProperty,
  SmilesQueryRequest,
  SmilesQueryResponse,
  StructureWorkspaceContext
} from "../types";
import "../styles/structure-workbench.css";
import "../styles/polymer-similarity-explorer.css";
import type { StructureCanvasOwnerHandle } from "./StructureWorkbenchPage";
import {
  SimilarityExplorerDrawer,
  type SimilarityExplorerSnapshot
} from "./polymer-similarity-explorer/SimilarityExplorerDrawer";
import { SimilarityExplorerParameters } from "./polymer-similarity-explorer/SimilarityExplorerParameters";
import { StructureCanvasSurface } from "./structure-workbench/StructureCanvasSurface";

type PolymerSimilarityExplorerPageProps = {
  structure: StructureWorkspaceContext;
  request: SmilesQueryRequest;
  setRequest: (request: SmilesQueryRequest) => void;
  isQueryLoading: boolean;
  queryError: string | null;
  queryData: SmilesQueryResponse | null;
  submitQuery: (request?: SmilesQueryRequest) => Promise<void>;
};

const SIMILARITY_EXAMPLE_SMILES = "*CC*";

export const PolymerSimilarityExplorerPage = forwardRef<
  StructureCanvasOwnerHandle,
  PolymerSimilarityExplorerPageProps
>(function PolymerSimilarityExplorerPage({
  structure,
  request,
  setRequest,
  isQueryLoading,
  queryError,
  queryData,
  submitQuery
}, forwardedRef) {
  const [parametersOpen, setParametersOpen] = useState(false);
  const [mode, setMode] = useState<MatchMode>(request.match_mode);
  const [similarityThreshold, setSimilarityThreshold] = useState(request.similarity_threshold);
  const [topK, setTopK] = useState(request.top_k);
  const [selectedProperty, setSelectedProperty] = useState<PredictableProperty>(
    request.property_name ?? DEFAULT_PREDICTABLE_PROPERTY
  );
  const [hasActivated3D, setHasActivated3D] = useState(false);
  const [isPreparing, setIsPreparing] = useState(false);
  const [hasAttempt, setHasAttempt] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerWidth, setDrawerWidth] = useState(380);
  const [snapshot, setSnapshot] = useState<SimilarityExplorerSnapshot | null>(null);
  const parameterPanelRef = useRef<HTMLElement | null>(null);
  const parameterButtonRef = useRef<HTMLButtonElement | null>(null);
  const restoreFocusFrameRef = useRef<number | null>(null);
  const canvas = useTgStructureCanvas({
    structure,
    onStructureChanged: useCallback(() => undefined, [])
  });

  const closeParameters = useCallback((restoreFocus = true) => {
    setParametersOpen(false);
    if (!restoreFocus) return;
    if (restoreFocusFrameRef.current !== null) window.cancelAnimationFrame(restoreFocusFrameRef.current);
    restoreFocusFrameRef.current = window.requestAnimationFrame(() => {
      restoreFocusFrameRef.current = null;
      parameterButtonRef.current?.focus();
    });
  }, []);

  useEffect(() => {
    return () => {
      if (restoreFocusFrameRef.current !== null) {
        window.cancelAnimationFrame(restoreFocusFrameRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!parametersOpen) return;
    const frame = window.requestAnimationFrame(() => {
      parameterPanelRef.current?.querySelector<HTMLElement>("button, input, select")?.focus();
    });
    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (!parameterPanelRef.current?.contains(target) && !parameterButtonRef.current?.contains(target)) {
        closeParameters(false);
      }
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeParameters(true);
    }
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [closeParameters, parametersOpen]);

  useImperativeHandle(
    forwardedRef,
    () => ({
      async syncBeforeLeave() {
        if (!(await canvas.flushSmilesDraft())) return;
        await canvas.syncSmilesFromCanvas({ preserveExisting: true, quiet: true });
      }
    }),
    [canvas]
  );

  async function toggle3D() {
    const activating = !canvas.isFlipped;
    const changed = await canvas.toggle3D();
    if (changed && activating) setHasActivated3D(true);
  }

  async function submitSimilarity(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const thresholdValid = Number.isFinite(similarityThreshold) && similarityThreshold >= 0 && similarityThreshold <= 1;
    const topKValid = Number.isInteger(topK) && topK >= 1 && topK <= 100;
    if (!thresholdValid || !topKValid || isPreparing || isQueryLoading) return;

    const hadAttempt = hasAttempt;
    setIsPreparing(true);
    setHasAttempt(true);
    setDrawerOpen(true);
    closeParameters(false);
    try {
      const smiles = await canvas.resolveSmilesForSearch();
      if (!smiles) {
        setHasAttempt(hadAttempt);
        setDrawerOpen(false);
        setParametersOpen(true);
        return;
      }

      const nextSnapshot: SimilarityExplorerSnapshot = {
        smiles,
        mode,
        similarityThreshold,
        topK,
        property: mode === "property" ? selectedProperty : null
      };
      const nextRequest: SmilesQueryRequest = {
        smiles,
        match_mode: mode,
        similarity_threshold: similarityThreshold,
        top_k: topK,
        property_name: nextSnapshot.property
      };
      setSnapshot(nextSnapshot);
      setRequest(nextRequest);
      setIsPreparing(false);
      await submitQuery(nextRequest);
    } finally {
      setIsPreparing(false);
    }
  }

  function adjustParameters() {
    setDrawerOpen(false);
    setParametersOpen(true);
  }

  const stale = Boolean(
    snapshot &&
      (structure.smiles.trim() !== snapshot.smiles ||
        mode !== snapshot.mode ||
        similarityThreshold !== snapshot.similarityThreshold ||
        topK !== snapshot.topK ||
        (mode === "property" ? selectedProperty : null) !== snapshot.property)
  );
  const operationBusy = canvas.isBusy || isPreparing || isQueryLoading;
  const workbenchStyle = { "--np-sw-drawer-width": `${drawerWidth}px` } as CSSProperties;

  return (
    <div
      className="np-structure-workbench np-similarity-explorer"
      data-module="polymer-similarity-explorer"
      style={workbenchStyle}
    >
      <div className={`np-sw-page${drawerOpen ? " has-open-drawer" : ""}`}>
        <h1 className="np-sw-page-title">聚合物相似性探索</h1>
        <div className={`np-sw-layout${drawerOpen ? " has-open-drawer" : ""}`}>
          <main className="np-sw-workspace">
            <StructureCanvasSurface
              structure={structure}
              canvas={canvas}
              hasActivated3D={hasActivated3D}
              operationBusy={operationBusy}
              editorTitle="聚合物相似性探索结构编辑器"
              utilityActions={[
                {
                  id: "similarity-parameters",
                  label: "探索参数",
                  icon: <SlidersHorizontal aria-hidden="true" />,
                  active: parametersOpen,
                  buttonRef: parameterButtonRef,
                  controls: "polymer-similarity-parameters",
                  onClick: () => setParametersOpen((current) => !current)
                }
              ]}
              onLoadExample={() => canvas.loadStructure(SIMILARITY_EXAMPLE_SMILES)}
              onImportFile={(file) => canvas.importImageFile(file)}
              onClear={() => canvas.clearCanvas()}
              onSync={() => canvas.syncSmilesFromCanvas()}
              onToggle3D={toggle3D}
            />

            <SimilarityExplorerParameters
              open={parametersOpen}
              panelRef={parameterPanelRef}
              mode={mode}
              similarityThreshold={similarityThreshold}
              topK={topK}
              selectedProperty={selectedProperty}
              submitting={isPreparing || isQueryLoading}
              onClose={closeParameters}
              onModeChange={setMode}
              onSimilarityThresholdChange={setSimilarityThreshold}
              onTopKChange={setTopK}
              onSelectedPropertyChange={setSelectedProperty}
              onSubmit={submitSimilarity}
            />
          </main>

          <SimilarityExplorerDrawer
            open={drawerOpen}
            hasAttempt={hasAttempt}
            width={drawerWidth}
            preparing={isPreparing}
            loading={isQueryLoading}
            error={queryError}
            data={queryData}
            snapshot={snapshot}
            stale={stale}
            onWidthChange={setDrawerWidth}
            onClose={() => setDrawerOpen(false)}
            onOpen={() => setDrawerOpen(true)}
            onAdjustParameters={adjustParameters}
          />
        </div>
      </div>
    </div>
  );
});
