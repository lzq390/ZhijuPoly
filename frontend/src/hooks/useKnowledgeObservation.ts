import { useCallback } from "react";
import { postKnowledgeObservation } from "../services/api";
import type { KnowledgeObservationRequest, LiteratureDocumentResult } from "../types";
import { useKnowledgeRecording } from "./useKnowledgeRecording";

export function useKnowledgeObservation(searchId?: string) {
  const track = useKnowledgeRecording()?.track;
  return useCallback((record: number | LiteratureDocumentResult, source: KnowledgeObservationRequest["source"]) => {
    if (!searchId) return;
    const request = (recordingId?: string) => postKnowledgeObservation({
      search_id: searchId, source,
      ...(typeof record === "number" ? { knowledge_id: record } : {
        document_uid: record.document_uid,
        generation_id: record.generation_id,
        snapshot_uid: record.snapshot_uid
      }),
      ...(recordingId ? { recording_id: recordingId } : {})
    });
    void (track ? track(request) : request())
      .catch((error: unknown) => {
        console.warn("Knowledge observation failed", error);
      });
  }, [searchId, track]);
}
