import { ArrowRight, Atom, Database, Microscope } from "lucide-react";
import { KetcherEditor } from "./components/KetcherEditor";
import { Layout } from "./components/Layout";
import { QueryPanel } from "./components/QueryPanel";
import { ResultsDisplay } from "./components/ResultsDisplay";
import { StructurePreview3D } from "./components/StructurePreview3D";
import { Badge } from "./components/ui/badge";
import { useKetcher } from "./hooks/useKetcher";
import { useQuery } from "./hooks/useQuery";

export default function App() {
  const { smiles, setSmiles, iframeRef, setIsReady } = useKetcher("*CC*");
  const { request, setRequest, isLoading, error, data, submit } = useQuery();
  const canRun = !isLoading && smiles.trim().length > 0;
  const resultCount = data?.total ?? 0;
  const activeMode = request.match_mode === "similarity" ? "Similarity retrieval" : "Exact retrieval";

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
            <Badge>聚合物结构检索</Badge>
          </div>

          <div className="mt-6 max-w-4xl">
            <h1 className="font-heading text-balance max-w-4xl text-[2.5rem] font-semibold tracking-[-0.04em] text-slate-950 md:text-[4rem] md:leading-[0.95]">
              分子结构检索与属性分析工作台
            </h1>
            <p className="mt-4 max-w-2xl text-base leading-7 text-slate-600 md:text-lg">
              让结构编辑、相似度检索、三维核对和属性分组分析汇聚到同一张科研操作台上，
              用更清晰的视觉层次承接高密度信息。
            </p>
          </div>

          <div className="mt-8 grid gap-3 md:grid-cols-3">
            <div className="flex min-h-[188px] flex-col justify-center rounded-[26px] border border-white/80 bg-white/80 p-5 text-center shadow-sm backdrop-blur">
              <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-mutedForeground">
                <Atom className="h-4 w-4 text-teal-600" />
                当前模式
              </div>
              <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight text-slate-950">
                {activeMode}
              </div>
              <div className="mt-2 text-sm leading-6 text-mutedForeground">
                控制卡中可切换精确匹配或相似度检索，并同步更新参数摘要。
              </div>
            </div>

            <div className="flex min-h-[188px] flex-col justify-center rounded-[26px] border border-white/80 bg-white/80 p-5 text-center shadow-sm backdrop-blur">
              <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-mutedForeground">
                <Microscope className="h-4 w-4 text-sky-600" />
                结构输入
              </div>
              <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight text-slate-950">
                {smiles.trim().length > 0 ? "已准备" : "等待输入"}
              </div>
              <div className="mt-2 text-sm leading-6 text-mutedForeground">
                编辑器内容会同步到 SMILES 文本回退输入，作为本次检索的主结构来源。
              </div>
            </div>

            <div className="flex min-h-[188px] flex-col justify-center rounded-[26px] border border-white/80 bg-slate-950 p-5 text-center text-slate-50 shadow-[0_22px_50px_rgba(8,17,31,0.2)]">
              <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-slate-400">
                <Database className="h-4 w-4 text-teal-300" />
                最近结果
              </div>
              <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight">
                {resultCount}
              </div>
              <div className="mt-2 text-sm leading-6 text-slate-300">
                {data ? `${data.query_time_ms.toFixed(1)} ms 返回` : "查询完成后显示结果规模与耗时"}
              </div>
            </div>
          </div>
        </div>
      </section>

      <section className="space-y-5">
        <div className="grid gap-6 xl:grid-cols-[minmax(0,1.22fr)_minmax(0,0.92fr)]">
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-teal-700/80">
                Primary Workspace
              </div>
              <h2 className="font-heading mt-2 text-[1.55rem] font-semibold tracking-tight text-slate-950">主工作区</h2>
              <p className="mt-1 text-sm text-mutedForeground">
                结构编辑作为主视图，输入与同步操作围绕编辑器展开。
              </p>
            </div>
            <div className="hidden items-center gap-2 rounded-full border border-white/80 bg-white/80 px-4 py-2 text-sm text-slate-600 shadow-sm backdrop-blur lg:flex">
              结构输入
              <ArrowRight className="h-4 w-4 text-slate-400" />
              参数控制
              <ArrowRight className="h-4 w-4 text-slate-400" />
              结果分析
            </div>
          </div>

          <div>
            <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-sky-700/80">
              Secondary Surface
            </div>
            <h2 className="font-heading mt-2 text-[1.55rem] font-semibold tracking-tight text-slate-950">辅助面板</h2>
            <p className="mt-1 text-sm text-mutedForeground">三维预览与查询控制保持统一起始线和栅格边界。</p>
          </div>
        </div>

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
              request={{ ...request, smiles }}
              onChange={setRequest}
              onSubmit={() => submit({ ...request, smiles })}
              disabled={!canRun}
              isLoading={isLoading}
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
                  <div className="text-xs font-medium uppercase tracking-[0.18em] text-teal-700/70">
                    Results
                  </div>
                  <h2 className="font-heading mt-2 text-[1.8rem] font-semibold tracking-tight text-slate-950">
                    查询结果面板
                  </h2>
                  <p className="mt-1 text-sm leading-6 text-mutedForeground">
                    运行查询后，这里会显示摘要、命中记录和属性分组。
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <Badge className="bg-slate-100 text-slate-700">
                    {data ? `${data.total} records` : "No results"}
                  </Badge>
                  <Badge className="bg-slate-100 text-slate-700">
                    {request.match_mode === "similarity" ? "Similarity mode" : "Exact mode"}
                  </Badge>
                </div>
              </div>
            </div>
            <div className="px-4 py-4 md:px-5 md:py-5">
              <ResultsDisplay
                data={data}
                error={error}
                isLoading={isLoading}
                request={{ ...request, smiles }}
              />
            </div>
          </div>
        </div>
      </section>
    </Layout>
  );
}
