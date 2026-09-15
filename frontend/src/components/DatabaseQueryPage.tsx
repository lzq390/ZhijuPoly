import { BrowsingRecordingControls } from "./browsing-recording/BrowsingRecording";
import { ModulePageHeader } from "./ModulePageHeader";
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
import { useTgStructureCanvas } from "../hooks/useTgStructureCanvas";
import { lookupSmilesInDatabase } from "../services/api";
import type {
  SmilesLookupResponse,
  SmilesLookupTable,
  StructureWorkspaceContext
} from "../types";
import "../styles/structure-workbench.css";
import "../styles/database-query.css";
import type { StructureCanvasOwnerHandle } from "./StructureWorkbenchPage";
import {
  DatabaseQueryDrawer,
  type DatabaseQuerySnapshot
} from "./database-query/DatabaseQueryDrawer";
import { DatabaseQueryParameters } from "./database-query/DatabaseQueryParameters";
import { DATABASE_QUERY_TABLE_META } from "./database-query/config";
import { StructureCanvasSurface } from "./structure-workbench/StructureCanvasSurface";

type DatabaseQueryPageProps = {
  structure: StructureWorkspaceContext;
};

const DATABASE_QUERY_EXAMPLE_SMILES = "*CC*";

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === "AbortError";
}

export const DatabaseQueryPage = forwardRef<
  StructureCanvasOwnerHandle,
  DatabaseQueryPageProps
