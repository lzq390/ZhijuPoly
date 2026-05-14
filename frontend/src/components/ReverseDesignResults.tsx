import { useEffect, useRef, useState, type ReactNode } from "react";
import { Atom, BookOpen, ChevronLeft, ChevronRight, Database, LoaderCircle, SearchX, Target, Timer, TriangleAlert } from "lucide-react";
import type {
  KnowledgeNavigationRequest,
  ReverseDesignTgCandidate,
  ReverseDesignTgJobStatusResponse,
  ReverseDesignTgResponse
} from "../types";
import { Alert } from "./ui/alert";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";

type ReverseDesignResultsProps = {
  data: ReverseDesignTgResponse | null;
  error: string | null;
  isLoading?: boolean;
  job?: ReverseDesignTgJobStatusResponse | null;
  onOpenKnowledge: (request: KnowledgeNavigationRequest) => void;
};

type KnowledgeMenuPlacement = "left" | "right";

const KNOWLEDGE_MENU_WIDTH = 144;
const KNOWLEDGE_MENU_GAP = 8;
const KNOWLEDGE_MENU_MARGIN = 12;
const RESULTS_PAGE_SIZE = 20;

function getCandidateColumnCount() {
  if (typeof window === "undefined") {
    return 1;
  }

  if (window.innerWidth >= 1280) {
    return 4;
  }

  if (window.innerWidth >= 1024) {
    return 3;
  }

  if (window.innerWidth >= 640) {
    return 2;
  }

  return 1;
}

function formatInteger(value: number | null | undefined) {
  return value == null ? "0" : value.toLocaleString();
}

function formatOptionalNumber(value: number | null | undefined, digits = 2) {
  return value == null ? "Pending" : value.toFixed(digits);
}

function EmptyPanel({
  icon,
  title,
  description
}: {
  icon: ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div className="flex min-h-[320px] flex-col items-center justify-center rounded-[24px] border border-dashed border-white bg-[linear-gradient(180deg,rgba(255,255,255,0.72)_0%,rgba(244,248,249,0.78)_100%)] px-6 py-12 text-center">
      <div className="flex h-14 w-14 items-center justify-center rounded-2xl border border-white bg-white/85 text-slate-600 shadow-sm">
        {icon}
      </div>
      <div className="mt-5 text-lg font-semibold text-slate-900">{title}</div>
      <div className="mt-2 max-w-xl text-sm leading-6 text-mutedForeground">{description}</div>
    </div>
  );
}

