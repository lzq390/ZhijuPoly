import {
  ArrowLeft,
  BadgeCheck,
  ClipboardCheck,
  ClipboardList,
  Database,
  Plus,
  Save,
  Send,
  TestTube2,
} from "lucide-react";
import { type ChangeEvent, type ReactNode, useEffect, useMemo, useState } from "react";
import { cn } from "../lib/utils";
import "./ExperimentWorkflowDemoPage.css";

type DemoTab = "dispatch" | "acceptance" | "tests" | "review" | "medicine";
type ReagentCategory = "polyol" | "isocyanate" | "chainExtender";
type CalculationSystemId = "polyurethane" | "polyimide";
type MedicineSystemId = "polyurethane" | "polyimide";
type MedicineTypeId = "polyol" | "isocyanate" | "chainExtender" | "dianhydride" | "diamine";

type Reagent = {
  id: string;
  name: string;
  category: ReagentCategory;
  molecularWeight: number;
};

type Ingredient = {
  id: string;
  category: string;
  name: string;
  molecularWeight: number;
  ratio: number;
  mmol: number;
  theoreticalMass: number;
};

type ReactionCondition = {
  stage: string;
  time: string;
  temperature: string;
  note: string;
};

type DemoProject = {
  id: string;
  code: string;
  name: string;
  tasks: DemoTask[];
};

type DemoTask = {
  id: string;
  code: string;
  title: string;
  goal: string;
  note: string;
  systemId: CalculationSystemId;
};

type ExperimentRecord = {
  id: string;
  projectId: string;
  taskId: string;
  taskCode: string;
  taskTitle: string;
  assigneeId: string;
  assigneeName: string;
  createdAt: string;
  systemId: CalculationSystemId;
  experimentNo: string;
  title: string;
  note: string;
  ratios: {
    polyol: number;
    isocyanate: number;
    chainExtender: number;
  };
  ingredients: Ingredient[];
  conditions: ReactionCondition[];
  totalMass: number;
};

type AcceptanceWeight = {
  ingredientId: string;
  theoreticalMass: number;
  actualMass: number;
  difference: number;
  differencePercent: number;
  isOutOfTolerance: boolean;
};

type AcceptanceRecord = {
  experimentId: string;
  submittedAt: string;
  note?: string;
  weights: AcceptanceWeight[];
};

type TestAssignment = {
  id: string;
  name: string;
  category: string;
  method: string;
  unit: string;
};

type TestRecord = {
  experimentId: string;
  testId: string;
  value: string;
  unit: string;
  method: string;
  note?: string;
  attachmentName?: string;
  attachmentType?: string;
  attachmentSize?: number;
  submittedAt: string;
};

type MedicineEntry = {
  id: string;
  systemId: MedicineSystemId;
  typeId: MedicineTypeId;
  typeLabel: string;
  name: string;
  molecularWeight: number;
  structureText: string;
  createdAt: string;
};

type WeightDraft = Record<string, string>;

const EXPERIMENTS_KEY = "polyprop_experiment_demo_experiments";
const ACCEPTANCE_KEY = "polyprop_experiment_demo_acceptance_records";
const TEST_RECORDS_KEY = "polyprop_experiment_demo_test_records";
const MEDICINES_KEY = "polyprop_experiment_demo_medicines";
const TOLERANCE_GRAMS = 0.001;

const PEOPLE = [
  { id: "researcher-01", name: "研究员 01" },
  { id: "researcher-02", name: "研究员 02" },
  { id: "researcher-03", name: "研究员 03" },
  { id: "researcher-04", name: "研究员 04" },
  { id: "researcher-05", name: "研究员 05" },
  { id: "researcher-06", name: "研究员 06" },
];

const POLYOLS: Reagent[] = [
  { id: "po3g", category: "polyol", name: "PO3G", molecularWeight: 2000 },
  { id: "peg-1000", category: "polyol", name: "PEG-1000", molecularWeight: 1000 },
  { id: "pcdl-2000", category: "polyol", name: "PCDL-2000", molecularWeight: 2000 },
];

const ISOCYANATES: Reagent[] = [
  { id: "hmdi", category: "isocyanate", name: "HMDI", molecularWeight: 262.35 },
  { id: "ipdi", category: "isocyanate", name: "IPDI", molecularWeight: 222.29 },
  { id: "hdi", category: "isocyanate", name: "HDI", molecularWeight: 168.2 },
];

const CHAIN_EXTENDERS: Reagent[] = [
  { id: "isophthalic-dihydrazide", category: "chainExtender", name: "间苯二甲酸酰肼", molecularWeight: 194.19 },
  { id: "bhmf", category: "chainExtender", name: "BHMF", molecularWeight: 128.13 },
  { id: "oda", category: "chainExtender", name: "ODA", molecularWeight: 200.24 },
];

const DEMO_PROJECTS: DemoProject[] = [
  {
    id: "project-pu",
    code: "PU",
    name: "聚氨酯弹性体项目",
    tasks: [
      {
        id: "task-pu-001",
        code: "TASK-PU-001",
        title: "聚氨酯基准投料演示",
        goal: "验证不同异氰酸酯与扩链剂组合的投料质量。",
        note: "小锅验证，记录成膜状态。",
        systemId: "polyurethane",
      },
      {
        id: "task-pu-002",
        code: "TASK-PU-002",
        title: "低温柔性样品筛选",
        goal: "比较多元醇分子量对低温柔性的影响。",
        note: "优先完成小锅配方。",
        systemId: "polyurethane",
      },
    ],
  },
  {
    id: "project-pi",
    code: "PI",
    name: "聚酰亚胺薄膜项目",
    tasks: [
      {
        id: "task-pi-001",
        code: "TASK-PI-001",
        title: "PI 体系任务占位演示",
        goal: "展示跨 Project 的任务组织方式。",
        note: "PI 投料公式后续生产化时再接入。",
        systemId: "polyimide",
      },
    ],
  },
];

const TEST_ASSIGNMENTS: TestAssignment[] = [
  { id: "tg", name: "Tg", category: "热学", method: "DSC, 10 ℃/min", unit: "℃" },
  { id: "elongation", name: "断裂伸长率", category: "力学", method: "万能试验机, 50 mm/min", unit: "%" },
  { id: "hardness", name: "硬度", category: "力学", method: "Shore A", unit: "HA" },
];

