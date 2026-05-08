import { type FormEvent, useEffect, useState } from "react";
import { ArrowLeft, BookOpen, Clock3, Expand, FileText, Globe2, Loader2, Search, X } from "lucide-react";
import { useKnowledgeSearch } from "../hooks/useKnowledgeSearch";
import type { KnowledgeDocumentResult } from "../types";
import { Alert } from "./ui/alert";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Input } from "./ui/input";

type KnowledgeSearchProps = {
  onBackHome: () => void;
};

type MetaItemProps = {
  label: string;
  value: string | null;
  onOpen: (label: string, value: string) => void;
};

type ActiveDetail = {
  label: string;
  value: string;
} | null;

type KnowledgeMode = "local" | "online";

const ONLINE_RETRIEVAL_URL = import.meta.env.VITE_ONLINE_RETRIEVAL_URL ?? "/online-retrieval/";

function MetaItem({ label, value, onOpen }: MetaItemProps) {
  if (!value) {
    return null;
  }

  return (
    <div className="group grid min-w-0 grid-cols-[minmax(0,1fr)_2rem] items-center gap-2 rounded-2xl border border-slate-200/80 bg-white/70 px-3 py-2 transition-colors hover:border-teal-300/70 hover:bg-white">
      <div className="min-w-0">
        <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">{label}</div>
        <div className="mt-1 truncate text-sm font-medium text-slate-900">{value}</div>
      </div>
      <button
        type="button"
        onClick={() => onOpen(label, value)}
        className="flex h-8 w-8 items-center justify-center rounded-xl text-slate-500 transition-colors hover:bg-teal-50 hover:text-teal-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        aria-label={`View full ${label} content`}
        title="View full content"
      >
        <Expand className="h-4 w-4" />
      </button>
    </div>
  );
}