function ResultsPagination({
  currentPage,
  totalPages,
  startIndex,
  endIndex,
  total,
  onPageChange
}: {
  currentPage: number;
  totalPages: number;
  startIndex: number;
  endIndex: number;
  total: number;
  onPageChange: (page: number) => void;
}) {
  if (total <= RESULTS_PAGE_SIZE) {
    return null;
  }

  return (
    <div className="flex flex-col gap-3 rounded-[20px] border border-white/80 bg-white/80 px-4 py-3 text-sm text-slate-700 shadow-sm sm:flex-row sm:items-center sm:justify-between">
      <div className="font-medium text-slate-700">
        {`${formatInteger(startIndex + 1)}-${formatInteger(endIndex)} of ${formatInteger(total)}`}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          variant="outline"
          className="min-h-[38px] px-3 text-xs"
          onClick={() => onPageChange(currentPage - 1)}
          disabled={currentPage <= 1}
          aria-label="Previous page"
        >
          <ChevronLeft className="mr-1.5 h-4 w-4" />
          Prev
        </Button>
        <Badge className="text-slate-700">{`Page ${currentPage} / ${totalPages}`}</Badge>
        <Button
          type="button"
          variant="outline"
          className="min-h-[38px] px-3 text-xs"
          onClick={() => onPageChange(currentPage + 1)}
          disabled={currentPage >= totalPages}
          aria-label="Next page"
        >
          Next
          <ChevronRight className="ml-1.5 h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}

function MonomerSmilesPreview({
  label,
  smiles,
  structureSvg
}: {
  label: string;
  smiles: string;
  structureSvg: string | null;
}) {
  if (!smiles) {
    return <div className="font-mono-ui break-all text-mutedForeground">Not available</div>;
  }

  return (
    <div className="group relative">
      <div
        tabIndex={0}
        className="cursor-default rounded-[10px] px-1 py-0.5 font-mono-ui break-all text-mutedForeground outline-none transition-colors hover:bg-teal-50 hover:text-teal-800 focus:bg-teal-50 focus:text-teal-800"
      >
        {smiles}
      </div>
      <div className="pointer-events-auto absolute bottom-full left-1/2 z-50 mb-2 hidden w-72 max-w-[calc(100vw-3rem)] rounded-[18px] border border-white/80 bg-white/95 p-3 text-left shadow-[0_18px_45px_rgba(8,17,31,0.18)] backdrop-blur group-hover:block group-focus-within:block">
        <div className="mb-2 flex items-center justify-between gap-3 text-[11px] font-semibold uppercase tracking-[0.16em] text-mutedForeground">
          <span>Monomer {label}</span>
          <span>2D</span>
        </div>
        {structureSvg ? (
          <div
            className="rounded-[14px] border border-slate-200/70 bg-white p-2 [&_svg]:mx-auto [&_svg]:h-auto [&_svg]:max-h-[180px] [&_svg]:w-full [&_svg]:max-w-full"
            dangerouslySetInnerHTML={{ __html: structureSvg }}
          />
        ) : (
          <div className="flex min-h-28 items-center justify-center rounded-[14px] border border-slate-200/70 bg-slate-50 px-3 text-center text-xs text-mutedForeground">
            2D structure unavailable
          </div>
        )}
      </div>
    </div>
  );
}

function CandidateMasonryGrid({
  candidates,
  onOpenKnowledge
}: {
  candidates: ReverseDesignTgCandidate[];
  onOpenKnowledge: (request: KnowledgeNavigationRequest) => void;
}) {
  const [columnCount, setColumnCount] = useState(getCandidateColumnCount);
  const columns = Array.from({ length: columnCount }, () => [] as ReverseDesignTgCandidate[]);

  candidates.forEach((candidate, index) => {
    columns[index % columnCount].push(candidate);
  });

  useEffect(() => {
    function handleResize() {
      setColumnCount(getCandidateColumnCount());
    }

    handleResize();
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  return (
    <div className="grid items-start gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {columns.map((columnCandidates, columnIndex) => (
        <div key={columnIndex} className="flex min-w-0 flex-col gap-4">
          {columnCandidates.map((candidate) => (
            <CandidateCard
              key={candidate.pi_id}
              candidate={candidate}
              onOpenKnowledge={onOpenKnowledge}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

function CandidateCard({
  candidate,
  onOpenKnowledge
}: {
  candidate: ReverseDesignTgCandidate;
  onOpenKnowledge: (request: KnowledgeNavigationRequest) => void;
}) {
  const [showIupac, setShowIupac] = useState(false);
  const [knowledgeMenuOpen, setKnowledgeMenuOpen] = useState(false);
  const [knowledgeMenuPlacement, setKnowledgeMenuPlacement] = useState<KnowledgeMenuPlacement>("right");
  const knowledgeMenuRef = useRef<HTMLDivElement | null>(null);
  const knowledgeMenuPanelRef = useRef<HTMLDivElement | null>(null);
  const displaySmiles = candidate.canonical_polym || candidate.polymer_smiles;
  const monomerATerm = candidate.monomer_a_iupac?.trim() || candidate.monomer_a_smiles.trim();
  const monomerBTerm = candidate.monomer_b_iupac?.trim() || candidate.monomer_b_smiles.trim();
  const pairTerms = [monomerATerm, monomerBTerm].filter((value) => value.length > 0);
  const knowledgeMenuId = `knowledge-menu-${candidate.pi_id}`;

  function openKnowledge(terms: string[]) {
    setKnowledgeMenuOpen(false);
    onOpenKnowledge({
      query: terms.join(" OR "),
      terms
    });
  }

  function updateKnowledgeMenuPlacement() {
    const triggerRect = knowledgeMenuRef.current?.getBoundingClientRect();
    if (!triggerRect) {
      return;
    }

    const viewportWidth = window.innerWidth;
    const rightLeft = triggerRect.right + KNOWLEDGE_MENU_GAP;
    const leftLeft = triggerRect.left - KNOWLEDGE_MENU_GAP - KNOWLEDGE_MENU_WIDTH;
    const fitsRight = rightLeft + KNOWLEDGE_MENU_WIDTH <= viewportWidth - KNOWLEDGE_MENU_MARGIN;
    const fitsLeft = leftLeft >= KNOWLEDGE_MENU_MARGIN;
    const placement = fitsRight || !fitsLeft ? "right" : "left";

    setKnowledgeMenuPlacement(placement);
  }

  useEffect(() => {
    if (!knowledgeMenuOpen) {
      return;
    }

    updateKnowledgeMenuPlacement();
    window.addEventListener("resize", updateKnowledgeMenuPlacement);
    window.addEventListener("scroll", updateKnowledgeMenuPlacement, true);

    return () => {
      window.removeEventListener("resize", updateKnowledgeMenuPlacement);
      window.removeEventListener("scroll", updateKnowledgeMenuPlacement, true);
    };
  }, [knowledgeMenuOpen]);

  useEffect(() => {
    if (!knowledgeMenuOpen) {
      return;
    }

    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (
        !(target instanceof Node) ||
        knowledgeMenuRef.current?.contains(target) ||
        knowledgeMenuPanelRef.current?.contains(target)
      ) {
        return;
      }

      setKnowledgeMenuOpen(false);
    }

    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [knowledgeMenuOpen]);

  return (
    <Card
      className={[
        "relative z-0 self-start overflow-visible rounded-[24px] border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(244,248,249,0.88)_100%)] p-3 hover:z-[70] focus-within:z-[70]",
        knowledgeMenuOpen ? "z-[80]" : ""
      ].join(" ")}
    >
      <div className="overflow-hidden rounded-[18px] border border-white/80 bg-white/90 p-2.5 shadow-sm">
        <div className="mb-1.5 flex items-center justify-between gap-2 text-[10px] font-medium uppercase tracking-[0.18em] text-mutedForeground">
          <span className="inline-flex items-center gap-2">
            <Atom className="h-3.5 w-3.5 text-teal-600" />
            PI #{candidate.pi_id}
          </span>
          <Badge className="bg-teal-50 text-teal-800">Rank {candidate.rank}</Badge>
        </div>
        {candidate.structure_svg ? (
          <div
            className="[&_svg]:mx-auto [&_svg]:h-auto [&_svg]:max-h-[170px] [&_svg]:w-full [&_svg]:max-w-full"
            dangerouslySetInnerHTML={{ __html: candidate.structure_svg }}
          />
        ) : (
          <div className="flex min-h-[150px] items-center justify-center rounded-[14px] bg-slate-50 px-3 text-center font-mono-ui text-xs leading-5 text-mutedForeground">
            {displaySmiles}
          </div>
        )}
      </div>

      <div className="mt-2.5 space-y-2 rounded-[16px] border border-white/80 bg-white/75 px-3 py-2.5 shadow-sm">
        <div className="font-mono-ui break-all text-xs leading-5 text-slate-800">{displaySmiles}</div>
        <div className="grid gap-2 text-xs">
          <div className="flex items-center justify-between gap-3">
            <span className="text-mutedForeground">Tg</span>
            <span className="text-right font-semibold text-slate-800">
              {candidate.tg_value.toFixed(2)} {candidate.tg_unit}
            </span>
          </div>
          <div className="flex items-center justify-between gap-3">
            <span className="text-mutedForeground">Tg Difference</span>
            <span className="text-right font-semibold text-teal-700">{candidate.tg_difference.toFixed(2)} °C</span>
          </div>
          <div className="flex items-center justify-between gap-3">
            <span className="text-mutedForeground">Similarity</span>
            <span className="text-right font-semibold text-slate-800">{candidate.similarity_score.toFixed(3)}</span>
          </div>
        </div>
      </div>

      <div className="mt-2.5 rounded-[16px] border border-white/80 bg-white/75 px-3 py-2.5 shadow-sm">
        <div className="text-xs font-semibold uppercase tracking-[0.16em] text-mutedForeground">Monomers</div>
        <div className="mt-2 space-y-3 text-xs leading-5 text-slate-800">
          <div className="space-y-1">
            <div className="font-semibold text-slate-700">A</div>
            {showIupac ? (
              <div className="break-words text-slate-900">{candidate.monomer_a_iupac || "IUPAC not available"}</div>
            ) : null}
            <MonomerSmilesPreview
              label="A"
              smiles={candidate.monomer_a_smiles}
              structureSvg={candidate.monomer_a_structure_svg}
            />
          </div>
          <div className="space-y-1">
            <div className="font-semibold text-slate-700">B</div>
            {showIupac ? (
              <div className="break-words text-slate-900">{candidate.monomer_b_iupac || "IUPAC not available"}</div>
            ) : null}
            <MonomerSmilesPreview
              label="B"
              smiles={candidate.monomer_b_smiles}
              structureSvg={candidate.monomer_b_structure_svg}
            />
          </div>
        </div>
        <div className="mt-3 grid grid-cols-[minmax(5.5rem,0.72fr)_minmax(9.5rem,1.28fr)] gap-2">
          <Button
            type="button"
            variant="outline"
            className="min-h-[38px] whitespace-nowrap px-3 text-xs"
            onClick={() => setShowIupac((current) => !current)}
          >
            <Atom className="mr-1.5 h-4 w-4 shrink-0" />
            {showIupac ? "Hide" : "IUPAC"}
          </Button>
          <div className="relative" ref={knowledgeMenuRef}>
            <Button
              type="button"
              variant="outline"
              className="min-h-[38px] w-full whitespace-nowrap px-3 text-xs"
              onClick={() => setKnowledgeMenuOpen((current) => !current)}
              disabled={pairTerms.length === 0}
              aria-expanded={knowledgeMenuOpen}
              aria-controls={knowledgeMenuId}
            >
              <BookOpen className="mr-1.5 h-4 w-4 shrink-0" />
              Local Knowledge
              <ChevronRight
                className={[
                  "ml-1.5 h-4 w-4 shrink-0 transition-transform",
                  knowledgeMenuOpen && knowledgeMenuPlacement === "left"
                    ? "rotate-180"
                    : knowledgeMenuOpen
                      ? "translate-x-0.5"
                      : ""
                ].join(" ")}
              />
            </Button>
            {knowledgeMenuOpen ? (
              <div
                id={knowledgeMenuId}
                ref={knowledgeMenuPanelRef}
                role="menu"
                className={[
                  "absolute top-1/2 z-50 w-36 -translate-y-1/2 rounded-[18px] border border-white/80 bg-white/95 p-1.5 shadow-[0_18px_45px_rgba(8,17,31,0.18)] backdrop-blur",
                  knowledgeMenuPlacement === "left" ? "right-full mr-2" : "left-full ml-2"
                ].join(" ")}
              >
                <button
                  type="button"
                  role="menuitem"
                  className="flex min-h-9 w-full items-center rounded-[14px] px-3 text-left text-xs font-semibold text-slate-700 transition-colors hover:bg-teal-50 hover:text-teal-800 disabled:pointer-events-none disabled:opacity-45"
                  onClick={() => openKnowledge([monomerATerm])}
                  disabled={!monomerATerm}
                  title="Search monomer A"
                >
                  Monomer A
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="flex min-h-9 w-full items-center rounded-[14px] px-3 text-left text-xs font-semibold text-slate-700 transition-colors hover:bg-teal-50 hover:text-teal-800 disabled:pointer-events-none disabled:opacity-45"
                  onClick={() => openKnowledge([monomerBTerm])}
                  disabled={!monomerBTerm}
                  title="Search monomer B"
                >
                  Monomer B
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="flex min-h-9 w-full items-center rounded-[14px] px-3 text-left text-xs font-semibold text-slate-700 transition-colors hover:bg-teal-50 hover:text-teal-800 disabled:pointer-events-none disabled:opacity-45"
                  onClick={() => openKnowledge(pairTerms)}
                  disabled={pairTerms.length === 0}
                  title="Search monomers A and B"
                >
                  A + B
                </button>
              </div>
            ) : null}
          </div>
        </div>
      </div>
    </Card>
  );
}

export function ReverseDesignResults({
  data,
  error,
  isLoading = false,
  job,
  onOpenKnowledge
}: ReverseDesignResultsProps) {
  const [resultPage, setResultPage] = useState(1);
  const totalResultCount = data?.results.length ?? 0;
  const totalPages = Math.max(1, Math.ceil(totalResultCount / RESULTS_PAGE_SIZE));
  const currentPage = Math.min(resultPage, totalPages);
  const startIndex = (currentPage - 1) * RESULTS_PAGE_SIZE;
  const endIndex = Math.min(startIndex + RESULTS_PAGE_SIZE, totalResultCount);
  const visibleResults = data?.results.slice(startIndex, endIndex) ?? [];

  useEffect(() => {
    setResultPage(1);
  }, [data]);

  function updateResultPage(page: number) {
    setResultPage(Math.min(Math.max(page, 1), totalPages));
  }

  if (error) {
    return (
      <Card className="overflow-hidden rounded-[28px] border-destructive/20 shadow-none">
        <CardHeader className="min-h-[112px] border-b border-destructive/10 bg-destructiveForeground">
          <CardTitle className="flex items-center gap-2 text-lg text-destructive">
            <TriangleAlert className="h-5 w-5" />
            Reverse Design Failed
          </CardTitle>
          <CardDescription>The Tg search did not complete. Check the target Tg, structure, or PI database status.</CardDescription>
        </CardHeader>
        <CardContent className="pt-5">
          <Alert variant="destructive">{error}</Alert>
        </CardContent>
      </Card>
    );
  }

  if (isLoading) {
    return (
      <Card className="overflow-hidden rounded-[28px] border-white/70 shadow-none">
        <CardHeader className="min-h-[112px] border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(244,248,249,0.88)_100%)]">
          <CardTitle className="text-xl">Tg Reverse Design Results</CardTitle>
          <CardDescription>Scanning PI candidates by Tg distance until 200 threshold-matched results are found.</CardDescription>
        </CardHeader>
        <CardContent className="pt-5">
          <div className="space-y-4 rounded-[20px] border border-white/80 bg-white/80 px-4 py-4 text-sm text-slate-700">
            <div className="flex items-center gap-3">
              <LoaderCircle className="h-4 w-4 animate-spin" />
              Searching PI candidates.
            </div>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <div className="rounded-[16px] border border-slate-200/70 bg-slate-50/80 p-3">
                <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-mutedForeground">Scanned</div>
                <div className="mt-1 text-lg font-semibold text-slate-950">{formatInteger(job?.scanned_rows)}</div>
              </div>
              <div className="rounded-[16px] border border-slate-200/70 bg-slate-50/80 p-3">
                <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-mutedForeground">Matched</div>
                <div className="mt-1 text-lg font-semibold text-slate-950">{formatInteger(job?.matched_count)}</div>
              </div>
              <div className="rounded-[16px] border border-slate-200/70 bg-slate-50/80 p-3">
                <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-mutedForeground">Tg Radius</div>
                <div className="mt-1 text-lg font-semibold text-slate-950">
                  {job?.current_tg_radius == null ? "Pending" : `±${job.current_tg_radius.toFixed(2)} °C`}
                </div>
              </div>
              <div className="rounded-[16px] border border-slate-200/70 bg-slate-50/80 p-3">
                <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-mutedForeground">Best Similarity</div>
                <div className="mt-1 text-lg font-semibold text-slate-950">{formatOptionalNumber(job?.best_similarity_score, 3)}</div>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>
    );
  }

  if (!data) {
    return (
      <Card className="overflow-hidden rounded-[28px] border-white/70 shadow-none">
        <CardHeader className="min-h-[112px] border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(244,248,249,0.88)_100%)]">
          <CardTitle className="text-xl">Tg Reverse Design Results</CardTitle>
          <CardDescription>No reverse-design results yet.</CardDescription>
        </CardHeader>
        <CardContent className="pt-5">
          <EmptyPanel
            icon={<Database className="h-6 w-6" />}
            title="Reverse Design Ready"
            description="Enter a target Tg and run Tg search to inspect similar PI candidates."
          />
        </CardContent>
      </Card>
    );
  }

  if (data.total === 0) {
    return (
      <Card className="overflow-hidden rounded-[28px] border-white/70 shadow-none">
        <CardHeader className="min-h-[112px] border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(244,248,249,0.88)_100%)]">
          <CardTitle className="text-xl">Tg Reverse Design Results</CardTitle>
          <CardDescription>The PI database scan completed but no candidates satisfied the similarity threshold.</CardDescription>
        </CardHeader>
        <CardContent className="pt-5">
          <EmptyPanel
            icon={<SearchX className="h-6 w-6" />}
            title="No Candidates Found"
            description="Try lowering the similarity threshold or checking the drawn polymer structure."
          />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="overflow-visible rounded-[28px] border-white/70 shadow-none">
      <CardHeader className="min-h-[120px] gap-4 border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(244,248,249,0.88)_100%)]">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div className="space-y-2">
            <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-teal-700/80">PI Candidate Search</div>
            <CardTitle className="text-[1.4rem] tracking-tight">Tg Reverse Design Results</CardTitle>
            <CardDescription>Similar PI candidates sorted by distance to the target Tg.</CardDescription>
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge>{`${visibleResults.length} shown`}</Badge>
            <Badge className="text-slate-700">{`${data.total} total`}</Badge>
            <Badge className="text-slate-700">{`${data.candidate_pool_size} matched`}</Badge>
            {job?.scanned_rows != null ? (
              <Badge className="text-slate-700">{`${formatInteger(job.scanned_rows)} scanned`}</Badge>
            ) : null}
            <Badge className="text-slate-700">{`${data.query_time_ms.toFixed(1)} ms`}</Badge>
          </div>
        </div>

        <div className="grid gap-3 md:grid-cols-3">
          <div className="rounded-[18px] border border-white/80 bg-white/80 p-4 shadow-sm">
            <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.16em] text-mutedForeground">
              <Target className="h-4 w-4 text-teal-600" />
              Target Tg
            </div>
            <div className="mt-2 text-xl font-semibold text-slate-950">{data.target_tg.toFixed(2)} °C</div>
          </div>
          <div className="rounded-[18px] border border-white/80 bg-white/80 p-4 shadow-sm">
            <div className="text-xs font-semibold uppercase tracking-[0.16em] text-mutedForeground">Candidate Pool</div>
            <div className="mt-2 text-xl font-semibold text-slate-950">{data.candidate_pool_size}</div>
          </div>
          <div className="rounded-[18px] border border-white/80 bg-white/80 p-4 shadow-sm">
            <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.16em] text-mutedForeground">
              <Timer className="h-4 w-4 text-teal-600" />
              Elapsed
            </div>
            <div className="mt-2 text-xl font-semibold text-slate-950">{data.query_time_ms.toFixed(1)} ms</div>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-4 pt-5">
        <ResultsPagination
          currentPage={currentPage}
          totalPages={totalPages}
          startIndex={startIndex}
          endIndex={endIndex}
          total={totalResultCount}
          onPageChange={updateResultPage}
        />
        <CandidateMasonryGrid
          candidates={visibleResults}
          onOpenKnowledge={onOpenKnowledge}
        />
        <ResultsPagination
          currentPage={currentPage}
          totalPages={totalPages}
          startIndex={startIndex}
          endIndex={endIndex}
          total={totalResultCount}
          onPageChange={updateResultPage}
        />
      </CardContent>
    </Card>
  );
}
