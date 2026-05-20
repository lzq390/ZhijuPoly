import { ArrowLeft, Atom, LoaderCircle, Microscope, Sparkles } from "lucide-react";
import { KetcherEditor } from "./KetcherEditor";
import { StructurePreview3D } from "./StructurePreview3D";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";
import { Input } from "./ui/input";
import { useConditionalGeneration } from "../hooks/useConditionalGeneration";
import { useKetcher } from "../hooks/useKetcher";
import type { ConditionalGenerationCandidate, ConditionalGenerationTgRequest } from "../types";

type ConditionalGenerationPageProps = {
  onBackHome: () => void;
};

const DEMO_GENERATION_SMILES = "[*]COCC([*])(C)COCCC#N";

function parseNumber(value: string, fallback: number) {
  const parsed = Number(value);
  return Number.isNaN(parsed) ? fallback : parsed;
}

function formatOptionalNumber(value: number | null | undefined, digits = 2) {
  return value == null ? "Pending" : value.toFixed(digits);
}

function CandidateCard({ candidate }: { candidate: ConditionalGenerationCandidate }) {
  return (
    <Card className="self-start overflow-hidden rounded-[24px] border-white/70 bg-white/90">
      <div className="border-b border-white/80 bg-white p-3">
        <div className="mb-2 flex items-center justify-between gap-2 text-[10px] font-medium uppercase tracking-[0.18em] text-mutedForeground">
          <span className="inline-flex items-center gap-2">
            <Atom className="h-3.5 w-3.5 text-teal-600" />
            Generated
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
            {candidate.generated_smiles}
          </div>
        )}
      </div>
      <CardContent className="space-y-3 pt-4">
        <div className="font-mono-ui break-all rounded-[16px] border border-white/80 bg-white/75 px-3 py-2 text-xs leading-5 text-slate-800">
          {candidate.generated_smiles}
        </div>
        <div className="grid gap-2 text-xs">
          <div className="flex items-center justify-between gap-3">
            <span className="text-mutedForeground">Predicted Tg</span>
            <span className="text-right font-semibold text-slate-800">
              {formatOptionalNumber(candidate.predicted_tg)} {candidate.tg_unit}
            </span>
          </div>
          <div className="flex items-center justify-between gap-3">
            <span className="text-mutedForeground">Similarity</span>
            <span className="text-right font-semibold text-slate-800">
              {formatOptionalNumber(candidate.similarity_score, 3)}
            </span>
          </div>
          <div className="flex items-center justify-between gap-3">
            <span className="text-mutedForeground">SA Score</span>
            <span className="text-right font-semibold text-slate-800">{formatOptionalNumber(candidate.sa_score)}</span>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export function ConditionalGenerationPage({ onBackHome }: ConditionalGenerationPageProps) {
  const { smiles, setSmiles, iframeRef, setIsReady } = useKetcher(DEMO_GENERATION_SMILES);
  const generation = useConditionalGeneration();

  function updateRequest(partial: Partial<ConditionalGenerationTgRequest>) {
    generation.setRequest({
      ...generation.request,
      ...partial
    });
  }

  const canSubmit =
    !generation.isLoading &&
    smiles.trim().length > 0 &&
    Number.isFinite(generation.request.delta_tg) &&
    generation.request.candidate_count >= 1 &&
    generation.request.candidate_count <= 50 &&
    generation.request.top_k >= 1 &&
    generation.request.top_k <= 20 &&
    generation.request.temperature >= 0.1 &&
    generation.request.temperature <= 2.0;

  async function handleSubmit() {
    await generation.submit({
      ...generation.request,
      smiles
    });
  }

  return (
    <div className="space-y-6">
      <nav className="flex flex-col gap-3 rounded-[26px] border border-white/70 bg-white/80 px-4 py-4 shadow-sm backdrop-blur md:flex-row md:items-center md:justify-between md:px-5">
        <div className="flex items-center gap-3">
          <Button type="button" variant="outline" onClick={onBackHome}>
            <ArrowLeft className="mr-2 h-4 w-4" />
            Home
          </Button>
          <div>
            <div className="text-[11px] font-medium uppercase tracking-[0.2em] text-teal-700/70">Current Module</div>
            <div className="font-heading text-lg font-semibold tracking-tight text-slate-950">
              Conditional Polymer Generation
            </div>
          </div>
        </div>
        <Badge className="bg-teal-50 text-teal-800">Generation</Badge>
      </nav>

      <section className="hero-glow mesh-surface relative overflow-hidden rounded-[36px] border border-white/70 px-6 py-6 md:px-8 md:py-8">
        <div className="animate-fade-up">
          <div className="flex flex-wrap items-center gap-3">
            <div className="rounded-full border border-white/80 bg-white/80 px-4 py-2 text-sm font-semibold tracking-[0.16em] text-slate-950 shadow-sm">
              NEXPOLY
            </div>
            <Badge>Conditional Tg Generation</Badge>
          </div>
          <div className="mt-6 max-w-4xl">
            <h1 className="font-heading text-[2.35rem] font-semibold tracking-[-0.03em] text-slate-950 md:text-[3.7rem] md:leading-[0.95]">
              Conditional Polymer Generation
            </h1>
            <p className="mt-4 max-w-3xl text-base leading-7 text-slate-600 md:text-lg">
              Generate polymer candidates from a seed structure and a Tg shift.
            </p>
          </div>

          <div className="mt-8 grid gap-3 md:grid-cols-3">
            <div className="flex min-h-[150px] flex-col justify-center rounded-[26px] border border-white/80 bg-white/80 p-5 text-center shadow-sm backdrop-blur">
              <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-mutedForeground">
                <Sparkles className="h-4 w-4 text-teal-600" />
                ΔTg Input
              </div>
              <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight text-slate-950">
                {generation.request.delta_tg.toFixed(1)} °C
              </div>
            </div>
            <div className="flex min-h-[150px] flex-col justify-center rounded-[26px] border border-white/80 bg-white/80 p-5 text-center shadow-sm backdrop-blur">
              <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-mutedForeground">
                <Atom className="h-4 w-4 text-sky-600" />
                Structure Input
              </div>
              <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight text-slate-950">
                {smiles.trim().length > 0 ? "Ready" : "Waiting"}
              </div>
            </div>
            <div className="flex min-h-[150px] flex-col justify-center rounded-[26px] border border-white/80 bg-slate-950 p-5 text-center text-slate-50 shadow-[0_22px_50px_rgba(8,17,31,0.2)]">
              <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-slate-400">
                <Sparkles className="h-4 w-4 text-teal-300" />
                Results
              </div>
              <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight">
                {generation.data?.returned_count ?? generation.job?.accepted_count ?? 0}
              </div>
            </div>
          </div>
        </div>
      </section>

      <section className="grid items-stretch gap-6 xl:grid-cols-[minmax(0,1.18fr)_minmax(380px,0.82fr)]">
        <div className="min-w-0">
          <KetcherEditor
            smiles={smiles}
            iframeRef={iframeRef}
            onReadyChange={setIsReady}
            presetStructure={{
              label: "Load Demo Structure",
              smiles: DEMO_GENERATION_SMILES
            }}
            smilesPlaceholder="For example: [*]COCC([*])(C)COCCC#N"
            onChange={(value) => {
              setSmiles(value);
              generation.setRequest({ ...generation.request, smiles: value });
            }}
          />
        </div>

        <div className="flex min-w-0 flex-col gap-6">
          <StructurePreview3D
            smiles={smiles}
            className="xl:flex xl:flex-1 xl:flex-col"
            contentClassName="xl:flex xl:flex-1 xl:flex-col"
            previewClassName="h-[320px] xl:h-auto xl:min-h-[360px] xl:flex-1"
          />
          <Card className="overflow-hidden rounded-[30px] border-white/70">
            <CardHeader className="gap-3 border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.96)_0%,rgba(244,248,249,0.86)_100%)]">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-teal-700/80">
                    Generation
                  </div>
                  <CardTitle className="mt-2 text-[1.35rem] tracking-tight">Generation Settings</CardTitle>
                  <CardDescription>Set the Tg change and sampling controls.</CardDescription>
                </div>
                <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-slate-950 text-white shadow-[0_12px_30px_rgba(8,17,31,0.18)]">
                  <Microscope className="h-4 w-4" />
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-4 pt-5">
              <div className="space-y-3">
                <div className="grid gap-3 sm:grid-cols-2">
                  <label className="space-y-1.5">
                    <span className="whitespace-nowrap text-[10px] font-semibold uppercase tracking-[0.08em] text-mutedForeground">
                      ΔTg (°C)
                    </span>
                    <Input
                      type="number"
                      value={generation.request.delta_tg}
                      onChange={(event) =>
                        updateRequest({ delta_tg: parseNumber(event.target.value, generation.request.delta_tg) })
                      }
                    />
                  </label>
                  <label className="space-y-1.5">
                    <span className="whitespace-nowrap text-[10px] font-semibold uppercase tracking-[0.08em] text-mutedForeground">
                      Candidate Count
                    </span>
                    <Input
                      type="number"
                      min={1}
                      max={50}
                      step={1}
                      value={generation.request.candidate_count}
                      onChange={(event) =>
                        updateRequest({
                          candidate_count: parseNumber(event.target.value, generation.request.candidate_count)
                        })
                      }
                    />
                  </label>
                </div>
                <div className="grid gap-3 sm:grid-cols-2">
                  <label className="space-y-1.5">
                    <span className="whitespace-nowrap text-[10px] font-semibold uppercase tracking-[0.08em] text-mutedForeground">
                      Temperature
                    </span>
                    <Input
                      type="number"
                      min={0.1}
                      max={2}
                      step={0.05}
                      value={generation.request.temperature}
                      onChange={(event) =>
                        updateRequest({ temperature: parseNumber(event.target.value, generation.request.temperature) })
                      }
                    />
                  </label>
                  <label className="space-y-1.5">
                    <span className="whitespace-nowrap text-[10px] font-semibold uppercase tracking-[0.08em] text-mutedForeground">
                      Top-K Sampling
                    </span>
                    <Input
                      type="number"
                      min={1}
                      max={20}
                      step={1}
                      value={generation.request.top_k}
                      onChange={(event) => updateRequest({ top_k: parseNumber(event.target.value, generation.request.top_k) })}
                    />
                  </label>
                </div>
              </div>

              <div className="flex flex-col gap-3 border-t border-slate-200/70 pt-4 sm:flex-row sm:items-center sm:justify-between">
                <div className="text-sm leading-6 text-mutedForeground">
                  {generation.job
                    ? `${generation.job.status} | attempts ${generation.job.attempts} | accepted ${generation.job.accepted_count}`
                    : canSubmit
                      ? "Structure and ΔTg settings are ready."
                      : "Enter a seed structure and valid ΔTg settings."}
                </div>
                <Button type="button" className="min-h-[44px] min-w-[190px]" onClick={handleSubmit} disabled={!canSubmit}>
                  {generation.isLoading ? (
                    <LoaderCircle className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <Sparkles className="mr-2 h-4 w-4" />
                  )}
                  {generation.isLoading ? "Generating..." : "Run Generation"}
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>
      </section>

      <section className="overflow-hidden rounded-[32px] border border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(243,248,250,0.92)_100%)] shadow-soft">
        <div className="border-b border-slate-200/80 px-6 py-5 md:px-8">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
            <div>
              <div className="text-xs font-medium uppercase tracking-[0.18em] text-teal-700/70">Results</div>
              <h2 className="font-heading mt-2 text-[1.8rem] font-semibold tracking-tight text-slate-950">
                Generated Candidates
              </h2>
              <p className="mt-1 text-sm leading-6 text-mutedForeground">
                Generated structures are ranked by similarity, SA score, and deterministic tie-breakers.
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Badge className="bg-slate-100 text-slate-700">{generation.data?.returned_count ?? 0} candidates</Badge>
              <Badge className="bg-slate-100 text-slate-700">
                {generation.data ? `${generation.data.query_time_ms.toFixed(1)} ms` : "Idle"}
              </Badge>
            </div>
          </div>
        </div>
        <div className="px-4 py-5 md:px-5">
          {generation.error ? (
            <div className="rounded-[24px] border border-red-200 bg-red-50 px-5 py-4 text-sm font-medium text-red-700">
              {generation.error}
            </div>
          ) : null}
          {generation.isLoading ? (
            <div className="flex min-h-[240px] items-center justify-center rounded-[24px] border border-dashed border-white bg-white/70 text-sm font-semibold text-mutedForeground">
              <LoaderCircle className="mr-2 h-5 w-5 animate-spin text-teal-700" />
              Generating candidates
            </div>
          ) : null}
          {!generation.isLoading && !generation.data && !generation.error ? (
            <div className="flex min-h-[240px] flex-col items-center justify-center rounded-[24px] border border-dashed border-white bg-white/70 px-6 py-12 text-center">
              <div className="flex h-14 w-14 items-center justify-center rounded-2xl border border-white bg-white/85 text-slate-600 shadow-sm">
                <Sparkles className="h-5 w-5" />
              </div>
              <div className="mt-5 text-lg font-semibold text-slate-900">Generation Ready</div>
              <div className="mt-2 max-w-xl text-sm leading-6 text-mutedForeground">
                Results appear here after a generation job completes.
              </div>
            </div>
          ) : null}
          {!generation.isLoading && generation.data ? (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {generation.data.results.map((candidate) => (
                <CandidateCard key={`${candidate.rank}-${candidate.generated_smiles}`} candidate={candidate} />
              ))}
            </div>
          ) : null}
        </div>
      </section>
    </div>
  );
}
