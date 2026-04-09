import {
  Activity,
  ArrowRight,
  Beaker,
  Database,
  Orbit,
  ScanSearch,
  SlidersHorizontal
} from "lucide-react";
import { KetcherEditor } from "./components/KetcherEditor";
import { Layout } from "./components/Layout";
import { OverviewCard } from "./components/OverviewCard";
import { QueryPanel } from "./components/QueryPanel";
import { ResultsDisplay } from "./components/ResultsDisplay";
import { StructurePreview3D } from "./components/StructurePreview3D";
import { Badge } from "./components/ui/badge";
import { useKetcher } from "./hooks/useKetcher";
import { useQuery } from "./hooks/useQuery";

const workflowSteps = [
  {
    icon: Beaker,
    title: "编辑结构",
    description: "绘制目标结构"
  },
  {
    icon: SlidersHorizontal,
    title: "配置参数",
    description: "设置阈值与规模"
  },
  {
    icon: ScanSearch,
    title: "运行查询",
    description: "提交当前请求"
  },
  {
    icon: Database,
    title: "查看结果",
    description: "浏览属性分组"
  }
];

export default function App() {
  const { smiles, setSmiles, iframeRef, setIsReady } = useKetcher("*CC*");
  const { request, setRequest, isLoading, error, data, submit } = useQuery();
  const canRun = !isLoading && smiles.trim().length > 0;
  const resultCount = data?.total ?? 0;

  return (
    <Layout>
      <section className="overflow-hidden rounded-[30px] border border-slate-200/80 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(247,250,253,0.96)_100%)] shadow-soft">
        <div className="border-b border-slate-200/80 px-6 py-5 md:px-8">
          <div className="flex flex-wrap items-center gap-3">
            <div className="rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm font-semibold text-slate-950">
              PolyProp
            </div>
            <Badge className="bg-blue-50 text-blue-700">Molecular Retrieval Workspace</Badge>
          </div>
          <div className="mt-4 max-w-4xl">
            <h1 className="text-[2rem] font-semibold tracking-tight text-slate-950 md:text-[2.2rem]">
              分子结构检索与属性分析工作台
            </h1>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-600">
              聚合结构编辑、参数控制、三维预览和属性结果浏览的专业查询界面。
            </p>
          </div>
        </div>

        <div className="border-b border-slate-200/80 px-6 py-4 md:px-8">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {workflowSteps.map((step, index) => {
              const Icon = step.icon;
              return (
                <div
                  key={step.title}
                  className="grid min-h-[88px] grid-cols-[40px_minmax(0,1fr)_auto] items-center gap-3 rounded-[20px] border border-slate-200/80 bg-white/90 px-4 py-3"
                >
                  <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-slate-950 text-white">
                    <Icon className="h-4 w-4" />
                  </div>
                  <div className="min-w-0">
                    <div className="text-sm font-semibold text-slate-950">{step.title}</div>
                    <div className="line-clamp-1 text-xs leading-5 text-mutedForeground">
                      {step.description}
                    </div>
                  </div>
                  <div className="text-[11px] font-medium uppercase tracking-[0.16em] text-slate-400">
                    0{index + 1}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        <div className="grid auto-rows-fr gap-4 px-6 py-4 md:grid-cols-3 md:px-8">
          <OverviewCard
            title="查询状态"
            value={isLoading ? "运行中" : canRun ? "已就绪" : "待输入"}
            detail={smiles.trim().length > 0 ? "结构已同步" : "等待结构输入"}
            icon={<Activity className="h-4 w-4 text-blue-300" />}
            dark
          />
          <OverviewCard
            title="当前策略"
            value={request.match_mode === "similarity" ? "Similarity" : "Exact"}
            detail="当前查询参数"
            tags={[`Threshold ${request.similarity_threshold}`, `Top K ${request.top_k}`]}
          />
          <OverviewCard
            title="最近结果"
            value={String(resultCount)}
            detail={data ? `${data.query_time_ms.toFixed(1)} ms` : "尚未查询"}
            icon={<Orbit className="h-4 w-4 text-blue-600" />}
          />
        </div>
      </section>

      <section className="space-y-4">
        <div className="grid gap-6 xl:grid-cols-[minmax(0,1.22fr)_minmax(0,0.92fr)]">
          <div className="flex items-center justify-between gap-4">
            <div>
              <h2 className="text-lg font-semibold text-slate-950">主工作区</h2>
              <p className="mt-1 text-sm text-mutedForeground">
                结构编辑作为主视图，输入与同步操作围绕编辑器展开。
              </p>
            </div>
            <div className="hidden items-center gap-2 rounded-full border border-slate-200 bg-white px-4 py-2 text-sm text-slate-600 lg:flex">
              结构输入
              <ArrowRight className="h-4 w-4 text-slate-400" />
              参数控制
              <ArrowRight className="h-4 w-4 text-slate-400" />
              结果分析
            </div>
          </div>

          <div>
            <h2 className="text-lg font-semibold text-slate-950">辅助面板</h2>
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
              className="flex-1"
              request={{ ...request, smiles }}
              onChange={setRequest}
              onSubmit={() => submit({ ...request, smiles })}
              disabled={!canRun}
              isLoading={isLoading}
            />
          </div>
        </div>
      </section>

      <section className="relative">
        <div className="absolute left-0 right-0 top-0 h-px bg-gradient-to-r from-transparent via-slate-300/80 to-transparent" />
        <div className="pt-6">
          <div className="overflow-hidden rounded-[30px] border border-slate-200/80 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(246,249,253,0.98)_100%)] shadow-soft">
            <div className="border-b border-slate-200/80 px-6 py-5 md:px-8">
              <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
                <div>
                  <div className="text-xs font-medium uppercase tracking-[0.18em] text-slate-400">
                    Results
                  </div>
                  <h2 className="mt-2 text-2xl font-semibold tracking-tight text-slate-950">
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
