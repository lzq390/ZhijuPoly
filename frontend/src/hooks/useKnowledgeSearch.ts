import { useCallback, useEffect, useRef, useState } from "react";
import { normalizeKnowledgeSearchGroups } from "../lib/knowledgeSearchExpression";
import { searchKnowledge } from "../services/api";
import { useKnowledgeRecording } from "./useKnowledgeRecording";
import type { KnowledgeSearchGroup, KnowledgeSearchResponse } from "../types";

type KnowledgeSearchState = {
  isLoading: boolean;
  error: string | null;
  data: KnowledgeSearchResponse | null;
};

export function useKnowledgeSearch() {
  const recording = useKnowledgeRecording();
  const track = recording?.track;
  const isRecording = recording?.isRecording;
  const [state, setState] = useState<KnowledgeSearchState>({
    isLoading: false,
    error: null,
    data: null
  });
  const requestTokenRef = useRef(0);
  const requestAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => {
      requestTokenRef.current += 1;
      if (!isRecording?.()) requestAbortRef.current?.abort();
      requestAbortRef.current = null;
    };
  }, [isRecording]);

  const submit = useCallback(async (
    query: string,
    topK: number,
    groups?: KnowledgeSearchGroup[],
    page = 1,
    pageSize?: number
  ): Promise<KnowledgeSearchResponse | null> => {
    requestAbortRef.current?.abort();
    const token = requestTokenRef.current + 1;
    requestTokenRef.current = token;
    const controller = new AbortController();
    requestAbortRef.current = controller;

    setState({
      isLoading: true,
      error: null,
      data: null
    });

    try {
      const cleanedGroups = normalizeKnowledgeSearchGroups(groups ?? []);
      const request = (recordingId?: string) => searchKnowledge(
        {
          ...(recordingId ? { recording_id: recordingId } : {}),
          query,
          top_k: topK,
          page,
          ...(pageSize ? { page_size: pageSize } : {}),
          ...(cleanedGroups.length ? { groups: cleanedGroups } : {})
        },
        controller.signal
      );
      const data = await (track ? track(request) : request());
      if (requestTokenRef.current !== token || controller.signal.aborted) {
        return null;
      }
      setState({
        isLoading: false,
        error: null,
        data
      });
      return data;
    } catch (error) {
      if (
        requestTokenRef.current !== token ||
        controller.signal.aborted ||
        (error instanceof DOMException && error.name === "AbortError")
      ) {
        return null;
      }
      setState({
        isLoading: false,
        error: error instanceof Error ? error.message : "Unknown error",
        data: null
      });
      return null;
    } finally {
      if (requestAbortRef.current === controller) {
        requestAbortRef.current = null;
      }
    }
  }, [track]);

  return {
    ...state,
    submit
  };
}
