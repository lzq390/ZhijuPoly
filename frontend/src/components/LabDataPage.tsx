import { type FormEvent, useEffect, useMemo, useState } from "react";
import { ArrowLeft, BarChart3, ClipboardList, Copy, Database, RefreshCw, Save } from "lucide-react";
import {
  createLabDataSampleMeasurement,
  fetchLabDataSampleMeasurements,
  fetchLabDataSummary,
  fetchLabDataTestProjects
} from "../services/api";
import type {
  LabDataSampleMeasurement,
  LabDataSampleMeasurementPayload,
  LabDataSampleMeasurementPage,
  LabDataSummary,
  LabDataTestProject
} from "../types";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";

export type LabDataView = "collect" | "dashboard";

type LabDataPageProps = {
  view: LabDataView;
  onBackHome: () => void;
  onChangeView: (view: LabDataView) => void;
};

type LabDataFormState = {
  sampleId: string;
  experimentProject: string;
  instrumentId: string;
  operator: string;
  collectionTime: string;
  resultValue: string;
  resultUnit: string;
  remarks: string;
};

type Notice = {
  type: "success" | "error";
  message: string;
};

const pageSize = 20;

const emptyForm: LabDataFormState = {
  sampleId: "",
  experimentProject: "",
  instrumentId: "",
  operator: "",
  collectionTime: "",
  resultValue: "",
  resultUnit: "",
  remarks: ""
};

const exampleForm: LabDataFormState = {
  sampleId: "SAMPLE-20260511-073",
  experimentProject: "Tg",
  instrumentId: "INST-TEMP-05",
  operator: "operator1",
  collectionTime: "2026-05-09T21:14:25.545",
  resultValue: "412.3500",
  resultUnit: "K",
  remarks: "无异常"
};

const requiredFields: Array<[keyof LabDataFormState, string]> = [
  ["sampleId", "样本编号"],
  ["experimentProject", "测试项目"],
  ["instrumentId", "仪器编号"],
  ["operator", "操作员"],
  ["collectionTime", "采集时间"],
  ["resultValue", "结果值"],
  ["resultUnit", "结果单位"]
];

function getProjectUnit(projects: LabDataTestProject[], projectName: string) {
  return projects.find((project) => project.projectName === projectName)?.resultUnit ?? "";
}

function buildPayload(form: LabDataFormState): LabDataSampleMeasurementPayload {
  return {
    sampleId: form.sampleId.trim(),
    experimentProject: form.experimentProject.trim(),
    instrumentId: form.instrumentId.trim(),
    operator: form.operator.trim(),
    collectionTime: form.collectionTime,
    temperature: null,
    concentration: null,
    resultValue: Number(form.resultValue),
    resultUnit: form.resultUnit.trim(),
    remarks: form.remarks.trim() || null
  };
}

function formatCollectionTime(value: string) {
  if (!value) {
    return "";
  }
  return value.replace("T", " ").slice(0, 19);
}

function formatResult(measurement: LabDataSampleMeasurement) {
  return `${measurement.resultValue ?? ""} ${measurement.resultUnit ?? ""}`.trim();
}

async function copyText(text: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  document.body.removeChild(textarea);
}

function LabDataNav({ view, onBackHome, onChangeView }: LabDataPageProps) {
  return (
    <nav className="flex flex-col gap-3 rounded-[26px] border border-white/70 bg-white/80 px-4 py-4 shadow-sm backdrop-blur md:flex-row md:items-center md:justify-between md:px-5">
      <div className="flex items-center gap-3">
        <Button type="button" variant="outline" onClick={onBackHome}>
          <ArrowLeft className="mr-2 h-4 w-4" />
          Home
        </Button>
        <div>
          <div className="text-[11px] font-medium uppercase tracking-[0.2em] text-teal-700/70">Current Module</div>
          <div className="font-heading text-lg font-semibold tracking-tight text-slate-950">Lab Data Collection</div>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2 rounded-2xl border border-white/70 bg-white/75 p-1">
        <button
          type="button"
          onClick={() => onChangeView("collect")}
          className={[
            "inline-flex min-h-10 items-center justify-center rounded-xl px-3 text-sm font-semibold transition",
            view === "collect" ? "bg-slate-950 text-white" : "text-slate-600 hover:bg-white"
          ].join(" ")}
        >
          <ClipboardList className="mr-2 h-4 w-4" />
          Collect
        </button>
        <button
          type="button"
          onClick={() => onChangeView("dashboard")}
          className={[
            "inline-flex min-h-10 items-center justify-center rounded-xl px-3 text-sm font-semibold transition",
            view === "dashboard" ? "bg-slate-950 text-white" : "text-slate-600 hover:bg-white"
          ].join(" ")}
        >
          <BarChart3 className="mr-2 h-4 w-4" />
          Dashboard
        </button>
      </div>
    </nav>
  );
}