function DetailDialog({
  detail,
  onClose
}: {
  detail: ActiveDetail;
  onClose: () => void;
}) {
  useEffect(() => {
    if (!detail) {
      return;
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [detail, onClose]);

  if (!detail) {
    return null;
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 p-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="knowledge-detail-title"
      onClick={onClose}
    >
      <div
        className="max-h-[78vh] w-full max-w-3xl overflow-hidden rounded-[24px] border border-white/80 bg-white shadow-[0_30px_90px_rgba(8,17,31,0.28)]"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-4">
          <div className="min-w-0">
            <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-teal-700">{detail.label}</div>
            <h2 id="knowledge-detail-title" className="font-heading mt-1 text-xl font-semibold tracking-tight text-slate-950">
              Full Content
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-950 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="max-h-[60vh] overflow-auto px-5 py-5">
          <p className="whitespace-pre-wrap break-words text-sm leading-7 text-slate-800">{detail.value}</p>
        </div>
      </div>
    </div>
  );
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function HighlightedText({ text, query }: { text: string; query: string }) {
  const trimmedQuery = query.trim();
  if (!trimmedQuery) {
    return <>{text}</>;
  }

  const parts = text.split(new RegExp(`(${escapeRegExp(trimmedQuery)})`, "ig"));
  return (
    <>
      {parts.map((part, index) =>
        part.toLocaleLowerCase() === trimmedQuery.toLocaleLowerCase() ? (
          <mark key={`${part}-${index}`} className="rounded bg-amber-200/80 px-1 text-slate-950">
            {part}
          </mark>
        ) : (
          <span key={`${part}-${index}`}>{part}</span>
        )
      )}
    </>
  );
}

function KnowledgeResultCard({
  result,
  query,
  onOpenDetail
}: {
  result: KnowledgeDocumentResult;
  query: string;
  onOpenDetail: (label: string, value: string) => void;
}) {
  const title = result.title_en || result.title_zh || `Knowledge record #${result.knowledge_id}`;

  return (
    <article className="rounded-[24px] border border-white/80 bg-white/85 p-5 shadow-sm">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <h3 className="font-heading text-xl font-semibold tracking-tight text-slate-950">{title}</h3>
          {result.title_zh ? <p className="mt-1 text-sm text-mutedForeground">{result.title_zh}</p> : null}
        </div>
        <Badge className="bg-teal-50 text-teal-800">#{result.knowledge_id}</Badge>
      </div>

      <p className="mt-4 text-sm leading-7 text-slate-700">
        <HighlightedText text={result.abstract_snippet} query={query} />
      </p>

      <div className="mt-4 grid gap-2 md:grid-cols-2 xl:grid-cols-4">
        <MetaItem label="Polymer" value={result.polymer_iupac} onOpen={onOpenDetail} />
        <MetaItem label="Formulation" value={result.formulation} onOpen={onOpenDetail} />
        <MetaItem label="Catalyst" value={result.catalyst} onOpen={onOpenDetail} />
        <MetaItem label="Solvent" value={result.solvent} onOpen={onOpenDetail} />
        <MetaItem label="Temperature" value={result.temperature} onOpen={onOpenDetail} />
        <MetaItem label="Time" value={result.reaction_time} onOpen={onOpenDetail} />
      </div>
    </article>
  );
}

export function KnowledgeSearch({ onBackHome }: KnowledgeSearchProps) {
  const [mode, setMode] = useState<KnowledgeMode>("local");
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(25);
  const [activeDetail, setActiveDetail] = useState<ActiveDetail>(null);
  const searchState = useKnowledgeSearch();
  const canSearch = query.trim().length > 0 && topK >= 1 && topK <= 100 && !searchState.isLoading;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSearch) {
      return;
    }
    await searchState.submit(query.trim(), topK);
  }

  const resultBadge = searchState.data
    ? `${searchState.data.results.length} / ${searchState.data.total} shown`
    : "Ready";
  const timingBadge = searchState.data ? `${searchState.data.query_time_ms.toFixed(1)} ms` : "Ready";

  return (
    <div className="contents">
      <nav className="flex flex-col gap-3 rounded-[26px] border border-white/70 bg-white/80 px-4 py-4 shadow-sm backdrop-blur md:flex-row md:items-center md:justify-between md:px-5">
        <div className="flex items-center gap-3">
          <Button type="button" variant="outline" onClick={onBackHome}>
            <ArrowLeft className="mr-2 h-4 w-4" />
            Home
          </Button>
          <div>
            <div className="text-[11px] font-medium uppercase tracking-[0.2em] text-teal-700/70">Current Module</div>
            <div className="font-heading text-lg font-semibold tracking-tight text-slate-950">Local Knowledge Search</div>
          </div>
        </div>
        <Badge className="bg-teal-50 text-teal-800">{mode === "local" ? "Local Retrieval" : "Online Retrieval"}</Badge>
      </nav>

      <section className="hero-glow mesh-surface relative overflow-hidden rounded-[36px] border border-white/70 px-6 py-7 md:px-8">
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px] lg:items-center">
          <div>
            <div className="flex flex-wrap items-center gap-3">
              <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-slate-950 text-white shadow-sm">
                <BookOpen className="h-5 w-5" />
              </div>
              <Badge>Knowledge Retrieval</Badge>
            </div>
            <h1 className="font-heading mt-6 text-[2.4rem] font-semibold leading-tight tracking-tight text-slate-950 md:text-[3.7rem]">
              Knowledge Search
            </h1>
            <div className="mt-7 flex flex-wrap gap-2 rounded-[22px] border border-white/80 bg-white/70 p-2 shadow-sm">
              <button
                type="button"
                onClick={() => setMode("local")}
                className={[
                  "inline-flex min-h-11 items-center rounded-2xl px-4 text-sm font-semibold transition-colors",
                  mode === "local" ? "bg-slate-950 text-white" : "text-slate-600 hover:bg-white"
                ].join(" ")}
              >
                <Search className="mr-2 h-4 w-4" />
                Local
              </button>
              <button
                type="button"
                onClick={() => setMode("online")}
                className={[
                  "inline-flex min-h-11 items-center rounded-2xl px-4 text-sm font-semibold transition-colors",
                  mode === "online" ? "bg-slate-950 text-white" : "text-slate-600 hover:bg-white"
                ].join(" ")}
              >
                <Globe2 className="mr-2 h-4 w-4" />
                Online
              </button>
            </div>
            {mode === "local" ? (
              <form onSubmit={handleSubmit} className="mt-4 grid gap-3 lg:grid-cols-[minmax(0,1fr)_132px_120px]">
                <Input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Enter an abstract keyword"
                  aria-label="Abstract keyword"
                />
                <Input
                  type="number"
                  min={1}
                  max={100}
                  value={topK}
                  onChange={(event) => setTopK(Number(event.target.value))}
                  aria-label="Result limit"
                />
                <Button type="submit" disabled={!canSearch}>
                  {searchState.isLoading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Search className="mr-2 h-4 w-4" />}
                  Search
                </Button>
              </form>
            ) : null}
          </div>

          <div className="grid gap-3">
            <div className="rounded-[24px] border border-white/80 bg-white/85 p-5 shadow-sm">
              <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">
                {mode === "local" ? <FileText className="h-4 w-4 text-teal-700" /> : <Globe2 className="h-4 w-4 text-teal-700" />}
                {mode === "local" ? "Local Results" : "Search Mode"}
              </div>
              <div className="font-heading mt-3 text-3xl font-semibold tracking-tight text-slate-950">
                {mode === "local" ? resultBadge : "Online"}
              </div>
            </div>
            <div className="rounded-[24px] border border-white/80 bg-slate-950 p-5 text-slate-50 shadow-sm">
              <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">
                <Clock3 className="h-4 w-4 text-teal-300" />
                {mode === "local" ? "Search Time" : "Data Source"}
              </div>
              <div className="font-heading mt-3 text-3xl font-semibold tracking-tight">
                {mode === "local" ? timingBadge : "Literature"}
              </div>
            </div>
          </div>
        </div>
      </section>

      {mode === "local" ? (
        <section className="rounded-[32px] border border-white/70 bg-white/75 p-4 shadow-soft md:p-5">
          {searchState.error ? (
            <Alert variant="destructive">{searchState.error}</Alert>
          ) : searchState.isLoading ? (
            <div className="flex min-h-[220px] items-center justify-center gap-3 text-sm font-medium text-slate-600">
              <Loader2 className="h-5 w-5 animate-spin text-teal-700" />
              Searching
            </div>
          ) : searchState.data ? (
            searchState.data.results.length > 0 ? (
              <div className="space-y-4">
                {searchState.data.results.map((result) => (
                  <KnowledgeResultCard
                    key={result.knowledge_id}
                    result={result}
                    query={searchState.data?.query ?? query}
                    onOpenDetail={(label, value) => setActiveDetail({ label, value })}
                  />
                ))}
              </div>
            ) : (
              <div className="flex min-h-[220px] items-center justify-center text-center text-sm text-mutedForeground">
                No records found with abstracts containing "{searchState.data.query}".
              </div>
            )
          ) : (
            <div className="flex min-h-[220px] items-center justify-center text-center text-sm text-mutedForeground">
              Enter a keyword to show matching abstract records.
            </div>
          )}
        </section>
      ) : (
        <section className="overflow-hidden rounded-[32px] border border-white/70 bg-white/75 p-3 shadow-soft md:p-4">
          <div className="overflow-hidden rounded-[24px] border border-slate-200/80 bg-white shadow-sm">
            <iframe
              title="Online Knowledge Retrieval"
              src={ONLINE_RETRIEVAL_URL}
              className="h-[calc(100vh-180px)] min-h-[760px] w-full border-0"
            />
          </div>
        </section>
      )}

      <DetailDialog detail={activeDetail} onClose={() => setActiveDetail(null)} />
    </div>
  );
}
