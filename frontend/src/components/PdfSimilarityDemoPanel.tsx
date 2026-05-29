import {
  type ChangeEvent,
  type CSSProperties,
  type DragEvent,
  useEffect,
  useRef,
  useState
} from "react";
import "./PdfSimilarityDemoPanel.css";

const DEFAULT_API_BASE_URL = (import.meta.env.VITE_DEFAULT_API_BASE_URL || "https://api.vectorengine.ai/v1").trim();
const DEFAULT_MODEL = (import.meta.env.VITE_DEFAULT_MODEL || "gpt-5.4-mini").trim();
const API_SETTINGS_STORAGE_KEY = "polyprop.pdfSimilarityDemo.apiSettings";
const UPLOAD_HISTORY_STORAGE_KEY = "polyprop.pdfSimilarityDemo.uploadHistory";
const MAX_HISTORY_ITEMS = 12;
const SIMILAR_PAPERS_DELAY_MS = 10000;

type ApiSettings = {
  baseUrl: string;
  apiKey: string;
  model: string;
  savedAt: string;
};

type UploadState = {
  state: "idle" | "uploaded" | "error";
  message: string;
  fileName: string;
};

type SimilarPaperCard = {
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

type EditableSettingsField = keyof Pick<ApiSettings, "baseUrl" | "apiKey" | "model">;

const curatedPaperCards: SimilarPaperCard[] = [
  {
    id: "similar-paper-1",
    title: "Similar Paper 1",
    authors: "M. Ivanov, S. Patel, and E. Brooks",
    year: "2024",
    journal: "Information Processing Letters",
    doi: "10.1145/24.1187/ipr.2024.019",
    reason: "Aligned document-structure cues and abstract segmentation patterns.",
    image: "/images/pdf-similarity/similar_paper_1.png"
  },
  {
    id: "similar-paper-2",
    title: "Similar Paper 2",
    authors: "L. Chen, A. Moreno, and J. Sinclair",
    year: "2023",
    journal: "Neural Computing Review",
    doi: "10.1109/ncr.2023.44126",
    reason: "Matched retrieval embeddings around citation density and topic framing.",
    image: "/images/pdf-similarity/similar_paper_2.png"
  },
  {
    id: "similar-paper-3",
    title: "Similar Paper 3",
    authors: "H. Sato, R. Mendes, and P. Klein",
    year: "2025",
    journal: "Journal of Applied AI Systems",
    doi: "10.1016/jaais.2025.00814",
    reason: "Similar methodology blocks and evaluation pipeline terminology.",
    image: "/images/pdf-similarity/similar_paper_3.png"
  },
  {
    id: "similar-paper-4",
    title: "Similar Paper 4",
    authors: "D. Alvarez, C. Ng, and T. Hoffman",
    year: "2022",
    journal: "Pattern Discovery Quarterly",
    doi: "10.1007/pdq.2022.30118",
    reason: "Related graph features and overlapping keyword clusters in the intro.",
    image: "/images/pdf-similarity/similar_paper_4.png"
  },
  {
    id: "similar-paper-5",
    title: "Similar Paper 5",
    authors: "N. Rahman, V. Costa, and B. Osei",
    year: "2024",
    journal: "Computational Research Forum",
    doi: "10.5555/crf.2024.88210",
    reason: "Comparable benchmark setup and discussion structure across sections.",
    image: "/images/pdf-similarity/similar_paper_5.png"
  },
  {
    id: "similar-paper-6",
    title: "Similar Paper 6",
    authors: "K. Dubois, Y. Park, and A. Menon",
    year: "2025",
    journal: "Digital Scholarship Systems",
    doi: "10.1142/dss.2025.56047",
    reason: "High overlap in semantic signals from figures, captions, and references.",
    image: "/images/pdf-similarity/similar_paper_6.png"
  }
];

const defaultApiSettings: ApiSettings = {
  baseUrl: DEFAULT_API_BASE_URL,
  apiKey: "",
  model: DEFAULT_MODEL,
  savedAt: ""
};

function readStorage<T>(key: string, fallback: T): T {
  if (typeof window === "undefined") {
    return fallback;
  }

  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) {
      return fallback;
    }
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function writeStorage(key: string, value: unknown) {
  if (typeof window === "undefined") {
    return true;
  }

  try {
    window.localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

function formatTimestamp(isoString: string) {
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) {
    return "Unknown time";
  }
  return new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(date);
}

function sanitizeSettings(value: Partial<ApiSettings> | null | undefined): ApiSettings {
  return {
    baseUrl: String(value?.baseUrl || defaultApiSettings.baseUrl),
    apiKey: String(value?.apiKey || ""),
    model: String(value?.model || defaultApiSettings.model),
    savedAt: String(value?.savedAt || "")
  };
}

function sanitizePaperCards(value: unknown): SimilarPaperCard[] {
  if (!Array.isArray(value)) {
    return curatedPaperCards;
  }

  const cards = value
    .map((item) => {
      if (!item || typeof item !== "object") {
        return null;
      }

      const paper = item as Partial<SimilarPaperCard>;
      const id = String(paper.id || "").trim();
      const title = String(paper.title || "").trim();
      const image = String(paper.image || "").trim();
      if (!id || !title || !image) {
        return null;
      }

      return {
        id,
        title,
        authors: String(paper.authors || ""),
        year: String(paper.year || ""),
        journal: String(paper.journal || ""),
        doi: String(paper.doi || ""),
        reason: String(paper.reason || ""),
        image
      };
    })
    .filter((paper): paper is SimilarPaperCard => paper !== null);

  return cards.length > 0 ? cards : curatedPaperCards;
}

function sanitizeHistory(value: unknown): UploadHistoryRecord[] {
  if (!Array.isArray(value)) {
    return [];
  }

  return value
    .map((item) => {
      if (!item || typeof item !== "object") {
        return null;
      }
      const record = item as Partial<UploadHistoryRecord>;
      const fileName = String(record.fileName || "").trim();
      if (!fileName) {
        return null;
      }
      return {
        id: String(record.id || `${record.uploadedAt || new Date().toISOString()}-${fileName}`),
        fileName,
        uploadedAt: String(record.uploadedAt || new Date().toISOString()),
        status: String(record.status || "Demo ready"),
        note: String(record.note || "Local preview"),
        papers: sanitizePaperCards(record.papers)
      };
    })
    .filter((item): item is UploadHistoryRecord => item !== null)
    .slice(0, MAX_HISTORY_ITEMS);
}

function buildHistoryRecord(file: File): UploadHistoryRecord {
  const createdAt = new Date().toISOString();
  return {
    id: `${createdAt}-${file.name}`,
    fileName: file.name,
    uploadedAt: createdAt,
    status: "Demo ready",
    note: "Local preview",
    papers: curatedPaperCards
  };
}

function isPdfFile(file: File) {
  return file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
}

function ParticleField() {
  const [particles] = useState(() =>
    Array.from({ length: 42 }, (_, index) => ({
      id: index,
      size: Math.random() * 6 + 2,
      left: `${Math.random() * 100}%`,
      top: `${Math.random() * 100}%`,
      delay: `${Math.random() * 14}s`,
      duration: `${Math.random() * 10 + 10}s`
    }))
  );

  return (
    <div className="pdf-demo-particles" aria-hidden="true">
      {particles.map((particle) => (
        <div
          key={particle.id}
          className="pdf-demo-particle"
          style={
            {
              width: particle.size,
              height: particle.size,
              left: particle.left,
              top: particle.top,
              animationDelay: particle.delay,
              animationDuration: particle.duration
            } satisfies CSSProperties
          }
        />
      ))}
    </div>
  );
}

function SimilarPapersLoading() {
  return (
    <div className="pdf-demo-similar-loading-panel">
      <div className="pdf-demo-similar-loading-backdrop" aria-hidden="true" />
      <div className="pdf-demo-retrieval-orbit" aria-hidden="true">
        <div className="pdf-demo-orbit pdf-demo-orbit-one" />
        <div className="pdf-demo-orbit pdf-demo-orbit-two" />
        <div className="pdf-demo-orbit pdf-demo-orbit-three" />
        <div className="pdf-demo-orbit-dot pdf-demo-dot-one" />
        <div className="pdf-demo-orbit-dot pdf-demo-dot-two" />
        <div className="pdf-demo-orbit-dot pdf-demo-dot-three" />

        <div className="pdf-demo-knowledge-mesh">
          <div className="pdf-demo-knowledge-link pdf-demo-link-one" />
          <div className="pdf-demo-knowledge-link pdf-demo-link-two" />
          <div className="pdf-demo-knowledge-link pdf-demo-link-three" />
          <div className="pdf-demo-knowledge-link pdf-demo-link-four" />
          <div className="pdf-demo-knowledge-node pdf-demo-node-one" />
          <div className="pdf-demo-knowledge-node pdf-demo-node-two" />
          <div className="pdf-demo-knowledge-node pdf-demo-node-three" />
          <div className="pdf-demo-knowledge-node pdf-demo-node-four" />
          <div className="pdf-demo-knowledge-node pdf-demo-node-five" />
        </div>

        <div className="pdf-demo-paper-stack">
          <div className="pdf-demo-paper-sheet pdf-demo-paper-sheet-back">
            <div className="pdf-demo-paper-sheet-lines">
              <span />
              <span />
              <span />
              <span />
            </div>
          </div>
          <div className="pdf-demo-paper-sheet pdf-demo-paper-sheet-mid">
            <div className="pdf-demo-paper-sheet-lines">
              <span />
              <span />
              <span />
              <span />
            </div>
          </div>
          <div className="pdf-demo-paper-sheet pdf-demo-paper-sheet-front">
            <div className="pdf-demo-paper-sheet-lines">
              <span />
              <span />
              <span />
              <span />
              <span />
            </div>
            <div className="pdf-demo-scan-beam" />
          </div>
        </div>
      </div>

      <div className="pdf-demo-loading-text">Searching similar papers...</div>
      <div className="pdf-demo-wave-progress" aria-hidden="true">
        <div className="pdf-demo-wave-progress-fill" />
      </div>
    </div>
  );
}

function PaperPreviewCard({ paper }: { paper: SimilarPaperCard }) {
  const [failed, setFailed] = useState(false);

  return (
    <article className="pdf-demo-paper-preview-card">
      <div className={`pdf-demo-paper-preview-frame ${failed ? "is-fallback" : ""}`}>
        {failed ? (
          <div className="pdf-demo-paper-fallback">
            <span className="pdf-demo-paper-fallback-title">{paper.title}</span>
            <span className="pdf-demo-paper-fallback-text">Preview unavailable</span>
          </div>
        ) : (
          <img
            src={paper.image}
            alt={paper.title}
            className="pdf-demo-paper-preview-image"
            loading="lazy"
            onError={() => setFailed(true)}
          />
        )}
      </div>
      <div className="pdf-demo-paper-preview-footer">
        <span className="pdf-demo-paper-preview-title">{paper.title}</span>
      </div>
    </article>
  );
}

function SettingsModal({
  open,
  settings,
  onChange,
  onClose,
  onSave
}: {
  open: boolean;
  settings: ApiSettings;
  onChange: (field: EditableSettingsField, value: string) => void;
  onClose: () => void;
  onSave: () => void;
}) {
  useEffect(() => {
    if (!open) {
      return;
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) {
    return null;
  }

  return (
    <div
      className="pdf-demo-settings-modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="pdf-demo-settings-title"
      onClick={onClose}
    >
      <section className="pdf-demo-settings-modal-card" onClick={(event) => event.stopPropagation()}>
        <div className="pdf-demo-settings-modal-header">
          <div>
            <p className="pdf-demo-section-kicker">Settings</p>
            <h2 id="pdf-demo-settings-title">API Settings</h2>
          </div>
          <button
            type="button"
            className="pdf-demo-modal-close-btn"
            aria-label="Close settings"
            onClick={onClose}
          />
        </div>

        <div className="pdf-demo-settings-stack">
          <label className="pdf-demo-field">
            <span>API Base URL</span>
            <input
              type="text"
              value={settings.baseUrl}
              placeholder="https://api.vectorengine.ai/v1"
              onChange={(event) => onChange("baseUrl", event.target.value)}
            />
          </label>
          <label className="pdf-demo-field">
            <span>API Key</span>
            <input
              type="password"
              value={settings.apiKey}
              placeholder="Optional for local demo"
              autoComplete="off"
              onChange={(event) => onChange("apiKey", event.target.value)}
            />
          </label>
          <label className="pdf-demo-field">
            <span>Model Name</span>
            <input
              type="text"
              value={settings.model}
              placeholder="gpt-5.4-mini"
              onChange={(event) => onChange("model", event.target.value)}
            />
          </label>
          <button className="pdf-demo-primary-btn" type="button" onClick={onSave}>
            Save Settings
          </button>
          {settings.savedAt ? <p className="pdf-demo-helper-text">Saved: {formatTimestamp(settings.savedAt)}</p> : null}
        </div>
      </section>
    </div>
  );
}

export function PdfSimilarityDemoPanel() {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const revealTimerRef = useRef<number | null>(null);
  const [apiSettings, setApiSettings] = useState<ApiSettings>(defaultApiSettings);
  const [historyItems, setHistoryItems] = useState<UploadHistoryRecord[]>([]);
  const [selectedRecordId, setSelectedRecordId] = useState("");
  const [uploadState, setUploadState] = useState<UploadState>({
    state: "idle",
    message: "Upload a PDF to preview similar papers.",
    fileName: ""
  });
  const [isDragging, setIsDragging] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [isLoadingSimilarPapers, setIsLoadingSimilarPapers] = useState(false);
  const [similarPapersVisible, setSimilarPapersVisible] = useState(false);
  const [pendingRecordId, setPendingRecordId] = useState("");
  const [storageWarning, setStorageWarning] = useState("");

  useEffect(() => {
    const storedSettings = sanitizeSettings(readStorage<Partial<ApiSettings>>(API_SETTINGS_STORAGE_KEY, defaultApiSettings));
    const storedHistory = sanitizeHistory(readStorage<unknown>(UPLOAD_HISTORY_STORAGE_KEY, []));

    setApiSettings(storedSettings);
    setHistoryItems(storedHistory);
    if (storedHistory[0]) {
      setSelectedRecordId(storedHistory[0].id);
      setUploadState({
        state: "uploaded",
        message: "Latest local preview restored.",
        fileName: storedHistory[0].fileName
      });
      setSimilarPapersVisible(true);
    }
  }, []);

  useEffect(() => {
    return () => {
      if (revealTimerRef.current !== null) {
        window.clearTimeout(revealTimerRef.current);
      }
    };
  }, []);

  const selectedRecord = historyItems.find((item) => item.id === selectedRecordId) || null;
  const visiblePapers = similarPapersVisible && selectedRecord?.papers.length ? selectedRecord.papers : [];

  function persistSettings(nextSettings: ApiSettings) {
    const saved = writeStorage(API_SETTINGS_STORAGE_KEY, nextSettings);
    setApiSettings(nextSettings);
    setStorageWarning(saved ? "" : "Local storage is unavailable. Settings are kept for this session only.");
  }

  function handleSettingsChange(field: EditableSettingsField, value: string) {
    setApiSettings((current) => ({ ...current, [field]: value }));
  }

  function handleSaveSettings() {
    const nextSettings: ApiSettings = {
      baseUrl: apiSettings.baseUrl.trim() || defaultApiSettings.baseUrl,
      apiKey: apiSettings.apiKey,
      model: apiSettings.model.trim() || defaultApiSettings.model,
      savedAt: new Date().toISOString()
    };
    persistSettings(nextSettings);
    setSettingsOpen(false);
  }

  function persistHistory(nextHistory: UploadHistoryRecord[]) {
    const saved = writeStorage(UPLOAD_HISTORY_STORAGE_KEY, nextHistory);
    setHistoryItems(nextHistory);
    setStorageWarning(saved ? "" : "Local storage is unavailable. Upload history is kept for this session only.");
  }

  function scheduleSimilarPapers(recordId: string, fileName: string, message: string) {
    if (revealTimerRef.current !== null) {
      window.clearTimeout(revealTimerRef.current);
    }

    setPendingRecordId(recordId);
    setIsLoadingSimilarPapers(true);
    setSimilarPapersVisible(false);
    setUploadState({
      state: "uploaded",
      message,
      fileName
    });

    revealTimerRef.current = window.setTimeout(() => {
      setSelectedRecordId(recordId);
      setPendingRecordId("");
      setIsLoadingSimilarPapers(false);
      setSimilarPapersVisible(true);
      setUploadState({
        state: "uploaded",
        message: "Upload complete. Similar papers are ready.",
        fileName
      });
      revealTimerRef.current = null;
    }, SIMILAR_PAPERS_DELAY_MS);
  }

  function applyUpload(file: File | undefined) {
    if (!file) {
      return;
    }

    if (!isPdfFile(file)) {
      if (revealTimerRef.current !== null) {
        window.clearTimeout(revealTimerRef.current);
      }
      setIsLoadingSimilarPapers(false);
      setSimilarPapersVisible(false);
      setPendingRecordId("");
      setUploadState({
        state: "error",
        message: "Please upload a PDF file for this demo.",
        fileName: file.name || ""
      });
      return;
    }

    const record = buildHistoryRecord(file);
    const nextHistory = [record, ...historyItems.filter((item) => item.fileName !== record.fileName)].slice(0, MAX_HISTORY_ITEMS);
    persistHistory(nextHistory);
    scheduleSimilarPapers(record.id, file.name, "Searching similar papers...");
  }

  function handleFileSelection(event: ChangeEvent<HTMLInputElement>) {
    applyUpload(event.target.files?.[0]);
    event.target.value = "";
  }

  function handleDrop(event: DragEvent<HTMLButtonElement>) {
    event.preventDefault();
    setIsDragging(false);
    applyUpload(event.dataTransfer.files?.[0]);
  }

  function handleDragOver(event: DragEvent<HTMLButtonElement>) {
    event.preventDefault();
    setIsDragging(true);
  }

  function handleDragLeave(event: DragEvent<HTMLButtonElement>) {
    event.preventDefault();
    setIsDragging(false);
  }

  function handleSelectHistory(record: UploadHistoryRecord) {
    scheduleSimilarPapers(record.id, record.fileName, "Searching similar papers...");
  }

  function handleClearHistory() {
    if (revealTimerRef.current !== null) {
      window.clearTimeout(revealTimerRef.current);
    }

    persistHistory([]);
    setSelectedRecordId("");
    setPendingRecordId("");
    setIsLoadingSimilarPapers(false);
    setSimilarPapersVisible(false);
    setUploadState({
      state: "idle",
      message: "Upload a PDF to preview similar papers.",
      fileName: ""
    });
  }

  return (
    <section className="pdf-demo-shell" aria-label="PDF similarity demo">
      <ParticleField />

      <main className="pdf-demo-workbench-layout">
        <aside className="pdf-demo-sidebar">
          <div className="pdf-demo-sidebar-shell">
            <section className="pdf-demo-sidebar-section pdf-demo-sidebar-settings-section">
              <button
                className={`pdf-demo-settings-entry ${settingsOpen ? "is-active" : ""}`}
                type="button"
                onClick={() => setSettingsOpen((current) => !current)}
              >
                <span className="pdf-demo-settings-entry-title">Settings</span>
                <span className="pdf-demo-settings-entry-meta">Click to open API settings</span>
                <span className="pdf-demo-settings-entry-meta">{apiSettings.model}</span>
              </button>
            </section>

            <section className="pdf-demo-sidebar-section pdf-demo-sidebar-history-section">
              <div className="pdf-demo-sidebar-section-header">
                <div>
                  <p className="pdf-demo-section-kicker">Recent</p>
                  <h2>Upload History</h2>
                </div>
                {historyItems.length ? (
                  <button className="pdf-demo-ghost-btn" type="button" onClick={handleClearHistory}>
                    Clear
                  </button>
                ) : null}
              </div>
              <div className="pdf-demo-history-stack">
                {historyItems.length ? (
                  historyItems.map((item) => {
                    const isPending = item.id === pendingRecordId && isLoadingSimilarPapers;
                    return (
                      <button
                        key={item.id}
                        type="button"
                        className={`pdf-demo-history-entry ${item.id === selectedRecordId ? "is-active" : ""} ${
                          isPending ? "is-pending" : ""
                        }`}
                        onClick={() => handleSelectHistory(item)}
                      >
                        <span className="pdf-demo-history-file">{item.fileName || "Untitled PDF"}</span>
                        <span className="pdf-demo-history-meta">{formatTimestamp(item.uploadedAt)}</span>
                      </button>
                    );
                  })
                ) : (
                  <div className="pdf-demo-empty-state compact">
                    Upload history will appear here after a PDF is selected.
                  </div>
                )}
              </div>
            </section>
          </div>
        </aside>

        <section className="pdf-demo-main-panel">
          <section className="pdf-demo-hero-card">
            <div className="pdf-demo-section-heading pdf-demo-hero-heading">
              <div>
                <p className="pdf-demo-section-kicker">Upload</p>
                <h1>Upload a PDF to start the demo</h1>
              </div>
              <div className={`pdf-demo-status-chip ${uploadState.state}`}>
                {uploadState.state === "uploaded" ? "Uploaded" : uploadState.state === "error" ? "Needs attention" : "Ready"}
              </div>
            </div>

            <button
              type="button"
              className={`pdf-demo-upload-dropzone ${isDragging ? "is-dragging" : ""}`}
              onClick={() => fileInputRef.current?.click()}
              onDrop={handleDrop}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
            >
              <span className="pdf-demo-upload-icon">PDF</span>
              <strong>Select or drop a PDF file</strong>
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept=".pdf,application/pdf"
              className="pdf-demo-sr-only"
              onChange={handleFileSelection}
            />

            <div className={`pdf-demo-upload-inline-status ${uploadState.state === "error" ? "is-error" : ""}`}>
              {uploadState.fileName ? `${uploadState.fileName} - ${uploadState.message}` : uploadState.message}
            </div>
            {storageWarning ? <div className="pdf-demo-upload-inline-status is-warning">{storageWarning}</div> : null}
          </section>

          <section className="pdf-demo-results-card">
            <div className="pdf-demo-section-heading">
              <div>
                <p className="pdf-demo-section-kicker">Workspace</p>
                <h2>Similar Papers</h2>
              </div>
              {selectedRecord && !isLoadingSimilarPapers ? <div className="pdf-demo-results-note">{selectedRecord.note}</div> : null}
            </div>

            <div className="pdf-demo-similar-papers-stage">
              {isLoadingSimilarPapers ? <SimilarPapersLoading /> : null}

              {!isLoadingSimilarPapers && visiblePapers.length ? (
                <div className="pdf-demo-similar-papers-grid">
                  {visiblePapers.map((paper) => (
                    <PaperPreviewCard key={paper.id} paper={paper} />
                  ))}
                </div>
              ) : null}

              {!isLoadingSimilarPapers && !visiblePapers.length ? (
                <div className="pdf-demo-empty-state">
                  Upload a PDF to render the six prepared similar-paper images in this workspace.
                </div>
              ) : null}
            </div>
          </section>
        </section>
      </main>

      <SettingsModal
        open={settingsOpen}
        settings={apiSettings}
        onChange={handleSettingsChange}
        onClose={() => setSettingsOpen(false)}
        onSave={handleSaveSettings}
      />
    </section>
  );
}
