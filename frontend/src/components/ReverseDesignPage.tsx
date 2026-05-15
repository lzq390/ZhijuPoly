import { ArrowLeft, Database, Microscope, Search, Target } from "lucide-react";
import { KetcherEditor } from "./KetcherEditor";
import { ReverseDesignResults } from "./ReverseDesignResults";
import { StructurePreview3D } from "./StructurePreview3D";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";
import { Input } from "./ui/input";
import { REVERSE_DESIGN_DEMO_SMILES } from "../constants/reverseDesignDefaults";
import { useKetcher } from "../hooks/useKetcher";
import { useReverseDesign } from "../hooks/useReverseDesign";
import type { KnowledgeNavigationRequest, ReverseDesignTgRequest } from "../types";

type ReverseDesignPageProps = {
  onBackHome: () => void;
  onOpenKnowledge: (request: KnowledgeNavigationRequest) => void;
};

function parseOptionalNumber(value: string) {
  if (!value.trim()) {
    return null;
  }

  const parsed = Number(value);
  return Number.isNaN(parsed) ? null : parsed;
}

export function ReverseDesignPage({ onBackHome, onOpenKnowledge }: ReverseDesignPageProps) {
  const { smiles, setSmiles, iframeRef, setIsReady } = useKetcher();
  const reverseDesign = useReverseDesign();

  function updateRequest(partial: Partial<ReverseDesignTgRequest>) {
    reverseDesign.setRequest({
      ...reverseDesign.request,
      ...partial
    });
  }

  const canSubmit =
    !reverseDesign.isLoading &&
    smiles.trim().length > 0 &&
    reverseDesign.request.target_tg !== null &&
    !Number.isNaN(reverseDesign.request.target_tg) &&
    reverseDesign.request.similarity_threshold >= 0 &&
    reverseDesign.request.similarity_threshold <= 1 &&
    reverseDesign.request.candidate_size >= 1 &&
    reverseDesign.request.candidate_size <= 200;

  async function handleSubmit() {
    await reverseDesign.submit({
      ...reverseDesign.request,
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
            <div className="font-heading text-lg font-semibold tracking-tight text-slate-950">Tg Reverse Design</div>
          </div>
        </div>
        <Badge className="bg-teal-50 text-teal-800">Reverse Design</Badge>
      </nav>

      <section className="hero-glow mesh-surface relative overflow-hidden rounded-[36px] border border-white/70 px-6 py-6 md:px-8 md:py-8">
        <div className="animate-fade-up">
          <div className="flex flex-wrap items-center gap-3">
            <div className="rounded-full border border-white/80 bg-white/80 px-4 py-2 text-sm font-semibold tracking-[0.16em] text-slate-950 shadow-sm">
              NEXPOLY
            </div>
            <Badge>PI Candidate Search</Badge>
          </div>
          <div className="mt-6 max-w-4xl">
            <h1 className="font-heading text-[2.4rem] font-semibold tracking-[-0.03em] text-slate-950 md:text-[4rem] md:leading-[0.95]">
              Tg Reverse Design
            </h1>
            <p className="mt-4 max-w-3xl text-base leading-7 text-slate-600 md:text-lg">
              Draw a target polymer structure, set the desired Tg, and search the local PI candidate database for similar candidates.
            </p>
          </div>

          <div className="mt-8 grid gap-3 md:grid-cols-3">
            <div className="flex min-h-[150px] flex-col justify-center rounded-[26px] border border-white/80 bg-white/80 p-5 text-center shadow-sm backdrop-blur">
              <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-mutedForeground">
                <Target className="h-4 w-4 text-teal-600" />
                Target Tg
              </div>
              <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight text-slate-950">
                {reverseDesign.request.target_tg === null ? "Waiting" : `${reverseDesign.request.target_tg} °C`}
              </div>
            </div>
            <div className="flex min-h-[150px] flex-col justify-center rounded-[26px] border border-white/80 bg-white/80 p-5 text-center shadow-sm backdrop-blur">
              <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-mutedForeground">
                <Microscope className="h-4 w-4 text-sky-600" />
                Structure Input
              </div>
              <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight text-slate-950">
                {smiles.trim().length > 0 ? "Ready" : "Waiting"}
              </div>
            </div>
            <div className="flex min-h-[150px] flex-col justify-center rounded-[26px] border border-white/80 bg-slate-950 p-5 text-center text-slate-50 shadow-[0_22px_50px_rgba(8,17,31,0.2)]">
              <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-slate-400">
                <Database className="h-4 w-4 text-teal-300" />
                Results
              </div>
              <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight">
                {reverseDesign.data?.total ?? reverseDesign.job?.matched_count ?? 0}
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
              smiles: REVERSE_DESIGN_DEMO_SMILES
            }}
            onChange={(value) => {
              setSmiles(value);
              reverseDesign.setRequest({ ...reverseDesign.request, smiles: value });
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
                  <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-teal-700/80">Reverse Design</div>
                  <CardTitle className="mt-2 text-[1.35rem] tracking-tight">Reverse Settings</CardTitle>
                  <CardDescription>Search PI candidates by target Tg and Tanimoto similarity.</CardDescription>
                </div>
                <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-slate-950 text-white shadow-[0_12px_30px_rgba(8,17,31,0.18)]">
                  <Database className="h-4 w-4" />
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-4 pt-5">
              <div className="grid gap-3 sm:grid-cols-3">
                <label className="space-y-1.5">
                  <span className="whitespace-nowrap text-[10px] font-semibold uppercase tracking-[0.08em] text-mutedForeground">Target Tg (°C)</span>
                  <Input
                    type="number"
                    value={reverseDesign.request.target_tg ?? ""}
                    onChange={(event) => updateRequest({ target_tg: parseOptionalNumber(event.target.value) })}
                    placeholder="500"
                  />
                </label>
                <label className="space-y-1.5">
                  <span className="whitespace-nowrap text-[10px] font-semibold uppercase tracking-[0.08em] text-mutedForeground">Similarity Threshold</span>
                  <Input
                    type="number"
                    min={0}
                    max={1}
                    step={0.01}
                    value={reverseDesign.request.similarity_threshold}
                    onChange={(event) => updateRequest({ similarity_threshold: Number(event.target.value) })}
                  />
                </label>
                <label className="space-y-1.5">
                  <span className="whitespace-nowrap text-[10px] font-semibold uppercase tracking-[0.08em] text-mutedForeground">Candidate Size</span>
                  <Input
                    type="number"
                    min={1}
                    max={200}
                    step={1}
                    value={reverseDesign.request.candidate_size}
                    onChange={(event) => updateRequest({ candidate_size: Number(event.target.value) })}
                  />
                </label>
              </div>

              <div className="flex flex-col gap-3 border-t border-slate-200/70 pt-4 sm:flex-row sm:items-center sm:justify-between">
                <div className="text-sm leading-6 text-mutedForeground">
                  {canSubmit ? "Target and structure are ready." : "Enter a structure and a numeric target Tg."}
                </div>
                <Button type="button" className="min-h-[44px] min-w-[180px]" onClick={handleSubmit} disabled={!canSubmit}>
                  <Search className="mr-2 h-4 w-4" />
                  {reverseDesign.isLoading ? "Searching..." : "Run Tg Search"}
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>
      </section>

      <ReverseDesignResults
        data={reverseDesign.data}
        error={reverseDesign.error}
        isLoading={reverseDesign.isLoading}
        job={reverseDesign.job}
        onOpenKnowledge={onOpenKnowledge}
      />
    </div>
  );
}
