import { useCallback } from "react";
import { postPropertyFilterObservation } from "../services/api";
import type { PropertyFilterObservationRequest } from "../types";
import { useKnowledgeRecording } from "./useKnowledgeRecording";

type Target =
  | { source: "measurement_details"; filter_index: number }
  | { source: "smiles"; smiles_field: "smiles" | "canonical_smiles" };

export function usePropertyFilterObservation(searchId: string | undefined, resultIndex: number) {
  const track = useKnowledgeRecording()?.track;
  return useCallback((target: Target) => {
    // Ordinary platform responses have no POC snapshot ID.
    if (!searchId) return;
    const payload: PropertyFilterObservationRequest = { search_id: searchId, result_index: resultIndex, ...target };
    const request = (recordingId?: string) => postPropertyFilterObservation({ ...payload,
      ...(recordingId ? { recording_id: recordingId } : {}) });
    void (track ? track(request) : request()).catch((error: unknown) => {
      console.warn("Property filter observation failed", error);
    });
  }, [searchId, resultIndex, track]);
}
