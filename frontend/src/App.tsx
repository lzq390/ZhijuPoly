import { useState } from "react";
import { Atom, Database, Microscope, Sparkles } from "lucide-react";
import { KetcherEditor } from "./components/KetcherEditor";
import { Layout } from "./components/Layout";
import { QueryPanel } from "./components/QueryPanel";
import { ResultsDisplay } from "./components/ResultsDisplay";
import { StructurePreview3D } from "./components/StructurePreview3D";
import { Badge } from "./components/ui/badge";
import { useKetcher } from "./hooks/useKetcher";
import { usePredict } from "./hooks/usePredict";
import { useQuery } from "./hooks/useQuery";
import {
  type PredictableProperty,
  type ResultsTab,
  type WorkspaceMode
} from "./types";

export default function App() {
  const { smiles, setSmiles, iframeRef, setIsReady } = useKetcher("*CC*");
  const { request, setRequest, isLoading, error, data, submit } = useQuery();
  const predict = usePredict();
  const [panelMode, setPanelMode] = useState<WorkspaceMode>("query");
  const [activeResultsTab, setActiveResultsTab] = useState<ResultsTab>("query");
  const [selectedProperties, setSelectedProperties] = useState<PredictableProperty[]>([]);

  const canQuery =
    !isLoading &&
    smiles.trim().length > 0 &&
    (request.match_mode === "structure" || request.property_name !== null);
  const canPredict = !predict.isLoading && smiles.trim().length > 0 && selectedProperties.length > 0;

  const activeMode =
    panelMode === "predict"
      ? "Property prediction"
      : request.match_mode === "property"
        ? "性质相似匹配"
        : "结构相似匹配";
  const activeModeLabel =
    panelMode === "predict"
      ? "Property Prediction"
      : request.match_mode === "property"
        ? "Property Similarity"
        : "Structural Similarity";

  const resultCount =
    activeResultsTab === "predict" ? Object.keys(predict.data?.predictions ?? {}).length : data?.total ?? 0;
  const resultTiming =
    activeResultsTab === "predict" ? predict.data?.query_time_ms : data?.query_time_ms;

  async function handleQuerySubmit() {
    setActiveResultsTab("query");
    await submit({ ...request, smiles });
  }

  async function handlePredictSubmit() {
    setActiveResultsTab("predict");
    try {
      await predict.submit({
        smiles,
        properties: selectedProperties
      });
    } catch {
      // Error state is already captured by the hook and shown in the results panel.
    }
  }

  const resultPanelTitle = activeResultsTab === "predict" ? "预测结果面板" : "相似匹配结果面板";
  const resultPanelDescription =
    activeResultsTab === "predict"
      ? "模型推理完成后，这里会显示所选性质的预测结果与耗时。"
      : "运行相似匹配后，这里会显示摘要、2D 结构图、SMILES 和相似度。";
  const resultPrimaryBadge =
    activeResultsTab === "predict"
      ? predict.data
        ? `${Object.keys(predict.data.predictions).length} predictions`
        : "No predictions"
      : data
        ? `${data.total} records`
        : "No results";
  const resultSecondaryBadge =
    activeResultsTab === "predict"
      ? predict.isLoading
        ? "Predicting"
        : "Prediction mode"
      : request.match_mode === "property"
        ? "性质相似匹配"
        : "结构相似匹配";

  return (
    <Layout>
      <section className="hero-glow mesh-surface relative overflow-hidden rounded-[36px] border border-white/70 px-6 py-6 md:px-8 md:py-8">
        <div className="pointer-events-none absolute inset-y-0 right-0 hidden w-[36%] bg-[radial-gradient(circle_at_center,rgba(15,118,110,0.14),transparent_58%)] lg:block" />
        <div className="pointer-events-none absolute -right-10 top-12 h-40 w-40 rounded-full border border-white/40 bg-white/20 blur-2xl" />
        <div className="pointer-events-none absolute left-8 top-24 h-24 w-24 rounded-full bg-teal-300/20 blur-3xl" />

        <div className="animate-fade-up">
          <div className="flex flex-wrap items-center gap-3">
            <div className="rounded-full border border-white/80 bg-white/80 px-4 py-2 text-sm font-semibold tracking-[0.16em] text-slate-950 shadow-sm">
              POLYPROP
            </div>
            <Badge>Polymer Similarity Matching & Property Prediction</Badge>
          </div>

          <div className="mt-6 overflow-x-auto">
            <h1 className="font-heading whitespace-nowrap text-[2.5rem] font-semibold tracking-[-0.04em] text-slate-950 md:text-[4rem] md:leading-[0.95]">
              Polymer Property Explorer
            </h1>
            <p className="mt-4 whitespace-nowrap text-base leading-7 text-slate-600 md:text-lg">
              Bring structure editing, similarity matching, 3D review, and property prediction into one focused research workspace.
            </p>
          </div>

          <div className="mt-8 grid gap-3 md:grid-cols-3">
            <div className="flex min-h-[188px] flex-col justify-center rounded-[26px] border border-white/80 bg-white/80 p-5 text-center shadow-sm backdrop-blur">
              <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-mutedForeground">
                {panelMode === "predict" ? <Sparkles className="h-4 w-4 text-teal-600" /> : <Atom className="h-4 w-4 text-teal-600" />}
                Current Mode
              </div>
              <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight text-slate-950">
                {activeModeLabel}
              </div>
              <div className="mt-2 text-sm leading-6 text-mutedForeground">
                {panelMode === "predict"
                  ? "Select target properties in the control card and send the current structure to the prediction models."
                  : "Switch between structural similarity and property similarity matching in the control card."}
              </div>
            </div>

            <div className="flex min-h-[188px] flex-col justify-center rounded-[26px] border border-white/80 bg-white/80 p-5 text-center shadow-sm backdrop-blur">
              <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-mutedForeground">
                <Microscope className="h-4 w-4 text-sky-600" />
                Structure Input
              </div>
              <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight text-slate-950">
                {smiles.trim().length > 0 ? "Ready" : "Waiting"}
              </div>
              <div className="mt-2 text-sm leading-6 text-mutedForeground">
                Editor content syncs into the SMILES fallback input as the source structure for matching or prediction.
              </div>
            </div>

            <div className="flex min-h-[188px] flex-col justify-center rounded-[26px] border border-white/80 bg-slate-950 p-5 text-center text-slate-50 shadow-[0_22px_50px_rgba(8,17,31,0.2)]">
              <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-slate-400">
                <Database className="h-4 w-4 text-teal-300" />
                Latest Results
              </div>
              <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight">{resultCount}</div>
              <div className="mt-2 text-sm leading-6 text-slate-300">
                {resultTiming ? `${resultTiming.toFixed(1)} ms returned` : "Result count and latency appear after execution."}
              </div>
            </div>
          </div>
        </div>
      </section>

      <section>
        <div className="grid items-stretch gap-6 xl:grid-cols-[minmax(0,1.22fr)_minmax(0,0.92fr)]">
          <div className="min-w-0">
            <KetcherEditor
              smiles={smiles}
              iframeRef={iframeRef}
              onReadyChange={setIsReady}
              onChange={(value) => {
                setSmiles(value);
                setRequest({ ...request, smiles: value });
              }}
            />
          </div>

          <div className="flex min-w-0 flex-col gap-6">
            <StructurePreview3D smiles={smiles} />
            <QueryPanel
              className="w-full self-start"
              mode={panelMode}
              onModeChange={setPanelMode}
              request={{ ...request, smiles }}
              onChange={setRequest}
              onQuerySubmit={handleQuerySubmit}
              onPredictSubmit={handlePredictSubmit}
              selectedProperties={selectedProperties}
              onSelectedPropertiesChange={setSelectedProperties}
              queryDisabled={!canQuery}
              predictDisabled={!canPredict}
              isQueryLoading={isLoading}
              isPredicting={predict.isLoading}
            />
          </div>
        </div>
      </section>

      <section className="relative pt-2">
        <div className="absolute left-0 right-0 top-0 h-px bg-gradient-to-r from-transparent via-slate-400/40 to-transparent" />
        <div className="pt-6">
          <div className="overflow-hidden rounded-[32px] border border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(243,248,250,0.92)_100%)] shadow-soft">
            <div className="border-b border-slate-200/80 px-6 py-5 md:px-8">
              <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
                <div>
                  <div className="text-xs font-medium uppercase tracking-[0.18em] text-teal-700/70">Results</div>
                  <h2 className="font-heading mt-2 text-[1.8rem] font-semibold tracking-tight text-slate-950">
                    {resultPanelTitle}
                  </h2>
                  <p className="mt-1 text-sm leading-6 text-mutedForeground">{resultPanelDescription}</p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <Badge className="bg-slate-100 text-slate-700">{resultPrimaryBadge}</Badge>
                  <Badge className="bg-slate-100 text-slate-700">{resultSecondaryBadge}</Badge>
                </div>
              </div>
            </div>
            <div className="px-4 py-4 md:px-5 md:py-5">
              <ResultsDisplay
                data={data}
                error={error}
                isLoading={isLoading}
                request={{ ...request, smiles }}
                predictData={predict.data}
                isPredicting={predict.isLoading}
                predictError={predict.error}
                activeTab={activeResultsTab}
                onTabChange={setActiveResultsTab}
              />
            </div>
          </div>
        </div>
      </section>
    </Layout>
  );
}
