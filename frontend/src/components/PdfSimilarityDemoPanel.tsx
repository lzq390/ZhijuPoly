import {
  AlertTriangle,
  Clock3,
  FileClock,
  FileText,
  History,
  ImageOff,
  LoaderCircle,
  RefreshCw,
  Trash2,
  Upload
} from "lucide-react";
import {
  type ChangeEvent,
  type CSSProperties,
  type DragEvent,
  type ReactNode,
  useEffect,
  useMemo,
  useRef,
  useState
} from "react";
import { KnowledgeDetailDrawer, type KnowledgeDrawerTab } from "./knowledge-search/KnowledgeDetailDrawer";

const UPLOAD_HISTORY_STORAGE_KEY = "polyprop.pdfSimilarityDemo.uploadHistory";
const MAX_HISTORY_ITEMS = 12;
const SIMILAR_PAPERS_DELAY_MS = 950;

export type SimilarPaperCard = {
  id: string;
  title: string;
  authors: string;
  year: string;
  journal: string;
  doi: string;
  reason: string;
  image: string;
};

type UploadHistoryRecord = {
  id: string;
  fileName: string;
  uploadedAt: string;
  status: string;
  note: string;
  papers: SimilarPaperCard[];
};

type PdfStatus = "idle" | "loading" | "success" | "error";
type DrawerView = "detail" | "history";

type PdfSimilarityDemoPanelProps = {
  modeNavigation: ReactNode;
};

const curatedPaperCards: SimilarPaperCard[] = [
  { id: "similar-paper-1", title: "Similar Paper 1", authors: "M. Ivanov, S. Patel, and E. Brooks", year: "2024", journal: "Information Processing Letters", doi: "10.1145/24.1187/ipr.2024.019", reason: "Aligned document-structure cues and abstract segmentation patterns.", image: "/images/pdf-similarity/similar_paper_1.png" },
  { id: "similar-paper-2", title: "Similar Paper 2", authors: "L. Chen, A. Moreno, and J. Sinclair", year: "2023", journal: "Neural Computing Review", doi: "10.1109/ncr.2023.44126", reason: "Matched retrieval embeddings around citation density and topic framing.", image: "/images/pdf-similarity/similar_paper_2.png" },
  { id: "similar-paper-3", title: "Similar Paper 3", authors: "H. Sato, R. Mendes, and P. Klein", year: "2025", journal: "Journal of Applied AI Systems", doi: "10.1016/jaais.2025.00814", reason: "Similar methodology blocks and evaluation pipeline terminology.", image: "/images/pdf-similarity/similar_paper_3.png" },
  { id: "similar-paper-4", title: "Similar Paper 4", authors: "D. Alvarez, C. Ng, and T. Hoffman", year: "2022", journal: "Pattern Discovery Quarterly", doi: "10.1007/pdq.2022.30118", reason: "Related graph features and overlapping keyword clusters in the intro.", image: "/images/pdf-similarity/similar_paper_4.png" },
  { id: "similar-paper-5", title: "Similar Paper 5", authors: "N. Rahman, V. Costa, and B. Osei", year: "2024", journal: "Computational Research Forum", doi: "10.5555/crf.2024.88210", reason: "Comparable benchmark setup and discussion structure across sections.", image: "/images/pdf-similarity/similar_paper_5.png" },
  { id: "similar-paper-6", title: "Similar Paper 6", authors: "K. Dubois, Y. Park, and A. Menon", year: "2025", journal: "Digital Scholarship Systems", doi: "10.1142/dss.2025.56047", reason: "High overlap in semantic signals from figures, captions, and references.", image: "/images/pdf-similarity/similar_paper_6.png" }
];

function isDesktopViewport() {
  return typeof window === "undefined" || typeof window.matchMedia !== "function"
    ? true
    : !window.matchMedia("(max-width: 899px)").matches;
}

function readStorage(): UploadHistoryRecord[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(UPLOAD_HISTORY_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed
      .map((item) => sanitizeHistoryRecord(item))
      .filter((item): item is UploadHistoryRecord => item !== null)
      .slice(0, MAX_HISTORY_ITEMS);
  } catch {
    return [];
  }
}

