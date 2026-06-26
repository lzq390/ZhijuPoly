import {
  Box,
  ChevronDown,
  Copy,
  Database,
  Eraser,
  ImagePlus,
  LoaderCircle,
  RefreshCcw,
  Search,
  Table2,
  Trash2,
  X
} from "lucide-react";
import { useEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import { lookupSmilesInDatabase, recognizeStructureImage } from "../services/api";
import type { SmilesLookupResponse, SmilesLookupResult, SmilesLookupTable, StructureWorkspaceContext } from "../types";
import { StructurePreview3D } from "./StructurePreview3D";
import { StructureSvg } from "./StructureSvg";
import "../styles/polymer-desktop.css";

type DatabaseQueryPageProps = {
  structure: StructureWorkspaceContext;
  onEditStructure: () => void;
  onBackHome: () => void;
};

type TableOption = {
  value: SmilesLookupTable;
  label: string;
  shortLabel: string;
  detail: string;
  fields: string;
};

type KetcherApi = NonNullable<Window["ketcher"]>;
type KetcherSnapshot = { smiles: string; molfile: string };

const tableOptions: TableOption[] = [
  {
    value: "polymers",
    label: "结构-性能库 / Polymers",
    shortLabel: "Polymers",
    detail: "聚合物级记录，用于确认结构是否已进入主结构-性能库。",
    fields: "smiles, canonical_smiles"
  },
  {
    value: "properties",
    label: "结构-性能库 / Properties",
    shortLabel: "Properties",
    detail: "性能行级记录，会返回当前结构关联的每一条性能数据。",
    fields: "polymers.smiles, polymers.canonical_smiles"
  },
  {
    value: "pi_candidates",
    label: "PI 反向设计库",
    shortLabel: "PI Polymer",
    detail: "PI 候选聚合物、单体与计算属性，用于检查反向设计候选空间。",
    fields: "polym, canonical_polym, mon1, mon2"
  }
];

const tableOptionByValue = Object.fromEntries(
  tableOptions.map((option) => [option.value, option])
) as Record<SmilesLookupTable, TableOption>;

const fieldLabels: Record<string, string> = {
  polymer_id: "Polymer ID",
  property_id: "Property ID",
  property_count: "性能条目",
  rdkit_parse_ok: "RDKit 状态",
  property_name: "性能名称",
  property_value: "性能值",
  property_value_num: "数值",
  property_unit: "单位",
  label_source: "来源",
  pi_id: "PI ID",
  mon1: "单体 A",
  mon2: "单体 B",
  polym: "聚合物 SMILES",
  tg_celsius: "Tg (°C)",
  dielectric_const_dc: "介电常数 DC",
  static_dielectric_const: "静态介电常数",
  dipole_debye: "偶极矩",
  electrophilicity_index: "亲电指数",
  homo_lumo_gap_ev: "HOMO-LUMO Gap",
  hardness: "硬度",
  mulliken_electronegativity: "电负性",
  redox_window_v: "氧化还原窗口",
  linear_expansion: "线性膨胀",
  refractive_index: "折射率"
};

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function wildcardCount(value: string) {
  return value.match(/\*/g)?.length ?? 0;
}

function shouldAdoptEditorSmiles(sourceSmiles: string, editorSmiles: string) {
  const sourceWildcardCount = wildcardCount(sourceSmiles);
  if (sourceWildcardCount === 0) return editorSmiles.trim().length > 0;
  return wildcardCount(editorSmiles) >= sourceWildcardCount;
}

function getEditorLoadCandidates(sourceSmiles: string) {
  const normalized = sourceSmiles.trim();
  const stripped = normalized.replace(/\*/g, "").trim();
  return Array.from(new Set([normalized, stripped])).filter(Boolean);
}

function formatLookupValue(value: string | number | boolean | null) {
  if (value === null || value === "") return "-";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  return value;
}


async function waitForKetcherCommit() {
  await new Promise((resolve) => window.setTimeout(resolve, 80));
}

async function readEditorSmiles(ketcher: KetcherApi) {
  const editorSmiles = (await ketcher.getSmiles()).trim();
  return editorSmiles && editorSmiles !== "{}" ? editorSmiles : "";
}

async function readEditorMolfile(ketcher: KetcherApi) {
  if (typeof ketcher.getMolfile !== "function") return "";
  return (await ketcher.getMolfile()).trim();
}

async function waitForEditorSmilesState(ketcher: KetcherApi, matches: (value: string) => boolean, timeoutMs = 1200) {
  const startedAt = Date.now();
  let latest = await readEditorSmiles(ketcher);
  while (Date.now() - startedAt < timeoutMs) {
    if (matches(latest)) return latest;
    await new Promise((resolve) => window.setTimeout(resolve, 80));
    latest = await readEditorSmiles(ketcher);
  }
  return latest;
}

async function writeStructureToEditor(ketcher: KetcherApi, structureValue: string, fallbackSmiles: string) {
  if (typeof ketcher.setMolecule !== "function") return fallbackSmiles;
  await ketcher.setMolecule(structureValue);
  await waitForKetcherCommit();
  return readEditorSmiles(ketcher);
}

async function clearEditorForImageImport(ketcher: KetcherApi) {
  if (typeof ketcher.clear === "function") await ketcher.clear();
  else if (typeof ketcher.setMolecule === "function") await ketcher.setMolecule("");
  else throw new Error("结构编辑器无法清空画布。");
  await waitForKetcherCommit();

  let clearedSmiles = await waitForEditorSmilesState(ketcher, (value) => !value);
  if (clearedSmiles && typeof ketcher.setMolecule === "function") {
    await ketcher.setMolecule("");
    await waitForKetcherCommit();
    clearedSmiles = await waitForEditorSmilesState(ketcher, (value) => !value);
  }
  if (clearedSmiles) throw new Error("旧画布未能清空，图片结构未写入。保存当前结构后请手动清空画布再重试。");
}

async function writeImageStructureToEditor(ketcher: KetcherApi, structureValue: string) {
  if (typeof ketcher.setMolecule !== "function") throw new Error("结构编辑器无法加载分子。因无法确认画布写入结果，本次图片导入已取消。");
  await clearEditorForImageImport(ketcher);
  await ketcher.setMolecule(structureValue);
  await waitForKetcherCommit();
  const editorSmiles = await waitForEditorSmilesState(ketcher, (value) => Boolean(value), 1800);
  if (!editorSmiles) throw new Error("Ketcher 未接受识别出的结构，图片结构未写入画布。");
  return editorSmiles;
}

async function loadBestEffortStructureToEditor(ketcher: KetcherApi, sourceSmiles: string) {
  const normalizedSmiles = sourceSmiles.trim();
  for (const candidate of getEditorLoadCandidates(normalizedSmiles)) {
    try {
      const editorSmiles = await writeStructureToEditor(ketcher, candidate, normalizedSmiles);
      if (editorSmiles) {
        return shouldAdoptEditorSmiles(normalizedSmiles, editorSmiles) ? editorSmiles : normalizedSmiles;
      }
    } catch {
      // Try the next representation; polymer wildcard SMILES can fail in Ketcher.
    }
  }
  return normalizedSmiles;
}

function DrawerEmptyState({ title, description }: { title: string; description: string }) {
  return (
    <div className="empty-state database-lookup-empty-state">
      <div className="empty-state-icon"><Database width={22} height={22} /></div>
      <h4>{title}</h4>
      <p>{description}</p>
    </div>
  );
}

function LookupSkeleton() {
  return (
    <div className="database-lookup-results-stack database-lookup-skeleton">
      {[0, 1, 2].map((item) => (
        <div key={item} className="skeleton-card database-lookup-skeleton-card">
          <div className="skeleton-line skeleton-title" />
          <div className="skeleton-line" />
          <div className="skeleton-line short" />
        </div>
      ))}
    </div>
  );
}

function LookupResultCard({ result }: { result: SmilesLookupResult }) {
  const fields = Object.entries(result.fields).filter(([, value]) => value !== null && value !== "");
  const displaySmiles = result.canonical_smiles || result.smiles;

  return (
    <article className="database-lookup-result-card">
      <div className="database-lookup-structure-box">
        {result.structure_svg ? (
          <StructureSvg svg={result.structure_svg} alt={result.summary} className="database-lookup-structure-svg" />
        ) : (
          <div className="database-lookup-structure-fallback">{displaySmiles}</div>
        )}
      </div>
      <div className="database-lookup-card-meta">
        <div className="database-lookup-card-badges">
          <span>ID {result.record_id}</span>
          <span>{result.source_column}</span>
        </div>
        <h4>{result.summary}</h4>
        <div className="database-lookup-smiles-block">
          <span>Matched SMILES</span>
          <code>{result.smiles}</code>
        </div>
        {result.canonical_smiles ? (
          <div className="database-lookup-smiles-block">
            <span>Canonical SMILES</span>
            <code>{result.canonical_smiles}</code>
          </div>
        ) : null}
        {fields.length > 0 ? (
          <div className="database-lookup-field-grid">
            {fields.map(([key, value]) => (
              <div key={key} className="database-lookup-field-item">
                <span>{fieldLabels[key] ?? key}</span>
                <strong title={formatLookupValue(value)}>{formatLookupValue(value)}</strong>
              </div>
            ))}
          </div>
        ) : null}
      </div>
    </article>
  );
}

function LookupDrawerContent({
  data,
  error,
  isLoading,
  selectedTable
}: {
  data: SmilesLookupResponse | null;
  error: string | null;
  isLoading: boolean;
  selectedTable: SmilesLookupTable;
}) {
  const selectedOption = tableOptionByValue[selectedTable];

  if (isLoading) return <LookupSkeleton />;

  if (error) {
    return (
      <div className="database-lookup-error-state">
        <div className="polymer-error-graphic"><X width={18} height={18} /></div>
        <h4>查询失败</h4>
        <p>{error}</p>
      </div>
    );
  }

  if (!data) {
    return <DrawerEmptyState title="等待查询" description="输入或绘制 SMILES 后，选择数据表并运行精确查询。" />;
  }

  return (
    <div className="database-lookup-results-stack">
      <section className="database-lookup-result-summary">
        <div>
          <span>{selectedOption.label}</span>
          <h4>{data.exists ? "找到精确匹配" : "未找到精确匹配"}</h4>
        </div>
        <div className="database-lookup-summary-badges">
          <span>{data.total} 条</span>
          <span>{data.query_time_ms.toFixed(1)} ms</span>
        </div>
        <code>{data.canonical_smiles}</code>
      </section>

      {data.results.length > 0 ? (
        data.results.map((result) => <LookupResultCard key={`${result.source_column}-${result.record_id}`} result={result} />)
      ) : (
        <DrawerEmptyState title="未找到精确匹配" description="所选数据表中没有与 canonical SMILES 完全一致的记录。" />
      )}
    </div>
  );
}

export function DatabaseQueryPage({ structure }: DatabaseQueryPageProps) {
  const [selectedTable, setSelectedTable] = useState<SmilesLookupTable>("polymers");
  const [data, setData] = useState<SmilesLookupResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isFlipped, setIsFlipped] = useState(false);
  const [isFlipping, setIsFlipping] = useState(false);
  const [isSyncing, setIsSyncing] = useState(false);
  const [isClearing, setIsClearing] = useState(false);
  const [isImportingImage, setIsImportingImage] = useState(false);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const [isTableMenuOpen, setIsTableMenuOpen] = useState(false);
  const [isTextDirty, setIsTextDirty] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [hasAnalysisRun, setHasAnalysisRun] = useState(false);
  const [isAnalysisOpen, setIsAnalysisOpen] = useState(false);
  const [panelWidth, setPanelWidth] = useState(380);
  const lookupRequestId = useRef(0);
  const previousSmilesRef = useRef(structure.smiles);
  const debounceTimerRef = useRef<number | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const smiles = structure.smiles;
  const hasSmiles = smiles.trim().length > 0;
  const isBusy = isLoading || isSyncing || isClearing || isImportingImage;
  const canSubmit = hasSmiles && !isBusy;
  const selectedOption = tableOptionByValue[selectedTable];

  function getKetcher() {
    return structure.iframeRef.current?.contentWindow?.ketcher;
  }

  async function waitForKetcher(timeoutMs = 4000): Promise<KetcherApi | null> {
    const startedAt = Date.now();
    while (Date.now() - startedAt < timeoutMs) {
      const ketcher = getKetcher();
      if (ketcher && typeof ketcher.setMolecule === "function") return ketcher;
      await new Promise((resolve) => window.setTimeout(resolve, 120));
    }
    return null;
  }

  async function captureEditorSnapshot(ketcher: KetcherApi): Promise<KetcherSnapshot> {
    const fallbackSmiles = smiles.trim();
    let editorSmiles = fallbackSmiles;
    try {
      editorSmiles = (await readEditorSmiles(ketcher)) || fallbackSmiles;
    } catch {
      editorSmiles = fallbackSmiles;
    }

    let molfile = "";
    try {
      molfile = await readEditorMolfile(ketcher);
    } catch {
      molfile = "";
    }

    return { smiles: editorSmiles, molfile };
  }

  async function restoreEditorSnapshot(ketcher: KetcherApi, snapshot: KetcherSnapshot) {
    const restoreStructure = snapshot.molfile || snapshot.smiles;
    if (!restoreStructure) {
      if (typeof ketcher.clear === "function") await ketcher.clear();
      else if (typeof ketcher.setMolecule === "function") await ketcher.setMolecule("");
      await waitForKetcherCommit();
      structure.setSmiles("");
      setIsTextDirty(false);
      return;
    }
    if (typeof ketcher.setMolecule !== "function") throw new Error("结构编辑器无法恢复原画布。");

    await ketcher.setMolecule(restoreStructure);
    await waitForKetcherCommit();

    let restoredSmiles = snapshot.smiles;
    try {
      const editorSmiles = await waitForEditorSmilesState(ketcher, (value) => Boolean(value));
      restoredSmiles = snapshot.smiles && !shouldAdoptEditorSmiles(snapshot.smiles, editorSmiles) ? snapshot.smiles : editorSmiles || snapshot.smiles;
    } catch {
      restoredSmiles = snapshot.smiles;
    }
    structure.setSmiles(restoredSmiles);
    setIsTextDirty(false);
  }

  async function loadStructureIntoKetcher(molfile: string, smilesValue: string, activeKetcher?: KetcherApi) {
    const ketcher = activeKetcher ?? await waitForKetcher();
    if (!ketcher) {
      structure.setIsReady(false);
      throw new Error("结构编辑器尚未就绪，请稍后重试。");
    }
    const normalizedSmiles = smilesValue.trim();
    if (molfile.trim()) {
      try {
        const editorSmiles = await writeImageStructureToEditor(ketcher, molfile);
        if (editorSmiles) return shouldAdoptEditorSmiles(normalizedSmiles, editorSmiles) ? editorSmiles : normalizedSmiles || editorSmiles;
      } catch (error) {
        console.error("Failed to load recognized molfile into Ketcher", error);
        if (!normalizedSmiles) throw error;
      }
    }

    let lastError: unknown = null;
    for (const candidate of getEditorLoadCandidates(normalizedSmiles)) {
      try {
        const editorSmiles = await writeImageStructureToEditor(ketcher, candidate);
        return shouldAdoptEditorSmiles(normalizedSmiles, editorSmiles) ? editorSmiles : normalizedSmiles || editorSmiles;
      } catch (error) {
        lastError = error;
      }
    }
    throw lastError instanceof Error ? lastError : new Error("Ketcher 未接受识别出的结构。");
  }

  function clearLookupFeedback(options: { closePanel?: boolean } = {}) {
    lookupRequestId.current += 1;
    setData(null);
    setError(null);
    setIsLoading(false);
    setHasAnalysisRun(false);
    if (options.closePanel !== false) {
      setIsAnalysisOpen(false);
    }
  }

  useEffect(() => {
    return () => {
      if (debounceTimerRef.current !== null) window.clearTimeout(debounceTimerRef.current);
    };
  }, []);

  useEffect(() => {
    if (previousSmilesRef.current !== smiles) {
      previousSmilesRef.current = smiles;
      clearLookupFeedback();
    }
  }, [smiles]);

  useEffect(() => {
    let cancelled = false;
    let attempts = 0;
    let isChecking = false;

    const timer = window.setInterval(() => {
      if (isChecking) return;
      attempts += 1;
      const ketcher = getKetcher();
      if (!ketcher) {
        if (attempts >= 50) window.clearInterval(timer);
        return;
      }

      isChecking = true;
      void (async () => {
        const sourceSmiles = smiles.trim();
        if (sourceSmiles && typeof ketcher.setMolecule === "function") {
          const editorSmiles = typeof ketcher.getSmiles === "function" ? await readEditorSmiles(ketcher) : "";
          if (!editorSmiles) {
            const adoptedSmiles = await loadBestEffortStructureToEditor(ketcher, sourceSmiles);
            if (!cancelled && adoptedSmiles && adoptedSmiles !== sourceSmiles && shouldAdoptEditorSmiles(sourceSmiles, adoptedSmiles)) {
              structure.setSmiles(adoptedSmiles);
            }
          }
        }
        if (!cancelled) structure.setIsReady(true);
        window.clearInterval(timer);
      })().catch(() => {
        if (!cancelled) structure.setIsReady(false);
        if (attempts >= 50) window.clearInterval(timer);
        isChecking = false;
      });
    }, 300);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [smiles, structure.iframeRef, structure.setIsReady, structure.setSmiles]);

  function scheduleLoadSmiles(value: string) {
    if (debounceTimerRef.current !== null) window.clearTimeout(debounceTimerRef.current);
    debounceTimerRef.current = window.setTimeout(() => {
      const sourceSmiles = value.trim();
      const ketcher = getKetcher();
      if (!sourceSmiles || !ketcher || typeof ketcher.setMolecule !== "function") return;
      void loadBestEffortStructureToEditor(ketcher, sourceSmiles).catch(() => undefined);
    }, 360);
  }

  function updateSmiles(value: string) {
    setIsTextDirty(true);
    structure.setSmiles(value);
    setFeedback(value.trim() ? null : "SMILES 已清空。请选择画布绘制或直接输入结构。");
    scheduleLoadSmiles(value);
  }

  async function syncSmilesFromCanvas(options: { preserveExisting?: boolean; suppressLookupReset?: boolean } = {}) {
    setIsSyncing(true);
    try {
      const fallbackSmiles = smiles.trim();
      const ketcher = getKetcher();
      if (!ketcher || typeof ketcher.getSmiles !== "function") {
        if (fallbackSmiles) {
          setFeedback("结构编辑器未就绪，已使用输入框中的 SMILES。");
          return fallbackSmiles;
        }
        setFeedback("结构编辑器尚未就绪，请稍后重试。");
        return "";
      }

      const editorSmiles = await readEditorSmiles(ketcher);
      if (editorSmiles) {
        const nextSmiles = shouldAdoptEditorSmiles(fallbackSmiles, editorSmiles) ? editorSmiles : fallbackSmiles || editorSmiles;
        if (options.suppressLookupReset) previousSmilesRef.current = nextSmiles;
        structure.setSmiles(nextSmiles);
        setIsTextDirty(false);
        setFeedback(nextSmiles === editorSmiles ? "SMILES 已从画布同步。" : "已保留当前聚合物 SMILES。有效端基没有被剥离。");
        return nextSmiles;
      }

      if (options.preserveExisting && fallbackSmiles) {
        setFeedback("画布为空，已继续使用输入框中的 SMILES。");
        return fallbackSmiles;
      }

      if (options.suppressLookupReset) previousSmilesRef.current = "";
      structure.setSmiles("");
      setIsTextDirty(false);
      setFeedback("请先在画布绘制结构，或直接输入 SMILES。");
      return "";
    } finally {
      setIsSyncing(false);
    }
  }

  async function clearCanvas() {
    setIsTableMenuOpen(false);
    setIsClearing(true);
    try {
      const ketcher = getKetcher();
      if (ketcher?.clear) await ketcher.clear();
      else if (ketcher?.setMolecule) await ketcher.setMolecule("");
      await waitForKetcherCommit();
      structure.setSmiles("");
      setIsTextDirty(false);
      setIsFlipped(false);
      clearLookupFeedback();
      setFeedback("画布、SMILES 和查询结果已清空。");
    } catch (clearError) {
      console.error("Failed to clear database query canvas", clearError);
      setFeedback(clearError instanceof Error ? clearError.message : "清空画布失败。");
    } finally {
      setIsClearing(false);
    }
  }

  async function toggle3D() {
    if (isFlipping || isBusy) return;
    setIsTableMenuOpen(false);
    setIsFlipping(true);
    try {
      if (!isFlipped) {
        const nextSmiles = await syncSmilesFromCanvas({ preserveExisting: true });
        if (!nextSmiles) return;
      }
      setIsFlipped((current) => !current);
    } finally {
      window.setTimeout(() => setIsFlipping(false), 620);
    }
  }

  async function copySmiles() {
    if (!hasSmiles) return;
    try {
      if (!navigator.clipboard) {
        throw new Error("Clipboard API unavailable");
      }
      await navigator.clipboard.writeText(smiles.trim());
      setCopyState("copied");
      setFeedback("SMILES 已复制。");
    } catch {
      setCopyState("failed");
      setFeedback("复制失败，请手动选择文本。");
    } finally {
      window.setTimeout(() => setCopyState("idle"), 1200);
    }
  }

  async function importImageFile(file: File) {
    if (!file.type.startsWith("image/")) {
      setFeedback("请选择图片文件。");
      return;
    }

    let previousSnapshot: KetcherSnapshot | null = null;
    const previousFlipped = isFlipped;
    setIsTableMenuOpen(false);
    setIsImportingImage(true);
    setFeedback("正在识别结构图片...");

    try {
      const result = await recognizeStructureImage(file);
      const molfile = result.molfile?.trim() ?? "";
      const recognizedSmiles = result.smiles.trim();
      if (!molfile && !recognizedSmiles) throw new Error("识别结果未返回结构。");

      const ketcher = await waitForKetcher();
      if (!ketcher) {
        structure.setIsReady(false);
        throw new Error("结构编辑器尚未就绪，请稍后重试。");
      }

      previousSnapshot = await captureEditorSnapshot(ketcher);
      setIsFlipped(false);
      setFeedback("正在写入结构画布...");
      const nextSmiles = await loadStructureIntoKetcher(molfile, recognizedSmiles, ketcher);
      structure.setSmiles(nextSmiles);
      setIsTextDirty(false);
      structure.setIsReady(true);
      setFeedback(result.warnings.length > 0 ? `图片结构已导入：${result.warnings[0]}` : "图片结构已导入。");
    } catch (importError) {
      console.error("Failed to import structure image in database query", importError);
      const message = importError instanceof Error ? importError.message : "图片导入失败。";
      if (previousSnapshot) {
        const ketcher = await waitForKetcher(1000);
        if (ketcher) {
          try {
            await restoreEditorSnapshot(ketcher, previousSnapshot);
            setIsFlipped(previousFlipped);
            structure.setIsReady(true);
            setFeedback(`${message} 已恢复原画布。`);
          } catch (restoreError) {
            console.error("Failed to restore Ketcher canvas after database image import failure", restoreError);
            setFeedback(`${message} 原画布恢复失败，请手动检查。`);
          }
        } else {
          setFeedback(`${message} 结构编辑器不可用，无法恢复原画布。`);
        }
      } else {
        setFeedback(message);
      }
    } finally {
      setIsImportingImage(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  function handleTableChange(nextTable: SmilesLookupTable) {
    setIsTableMenuOpen(false);
    if (nextTable !== selectedTable) {
      clearLookupFeedback();
      setFeedback(`已切换到 ${tableOptionByValue[nextTable].shortLabel}。`);
    }
    setSelectedTable(nextTable);
  }

  async function handleSubmit() {
    setIsTableMenuOpen(false);
    if (isLoading) return;
    let querySmiles = smiles.trim();
    if (!querySmiles) {
      querySmiles = await syncSmilesFromCanvas({ suppressLookupReset: true });
    } else if (!isTextDirty) {
      querySmiles = (await syncSmilesFromCanvas({ preserveExisting: true, suppressLookupReset: true })) || querySmiles;
    }

    if (!querySmiles) {
      setFeedback("请先绘制、导入或输入 SMILES，再运行数据库查询。");
      return;
    }

    const requestId = lookupRequestId.current + 1;
    lookupRequestId.current = requestId;
    setHasAnalysisRun(true);
    setIsAnalysisOpen(true);
    setIsLoading(true);
    setError(null);
    setData(null);
    setFeedback(`正在查询 ${selectedOption.shortLabel}。`);

    try {
      const result = await lookupSmilesInDatabase({ smiles: querySmiles, table: selectedTable });
      if (lookupRequestId.current !== requestId) return;
      setData(result);
      setFeedback(result.exists ? `查询完成：找到 ${result.total} 条精确匹配。` : "查询完成：未找到精确匹配。");
    } catch (requestError) {
      if (lookupRequestId.current !== requestId) return;
      setData(null);
      setError(requestError instanceof Error ? requestError.message : "数据库查询失败。");
      setFeedback("查询失败，详情已显示在右侧分析工作台。");
    } finally {
      if (lookupRequestId.current === requestId) setIsLoading(false);
    }
  }

  function startResize(event: ReactMouseEvent<HTMLDivElement>) {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = panelWidth;
    function handleMove(moveEvent: MouseEvent) {
      setPanelWidth(clamp(startWidth + startX - moveEvent.clientX, 320, 560));
    }
    function handleUp() {
      document.removeEventListener("mousemove", handleMove);
      document.removeEventListener("mouseup", handleUp);
    }
    document.addEventListener("mousemove", handleMove);
    document.addEventListener("mouseup", handleUp);
  }

  return (
    <div className="polymer-desktop-page polymer-desktop-page--embedded database-query-page">
      <h1 className="polymer-page-title">数据库查询</h1>
      <div className="polymer-centered-shell database-query-centered-shell">
        <div className="polymer-centered-column database-query-centered-column">
          <div className="app-container">
            <div className="main-layout">
              <main className="main-content">
                <div className="polymer-module-header database-query-module-header">
                  <div className="header-actions" id="database-query-header-actions">
                    <input ref={fileInputRef} type="file" accept="image/*" style={{ display: "none" }} onChange={(event) => { const file = event.currentTarget.files?.[0]; if (file) void importImageFile(file); }} />
                    <button className="btn btn--outline btn--sm" id="btn-import-img" type="button" onClick={() => fileInputRef.current?.click()} disabled={isImportingImage || isSyncing || isClearing || isLoading}>
                      {isImportingImage ? <LoaderCircle width={14} height={14} className="spin-icon" /> : <ImagePlus width={14} height={14} />}
                      <span>导入图片</span>
                    </button>
                    <button className="btn btn--outline btn--sm" id="btn-clear-canvas" type="button" onClick={() => void clearCanvas()} disabled={isClearing || isImportingImage}>
                      {isClearing ? <LoaderCircle width={14} height={14} className="spin-icon" /> : <Eraser width={14} height={14} />}
                      <span>清空画布</span>
                    </button>
                    <button className="btn btn--outline btn--sm" id="btn-sync-canvas" type="button" onClick={() => void syncSmilesFromCanvas()} disabled={isSyncing || isClearing || isImportingImage}>
                      {isSyncing ? <LoaderCircle width={14} height={14} className="spin-icon" /> : <RefreshCcw width={14} height={14} />}
                      <span>生成SMILES</span>
                    </button>
                    <button className="btn btn--outline btn--sm" id="btn-toggle-3d" type="button" title="在 2D 画布与 3D 构象之间翻转切换" onClick={() => void toggle3D()} disabled={isFlipping || isSyncing || isImportingImage || isClearing}>
                      <Box width={14} height={14} />
                      <span>{isFlipped ? "2D画布" : "3D构象"}</span>
                    </button>
                  </div>
                </div>

                <div className="polymer-workspace database-query-workspace">
                  <div className="workspace-left">
                    <div className={`workspace-card editor-card flip-card-container${isFlipped ? " is-flipped" : ""}${isFlipping ? " is-flipping" : ""}`}>
                      <div className="flip-card-inner">
                        <div className="flip-card-front">
                          <div className="editor-iframe-container">
                            <iframe title="Ketcher 结构编辑器" src="/ketcher/index.html" id="database-query-ketcher-iframe" ref={structure.iframeRef} className="ketcher-iframe" />
                          </div>
                        </div>
                        <div className="flip-card-back">
                          <div className="canvas-3d-container-full real-3d-container">
                            <StructurePreview3D smiles={smiles} variant="bare" visualStyle="polished-atoms" className="polymer-real-3d-preview" contentClassName="polymer-real-3d-content" previewClassName="polymer-real-3d-frame" viewerClassName="polymer-real-3d-viewer" />
                            <div className="canvas-tip">真实 3D 构象，支持拖拽旋转与缩放</div>
                          </div>
                        </div>
                      </div>
                    </div>

                    <div className={`smiles-smart-capsule database-query-capsule${hasSmiles ? " has-content" : ""}`}>
                      <div className="smiles-textarea-floating-actions">
                        <button className={`smiles-floating-action-btn btn-copy${copyState === "copied" ? " copied" : ""}`} type="button" title="复制 SMILES" onClick={() => void copySmiles()} disabled={!hasSmiles}>
                          <Copy width={13} height={13} />
                        </button>
                        <button className="smiles-floating-action-btn btn-clear" type="button" title="清空输入内容" onClick={() => updateSmiles("")} disabled={!hasSmiles}>
                          <Trash2 width={13} height={13} />
                        </button>
                      </div>

                      <textarea
                        className="smiles-textarea database-query-smiles-textarea"
                        value={smiles}
                        placeholder="输入聚合物 SMILES（例如 *CC*、CCO），或在上方画布绘制后点击生成SMILES"
                        onChange={(event) => updateSmiles(event.target.value)}
                      />

                      <div className="capsule-toolbar database-query-capsule-toolbar">
                        <div className="database-query-selected-table">
                          <span>{selectedOption.label}</span>
                          <code>{selectedOption.fields}</code>
                        </div>
                        <div className={`split-button-container database-query-split-button${isTableMenuOpen ? " menu-open" : ""}${isLoading ? " is-calculating" : ""}${!hasSmiles ? " is-disabled" : ""}`}>
                          <button className="btn-generate-main database-query-run-main" type="button" disabled={!canSubmit} onClick={() => void handleSubmit()} title="运行当前数据表的精确查询">
                            {isLoading ? (
                              <span className="dots-loader"><span className="dot dot-1" /><span className="dot dot-2" /><span className="dot dot-3" /></span>
                            ) : (
                              <>
                                <Search width={12} height={12} />
                                <span className="btn-text">{selectedOption.shortLabel} 精确查询</span>
                              </>
                            )}
                          </button>
                          <button className="btn-generate-dropdown database-query-table-dropdown" type="button" disabled={isBusy} title="选择查询数据表" onClick={(event) => { event.stopPropagation(); setIsTableMenuOpen((current) => !current); }}>
                            <ChevronDown width={10} height={10} />
                          </button>
                          <div className="generate-dropdown-menu database-query-table-menu" style={{ display: isTableMenuOpen ? "flex" : "none" }}>
                            {tableOptions.map((option) => (
                              <div key={option.value} className={`dropdown-item${selectedTable === option.value ? " active" : ""}`} title={option.detail} onClick={(event) => { event.stopPropagation(); handleTableChange(option.value); }}>
                                <Table2 width={12} height={12} />
                                <span>{option.label}</span>
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>

                      {feedback ? <div className="polymer-capsule-feedback">{feedback}</div> : null}
                    </div>
                  </div>
                </div>
              </main>
            </div>
          </div>
        </div>
      </div>

      <button className="btn-expand-analysis" type="button" title="展开查询结果" style={{ display: hasAnalysisRun && !isAnalysisOpen ? "flex" : "none" }} onClick={() => setIsAnalysisOpen(true)}>
        <Search width={14} height={14} />
      </button>
      <div className="analysis-resizer" title="拖拽调整宽度" style={{ display: isAnalysisOpen ? "flex" : "none", height: "100%", right: panelWidth }} onMouseDown={startResize}>
        <div className="resizer-line" />
      </div>
      <div className="workspace-analysis-panel workspace-card database-query-analysis-panel" style={{ display: isAnalysisOpen ? "flex" : "none", height: "100%", margin: 0, width: panelWidth }}>
        <div className="analysis-panel-header">
          <div className="panel-header-title">
            <span className="panel-tag-name">Database Lookup</span>
            <h3>查询结果</h3>
          </div>
          <button className="btn-close-analysis" type="button" title="收起查询结果" onClick={() => setIsAnalysisOpen(false)}>
            <X width={14} height={14} />
          </button>
        </div>
        <div className="analysis-panel-body">
          <LookupDrawerContent data={data} error={error} isLoading={isLoading} selectedTable={selectedTable} />
        </div>
      </div>
    </div>
  );
}