function CollectView({ projects }: { projects: LabDataTestProject[] }) {
  const [form, setForm] = useState<LabDataFormState>(emptyForm);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const missingField = useMemo(
    () => requiredFields.find(([field]) => String(form[field]).trim() === ""),
    [form]
  );

  function updateField(name: keyof LabDataFormState, value: string) {
    setNotice(null);
    setForm((current) => {
      if (name === "experimentProject") {
        return {
          ...current,
          experimentProject: value,
          resultUnit: getProjectUnit(projects, value)
        };
      }
      return { ...current, [name]: value };
    });
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (missingField) {
      setNotice({ type: "error", message: `请填写${missingField[1]}。` });
      return;
    }

    setIsSubmitting(true);
    setNotice(null);
    try {
      const saved = await createLabDataSampleMeasurement(buildPayload(form));
      setNotice({ type: "success", message: `提交成功，记录 ID：${saved.id}` });
      setForm(emptyForm);
    } catch (error) {
      setNotice({ type: "error", message: error instanceof Error ? error.message : "提交失败，请稍后重试。" });
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <section className="rounded-[32px] border border-white/75 bg-white/85 p-5 shadow-sm backdrop-blur md:p-7">
      <div className="mb-6 flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="text-[11px] font-medium uppercase tracking-[0.2em] text-teal-700/70">/lab-data/collect</div>
          <h1 className="font-heading mt-2 text-[2rem] font-semibold tracking-tight text-slate-950">实验数据采集</h1>
        </div>
        <Badge className="bg-teal-50 text-teal-800">{projects.length} Projects</Badge>
      </div>

      <form onSubmit={handleSubmit} className="grid gap-5">
        <fieldset className="grid gap-4 rounded-2xl border border-slate-200/80 p-4 md:grid-cols-4">
          <legend className="px-2 text-sm font-semibold text-teal-800">元信息</legend>
          <label className="grid gap-2 text-sm font-semibold text-slate-700">
            样本编号 *
            <input
              className="min-h-12 rounded-2xl border border-slate-200 bg-white px-4 text-slate-950 outline-none transition focus:border-teal-600 focus:ring-4 focus:ring-teal-600/10"
              value={form.sampleId}
              onChange={(event) => updateField("sampleId", event.target.value)}
              placeholder="SAMPLE-20260511-073"
            />
          </label>
          <label className="grid gap-2 text-sm font-semibold text-slate-700">
            仪器编号 *
            <input
              className="min-h-12 rounded-2xl border border-slate-200 bg-white px-4 text-slate-950 outline-none transition focus:border-teal-600 focus:ring-4 focus:ring-teal-600/10"
              value={form.instrumentId}
              onChange={(event) => updateField("instrumentId", event.target.value)}
              placeholder="INST-TEMP-05"
            />
          </label>
          <label className="grid gap-2 text-sm font-semibold text-slate-700">
            操作员 *
            <input
              className="min-h-12 rounded-2xl border border-slate-200 bg-white px-4 text-slate-950 outline-none transition focus:border-teal-600 focus:ring-4 focus:ring-teal-600/10"
              value={form.operator}
              onChange={(event) => updateField("operator", event.target.value)}
              placeholder="operator1"
            />
          </label>
          <label className="grid gap-2 text-sm font-semibold text-slate-700">
            采集时间 *
            <input
              className="min-h-12 rounded-2xl border border-slate-200 bg-white px-4 text-slate-950 outline-none transition focus:border-teal-600 focus:ring-4 focus:ring-teal-600/10"
              type="datetime-local"
              step="0.001"
              value={form.collectionTime}
              onChange={(event) => updateField("collectionTime", event.target.value)}
            />
          </label>
        </fieldset>

        <fieldset className="grid gap-4 rounded-2xl border border-slate-200/80 p-4 md:grid-cols-[minmax(180px,260px)_140px_minmax(160px,220px)]">
          <legend className="px-2 text-sm font-semibold text-teal-800">性质采集</legend>
          <label className="grid gap-2 text-sm font-semibold text-slate-700">
            测试项目 *
            <select
              className="min-h-12 rounded-2xl border border-slate-200 bg-white px-4 text-slate-950 outline-none transition focus:border-teal-600 focus:ring-4 focus:ring-teal-600/10"
              value={form.experimentProject}
              onChange={(event) => updateField("experimentProject", event.target.value)}
            >
              <option value="">请选择</option>
              {projects.map((project) => (
                <option key={project.id} value={project.projectName}>
                  {project.projectName}
                </option>
              ))}
            </select>
          </label>
          <label className="grid gap-2 text-sm font-semibold text-slate-700">
            单位 *
            <input
              className="min-h-12 rounded-2xl border border-slate-200 bg-slate-50 px-4 text-slate-700 outline-none"
              value={form.resultUnit}
              readOnly
              placeholder="自动带出"
            />
          </label>
          <label className="grid gap-2 text-sm font-semibold text-slate-700">
            结果值 *
            <input
              className="min-h-12 rounded-2xl border border-slate-200 bg-white px-4 text-slate-950 outline-none transition focus:border-teal-600 focus:ring-4 focus:ring-teal-600/10"
              type="number"
              step="0.0001"
              value={form.resultValue}
              onChange={(event) => updateField("resultValue", event.target.value)}
              placeholder="412.3500"
            />
          </label>
        </fieldset>

        <label className="grid gap-2 text-sm font-semibold text-slate-700">
          备注
          <textarea
            className="min-h-24 rounded-2xl border border-slate-200 bg-white px-4 py-3 text-slate-950 outline-none transition focus:border-teal-600 focus:ring-4 focus:ring-teal-600/10"
            rows={3}
            value={form.remarks}
            onChange={(event) => updateField("remarks", event.target.value)}
            placeholder="无异常"
          />
        </label>

        {notice ? (
          <div
            className={[
              "rounded-2xl px-4 py-3 text-sm font-semibold",
              notice.type === "success" ? "bg-emerald-50 text-emerald-800" : "bg-rose-50 text-rose-800"
            ].join(" ")}
          >
            {notice.message}
          </div>
        ) : null}

        <div className="grid gap-3 sm:grid-cols-[1fr_auto_auto]">
          <div />
          <Button
            type="button"
            variant="outline"
            onClick={() => setForm({ ...exampleForm, resultUnit: getProjectUnit(projects, exampleForm.experimentProject) || "K" })}
          >
            <Database className="mr-2 h-4 w-4" />
            填充示例数据
          </Button>
          <Button type="submit" disabled={isSubmitting}>
            <Save className="mr-2 h-4 w-4" />
            {isSubmitting ? "提交中..." : "提交数据"}
          </Button>
        </div>
      </form>
    </section>
  );
}

function DashboardView() {
  const [summary, setSummary] = useState<LabDataSummary>({ totalCount: 0, byProject: [] });
  const [selectedProject, setSelectedProject] = useState("");
  const [page, setPage] = useState(1);
  const [recentOnly, setRecentOnly] = useState(false);
  const [measurements, setMeasurements] = useState<LabDataSampleMeasurementPage>({
    items: [],
    total: 0,
    page: 1,
    pageSize: pageSize
  });
  const [isSummaryLoading, setIsSummaryLoading] = useState(true);
  const [isDetailLoading, setIsDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [copyNotice, setCopyNotice] = useState<Notice | null>(null);
  const [isCopying, setIsCopying] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function loadSummary() {
      setIsSummaryLoading(true);
      setError("");
      try {
        const data = await fetchLabDataSummary();
        if (!cancelled) {
          setSummary(data);
          setSelectedProject((current) => current || data.byProject[0]?.experimentProject || "");
        }
      } catch (loadError) {
        if (!cancelled) {
          setError(loadError instanceof Error ? loadError.message : "统计数据加载失败。");
        }
      } finally {
        if (!cancelled) {
          setIsSummaryLoading(false);
        }
      }
    }
    loadSummary();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!selectedProject) {
      return;
    }

    let cancelled = false;
    async function loadDetails() {
      setIsDetailLoading(true);
      setError("");
      try {
        const data = await fetchLabDataSampleMeasurements({
          experimentProject: selectedProject,
          page,
          pageSize,
          recentDays: recentOnly ? 7 : undefined
        });
        if (!cancelled) {
          setMeasurements(data);
        }
      } catch (loadError) {
        if (!cancelled) {
          setError(loadError instanceof Error ? loadError.message : "明细数据加载失败。");
        }
      } finally {
        if (!cancelled) {
          setIsDetailLoading(false);
        }
      }
    }

    loadDetails();
    return () => {
      cancelled = true;
    };
  }, [page, recentOnly, selectedProject]);

  const maxCount = useMemo(() => Math.max(1, ...summary.byProject.map((item) => item.count)), [summary.byProject]);
  const pageCount = Math.max(1, Math.ceil(measurements.total / pageSize));

  function selectProject(project: string) {
    setSelectedProject(project);
    setPage(1);
    setCopyNotice(null);
  }

  function toggleRecentOnly() {
    setRecentOnly((current) => !current);
    setPage(1);
    setCopyNotice(null);
  }

  async function copyRecentRows() {
    if (!selectedProject) {
      return;
    }

    setIsCopying(true);
    setCopyNotice(null);
    try {
      const data = await fetchLabDataSampleMeasurements({
        experimentProject: selectedProject,
        page: 1,
        pageSize: 10000,
        recentDays: 7
      });
      const header = ["测试样品名称", "性质名称", "数值+单位", "remarks", "测试时间"];
      const rows = data.items.map((item) => [
        item.sampleId,
        item.experimentProject,
        formatResult(item),
        item.remarks ?? "",
        formatCollectionTime(item.collectionTime)
      ]);
      await copyText([header, ...rows].map((row) => row.join("\t")).join("\n"));
      setCopyNotice({ type: "success", message: `已复制 ${rows.length} 条近一周数据` });
    } catch (copyError) {
      setCopyNotice({ type: "error", message: copyError instanceof Error ? copyError.message : "复制失败。" });
    } finally {
      setIsCopying(false);
    }
  }

  return (
    <section className="grid gap-5">
      <div className="rounded-[32px] border border-white/75 bg-white/85 p-5 shadow-sm backdrop-blur md:p-7">
        <div className="mb-6 flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
          <div>
            <div className="text-[11px] font-medium uppercase tracking-[0.2em] text-teal-700/70">/lab-data/dashboard</div>
            <h1 className="font-heading mt-2 text-[2rem] font-semibold tracking-tight text-slate-950">统计展示</h1>
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge className="bg-teal-50 text-teal-800">Total {isSummaryLoading ? "--" : summary.totalCount}</Badge>
            <Badge className="bg-sky-50 text-sky-800">Projects {isSummaryLoading ? "--" : summary.byProject.length}</Badge>
          </div>
        </div>

        {error ? <div className="mb-4 rounded-2xl bg-rose-50 px-4 py-3 text-sm font-semibold text-rose-800">{error}</div> : null}

        {isSummaryLoading ? <p className="text-sm font-semibold text-slate-500">加载统计中...</p> : null}
        {!isSummaryLoading && summary.byProject.length === 0 ? (
          <p className="text-sm font-semibold text-slate-500">暂无采集数据</p>
        ) : null}
        {summary.byProject.length > 0 ? (
          <div className="grid min-h-[330px] grid-flow-col auto-cols-[minmax(84px,1fr)] items-end gap-4 overflow-x-auto border-b border-slate-200 px-2 pt-3">
            {summary.byProject.map((item) => (
              <button
                key={item.experimentProject}
                type="button"
                onClick={() => selectProject(item.experimentProject)}
                className="grid h-[300px] grid-rows-[28px_220px_40px] justify-items-center gap-2 rounded-2xl px-2 text-slate-700 transition hover:bg-slate-50"
              >
                <span className="font-mono-ui text-sm font-semibold text-teal-700">{item.count}</span>
                <span className="flex h-[220px] w-12 items-end overflow-hidden rounded-t-2xl bg-slate-100">
                  <span
                    className={[
                      "block w-full rounded-t-2xl transition-all",
                      selectedProject === item.experimentProject
                        ? "bg-gradient-to-t from-teal-800 to-amber-400"
                        : "bg-gradient-to-t from-teal-800 to-sky-300"
                    ].join(" ")}
                    style={{ height: `${(item.count / maxCount) * 100}%`, minHeight: 8 }}
                  />
                </span>
                <span className="text-center text-sm font-semibold leading-tight">{item.experimentProject}</span>
              </button>
            ))}
          </div>
        ) : null}
      </div>

      <div className="rounded-[32px] border border-white/75 bg-white/85 p-5 shadow-sm backdrop-blur md:p-7">
        <div className="mb-5 flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
          <div>
            <div className="text-[11px] font-medium uppercase tracking-[0.2em] text-teal-700/70">Measurements</div>
            <h2 className="font-heading mt-2 text-[1.45rem] font-semibold tracking-tight text-slate-950">
              {selectedProject ? `${selectedProject} 明细数据` : "明细数据"}
            </h2>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant={recentOnly ? "default" : "outline"} onClick={toggleRecentOnly} disabled={!selectedProject}>
              <RefreshCw className="mr-2 h-4 w-4" />
              近一周
            </Button>
            <Button type="button" onClick={copyRecentRows} disabled={!selectedProject || isCopying}>
              <Copy className="mr-2 h-4 w-4" />
              {isCopying ? "复制中..." : "复制近一周"}
            </Button>
          </div>
        </div>

        {copyNotice ? (
          <div
            className={[
              "mb-4 rounded-2xl px-4 py-3 text-sm font-semibold",
              copyNotice.type === "success" ? "bg-emerald-50 text-emerald-800" : "bg-rose-50 text-rose-800"
            ].join(" ")}
          >
            {copyNotice.message}
          </div>
        ) : null}

        <div className="overflow-x-auto">
          <table className="min-w-[860px] w-full border-collapse text-left text-sm">
            <thead>
              <tr className="border-b border-slate-200 text-slate-500">
                <th className="px-3 py-3 font-semibold">测试样品名称</th>
                <th className="px-3 py-3 font-semibold">性质名称</th>
                <th className="px-3 py-3 font-semibold">数值+单位</th>
                <th className="px-3 py-3 font-semibold">remarks</th>
                <th className="px-3 py-3 text-right font-semibold">测试时间</th>
              </tr>
            </thead>
            <tbody>
              {selectedProject && !isDetailLoading
                ? measurements.items.map((item) => (
                    <tr key={item.id} className="border-b border-slate-100">
                      <td className="px-3 py-3 font-semibold text-slate-800">{item.sampleId}</td>
                      <td className="px-3 py-3 text-slate-700">{item.experimentProject}</td>
                      <td className="px-3 py-3 text-slate-700">{formatResult(item)}</td>
                      <td className="px-3 py-3 text-slate-600">{item.remarks ?? ""}</td>
                      <td className="px-3 py-3 text-right text-slate-600">{formatCollectionTime(item.collectionTime)}</td>
                    </tr>
                  ))
                : null}
              {selectedProject && !isDetailLoading && measurements.items.length === 0 ? (
                <tr>
                  <td className="px-3 py-6 text-center font-semibold text-slate-500" colSpan={5}>
                    暂无数据
                  </td>
                </tr>
              ) : null}
              {!selectedProject ? (
                <tr>
                  <td className="px-3 py-6 text-center font-semibold text-slate-500" colSpan={5}>
                    暂无数据
                  </td>
                </tr>
              ) : null}
              {isDetailLoading ? (
                <tr>
                  <td className="px-3 py-6 text-center font-semibold text-slate-500" colSpan={5}>
                    加载明细中...
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>

        {selectedProject ? (
          <div className="mt-5 flex flex-col gap-3 text-sm font-semibold text-slate-600 md:flex-row md:items-center md:justify-between">
            <span>
              共 {measurements.total} 条，第 {page} / {pageCount} 页
            </span>
            <div className="flex gap-2">
              <Button type="button" variant="outline" disabled={page <= 1} onClick={() => setPage((current) => current - 1)}>
                上一页
              </Button>
              <Button type="button" variant="outline" disabled={page >= pageCount} onClick={() => setPage((current) => current + 1)}>
                下一页
              </Button>
            </div>
          </div>
        ) : null}
      </div>
    </section>
  );
}

export function LabDataPage({ view, onBackHome, onChangeView }: LabDataPageProps) {
  const [projects, setProjects] = useState<LabDataTestProject[]>([]);
  const [projectError, setProjectError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function loadProjects() {
      try {
        const data = await fetchLabDataTestProjects();
        if (!cancelled) {
          setProjects(data);
        }
      } catch (error) {
        if (!cancelled) {
          setProjectError(error instanceof Error ? error.message : "测试项目加载失败。");
        }
      }
    }
    loadProjects();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="grid gap-5">
      <LabDataNav view={view} onBackHome={onBackHome} onChangeView={onChangeView} />
      {projectError ? (
        <div className="rounded-2xl bg-rose-50 px-4 py-3 text-sm font-semibold text-rose-800">{projectError}</div>
      ) : null}
      {view === "collect" ? <CollectView projects={projects} /> : <DashboardView />}
    </div>
  );
}