function sanitizeHistoryRecord(value: unknown): UploadHistoryRecord | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Partial<UploadHistoryRecord>;
  const fileName = String(record.fileName || "").trim();
  if (!fileName) return null;
  return {
    id: String(record.id || `${record.uploadedAt || Date.now()}-${fileName}`),
    fileName,
    uploadedAt: String(record.uploadedAt || new Date().toISOString()),
    status: String(record.status || "Demo ready"),
    note: String(record.note || "Local preview"),
    papers: curatedPaperCards
  };
}

function writeStorage(records: UploadHistoryRecord[]) {
  if (typeof window === "undefined") return true;
  try {
    window.localStorage.setItem(UPLOAD_HISTORY_STORAGE_KEY, JSON.stringify(records));
    return true;
  } catch {
    return false;
  }
}

function isPdfFile(file: File) {
  return file.type === "application/pdf" || file.name.toLocaleLowerCase().endsWith(".pdf");
}

function formatTimestamp(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未知时间";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }).format(date);
}

function PaperImage({ paper }: { paper: SimilarPaperCard }) {
  const [failed, setFailed] = useState(false);
  return failed ? (
    <span className="ks-paper-image-fallback"><ImageOff aria-hidden="true" /><small>预览不可用</small></span>
  ) : (
    <img src={paper.image} alt={`${paper.title} 固定示例预览`} loading="lazy" onError={() => setFailed(true)} />
  );
}

function buildPaperTabs(paper: SimilarPaperCard): KnowledgeDrawerTab[] {
  return [
    {
      id: "overview",
      label: "论文概览",
      content: (
        <div className="ks-drawer-stack">
          <section className="ks-drawer-section"><p className="ks-drawer-title-main">{paper.title}</p><p className="ks-drawer-title-secondary">{paper.authors}</p><div className="ks-chip-row"><span className="ks-chip">{paper.year}</span><span className="ks-chip">{paper.journal}</span></div></section>
          <section className="ks-drawer-section"><div className="ks-paper-drawer-preview"><PaperImage paper={paper} /></div></section>
          <section className="ks-drawer-section"><div className="ks-drawer-callout is-warning"><AlertTriangle aria-hidden="true" /><span>该论文卡片和图片为固定演示资产，与所选择 PDF 的实际内容无关。</span></div></section>
        </div>
      )
    },
    {
      id: "reason",
      label: "DOI 与原因",
      content: (
        <div className="ks-drawer-stack">
          <section className="ks-drawer-section"><h3><FileText aria-hidden="true" />示例元数据</h3><dl className="ks-detail-grid"><div><dt>DOI</dt><dd>{paper.doi}</dd></div><div><dt>期刊</dt><dd>{paper.journal}</dd></div><div><dt>年份</dt><dd>{paper.year}</dd></div><div><dt>作者</dt><dd>{paper.authors}</dd></div></dl></section>
          <section className="ks-drawer-section"><h3><FileText aria-hidden="true" />示例相似原因</h3><p className="ks-long-copy">{paper.reason}</p></section>
          <section className="ks-drawer-section"><div className="ks-drawer-callout"><FileText aria-hidden="true" /><span>DOI 与相似原因均来自准备好的本地演示数据，不是对上传文件的计算结果。</span></div></section>
        </div>
      )
    }
  ];
}

