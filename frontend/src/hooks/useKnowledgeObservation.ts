import { useCallback } from "react";
import { postKnowledgeObservation } from "../services/api";
import type { KnowledgeObservationRequest } from "../types";
import { useKnowledgeRecording } from "./useKnowledgeRecording";

export function useKnowledgeObservation(searchId?: string) {
  const track = useKnowledgeRecording()?.track;
  return useCallback((knowledgeId: number, source: KnowledgeObservationRequest["source"]) => {
    if (!searchId) return;
    const request = (recordingId?: string) => postKnowledgeObservation({
      search_id: searchId, knowledge_id: knowledgeId, source,
      ...(recordingId ? { recording_id: recordingId } : {})
    });
    void (track ? track(request) : request())
      .catch((error: unknown) => {
        console.warn("Knowledge observation failed", error);
      });
  }, [searchId, track]);
}