const MEDICINE_TYPES: Record<MedicineSystemId, Array<{ id: MedicineTypeId; label: string; code: string }>> = {
  polyurethane: [
    { id: "polyol", label: "多元醇", code: "POL" },
    { id: "isocyanate", label: "异氰酸酯", code: "ISO" },
    { id: "chainExtender", label: "扩链剂", code: "CE" },
  ],
  polyimide: [
    { id: "dianhydride", label: "二酐", code: "DA" },
    { id: "diamine", label: "二胺", code: "DM" },
  ],
};

const TAB_ITEMS: Array<{ id: DemoTab; label: string; Icon: typeof ClipboardList }> = [
  { id: "dispatch", label: "任务分发 / 投料计算", Icon: ClipboardList },
  { id: "acceptance", label: "任务接受", Icon: ClipboardCheck },
  { id: "tests", label: "测试数据填写", Icon: TestTube2 },
  { id: "review", label: "反应数据查看", Icon: BadgeCheck },
  { id: "medicine", label: "药品录入", Icon: Database },
];

function canUseStorage() {
  return typeof window !== "undefined" && typeof window.localStorage !== "undefined";
}

function loadStoredArray<T>(key: string): T[] {
  if (!canUseStorage()) {
    return [];
  }
  try {
    const raw = window.localStorage.getItem(key);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? (parsed as T[]) : [];
  } catch {
    return [];
  }
}

function saveStoredArray<T>(key: string, items: T[]) {
  if (canUseStorage()) {
    window.localStorage.setItem(key, JSON.stringify(items));
  }
}

function createId(prefix: string) {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function formatNumber(value: number | null | undefined, digits = 3) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "--";
}

function parsePositive(value: string) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function findById<T extends { id: string }>(items: T[], id: string): T {
  return items.find((item) => item.id === id) ?? items[0];
}

function calculateIngredients({
  baseMass,
  polyol,
  isocyanate,
  chainExtender,
  ratios,
}: {
  baseMass: number | null;
  polyol: Reagent;
  isocyanate: Reagent;
  chainExtender: Reagent;
  ratios: ExperimentRecord["ratios"];
}) {
  if (
    baseMass === null ||
    ratios.polyol <= 0 ||
    ratios.isocyanate <= 0 ||
    ratios.chainExtender <= 0
  ) {
    return null;
  }

  const polyolMol = baseMass / polyol.molecularWeight;
  const isocyanateMol = (polyolMol * ratios.isocyanate) / ratios.polyol;
  const chainExtenderMol = (polyolMol * ratios.chainExtender) / ratios.polyol;
  const ingredients: Ingredient[] = [
    {
      id: polyol.id,
      category: "多元醇",
      name: polyol.name,
      molecularWeight: polyol.molecularWeight,
      ratio: ratios.polyol,
      mmol: polyolMol * 1000,
      theoreticalMass: baseMass,
    },
    {
      id: isocyanate.id,
      category: "异氰酸酯",
      name: isocyanate.name,
      molecularWeight: isocyanate.molecularWeight,
      ratio: ratios.isocyanate,
      mmol: isocyanateMol * 1000,
      theoreticalMass: isocyanateMol * isocyanate.molecularWeight,
    },
    {
      id: chainExtender.id,
      category: "扩链剂",
      name: chainExtender.name,
      molecularWeight: chainExtender.molecularWeight,
      ratio: ratios.chainExtender,
      mmol: chainExtenderMol * 1000,
      theoreticalMass: chainExtenderMol * chainExtender.molecularWeight,
    },
  ];

  return {
    ingredients,
    totalMass: ingredients.reduce((total, ingredient) => total + ingredient.theoreticalMass, 0),
  };
}

function calculateWeightRows(experiment: ExperimentRecord, draft: WeightDraft) {
  return experiment.ingredients.map((ingredient) => {
    const actualMass = parsePositive(draft[ingredient.id] ?? "");
    if (actualMass === null) {
      return {
        ingredient,
        actualMass,
        difference: null,
        differencePercent: null,
        isFilled: false,
        isOutOfTolerance: false,
      };
    }

    const difference = Math.round((actualMass - ingredient.theoreticalMass) * 1000) / 1000;
    const differencePercent = Math.round((difference / ingredient.theoreticalMass) * 100000) / 1000;
    return {
      ingredient,
      actualMass,
      difference,
      differencePercent,
      isFilled: true,
      isOutOfTolerance: Math.abs(difference) > TOLERANCE_GRAMS,
    };
  });
}

function getAcceptedExperimentIds(records: AcceptanceRecord[]) {
  return new Set(records.map((record) => record.experimentId));
}

function formatDateTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "--";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function FieldLabel({ children }: { children: ReactNode }) {
  return <span className="field-label">{children}</span>;
}

