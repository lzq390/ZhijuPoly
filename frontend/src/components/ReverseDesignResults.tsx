import type { ReactNode } from "react";
import { Atom, BookOpen, Database, LoaderCircle, SearchX, Target, Timer, TriangleAlert } from "lucide-react";
import type {
  ReverseDesignKnowledgeResponse,
  ReverseDesignTgCandidate,
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
  selectedCandidate: ReverseDesignTgCandidate | null;
  knowledgeData: ReverseDesignKnowledgeResponse | null;
  knowledgeLoading?: boolean;
  knowledgeError: string | null;
  onLoadKnowledge: (candidate: ReverseDesignTgCandidate) => void;
};

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

function CandidateCard({
  candidate,
  onLoadKnowledge
}: {
  candidate: ReverseDesignTgCandidate;
  onLoadKnowledge: (candidate: ReverseDesignTgCandidate) => void;
}) {
  const displaySmiles = candidate.canonical_polym || candidate.polymer_smiles;

  return (
    <Card className="overflow-hidden rounded-[24px] border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(244,248,249,0.88)_100%)] p-3">
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
        <div className="mt-2 space-y-1.5 font-mono-ui text-xs leading-5 text-slate-800">
          <div className="break-all">{candidate.monomer_a_smiles || "Not available"}</div>
          <div className="break-all">{candidate.monomer_b_smiles || "Not available"}</div>
        </div>
        <Button
          type="button"
          variant="outline"
          className="mt-3 min-h-[38px] w-full"
          onClick={() => onLoadKnowledge(candidate)}
        >
          <BookOpen className="mr-2 h-4 w-4" />
          Knowledge
        </Button>
      </div>
    </Card>
  );
}

export function ReverseDesignResults({
  data,
  error,
  isLoading = false,
  selectedCandidate,
  knowledgeData,
  knowledgeLoading = false,
  knowledgeError,
  onLoadKnowledge
}: ReverseDesignResultsProps) {
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
          <CardDescription>Scanning local PI candidates and sorting sampled matches by Tg distance.</CardDescription>
        </CardHeader>
        <CardContent className="pt-5">
          <div className="flex items-center gap-3 rounded-[20px] border border-white/80 bg-white/80 px-4 py-3 text-sm text-slate-700">
            <LoaderCircle className="h-4 w-4 animate-spin" />
            Searching PI candidates.
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
          <CardDescription>The search completed but no sampled candidates were returned.</CardDescription>
        </CardHeader>
        <CardContent className="pt-5">
          <EmptyPanel
            icon={<SearchX className="h-6 w-6" />}
            title="No Candidates Found"
            description="Try lowering the similarity threshold or checking whether the PI candidate database is populated."
          />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="overflow-hidden rounded-[28px] border-white/70 shadow-none">
      <CardHeader className="min-h-[120px] gap-4 border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(244,248,249,0.88)_100%)]">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div className="space-y-2">
            <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-teal-700/80">PI Candidate Search</div>
            <CardTitle className="text-[1.4rem] tracking-tight">Tg Reverse Design Results</CardTitle>
            <CardDescription>Sampled similar PI candidates sorted by distance to the target Tg.</CardDescription>
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge>{`${data.total} shown`}</Badge>
            <Badge className="text-slate-700">{`${data.candidate_pool_size} matched`}</Badge>
            <Badge className="text-slate-700">{`${data.sampled_candidate_count} sampled`}</Badge>
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
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {data.results.map((candidate) => (
            <CandidateCard key={candidate.pi_id} candidate={candidate} onLoadKnowledge={onLoadKnowledge} />
          ))}
        </div>

        {selectedCandidate ? (
          <div className="rounded-[24px] border border-white/80 bg-white/80 p-4 shadow-sm">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
              <div>
                <div className="text-xs font-semibold uppercase tracking-[0.16em] text-mutedForeground">Knowledge Link</div>
                <div className="mt-1 text-sm font-semibold text-slate-950">PI #{selectedCandidate.pi_id}</div>
              </div>
              {knowledgeLoading ? (
                <Badge className="bg-slate-100 text-slate-700">Loading</Badge>
              ) : knowledgeData?.knowledge_query ? (
                <Badge className="bg-teal-50 text-teal-800">IUPAC query ready</Badge>
              ) : (
                <Badge className="bg-slate-100 text-slate-700">No IUPAC</Badge>
              )}
            </div>
            {knowledgeError ? <Alert variant="destructive" className="mt-3">{knowledgeError}</Alert> : null}
            {knowledgeData ? (
              <div className="mt-3 grid gap-2 text-sm text-slate-700">
                <div>
                  <span className="font-semibold">Monomer A:</span>{" "}
                  {knowledgeData.monomer_a_iupac || "Not converted"}
                </div>
                <div>
                  <span className="font-semibold">Monomer B:</span>{" "}
                  {knowledgeData.monomer_b_iupac || "Not converted"}
                </div>
                <div>
                  <span className="font-semibold">Knowledge records:</span>{" "}
                  {knowledgeData.knowledge ? knowledgeData.knowledge.length : 0}
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