export function PdfSimilarityDemoPanel({ modeNavigation }: PdfSimilarityDemoPanelProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const revealTimerRef = useRef<number | null>(null);
  const [historyItems, setHistoryItems] = useState<UploadHistoryRecord[]>([]);
  const [activeRecordId, setActiveRecordId] = useState("");
  const [pendingRecordId, setPendingRecordId] = useState("");
  const [fileName, setFileName] = useState("");
  const [status, setStatus] = useState<PdfStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [storageWarning, setStorageWarning] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [selectedPaperIndex, setSelectedPaperIndex] = useState<number | null>(null);
  const [drawerView, setDrawerView] = useState<DrawerView>("detail");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerWidth, setDrawerWidth] = useState(380);
  const [confirmClear, setConfirmClear] = useState(false);
  const activeRecord = historyItems.find((item) => item.id === activeRecordId) ?? null;
  const visiblePapers = status === "success" ? activeRecord?.papers ?? [] : [];
  const selectedPaper = selectedPaperIndex === null ? null : visiblePapers[selectedPaperIndex] ?? null;

  useEffect(() => {
    const history = readStorage();
    setHistoryItems(history);
    if (history[0]) {
      setActiveRecordId(history[0].id);
      setFileName(history[0].fileName);
      setStatus("success");
      setSelectedPaperIndex(0);
    }
  }, []);

  useEffect(() => () => {
    if (revealTimerRef.current !== null) window.clearTimeout(revealTimerRef.current);
  }, []);

  function persistHistory(nextHistory: UploadHistoryRecord[]) {
    setHistoryItems(nextHistory);
    const saved = writeStorage(nextHistory);
    setStorageWarning(saved ? null : "浏览器存储不可用；上传历史仅保留在本次会话中。");
  }

  function scheduleResults(record: UploadHistoryRecord) {
    if (revealTimerRef.current !== null) window.clearTimeout(revealTimerRef.current);
    setPendingRecordId(record.id);
    setFileName(record.fileName);
    setStatus("loading");
    setError(null);
    setSelectedPaperIndex(null);
    setDrawerOpen(false);
    revealTimerRef.current = window.setTimeout(() => {
      setActiveRecordId(record.id);
      setPendingRecordId("");
      setStatus("success");
      setSelectedPaperIndex(0);
      setDrawerView("detail");
      if (isDesktopViewport()) setDrawerOpen(true);
      revealTimerRef.current = null;
    }, SIMILAR_PAPERS_DELAY_MS);
  }

  function applyFile(file: File | undefined) {
    if (!file) return;
    setFileName(file.name || "未命名文件");
    if (!isPdfFile(file)) {
      if (revealTimerRef.current !== null) window.clearTimeout(revealTimerRef.current);
      setPendingRecordId("");
      setStatus("error");
      setError("仅支持 PDF 文件；页面只校验 MIME 类型或 .pdf 扩展名。");
      setSelectedPaperIndex(null);
      setDrawerOpen(false);
      return;
    }

    const uploadedAt = new Date().toISOString();
    const record: UploadHistoryRecord = {
      id: `${uploadedAt}-${file.name}`,
      fileName: file.name,
      uploadedAt,
      status: "Demo ready",
      note: "Prepared local examples",
      papers: curatedPaperCards
    };
    const nextHistory = [record, ...historyItems.filter((item) => item.fileName !== record.fileName)].slice(0, MAX_HISTORY_ITEMS);
    persistHistory(nextHistory);
    scheduleResults(record);
  }

  function handleFileSelection(event: ChangeEvent<HTMLInputElement>) {
    applyFile(event.target.files?.[0]);
    event.target.value = "";
  }

  function handleDrop(event: DragEvent<HTMLButtonElement>) {
    event.preventDefault();
    setDragging(false);
    applyFile(event.dataTransfer.files?.[0]);
  }

  function deleteHistoryItem(id: string) {
    const nextHistory = historyItems.filter((item) => item.id !== id);
    const deletingPendingRecord = pendingRecordId === id;
    const anotherRecordIsPending = Boolean(pendingRecordId) && !deletingPendingRecord;

    if (deletingPendingRecord && revealTimerRef.current !== null) {
      window.clearTimeout(revealTimerRef.current);
      revealTimerRef.current = null;
      setPendingRecordId("");
    }

    persistHistory(nextHistory);

    if (deletingPendingRecord || (activeRecordId === id && !anotherRecordIsPending)) {
      const next = nextHistory.find((item) => item.id === activeRecordId) ?? nextHistory[0];
      setActiveRecordId(next?.id ?? "");
      setFileName(next?.fileName ?? "");
      setStatus(next ? "success" : "idle");
      setError(null);
      setSelectedPaperIndex(next ? 0 : null);
    } else if (activeRecordId === id) {
      setActiveRecordId("");
    }
  }

  function clearHistory() {
    if (revealTimerRef.current !== null) window.clearTimeout(revealTimerRef.current);
    persistHistory([]);
    setActiveRecordId("");
    setPendingRecordId("");
    setFileName("");
    setStatus("idle");
    setError(null);
    setSelectedPaperIndex(null);
    setDrawerOpen(false);
    setConfirmClear(false);
  }

  const detailTabs = useMemo(
    () => (selectedPaper ? buildPaperTabs(selectedPaper) : []),
    [selectedPaper]
  );
  const statusLabel = status === "loading" ? "正在匹配" : status === "success" ? "示例就绪" : status === "error" ? "需要处理" : "等待上传";

  return (
    <div className={`ks-panel-layout${drawerOpen ? " is-drawer-open" : ""}`} style={{ "--ks-drawer-width": `${drawerWidth}px` } as CSSProperties}>
      <div className="ks-panel-scroll">
        <div className="ks-workbench-column">
          <div className="ks-module-toolbar">
            <span className="ks-toolbar-status"><i />准备就绪</span>
          </div>

          <section className="ks-surface ks-search-surface">
            <header className="ks-surface-header">
              <div className="ks-surface-heading">
                <span className="ks-surface-mark"><FileText aria-hidden="true" /></span>
                <div className="ks-surface-copy"><h2>PDF 相似度演示</h2><p>展示文件校验、固定结果与浏览器本地历史</p></div>
              </div>
              {modeNavigation}
            </header>

            <div className="ks-form-zone">
              <div className="ks-pdf-notice" role="status"><AlertTriangle aria-hidden="true" /><span><strong>静态演示：</strong>只校验文件类型和文件名，不读取、不上传 PDF 内容；所有结果均来自六篇固定本地样例。</span></div>
              <div className="ks-pdf-upload-row">
                <button
                  className={`ks-pdf-dropzone${dragging ? " is-dragging" : ""}`}
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  onDrop={handleDrop}
                  onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
                  onDragLeave={(event) => { event.preventDefault(); setDragging(false); }}
                >
                  <span><Upload aria-hidden="true" /></span><strong>选择或拖入 PDF 文件</strong><small>文件仅用于演示本地交互状态</small>
                </button>
                <input ref={fileInputRef} className="ks-sr-only" type="file" accept=".pdf,application/pdf" onChange={handleFileSelection} />
                <div className="ks-pdf-file-status"><strong>{statusLabel}</strong><span title={fileName || "尚未选择文件"}>{fileName || "尚未选择文件"}</span></div>
              </div>
              <div className="ks-meta-row"><span className="is-demo"><FileText aria-hidden="true" />Prepared local examples</span><span><History aria-hidden="true" />浏览器本地历史 · 最多 12 条</span></div>
              {storageWarning ? <div className="ks-inline-alert is-warning"><AlertTriangle aria-hidden="true" /><span>{storageWarning}</span></div> : null}
            </div>
          </section>

          <section className="ks-surface ks-results-surface" aria-busy={status === "loading"}>
            <header className="ks-results-header">
              <div className="ks-results-summary"><h2>相似论文示例</h2><p>{status === "success" ? "6 篇固定结果 · 本地静态预览" : status === "loading" ? "正在准备固定本地样例" : "选择 PDF 后展示固定本地样例"}</p></div>
              <button className="ks-button" type="button" onClick={() => { setDrawerView("history"); setDrawerOpen(true); }}><History aria-hidden="true" />上传历史 <b>{historyItems.length}</b></button>
            </header>

            <div className="ks-results-body">
              {status === "loading" ? (
                <div className="ks-state is-loading" role="status"><span><LoaderCircle className="is-spinning" aria-hidden="true" /></span><h3>正在匹配固定示例论文</h3><p>{fileName} · 此过程只模拟界面加载状态。</p><div className="ks-progress-track"><span style={{ width: "72%" }} /></div></div>
              ) : null}
              {status === "error" ? (
                <div className="ks-state is-error" role="alert"><span><AlertTriangle aria-hidden="true" /></span><h3>文件类型不受支持</h3><p>{error}</p><button className="ks-button" type="button" onClick={() => fileInputRef.current?.click()}><Upload aria-hidden="true" />重新选择</button></div>
              ) : null}
              {status === "idle" ? (
                <div className="ks-state"><span><FileText aria-hidden="true" /></span><h3>选择 PDF 预览相似论文</h3><p>不会读取文件内容或发起网络请求。</p><button className="ks-button" type="button" onClick={() => fileInputRef.current?.click()}><Upload aria-hidden="true" />选择 PDF 文件</button></div>
              ) : null}
              {status === "success" ? (
                <div className="ks-paper-grid">
                  {visiblePapers.map((paper, index) => (
                    <button className={`ks-paper-card${selectedPaperIndex === index ? " is-selected" : ""}`} type="button" aria-pressed={selectedPaperIndex === index} key={paper.id} onClick={() => { setSelectedPaperIndex(index); setDrawerView("detail"); setDrawerOpen(true); }}>
                      <span className="ks-paper-thumb"><PaperImage paper={paper} /></span>
                      <span className="ks-paper-copy"><strong>{paper.title}</strong><span>{paper.authors}</span><small><b>{paper.year}</b><b>{paper.journal}</b></small></span>
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          </section>
        </div>
      </div>

      <KnowledgeDetailDrawer
        id="pdf-similarity-detail"
        open={drawerOpen && (drawerView === "history" || detailTabs.length > 0)}
        width={drawerWidth}
        contentKey={drawerView === "history" ? "pdf-history" : `pdf-paper-${selectedPaper?.id || "empty"}`}
        title={drawerView === "history" ? "PDF 上传历史" : "相似论文详情"}
        subtitle={drawerView === "history" ? `${historyItems.length} 条记录 · 浏览器本地` : selectedPaper ? `固定示例 #${selectedPaper.id.replace("similar-paper-", "")} · PDF Demo` : "选择结果后查看"}
        icon={drawerView === "history" ? <History aria-hidden="true" /> : <FileText aria-hidden="true" />}
        tabs={drawerView === "detail" ? detailTabs : undefined}
        footer={drawerView === "detail" ? <><span><Clock3 aria-hidden="true" />{fileName || "演示 PDF"}</span><span>Static Demo</span></> : undefined}
        reopenLabel={drawerView === "history" ? "查看上传历史" : "查看论文详情"}
        showReopen={drawerView === "history" || detailTabs.length > 0}
        onWidthChange={setDrawerWidth}
        onClose={() => setDrawerOpen(false)}
        onOpen={() => setDrawerOpen(true)}
      >
        {drawerView === "history" ? (
          <div className="ks-drawer-stack">
            <div className="ks-drawer-callout"><History aria-hidden="true" /><span>仅保存文件名、时间和固定结果标识，不保存或读取 PDF 内容。</span></div>
            <div className="ks-history-toolbar">
              {!confirmClear ? <button className="ks-button is-danger" type="button" disabled={!historyItems.length} onClick={() => setConfirmClear(true)}><Trash2 aria-hidden="true" />清空全部</button> : <div className="ks-confirm-row"><span>确认清空浏览器本地历史？</span><button type="button" onClick={clearHistory}>确认</button><button type="button" onClick={() => setConfirmClear(false)}>取消</button></div>}
            </div>
            <div className="ks-history-list">
              {historyItems.length ? historyItems.map((item) => (
                <article className={`ks-history-item${pendingRecordId === item.id ? " is-pending" : ""}`} key={item.id}>
                  <div><strong>{item.fileName}</strong><span>{formatTimestamp(item.uploadedAt)} · 固定 6 篇示例</span></div>
                  <div><button type="button" onClick={() => scheduleResults(item)} disabled={status === "loading" && pendingRecordId === item.id}><RefreshCw className={pendingRecordId === item.id ? "is-spinning" : ""} aria-hidden="true" />恢复</button><button className="is-danger" type="button" onClick={() => deleteHistoryItem(item.id)} aria-label={`删除 ${item.fileName}`}><Trash2 aria-hidden="true" /></button></div>
                </article>
              )) : <div className="ks-state is-compact"><span><FileClock aria-hidden="true" /></span><h3>暂无上传历史</h3><p>选择 PDF 后仅保存本地演示状态。</p></div>}
            </div>
          </div>
        ) : null}
      </KnowledgeDetailDrawer>
    </div>
  );
}