function Panel({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return <section className={cn("panel", className)}>{children}</section>;
}

function SectionHeading({
  kicker,
  title,
  aside,
}: {
  kicker: string;
  title: string;
  aside?: ReactNode;
}) {
  return (
    <div className="section-heading">
      <div>
        <span className="section-kicker">{kicker}</span>
        <h2>{title}</h2>
      </div>
      {aside ? <div className="section-heading-aside">{aside}</div> : null}
    </div>
  );
}

function MiniMetric({ label, value, tone }: { label: string; value: string | number; tone?: "warning" | "success" }) {
  return (
    <div className={cn("metric", tone)}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function EmptyState({ children }: { children: ReactNode }) {
  return <div className="empty-state">{children}</div>;
}

function StatusPill({ children, tone = "pending" }: { children: ReactNode; tone?: "pending" | "success" | "warning" }) {
  return <span className={cn("status-pill", tone)}>{children}</span>;
}

export function ExperimentWorkflowDemoPage({ onBackHome }: { onBackHome: () => void }) {
  const [activeTab, setActiveTab] = useState<DemoTab>("dispatch");
  const [experiments, setExperiments] = useState<ExperimentRecord[]>(() => loadStoredArray<ExperimentRecord>(EXPERIMENTS_KEY));
  const [acceptanceRecords, setAcceptanceRecords] = useState<AcceptanceRecord[]>(() => loadStoredArray<AcceptanceRecord>(ACCEPTANCE_KEY));
  const [testRecords, setTestRecords] = useState<TestRecord[]>(() => loadStoredArray<TestRecord>(TEST_RECORDS_KEY));
  const [medicineRecords, setMedicineRecords] = useState<MedicineEntry[]>(() => loadStoredArray<MedicineEntry>(MEDICINES_KEY));

  useEffect(() => saveStoredArray(EXPERIMENTS_KEY, experiments), [experiments]);
  useEffect(() => saveStoredArray(ACCEPTANCE_KEY, acceptanceRecords), [acceptanceRecords]);
  useEffect(() => saveStoredArray(TEST_RECORDS_KEY, testRecords), [testRecords]);
  useEffect(() => saveStoredArray(MEDICINES_KEY, medicineRecords), [medicineRecords]);

  const acceptedExperimentIds = useMemo(() => getAcceptedExperimentIds(acceptanceRecords), [acceptanceRecords]);
  const acceptedExperiments = useMemo(
    () => experiments.filter((experiment) => acceptedExperimentIds.has(experiment.id)),
    [acceptedExperimentIds, experiments],
  );

  return (
    <div className="experiment-workflow-demo">
      <header className="lab-topbar">
        <div className="lab-brand">
          <ClipboardList aria-hidden="true" size={22} />
          <span>Experiment Workflow Demo</span>
        </div>
        <nav className="top-view-nav" aria-label="Experiment workflow demo modules">
          {TAB_ITEMS.map(({ id, label, Icon }) => (
            <button
              key={id}
              type="button"
              className="top-view-button"
              aria-pressed={activeTab === id}
              onClick={() => setActiveTab(id)}
            >
              <Icon aria-hidden="true" size={16} />
              {label}
            </button>
          ))}
        </nav>
        <div className="topbar-actions">
          <span className="service-badge">LocalStorage Demo</span>
          <button type="button" className="secondary-button home-button" onClick={onBackHome}>
            <ArrowLeft aria-hidden="true" size={16} />
            Home
          </button>
        </div>
      </header>

      <main className="demo-shell">
        <section className="portal-hero">
          <div className="portal-hero-copy">
            <span className="section-kicker">FRONTEND DEMO</span>
            <h1>实验任务与投料流程演示</h1>
            <p>沿用 calculator 原项目的实验台界面风格，演示任务分发、投料计算、实际称量、测试数据填写、管理员汇总与药品录入。</p>
          </div>
          <div className="portal-overview-panel">
            <div className="overview-header">
              <span>Browser Session</span>
              <strong>Demo Only</strong>
            </div>
            <div className="overview-metrics">
              <span>
                Exp
                <strong>{experiments.length}</strong>
              </span>
              <span>
                Accepted
                <strong>{acceptanceRecords.length}</strong>
              </span>
              <span>
                Tests
                <strong>{testRecords.length}</strong>
              </span>
            </div>
          </div>
        </section>

        <div className="demo-stage">
          {activeTab === "dispatch" ? (
            <DispatchDemo
              experiments={experiments}
              onExperimentsChange={setExperiments}
              onSwitchTab={setActiveTab}
            />
          ) : null}
          {activeTab === "acceptance" ? (
            <AcceptanceDemo
              experiments={experiments}
              records={acceptanceRecords}
              onRecordsChange={setAcceptanceRecords}
              onSwitchTab={setActiveTab}
            />
          ) : null}
          {activeTab === "tests" ? (
            <TestDataDemo
              experiments={acceptedExperiments}
              records={testRecords}
              onRecordsChange={setTestRecords}
              onSwitchTab={setActiveTab}
            />
          ) : null}
          {activeTab === "review" ? (
            <ReviewDemo
              experiments={experiments}
              acceptanceRecords={acceptanceRecords}
              testRecords={testRecords}
            />
          ) : null}
          {activeTab === "medicine" ? (
            <MedicineDemo records={medicineRecords} onRecordsChange={setMedicineRecords} />
          ) : null}
        </div>
      </main>
    </div>
  );
}

function DispatchDemo({
  experiments,
  onExperimentsChange,
  onSwitchTab,
}: {
  experiments: ExperimentRecord[];
  onExperimentsChange: (experiments: ExperimentRecord[]) => void;
  onSwitchTab: (tab: DemoTab) => void;
}) {
  const [projectId, setProjectId] = useState(DEMO_PROJECTS[0].id);
  const activeProject = findById(DEMO_PROJECTS, projectId);
  const [taskId, setTaskId] = useState(activeProject.tasks[0].id);
  const activeTask = findById(activeProject.tasks, taskId);
  const [assigneeId, setAssigneeId] = useState(PEOPLE[0].id);
  const assignee = findById(PEOPLE, assigneeId);
  const [polyolId, setPolyolId] = useState(POLYOLS[0].id);
  const [isocyanateId, setIsocyanateId] = useState(ISOCYANATES[0].id);
  const [chainExtenderId, setChainExtenderId] = useState(CHAIN_EXTENDERS[0].id);
  const [baseMass, setBaseMass] = useState("20.000");
  const [ratioPolyol, setRatioPolyol] = useState("1.0");
  const [ratioIsocyanate, setRatioIsocyanate] = useState("2.0");
  const [ratioChainExtender, setRatioChainExtender] = useState("1.0");
  const [experimentNo, setExperimentNo] = useState("PU-2026-001");
  const [experimentTitle, setExperimentTitle] = useState("HMDI 基准组合");
  const [experimentNote, setExperimentNote] = useState("小锅验证，记录成膜状态。");
  const [prepolyTime, setPrepolyTime] = useState("2-3 h");
  const [prepolyTemperature, setPrepolyTemperature] = useState("70 ℃");
  const [chainTime, setChainTime] = useState("3-5 h");
  const [chainTemperature, setChainTemperature] = useState("70 ℃");

  useEffect(() => {
    const project = findById(DEMO_PROJECTS, projectId);
    if (!project.tasks.some((task) => task.id === taskId)) {
      setTaskId(project.tasks[0].id);
      setExperimentNo(`${project.code}-2026-001`);
    }
  }, [projectId, taskId]);

  const polyol = findById(POLYOLS, polyolId);
  const isocyanate = findById(ISOCYANATES, isocyanateId);
  const chainExtender = findById(CHAIN_EXTENDERS, chainExtenderId);
  const ratios = {
    polyol: Number(ratioPolyol),
    isocyanate: Number(ratioIsocyanate),
    chainExtender: Number(ratioChainExtender),
  };
  const isPolyurethaneTask = activeTask.systemId === "polyurethane";
  const calculation = isPolyurethaneTask
    ? calculateIngredients({
        baseMass: parsePositive(baseMass),
        polyol,
        isocyanate,
        chainExtender,
        ratios,
      })
    : null;
  const taskExperiments = experiments.filter((experiment) => experiment.taskId === activeTask.id);

  function addExperiment() {
    if (!calculation || !isPolyurethaneTask) {
      return;
    }

    const record: ExperimentRecord = {
      id: createId("exp"),
      projectId: activeProject.id,
      taskId: activeTask.id,
      taskCode: activeTask.code,
      taskTitle: activeTask.title,
      assigneeId: assignee.id,
      assigneeName: assignee.name,
      createdAt: new Date().toISOString(),
      systemId: activeTask.systemId,
      experimentNo: experimentNo.trim() || `${activeProject.code}-DEMO`,
      title: experimentTitle.trim() || "未命名组合",
      note: experimentNote.trim(),
      ratios,
      ingredients: calculation.ingredients,
      conditions: [
        { stage: "预聚", time: prepolyTime, temperature: prepolyTemperature, note: "按计划投料后保温" },
        { stage: "扩链", time: chainTime, temperature: chainTemperature, note: "维持搅拌并观察黏度" },
      ],
      totalMass: calculation.totalMass,
    };

    onExperimentsChange([record, ...experiments]);
    setExperimentNo((current) => {
      const suffix = Number(current.match(/(\d+)$/)?.[1] ?? "1") + 1;
      return `${activeProject.code}-2026-${String(suffix).padStart(3, "0")}`;
    });
  }

  return (
    <div className="dispatch-view">
      <div className="project-topbar">
        <label className="project-switcher">
          <span>Project</span>
          <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
            {DEMO_PROJECTS.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="project-workspace">
        <aside className="task-sidebar">
          <div className="task-sidebar-header">
            <div>
              <span className="section-kicker">TASKS</span>
              <h2>Project Tasks</h2>
            </div>
          </div>
          <div className="task-nav-list">
            {activeProject.tasks.map((task) => (
              <button
                key={task.id}
                type="button"
                className="task-nav-button"
                aria-pressed={activeTask.id === task.id}
                onClick={() => setTaskId(task.id)}
              >
                <strong>{task.code}</strong>
                <span>{task.title}</span>
                <em>{task.systemId === "polyurethane" ? "聚氨酯体系" : "聚酰亚胺体系"}</em>
              </button>
            ))}
          </div>
        </aside>

        <div className="task-workspace-grid">
          <div className="task-workspace-left">
            <Panel className="task-info-panel">
              <SectionHeading
                kicker="Task Dispatch"
                title="任务分发 / 投料计算"
                aside={<StatusPill tone={isPolyurethaneTask ? "success" : "warning"}>{isPolyurethaneTask ? "可计算" : "占位任务"}</StatusPill>}
              />
              <div className="task-info-grid">
                <label className="field">
                  <FieldLabel>Task Goal</FieldLabel>
                  <textarea value={activeTask.goal} readOnly />
                </label>
                <label className="field">
                  <FieldLabel>Task Note</FieldLabel>
                  <textarea value={activeTask.note} readOnly />
                </label>
              </div>
            </Panel>

            <Panel className="material-panel">
              <SectionHeading kicker="Materials" title="投料原料" />
              {isPolyurethaneTask ? (
                <div className="reagent-grid">
                  <ReagentSelect label="多元醇" value={polyolId} reagents={POLYOLS} onChange={setPolyolId} />
                  <ReagentSelect label="异氰酸酯" value={isocyanateId} reagents={ISOCYANATES} onChange={setIsocyanateId} />
                  <ReagentSelect label="扩链剂" value={chainExtenderId} reagents={CHAIN_EXTENDERS} onChange={setChainExtenderId} />
                </div>
              ) : (
                <div className="system-placeholder-note">PI 体系原料选择与投料公式保持原项目占位状态，后续接入真实规则后再开放计算。</div>
              )}
            </Panel>

            <Panel className="dosing-panel">
              <SectionHeading kicker="Dosing" title="投料参数" />
              <div className="form-grid four-columns">
                <label className="field">
                  <FieldLabel>多元醇质量/g</FieldLabel>
                  <input type="number" step="0.001" value={baseMass} onChange={(event) => setBaseMass(event.target.value)} disabled={!isPolyurethaneTask} />
                </label>
                <label className="field">
                  <FieldLabel>多元醇比</FieldLabel>
                  <input type="number" step="0.1" value={ratioPolyol} onChange={(event) => setRatioPolyol(event.target.value)} disabled={!isPolyurethaneTask} />
                </label>
                <label className="field">
                  <FieldLabel>异氰酸酯比</FieldLabel>
                  <input type="number" step="0.1" value={ratioIsocyanate} onChange={(event) => setRatioIsocyanate(event.target.value)} disabled={!isPolyurethaneTask} />
                </label>
                <label className="field">
                  <FieldLabel>扩链剂比</FieldLabel>
                  <input type="number" step="0.1" value={ratioChainExtender} onChange={(event) => setRatioChainExtender(event.target.value)} disabled={!isPolyurethaneTask} />
                </label>
              </div>
              <div className="form-grid two-columns">
                <label className="field">
                  <FieldLabel>实验人员</FieldLabel>
                  <select value={assigneeId} onChange={(event) => setAssigneeId(event.target.value)}>
                    {PEOPLE.map((person) => (
                      <option key={person.id} value={person.id}>
                        {person.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <FieldLabel>实验编号</FieldLabel>
                  <input value={experimentNo} onChange={(event) => setExperimentNo(event.target.value)} />
                </label>
                <label className="field">
                  <FieldLabel>组合标题</FieldLabel>
                  <input value={experimentTitle} onChange={(event) => setExperimentTitle(event.target.value)} />
                </label>
                <label className="field">
                  <FieldLabel>实验备注</FieldLabel>
                  <input value={experimentNote} onChange={(event) => setExperimentNote(event.target.value)} />
                </label>
              </div>
              <div className="form-grid four-columns">
                <label className="field">
                  <FieldLabel>预聚时间</FieldLabel>
                  <input value={prepolyTime} onChange={(event) => setPrepolyTime(event.target.value)} disabled={!isPolyurethaneTask} />
                </label>
                <label className="field">
                  <FieldLabel>预聚温度</FieldLabel>
                  <input value={prepolyTemperature} onChange={(event) => setPrepolyTemperature(event.target.value)} disabled={!isPolyurethaneTask} />
                </label>
                <label className="field">
                  <FieldLabel>扩链时间</FieldLabel>
                  <input value={chainTime} onChange={(event) => setChainTime(event.target.value)} disabled={!isPolyurethaneTask} />
                </label>
                <label className="field">
                  <FieldLabel>扩链温度</FieldLabel>
                  <input value={chainTemperature} onChange={(event) => setChainTemperature(event.target.value)} disabled={!isPolyurethaneTask} />
                </label>
              </div>
              <div className="panel-actions">
                <button type="button" className="secondary-button" onClick={() => onSwitchTab("acceptance")} disabled={experiments.length === 0}>
                  去任务接受
                </button>
                <button type="button" className="primary-button" onClick={addExperiment} disabled={!calculation || !isPolyurethaneTask}>
                  <Plus aria-hidden="true" size={16} />
                  添加 Exp 组合
                </button>
              </div>
            </Panel>
          </div>

          <div className="task-workspace-right">
            <Panel className="formula-panel">
              <SectionHeading
                kicker="Formula Preview"
                title="投料预览"
                aside={<StatusPill>{formatNumber(calculation?.totalMass)} g</StatusPill>}
              />
              {calculation ? (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>类别</th>
                        <th>原料</th>
                        <th>摩尔比</th>
                        <th>mmol</th>
                        <th>质量/g</th>
                      </tr>
                    </thead>
                    <tbody>
                      {calculation.ingredients.map((ingredient) => (
                        <tr key={ingredient.id}>
                          <td>{ingredient.category}</td>
                          <th>{ingredient.name}</th>
                          <td>{formatNumber(ingredient.ratio, 1)}</td>
                          <td>{formatNumber(ingredient.mmol)}</td>
                          <td className="theoretical">{formatNumber(ingredient.theoreticalMass)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyState>{isPolyurethaneTask ? "请填写有效投料质量与摩尔比。" : "当前任务为 PI 占位体系，不生成 PU 投料记录。"}</EmptyState>
              )}
            </Panel>

            <Panel className="experiment-details">
              <SectionHeading kicker="Current Task" title="当前 Task 的 Exp" aside={<StatusPill>{taskExperiments.length} 个</StatusPill>} />
              <div className="record-list">
                {taskExperiments.length === 0 ? (
                  <EmptyState>暂无实验组合，添加后会出现在这里。</EmptyState>
                ) : (
                  taskExperiments.map((experiment) => (
                    <article key={experiment.id} className="record-card">
                      <div>
                        <strong>{experiment.experimentNo}</strong>
                        <span>{experiment.title}</span>
                        <em>{formatDateTime(experiment.createdAt)} · {experiment.assigneeName}</em>
                      </div>
                      <b>{formatNumber(experiment.totalMass)} g</b>
                    </article>
                  ))
                )}
              </div>
            </Panel>
          </div>
        </div>
      </div>
    </div>
  );
}

function ReagentSelect({
  label,
  value,
  reagents,
  onChange,
}: {
  label: string;
  value: string;
  reagents: Reagent[];
  onChange: (value: string) => void;
}) {
  const selected = findById(reagents, value);

  return (
    <article className="reagent-card">
      <label className="field reagent-hover-field">
        <FieldLabel>{label}</FieldLabel>
        <select value={value} onChange={(event) => onChange(event.target.value)}>
          {reagents.map((reagent) => (
            <option key={reagent.id} value={reagent.id}>
              {reagent.name}
            </option>
          ))}
        </select>
      </label>
      <div className="molecular-weight-panel">
        <span>相对分子质量</span>
        <strong>{formatNumber(selected.molecularWeight)} g/mol</strong>
      </div>
    </article>
  );
}

function AcceptanceDemo({
  experiments,
  records,
  onRecordsChange,
  onSwitchTab,
}: {
  experiments: ExperimentRecord[];
  records: AcceptanceRecord[];
  onRecordsChange: (records: AcceptanceRecord[]) => void;
  onSwitchTab: (tab: DemoTab) => void;
}) {
  const [selectedExperimentId, setSelectedExperimentId] = useState(experiments[0]?.id ?? "");
  const selectedExperiment = experiments.find((experiment) => experiment.id === selectedExperimentId) ?? experiments[0] ?? null;
  const existingRecord = selectedExperiment
    ? records.find((record) => record.experimentId === selectedExperiment.id)
    : undefined;
  const [draft, setDraft] = useState<WeightDraft>({});
  const [note, setNote] = useState("");

  useEffect(() => {
    if (experiments.length > 0 && !experiments.some((experiment) => experiment.id === selectedExperimentId)) {
      setSelectedExperimentId(experiments[0].id);
    }
  }, [experiments, selectedExperimentId]);

  useEffect(() => {
    if (existingRecord) {
      setDraft(Object.fromEntries(existingRecord.weights.map((weight) => [weight.ingredientId, formatNumber(weight.actualMass)])));
      setNote(existingRecord.note ?? "");
    } else {
      setDraft({});
      setNote("");
    }
  }, [existingRecord, selectedExperimentId]);

  const rows = selectedExperiment ? calculateWeightRows(selectedExperiment, draft) : [];
  const hasMissing = rows.some((row) => !row.isFilled);
  const hasOutOfTolerance = rows.some((row) => row.isOutOfTolerance);
  const canSubmit = Boolean(selectedExperiment) && !hasMissing && !hasOutOfTolerance;

  function submitAcceptance() {
    if (!selectedExperiment || !canSubmit) {
      return;
    }

    const record: AcceptanceRecord = {
      experimentId: selectedExperiment.id,
      submittedAt: new Date().toISOString(),
      note: note.trim() || undefined,
      weights: rows.map((row) => ({
        ingredientId: row.ingredient.id,
        theoreticalMass: row.ingredient.theoreticalMass,
        actualMass: row.actualMass!,
        difference: row.difference!,
        differencePercent: row.differencePercent!,
        isOutOfTolerance: row.isOutOfTolerance,
      })),
    };
    onRecordsChange([record, ...records.filter((item) => item.experimentId !== selectedExperiment.id)]);
  }

  if (experiments.length === 0) {
    return (
      <Panel>
        <EmptyState>请先在任务分发页添加一个 Exp 组合。</EmptyState>
      </Panel>
    );
  }

  return (
    <div className="acceptance-shell">
      <aside className="task-sidebar">
        <div className="sidebar-header">
          <div>
            <span className="section-kicker">EXP LIST</span>
            <h1>待接受任务</h1>
          </div>
          <span>{experiments.length}</span>
        </div>
        <div className="task-list">
          {experiments.map((experiment) => {
            const accepted = records.some((record) => record.experimentId === experiment.id);
            return (
              <button
                key={experiment.id}
                type="button"
                className="task-card"
                aria-pressed={selectedExperiment?.id === experiment.id}
                onClick={() => setSelectedExperimentId(experiment.id)}
              >
                <span className="task-card-main">
                  <strong>{experiment.experimentNo}</strong>
                  <span>{experiment.title}</span>
                  <span className="experiment-number">{experiment.taskCode}</span>
                </span>
                <span className={cn("task-status", !accepted && "alert")}>{accepted ? "已提交" : "待称量"}</span>
              </button>
            );
          })}
        </div>
      </aside>

      <div className="task-detail">
        <Panel className="summary-panel">
          <div>
            <span className="eyebrow">Task Acceptance</span>
            <h2>任务接受与实际称量</h2>
            <p className="summary-subtitle">{selectedExperiment?.taskCode ?? "--"} · {selectedExperiment?.assigneeName ?? "--"}</p>
          </div>
          <StatusPill tone={existingRecord ? "success" : "warning"}>{existingRecord ? "已提交" : "待称量"}</StatusPill>
        </Panel>

        {selectedExperiment ? (
          <>
            <div className="metric-strip">
              <MiniMetric label="理论总质量/g" value={formatNumber(selectedExperiment.totalMass)} />
              <MiniMetric label="原料数" value={selectedExperiment.ingredients.length} />
              <MiniMetric label="容差/g" value={formatNumber(TOLERANCE_GRAMS)} />
              <MiniMetric label="状态" value={existingRecord ? "Done" : "Pending"} tone={existingRecord ? "success" : "warning"} />
            </div>

            <Panel className="formula-panel">
              <SectionHeading kicker="Weights" title="称量数据" />
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>类别</th>
                      <th>原料</th>
                      <th>理论/g</th>
                      <th>实际/g</th>
                      <th>偏差/g</th>
                      <th>偏差/%</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => (
                      <tr key={row.ingredient.id} className={row.isOutOfTolerance ? "out-of-tolerance" : undefined}>
                        <td>{row.ingredient.category}</td>
                        <th>{row.ingredient.name}</th>
                        <td className="theoretical">{formatNumber(row.ingredient.theoreticalMass)}</td>
                        <td>
                          <input
                            type="number"
                            step="0.001"
                            value={draft[row.ingredient.id] ?? ""}
                            onChange={(event) => setDraft((current) => ({ ...current, [row.ingredient.id]: event.target.value }))}
                          />
                        </td>
                        <td className={cn("difference", row.isOutOfTolerance && "alert")}>
                          {row.difference === null ? "--" : formatNumber(row.difference)}
                        </td>
                        <td className={cn("difference", row.isOutOfTolerance && "alert")}>
                          {row.differencePercent === null ? "--" : `${formatNumber(row.differencePercent)}%`}
                        </td>
                        <td>{row.isOutOfTolerance ? "超差" : row.isFilled ? "合格" : "待填写"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>

            <Panel className="submit-panel">
              <label className="note-field">
                <span>称量备注</span>
                <textarea
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                  placeholder="记录样品状态、称量异常或替代批号"
                />
              </label>
              <div className="submit-actions">
                <div className={cn("submit-hint", canSubmit && "ok")}>
                  {canSubmit ? "可以提交" : hasOutOfTolerance ? "存在超差，请修正后提交" : "请填写全部实际称量"}
                </div>
                <button type="button" className="submit-button" onClick={submitAcceptance} disabled={!canSubmit}>
                  <Send aria-hidden="true" size={16} />
                  提交 Exp
                </button>
                <button type="button" className="secondary-button" onClick={() => onSwitchTab("tests")} disabled={records.length === 0}>
                  去测试填写
                </button>
              </div>
            </Panel>
          </>
        ) : null}
      </div>
    </div>
  );
}

function TestDataDemo({
  experiments,
  records,
  onRecordsChange,
  onSwitchTab,
}: {
  experiments: ExperimentRecord[];
  records: TestRecord[];
  onRecordsChange: (records: TestRecord[]) => void;
  onSwitchTab: (tab: DemoTab) => void;
}) {
  const [selectedExperimentId, setSelectedExperimentId] = useState(experiments[0]?.id ?? "");
  const selectedExperiment = experiments.find((experiment) => experiment.id === selectedExperimentId) ?? experiments[0] ?? null;
  const [selectedTestId, setSelectedTestId] = useState(TEST_ASSIGNMENTS[0].id);
  const selectedTest = findById(TEST_ASSIGNMENTS, selectedTestId);
  const existingRecord = selectedExperiment
    ? records.find((record) => record.experimentId === selectedExperiment.id && record.testId === selectedTest.id)
    : undefined;
  const [value, setValue] = useState("");
  const [unit, setUnit] = useState(selectedTest.unit);
  const [method, setMethod] = useState(selectedTest.method);
  const [note, setNote] = useState("");
  const [attachment, setAttachment] = useState<{ name: string; size: number; type: string } | null>(null);

  useEffect(() => {
    if (experiments.length > 0 && !experiments.some((experiment) => experiment.id === selectedExperimentId)) {
      setSelectedExperimentId(experiments[0].id);
    }
  }, [experiments, selectedExperimentId]);

  useEffect(() => {
    if (existingRecord) {
      setValue(existingRecord.value);
      setUnit(existingRecord.unit);
      setMethod(existingRecord.method);
      setNote(existingRecord.note ?? "");
      setAttachment(
        existingRecord.attachmentName
          ? {
              name: existingRecord.attachmentName,
              size: existingRecord.attachmentSize ?? 0,
              type: existingRecord.attachmentType ?? "未知类型",
            }
          : null,
      );
      return;
    }

    setValue("");
    setUnit(selectedTest.unit);
    setMethod(selectedTest.method);
    setNote("");
    setAttachment(null);
  }, [existingRecord, selectedTest.method, selectedTest.unit]);

  function handleFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0] ?? null;
    setAttachment(file ? { name: file.name, size: file.size, type: file.type || "未知类型" } : null);
    event.currentTarget.value = "";
  }

  function submitTestRecord() {
    if (!selectedExperiment || !value.trim() || !unit.trim()) {
      return;
    }

    const record: TestRecord = {
      experimentId: selectedExperiment.id,
      testId: selectedTest.id,
      value: value.trim(),
      unit: unit.trim(),
      method: method.trim() || selectedTest.method,
      note: note.trim() || undefined,
      attachmentName: attachment?.name,
      attachmentType: attachment?.type,
      attachmentSize: attachment?.size,
      submittedAt: new Date().toISOString(),
    };
    onRecordsChange([
      record,
      ...records.filter((item) => item.experimentId !== record.experimentId || item.testId !== record.testId),
    ]);
  }

  if (experiments.length === 0) {
    return (
      <Panel>
        <EmptyState>请先在任务接受页提交实际称量，之后再填写测试结果。</EmptyState>
      </Panel>
    );
  }

  return (
    <div className="characterization-shell">
      <aside className="task-sidebar">
        <div className="sidebar-header">
          <div>
            <span className="section-kicker">TESTS</span>
            <h1>测试项目</h1>
          </div>
        </div>
        <label className="field sidebar-select">
          <FieldLabel>已称量 Exp</FieldLabel>
          <select value={selectedExperiment?.id ?? ""} onChange={(event) => setSelectedExperimentId(event.target.value)}>
            {experiments.map((experiment) => (
              <option key={experiment.id} value={experiment.id}>
                {experiment.experimentNo} · {experiment.title}
              </option>
            ))}
          </select>
        </label>
        <div className="task-list">
          {TEST_ASSIGNMENTS.map((test) => {
            const isDone = selectedExperiment
              ? records.some((record) => record.experimentId === selectedExperiment.id && record.testId === test.id)
              : false;
            return (
              <button
                key={test.id}
                type="button"
                className="task-card"
                aria-pressed={selectedTestId === test.id}
                onClick={() => setSelectedTestId(test.id)}
              >
                <span className="task-card-main">
                  <strong>{test.name}</strong>
                  <span>{test.method}</span>
                  <span className="experiment-number">{test.category}</span>
                </span>
                <span className={cn("task-status", !isDone && "alert")}>{isDone ? "已填写" : "待填写"}</span>
              </button>
            );
          })}
        </div>
      </aside>

      <div className="task-detail">
        <Panel className="summary-panel">
          <div>
            <span className="eyebrow">Test Data Entry</span>
            <h2>{selectedExperiment?.experimentNo ?? "--"} · {selectedTest.name}</h2>
            <p className="summary-subtitle">{selectedTest.method}</p>
          </div>
          <StatusPill tone={existingRecord ? "success" : "pending"}>{existingRecord ? "已保存" : "待填写"}</StatusPill>
        </Panel>

        <Panel className="result-form-panel">
          <SectionHeading kicker="Result Form" title="测试数据填写" />
          <div className="form-grid two-columns">
            <label className="field">
              <FieldLabel>测试结果</FieldLabel>
              <input value={value} onChange={(event) => setValue(event.target.value)} placeholder="-42.6" />
            </label>
            <label className="field">
              <FieldLabel>单位</FieldLabel>
              <input value={unit} onChange={(event) => setUnit(event.target.value)} />
            </label>
            <label className="field span-two">
              <FieldLabel>测试方法</FieldLabel>
              <input value={method} onChange={(event) => setMethod(event.target.value)} />
            </label>
            <label className="field span-two">
              <FieldLabel>结果备注</FieldLabel>
              <textarea value={note} onChange={(event) => setNote(event.target.value)} />
            </label>
            <label className="field span-two">
              <FieldLabel>测试报告附件</FieldLabel>
              <input type="file" onChange={handleFile} />
            </label>
          </div>

          {attachment ? (
            <div className="attachment-chip">
              <strong>{attachment.name}</strong>
              <span>{attachment.type} · {(attachment.size / 1024).toFixed(1)} KB</span>
            </div>
          ) : null}

          <div className="panel-actions">
            <button type="button" className="secondary-button" onClick={() => onSwitchTab("review")} disabled={records.length === 0}>
              查看汇总
            </button>
            <button type="button" className="primary-button" onClick={submitTestRecord} disabled={!selectedExperiment || !value.trim() || !unit.trim()}>
              <Save aria-hidden="true" size={16} />
              填写并完成
            </button>
          </div>
        </Panel>
      </div>
    </div>
  );
}

function ReviewDemo({
  experiments,
  acceptanceRecords,
  testRecords,
}: {
  experiments: ExperimentRecord[];
  acceptanceRecords: AcceptanceRecord[];
  testRecords: TestRecord[];
}) {
  const acceptedIds = getAcceptedExperimentIds(acceptanceRecords);
  const completeExperimentCount = experiments.filter((experiment) =>
    TEST_ASSIGNMENTS.every((test) =>
      testRecords.some((record) => record.experimentId === experiment.id && record.testId === test.id),
    ),
  ).length;

  return (
    <div className="review-board">
      <aside className="review-sidebar">
        <div className="sidebar-heading">
          <span className="eyebrow">EXP REVIEW</span>
          <h1>管理员反应数据查看</h1>
          <p>汇总前端演示流中已经创建、称量和填写测试数据的 Exp。</p>
        </div>
        <div className="review-stat-list">
          <MiniMetric label="任务组合" value={experiments.length} />
          <MiniMetric label="已称量" value={acceptanceRecords.length} />
          <MiniMetric label="测试记录" value={testRecords.length} />
          <MiniMetric label="附件记录" value={testRecords.filter((record) => record.attachmentName).length} />
        </div>
      </aside>

      <Panel className="review-main-panel">
        <SectionHeading
          kicker="Review Console"
          title="反应数据汇总"
          aside={<StatusPill tone="success">{completeExperimentCount} 完整测试</StatusPill>}
        />
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Exp</th>
                <th>Task</th>
                <th>实验人员</th>
                <th>称量状态</th>
                {TEST_ASSIGNMENTS.map((test) => (
                  <th key={test.id}>{test.name}</th>
                ))}
                <th>总质量/g</th>
              </tr>
            </thead>
            <tbody>
              {experiments.length === 0 ? (
                <tr>
                  <td className="table-empty-cell" colSpan={8}>暂无演示数据</td>
                </tr>
              ) : (
                experiments.map((experiment) => (
                  <tr key={experiment.id}>
                    <th>
                      <span className="stacked-cell">
                        <strong>{experiment.experimentNo}</strong>
                        <em>{experiment.title}</em>
                      </span>
                    </th>
                    <td>{experiment.taskCode}</td>
                    <td>{experiment.assigneeName}</td>
                    <td>
                      <StatusPill tone={acceptedIds.has(experiment.id) ? "success" : "warning"}>
                        {acceptedIds.has(experiment.id) ? "已称量" : "待称量"}
                      </StatusPill>
                    </td>
                    {TEST_ASSIGNMENTS.map((test) => {
                      const record = testRecords.find((item) => item.experimentId === experiment.id && item.testId === test.id);
                      return (
                        <td key={test.id}>
                          {record ? `${record.value} ${record.unit}` : "Pending"}
                          {record?.attachmentName ? <span className="attachment-name">{record.attachmentName}</span> : null}
                        </td>
                      );
                    })}
                    <td className="theoretical">{formatNumber(experiment.totalMass)}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}

function MedicineDemo({
  records,
  onRecordsChange,
}: {
  records: MedicineEntry[];
  onRecordsChange: (records: MedicineEntry[]) => void;
}) {
  const [systemId, setSystemId] = useState<MedicineSystemId>("polyurethane");
  const activeTypes = MEDICINE_TYPES[systemId];
  const [typeId, setTypeId] = useState<MedicineTypeId>(activeTypes[0].id);
  const activeType = activeTypes.find((type) => type.id === typeId) ?? activeTypes[0];
  const [name, setName] = useState("");
  const [molecularWeight, setMolecularWeight] = useState("");
  const [structureText, setStructureText] = useState("SMILES: O=C=NCCCCCCN=C=O");

  useEffect(() => {
    const types = MEDICINE_TYPES[systemId];
    if (!types.some((type) => type.id === typeId)) {
      setTypeId(types[0].id);
    }
  }, [systemId, typeId]);

  const nextId = useMemo(() => {
    const systemCode = systemId === "polyurethane" ? "PU" : "PI";
    const nextIndex = records.filter((record) => record.systemId === systemId && record.typeId === typeId).length + 1;
    return `${systemCode}-${activeType.code}-${String(nextIndex).padStart(4, "0")}`;
  }, [activeType.code, records, systemId, typeId]);
  const parsedMolecularWeight = parsePositive(molecularWeight);

  function addMedicine() {
    if (!name.trim() || parsedMolecularWeight === null) {
      return;
    }

    const entry: MedicineEntry = {
      id: nextId,
      systemId,
      typeId,
      typeLabel: activeType.label,
      name: name.trim(),
      molecularWeight: parsedMolecularWeight,
      structureText: structureText.trim(),
      createdAt: new Date().toISOString(),
    };
    onRecordsChange([entry, ...records]);
    setName("");
    setMolecularWeight("");
  }

  return (
    <div className="medicine-workspace">
      <Panel className="medicine-panel">
        <SectionHeading kicker="Medicine Registry" title="药品录入演示" />
        <div className="medicine-system-tabs">
          <button type="button" aria-pressed={systemId === "polyurethane"} onClick={() => setSystemId("polyurethane")}>
            聚氨酯
          </button>
          <button type="button" aria-pressed={systemId === "polyimide"} onClick={() => setSystemId("polyimide")}>
            聚酰亚胺
          </button>
        </div>
        <div className="form-grid two-columns">
          <label className="field">
            <FieldLabel>药品类型</FieldLabel>
            <select value={typeId} onChange={(event) => setTypeId(event.target.value as MedicineTypeId)}>
              {activeTypes.map((type) => (
                <option key={type.id} value={type.id}>
                  {type.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <FieldLabel>ID</FieldLabel>
            <input value={nextId} readOnly />
          </label>
          <label className="field">
            <FieldLabel>药品名称</FieldLabel>
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="HMDI" />
          </label>
          <label className="field">
            <FieldLabel>分子量</FieldLabel>
            <input type="number" step="0.01" value={molecularWeight} onChange={(event) => setMolecularWeight(event.target.value)} placeholder="262.35" />
          </label>
          <label className="field span-two">
            <FieldLabel>结构文本 / SMILES</FieldLabel>
            <textarea value={structureText} onChange={(event) => setStructureText(event.target.value)} />
          </label>
        </div>
        <div className="structure-placeholder">
          <span>Structure Board</span>
          <strong>SMILES / Ketcher placeholder</strong>
        </div>
        <div className="panel-actions">
          <button type="button" className="primary-button" onClick={addMedicine} disabled={!name.trim() || parsedMolecularWeight === null}>
            <Plus aria-hidden="true" size={16} />
            Add to Demo Registry
          </button>
        </div>
      </Panel>

      <Panel className="medicine-panel">
        <SectionHeading kicker="Recently Added" title="最近添加" aside={<StatusPill>{records.length} records</StatusPill>} />
        <div className="record-list">
          {records.length === 0 ? (
            <EmptyState>暂无药品记录。</EmptyState>
          ) : (
            records.map((record) => (
              <article key={`${record.id}-${record.createdAt}`} className="medicine-record-card">
                <div className="medicine-record-main">
                  <span>{record.id}</span>
                  <strong>{record.name}</strong>
                  <em>{record.typeLabel} · {formatDateTime(record.createdAt)}</em>
                </div>
                <b>{formatNumber(record.molecularWeight)} g/mol</b>
                {record.structureText ? <code>{record.structureText}</code> : null}
              </article>
            ))
          )}
        </div>
      </Panel>
    </div>
  );
}
