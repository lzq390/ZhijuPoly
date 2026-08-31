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
import { PREDICTABLE_PROPERTIES } from "../constants/predictableProperties";
import { usePredict } from "../hooks/usePredict";
import { useTgStructureCanvas } from "../hooks/useTgStructureCanvas";
import type { PredictableProperty, StructureWorkspaceContext } from "../types";
import "../styles/structure-workbench.css";
import "../styles/homopolymer-property-prediction.css";
import type { StructureCanvasOwnerHandle } from "./StructureWorkbenchPage";
import { StructureCanvasSurface } from "./structure-workbench/StructureCanvasSurface";
import {
  HomopolymerPredictionDrawer,
  type HomopolymerPredictionSnapshot
} from "./homopolymer-prediction/HomopolymerPredictionDrawer";
import { HomopolymerPredictionParameters } from "./homopolymer-prediction/HomopolymerPredictionParameters";

type HomopolymerPropertyPredictionPageProps = {
  structure: StructureWorkspaceContext;
};

const HOMOPOLYMER_EXAMPLE_SMILES = "*CC*";

function sameProperties(left: readonly PredictableProperty[], right: readonly PredictableProperty[]) {
  return left.length === right.length && left.every((property, index) => property === right[index]);
}

export const HomopolymerPropertyPredictionPage = forwardRef<
  StructureCanvasOwnerHandle,
  HomopolymerPropertyPredictionPageProps
>(function HomopolymerPropertyPredictionPage({ structure }, forwardedRef) {
  const [parametersOpen, setParametersOpen] = useState(false);
  const [selectedProperties, setSelectedProperties] = useState<PredictableProperty[]>(() => [
    ...PREDICTABLE_PROPERTIES
  ]);
  const [hasActivated3D, setHasActivated3D] = useState(false);
  const [hasAttempt, setHasAttempt] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerWidth, setDrawerWidth] = useState(380);
  const [snapshot, setSnapshot] = useState<HomopolymerPredictionSnapshot | null>(null);
  const parameterPanelRef = useRef<HTMLElement | null>(null);
  const parameterButtonRef = useRef<HTMLButtonElement | null>(null);
  const restoreFocusFrameRef = useRef<number | null>(null);
  const predict = usePredict();
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
      parameterPanelRef.current?.querySelector<HTMLElement>("input, button")?.focus();
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

  async function submitPrediction(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (selectedProperties.length === 0) return;
    const smiles = await canvas.resolveSmilesForSearch();
    if (!smiles) {
      setParametersOpen(true);
      return;
    }
    const properties = PREDICTABLE_PROPERTIES.filter((property) => selectedProperties.includes(property));
    setSnapshot({ smiles, properties });
    setHasAttempt(true);
    setDrawerOpen(true);
    closeParameters(false);
    try {
      await predict.submit({ smiles, properties });
    } catch {
      // usePredict owns the visible error and ignores cancelled or stale responses.
    }
  }

  function adjustParameters() {
    setDrawerOpen(false);
    setParametersOpen(true);
  }

  const stale = Boolean(
    snapshot &&
      (structure.smiles.trim() !== snapshot.smiles ||
        !sameProperties(selectedProperties, snapshot.properties))
  );
  const operationBusy = canvas.isBusy || predict.isLoading;
  const workbenchStyle = { "--np-sw-drawer-width": `${drawerWidth}px` } as CSSProperties;

  return (
    <div
      className="np-structure-workbench np-homopolymer-prediction"
      data-module="homopolymer-property-prediction"
      style={workbenchStyle}
    >
      <div className={`np-sw-page${drawerOpen ? " has-open-drawer" : ""}`}>
        <h1 className="np-sw-page-title">均聚物性质预测</h1>
        <div className={`np-sw-layout${drawerOpen ? " has-open-drawer" : ""}`}>
          <main className="np-sw-workspace">
            <StructureCanvasSurface
              structure={structure}
              canvas={canvas}
              hasActivated3D={hasActivated3D}
              operationBusy={operationBusy}
              editorTitle="均聚物性质预测结构编辑器"
              utilityActions={[
                {
                  id: "prediction-parameters",
                  label: "预测参数",
                  icon: <SlidersHorizontal aria-hidden="true" />,
                  active: parametersOpen,
                  buttonRef: parameterButtonRef,
                  controls: "homopolymer-prediction-parameters",
                  onClick: () => setParametersOpen((current) => !current)
                }
              ]}
              onLoadExample={() => canvas.loadStructure(HOMOPOLYMER_EXAMPLE_SMILES)}
              onImportFile={(file) => canvas.importImageFile(file)}
              onClear={() => canvas.clearCanvas()}
              onSync={() => canvas.syncSmilesFromCanvas()}
              onToggle3D={toggle3D}
            />

            <HomopolymerPredictionParameters
              open={parametersOpen}
              panelRef={parameterPanelRef}
              selectedProperties={selectedProperties}
              submitting={predict.isLoading}
              onClose={closeParameters}
              onSelectedPropertiesChange={setSelectedProperties}
              onSubmit={submitPrediction}
            />
          </main>

          <HomopolymerPredictionDrawer
            open={drawerOpen}
            hasAttempt={hasAttempt}
            width={drawerWidth}
            loading={predict.isLoading}
            error={predict.error}
            data={predict.data}
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
