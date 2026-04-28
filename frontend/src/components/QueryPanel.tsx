import { Check, Search, SlidersHorizontal, Sparkles, X } from "lucide-react";
import { type ReactNode, useState } from "react";
import {
  PREDICTABLE_PROPERTIES,
  PREDICT_PROPERTY_META,
  type PredictableProperty,
  type SmilesQueryRequest,
  type WorkspaceMode
} from "../types";
import { cn } from "../lib/utils";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";

type PropertyDialogMode = "query" | "predict";

type QueryPanelProps = {
  mode: WorkspaceMode;
  onModeChange: (mode: WorkspaceMode) => void;
  request: SmilesQueryRequest;
  onChange: (request: SmilesQueryRequest) => void;
  onQuerySubmit: () => void;
  onPredictSubmit: () => void;
  selectedProperties: PredictableProperty[];
  onSelectedPropertiesChange: (properties: PredictableProperty[]) => void;
  queryDisabled?: boolean;
  predictDisabled?: boolean;
  isQueryLoading?: boolean;
  isPredicting?: boolean;
  className?: string;
};

function ModeButton({
  active,
  title,
  detail,
  onClick,
  children
}: {
  active: boolean;
  title: string;
  detail: string;
  onClick: () => void;
  children?: ReactNode;
}) {
  return (
    <div
      className={cn(
        "rounded-[22px] border px-4 py-[0.92rem] transition-all duration-300",
        active
          ? "border-teal-500/30 bg-[linear-gradient(180deg,rgba(15,118,110,0.12)_0%,rgba(255,255,255,0.96)_100%)] shadow-[0_16px_40px_rgba(15,118,110,0.12)]"
          : "border-white/80 bg-[linear-gradient(180deg,rgba(255,255,255,0.9)_0%,rgba(244,248,249,0.8)_100%)] shadow-sm hover:border-white hover:shadow-panel"
      )}
    >
      <button
        type="button"
        onClick={onClick}
        className="min-h-[64px] w-full text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <div className="flex items-center justify-between gap-3">
          <div className="text-sm font-semibold tracking-tight text-slate-950">{title}</div>
          <div
            className={cn(
              "rounded-full px-2.5 py-1 text-[11px] font-medium uppercase tracking-[0.16em]",
              active ? "bg-teal-600 text-white" : "bg-slate-100 text-slate-500"
            )}
          >
            {active ? "Active" : "Switch"}
          </div>
        </div>
        <div className="mt-2 text-sm leading-6 text-mutedForeground">{detail}</div>
      </button>
      {children ? <div className="mt-3 border-t border-slate-200/70 pt-3">{children}</div> : null}
    </div>
  );
}