>(function DatabaseQueryPage({ structure }, forwardedRef) {
  const [parametersOpen, setParametersOpen] = useState(false);
  const [selectedTable, setSelectedTable] = useState<SmilesLookupTable>("polymers");
  const [hasActivated3D, setHasActivated3D] = useState(false);
  const [isPreparing, setIsPreparing] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [hasAttempt, setHasAttempt] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerWidth, setDrawerWidth] = useState(380);
  const [snapshot, setSnapshot] = useState<DatabaseQuerySnapshot | null>(null);
  const [data, setData] = useState<SmilesLookupResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [propertyNameFilter, setPropertyNameFilter] = useState("");
  const parameterPanelRef = useRef<HTMLElement | null>(null);
  const parameterButtonRef = useRef<HTMLButtonElement | null>(null);
  const restoreFocusFrameRef = useRef<number | null>(null);
  const requestAbortRef = useRef<AbortController | null>(null);
  const requestRevisionRef = useRef(0);
  const mountedRef = useRef(true);
  const canvas = useTgStructureCanvas({
    structure,
    onStructureChanged: useCallback(() => undefined, [])
  });

  const closeParameters = useCallback((restoreFocus = true) => {
    setParametersOpen(false);
    if (!restoreFocus) return;
    if (restoreFocusFrameRef.current !== null) {
      window.cancelAnimationFrame(restoreFocusFrameRef.current);
    }
    restoreFocusFrameRef.current = window.requestAnimationFrame(() => {
      restoreFocusFrameRef.current = null;
      parameterButtonRef.current?.focus();
    });
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      requestRevisionRef.current += 1;
      requestAbortRef.current?.abort();
      requestAbortRef.current = null;
      if (restoreFocusFrameRef.current !== null) {
        window.cancelAnimationFrame(restoreFocusFrameRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!parametersOpen) return;
    const frame = window.requestAnimationFrame(() => {
      parameterPanelRef.current?.querySelector<HTMLElement>("button, input")?.focus();
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
      syncBeforeLeave: canvas.syncBeforeLeave
    }),
    [canvas]
  );

  async function toggle3D() {
    const activating = !canvas.isFlipped;
    const changed = await canvas.toggle3D();
    if (changed && activating) setHasActivated3D(true);
  }

  async function submitDatabaseQuery(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isPreparing || isLoading) return;

    const hadAttempt = hasAttempt;
    setIsPreparing(true);
    setHasAttempt(true);
    setDrawerOpen(true);
    closeParameters(false);
    try {
      const smiles = await canvas.resolveSmilesForSearch();
      if (!mountedRef.current) return;
      if (!smiles) {
        setHasAttempt(hadAttempt);
        setDrawerOpen(false);
        setParametersOpen(true);
        return;
      }

      const nextSnapshot: DatabaseQuerySnapshot = { smiles, table: selectedTable };
      const revision = requestRevisionRef.current + 1;
      requestRevisionRef.current = revision;
      requestAbortRef.current?.abort();
      const controller = new AbortController();
      requestAbortRef.current = controller;

      setSnapshot(nextSnapshot);
      setData(null);
      setError(null);
      setPropertyNameFilter("");
      setIsPreparing(false);
      setIsLoading(true);
      try {
        const response = await lookupSmilesInDatabase(
          { smiles, table: selectedTable },
          controller.signal
        );
        if (requestRevisionRef.current !== revision || controller.signal.aborted) return;
        setData(response);
        canvas.setFeedback(
          response.exists
            ? `查询完成：在 ${DATABASE_QUERY_TABLE_META[selectedTable].shortLabel} 中找到 ${response.total} 条精确匹配。`
            : `查询完成：${DATABASE_QUERY_TABLE_META[selectedTable].shortLabel} 中未找到精确匹配。`
        );
      } catch (requestError) {
        if (requestRevisionRef.current !== revision || isAbortError(requestError)) return;
        setError(requestError instanceof Error ? requestError.message : "数据库查询失败，请稍后重试。");
        canvas.setFeedback("数据库查询失败，详情已显示在结果抽屉中。");
      } finally {
        if (requestRevisionRef.current === revision) {
          if (requestAbortRef.current === controller) requestAbortRef.current = null;
          setIsLoading(false);
        }
      }
    } finally {
      if (mountedRef.current) setIsPreparing(false);
    }
  }

  function adjustParameters() {
    setDrawerOpen(false);
    setParametersOpen(true);
  }

  const stale = Boolean(
    snapshot &&
      (structure.smiles.trim() !== snapshot.smiles || selectedTable !== snapshot.table)
  );
  const operationBusy = canvas.isBusy || isPreparing || isLoading;
  const workbenchStyle = { "--np-sw-drawer-width": `${drawerWidth}px` } as CSSProperties;

  return (
    <div
      className="np-module-page np-structure-workbench np-database-query"
      data-module="database-query"
      style={workbenchStyle}
    >
      <ModulePageHeader actions={<BrowsingRecordingControls module="databaseQuery" />}>数据库查询</ModulePageHeader>
      <div className={`np-sw-page np-module-page-body${drawerOpen ? " has-open-drawer" : ""}`}>
        <div className={`np-sw-layout${drawerOpen ? " has-open-drawer" : ""}`}>
          <main className="np-sw-workspace">
            <StructureCanvasSurface
              structure={structure}
              canvas={canvas}
              hasActivated3D={hasActivated3D}
              operationBusy={operationBusy}
              editorTitle="数据库查询结构编辑器"
              utilityActions={[
                {
                  id: "database-query-parameters",
                  label: "查询参数",
                  icon: <SlidersHorizontal aria-hidden="true" />,
                  active: parametersOpen,
                  buttonRef: parameterButtonRef,
                  controls: "database-query-parameters",
                  onClick: () => setParametersOpen((current) => !current)
                }
              ]}
              onLoadExample={() => canvas.loadStructure(DATABASE_QUERY_EXAMPLE_SMILES)}
              onImportFile={(file) => canvas.importImageFile(file)}
              onClear={() => canvas.clearCanvas()}
              onSync={() => canvas.syncSmilesFromCanvas()}
              onToggle3D={toggle3D}
            />

            <DatabaseQueryParameters
              open={parametersOpen}
              panelRef={parameterPanelRef}
              selectedTable={selectedTable}
              submitting={isPreparing || isLoading}
              onClose={closeParameters}
              onTableChange={(table) => {
                setSelectedTable(table);
                if (table !== "properties") setPropertyNameFilter("");
              }}
              onSubmit={submitDatabaseQuery}
            />
          </main>

          <DatabaseQueryDrawer
            open={drawerOpen}
            hasAttempt={hasAttempt}
            width={drawerWidth}
            preparing={isPreparing}
            loading={isLoading}
            error={error}
            data={data}
            snapshot={snapshot}
            stale={stale}
            propertyNameFilter={propertyNameFilter}
            onPropertyNameFilterChange={setPropertyNameFilter}
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
