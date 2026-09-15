import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";
import { isApiRequestError, startKnowledgeRecording, stopKnowledgeRecording, summarizeKnowledgeRecording } from "../services/api";
import type { KnowledgeRecording, KnowledgeRecordingSummary } from "../types";

type Phase = "idle" | "starting" | "recording" | "stopping" | "stop_failed" | "summarizing" | "summary_failed" | "stopped";
type RecordingContextValue = {
  phase: Phase;
  pending: number;
  error: string | null;
  incomplete: boolean;
  result: KnowledgeRecording | null;
  summary: KnowledgeRecordingSummary | null;
  partialSummary: string;
  start: () => Promise<void>;
  stop: () => Promise<void>;
  retrySummary: () => Promise<void>;
  isRecording: () => boolean;
  track: <T>(operation: (recordingId?: string) => Promise<T>) => Promise<T>;
};

const RecordingContext = createContext<RecordingContextValue | null>(null);
export function useKnowledgeRecording() {
  return useContext(RecordingContext);
}

function createRecordingId(): string {
  const browserCrypto = globalThis.crypto;
  if (typeof browserCrypto?.randomUUID === "function") return browserCrypto.randomUUID();
  if (typeof browserCrypto?.getRandomValues !== "function") throw new Error("当前浏览器无法生成记录 ID");
  // Public HTTP pages can use getRandomValues even when randomUUID is unavailable.
  return Array.from(browserCrypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function KnowledgeRecordingProvider({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<Phase>("idle");
  const phaseRef = useRef<Phase>("idle");
  const idRef = useRef<string | undefined>(undefined);
  const pendingRef = useRef(0);
  const [pending, setPending] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [incomplete, setIncomplete] = useState(false);
  const [result, setResult] = useState<KnowledgeRecording | null>(null);
  const [summary, setSummary] = useState<KnowledgeRecordingSummary | null>(null);
  const [partialSummary, setPartialSummary] = useState("");

  const changePhase = useCallback((next: Phase) => {
    phaseRef.current = next;
    setPhase(next);
  }, []);

  const start = useCallback(async () => {
    if (!["idle", "stopped"].includes(phaseRef.current)) return;
    setError(null);
    try {
      if (phaseRef.current === "stopped") idRef.current = undefined;
      if (!idRef.current) idRef.current = createRecordingId();
      changePhase("starting");
      const response = await startKnowledgeRecording(idRef.current);
      if (response.status === "stopped") throw new Error("该记录已结束，请刷新后重新开始");
      setResult(null);
      setSummary(null);
      setPartialSummary("");
      setIncomplete(false);
      changePhase("recording");
    } catch (cause) {
      setError(isApiRequestError(cause, 404)
        ? "开始记录失败：当前服务尚未启用浏览记录功能，请更新服务后重试。"
        : `开始记录失败：${cause instanceof Error ? cause.message : "请求失败"}。请点击开始记录重试。`);
      changePhase("idle");
    }
  }, [changePhase]);

  const track = useCallback(async <T,>(operation: (recordingId?: string) => Promise<T>): Promise<T> => {
    const id = phaseRef.current === "recording" ? idRef.current : undefined;
    if (!id) return operation();
    pendingRef.current += 1;
    setPending(pendingRef.current);
    try {
      return await operation(id);
    } catch (cause) {
      setIncomplete(true);
      throw cause;
    } finally {
      pendingRef.current -= 1;
      setPending(pendingRef.current);
    }
  }, []);
  const isRecording = useCallback(() => phaseRef.current === "recording", []);

  const generateSummary = useCallback(async (recordingId: string) => {
    changePhase("summarizing");
    setError(null);
    setPartialSummary("");
    try {
      setSummary(await summarizeKnowledgeRecording(recordingId, setPartialSummary));
      changePhase("stopped");
    } catch (cause) {
      setError(`总结失败：${cause instanceof Error ? cause.message : "请求失败"}。本次记录已保留，可重试总结。`);
      changePhase("summary_failed");
    }
  }, [changePhase]);

  const retrySummary = useCallback(async () => {
    if (phaseRef.current === "summary_failed" && idRef.current) await generateSummary(idRef.current);
  }, [generateSummary]);

  const stop = useCallback(async () => {
    if (!["recording", "stop_failed"].includes(phaseRef.current) || pendingRef.current || !idRef.current) return;
    // Freeze local collection immediately; an uncertain response can be retried with the same ID.
    changePhase("stopping");
    setError(null);
    try {
      setResult(await stopKnowledgeRecording(idRef.current));
      await generateSummary(idRef.current);
    } catch (cause) {
      setError(`结束记录失败：${cause instanceof Error ? cause.message : "请求失败"}。已暂停收集，可重试结束。`);
      changePhase("stop_failed");
    }
  }, [changePhase, generateSummary]);

  return <RecordingContext.Provider value={{ phase, pending, error, incomplete, result, summary, partialSummary, start, stop, retrySummary, track, isRecording }}>
    {children}
  </RecordingContext.Provider>;
}