export function QueryPanel({
  mode,
  onModeChange,
  request,
  onChange,
  onQuerySubmit,
  onPredictSubmit,
  selectedProperties,
  onSelectedPropertiesChange,
  queryDisabled = false,
  predictDisabled = false,
  isQueryLoading = false,
  isPredicting = false,
  className
}: QueryPanelProps) {
  const [isPropertyDialogOpen, setIsPropertyDialogOpen] = useState(false);
  const [propertyDialogMode, setPropertyDialogMode] = useState<PropertyDialogMode>("predict");
  const isPropertyMatch = request.match_mode === "property";
  const isQueryMode = mode === "query";
  const selectedMatchProperty = request.property_name;
  const selectedMatchMeta = selectedMatchProperty ? PREDICT_PROPERTY_META[selectedMatchProperty] : null;
  const selectedSummary = selectedProperties
    .slice(0, 2)
    .map((property) => PREDICT_PROPERTY_META[property].label)
    .join(", ");

  function applyPreset(matchMode: SmilesQueryRequest["match_mode"]) {
    onChange({
      ...request,
      match_mode: matchMode,
      similarity_threshold: matchMode === "property" ? 0.72 : 1,
      top_k: 10
    });
  }

  function openPropertyDialog(dialogMode: PropertyDialogMode) {
    setPropertyDialogMode(dialogMode);
    setIsPropertyDialogOpen(true);
  }

  function selectMatchProperty(property: PredictableProperty) {
    onChange({
      ...request,
      match_mode: "property",
      property_name: property
    });
    setIsPropertyDialogOpen(false);
  }

  function toggleProperty(property: PredictableProperty) {
    if (selectedProperties.includes(property)) {
      onSelectedPropertiesChange(selectedProperties.filter((item) => item !== property));
      return;
    }

    onSelectedPropertiesChange([...selectedProperties, property]);
  }

  return (
    <>
      <Card className={cn("overflow-hidden rounded-[30px] border-white/70", className)}>
      <CardHeader className="gap-3 border-b border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.96)_0%,rgba(244,248,249,0.86)_100%)]">
        <div className="flex items-center justify-between gap-4">
          <div className="space-y-2">
            <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-teal-700/80">
              {isQueryMode ? "Similarity" : "Prediction"}
            </div>
            <CardTitle className="text-[1.35rem] tracking-tight">
              {isQueryMode ? "Similarity Matching" : "Prediction"}
            </CardTitle>
            <CardDescription>
              {isQueryMode ? "Choose structural or property-based similarity matching." : "Select target properties and run a prediction for the current structure."}
            </CardDescription>
          </div>
          <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-slate-950 text-white shadow-[0_12px_30px_rgba(8,17,31,0.18)]">
            {isQueryMode ? <SlidersHorizontal className="h-4 w-4" /> : <Sparkles className="h-4 w-4" />}
          </div>
        </div>
      </CardHeader>

      <CardContent className="flex flex-col gap-[0.72rem] pt-[0.96rem]">
        <div className="grid gap-[0.62rem] lg:grid-cols-2">
          <ModeButton
            active={isQueryMode}
            title="Similarity Matching"
            detail="Match existing polymer records by structural or property similarity."
            onClick={() => onModeChange("query")}
          />
          <ModeButton
            active={!isQueryMode}
            title="Prediction"
            detail="Estimate target properties for the current structure."
            onClick={() => onModeChange("predict")}
          />
        </div>

        {isQueryMode ? (
          <>
            <div className="grid gap-[0.62rem] lg:grid-cols-2">
              <ModeButton
                active={!isPropertyMatch}
                title="Structural Similarity"
                detail="Find polymer records with structures close to the current input."
                onClick={() => applyPreset("structure")}
              />
              <ModeButton
                active={isPropertyMatch}
                title="Property Similarity"
                detail={
                  selectedMatchMeta && selectedMatchProperty
                    ? `Selected: ${selectedMatchMeta.label} / ${selectedMatchProperty} (${selectedMatchMeta.unit})`
                    : "Select one property."
                }
                onClick={() => applyPreset("property")}
              >
                {isPropertyMatch ? (
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex shrink-0 flex-wrap items-center gap-2 sm:justify-end">
                      <Badge className="bg-slate-100 text-slate-700">
                        {selectedMatchProperty ? "1 selected" : "0 selected"}
                      </Badge>
                      <Button
                        type="button"
                        variant="outline"
                        className="min-h-[38px]"
                        onClick={() => openPropertyDialog("query")}
                      >
                        <Sparkles className="mr-2 h-4 w-4" />
                        Select Property
                      </Button>
                    </div>
                  </div>
                ) : null}
              </ModeButton>
            </div>

            <div className="flex flex-col gap-[0.5rem] border-t border-slate-200/70 pt-[0.8rem] sm:flex-row sm:items-center sm:justify-between">
              <div className="text-sm leading-6 text-mutedForeground">
                {queryDisabled
                  ? isPropertyMatch
                    ? "Enter a structure and select one property before running property similarity."
                    : "Enter a structure in the editor before running similarity matching."
                  : "The structure is ready. Run similarity matching to refresh the results panel."}
              </div>
              <Button className="min-h-[44px] min-w-[192px]" size="lg" onClick={onQuerySubmit} disabled={queryDisabled}>
                <Search className="mr-2 h-4 w-4" />
                {isQueryLoading ? "Matching..." : "Run Match"}
              </Button>
            </div>
          </>
        ) : (
          <>
            <div className="rounded-[24px] border border-white/80 bg-[linear-gradient(180deg,rgba(255,255,255,0.92)_0%,rgba(244,248,249,0.86)_100%)] p-4 shadow-sm">
              <div className="flex min-h-[94px] flex-col justify-between gap-4 sm:flex-row sm:items-center">
                <div>
                  <div className="text-sm font-semibold tracking-tight text-slate-950">Target Properties</div>
                  <div className="mt-1 text-sm leading-6 text-mutedForeground">
                    {selectedProperties.length > 0
                      ? `${selectedProperties.length} selected: ${selectedSummary}${selectedProperties.length > 2 ? ", and more" : ""}`
                      : "Open the picker and select one or more properties."}
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-2 sm:justify-end">
                  <Badge className="bg-slate-100 text-slate-700">{`${selectedProperties.length} selected`}</Badge>
                  <Button
                    type="button"
                    variant="outline"
                    className="min-h-[42px]"
                    onClick={() => openPropertyDialog("predict")}
                  >
                    <Sparkles className="mr-2 h-4 w-4" />
                    Select Properties
                  </Button>
                </div>
              </div>
            </div>

            <div className="flex flex-col gap-[0.5rem] border-t border-slate-200/70 pt-[0.8rem] sm:flex-row sm:items-center sm:justify-between">
              <div className="text-sm leading-6 text-mutedForeground">
                {predictDisabled
                  ? "Enter a structure and select at least one property before running prediction."
                  : "The structure and properties are ready. Run prediction and switch to the results."}
              </div>
              <Button className="min-h-[44px] min-w-[192px]" size="lg" onClick={onPredictSubmit} disabled={predictDisabled}>
                <Sparkles className="mr-2 h-4 w-4" />
                {isPredicting ? "Predicting..." : "Run Prediction"}
              </Button>
            </div>
          </>
        )}
      </CardContent>

      </Card>

      {isPropertyDialogOpen ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 px-4 py-6 backdrop-blur-sm">
          <div className="w-full max-w-3xl overflow-hidden rounded-[30px] border border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(242,248,250,0.94)_100%)] shadow-[0_32px_90px_rgba(8,17,31,0.28)]">
            <div className="flex items-start justify-between gap-4 border-b border-slate-200/80 px-5 py-5 md:px-6">
              <div>
                <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-teal-700/80">
                  {propertyDialogMode === "query" ? "Property Match" : "Prediction Properties"}
                </div>
                <div className="font-heading mt-2 text-[1.45rem] font-semibold tracking-tight text-slate-950">
                  {propertyDialogMode === "query" ? "Select Property Similarity Target" : "Select Prediction Properties"}
                </div>
                <div className="mt-1 text-sm leading-6 text-mutedForeground">
                  {propertyDialogMode === "query"
                    ? "Select one property to compare the current structure with the closest records."
                    : "Select one or more properties, then close the picker to continue."}
                </div>
              </div>
              <button
                type="button"
                onClick={() => setIsPropertyDialogOpen(false)}
                className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-white/80 bg-white/80 text-slate-600 shadow-sm transition-colors hover:text-slate-950"
                aria-label="Close property picker"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="max-h-[62vh] overflow-y-auto px-5 py-5 md:px-6">
              <div className="grid gap-3 md:grid-cols-2">
                {PREDICTABLE_PROPERTIES.map((property) => {
                  const selected =
                    propertyDialogMode === "query"
                      ? selectedMatchProperty === property
                      : selectedProperties.includes(property);
                  const meta = PREDICT_PROPERTY_META[property];

                  return (
                    <label
                      key={property}
                      className={cn(
                        "flex cursor-pointer items-start gap-3 rounded-[20px] border px-4 py-3 transition-all duration-200",
                        selected
                          ? "border-teal-500/30 bg-teal-50/80 shadow-[0_12px_30px_rgba(15,118,110,0.08)]"
                          : "border-white/80 bg-white/75 hover:border-slate-200"
                      )}
                    >
                      <input
                        type="checkbox"
                        className="sr-only"
                        checked={selected}
                        onChange={() => {
                          if (propertyDialogMode === "query") {
                            selectMatchProperty(property);
                            return;
                          }

                          toggleProperty(property);
                        }}
                      />
                      <span
                        className={cn(
                          "mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md border",
                          selected ? "border-teal-600 bg-teal-600 text-white" : "border-slate-300 bg-white"
                        )}
                      >
                        {selected ? <Check className="h-3.5 w-3.5" /> : null}
                      </span>
                      <span className="min-w-0">
                        <span className="block text-sm font-medium tracking-tight text-slate-950">{meta.label}</span>
                        <span className="mt-1 block text-xs uppercase tracking-[0.16em] text-mutedForeground">
                          {meta.unit}
                        </span>
                      </span>
                    </label>
                  );
                })}
              </div>
            </div>

            <div className="flex flex-col gap-3 border-t border-slate-200/80 px-5 py-4 sm:flex-row sm:items-center sm:justify-between md:px-6">
              <div className="text-sm text-mutedForeground">
                {propertyDialogMode === "query"
                  ? selectedMatchProperty
                    ? `1 / ${PREDICTABLE_PROPERTIES.length} properties selected`
                    : `0 / ${PREDICTABLE_PROPERTIES.length} properties selected`
                  : `${selectedProperties.length} / ${PREDICTABLE_PROPERTIES.length} properties selected`}
              </div>
              <div className="flex flex-wrap gap-2 sm:justify-end">
                {propertyDialogMode === "predict" ? (
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => onSelectedPropertiesChange([])}
                    disabled={selectedProperties.length === 0}
                  >
                    Clear Selection
                  </Button>
                ) : null}
                <Button type="button" onClick={() => setIsPropertyDialogOpen(false)}>
                  Done
                </Button>
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
