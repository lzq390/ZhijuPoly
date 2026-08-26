import { useCallback, useEffect, useRef, useState } from "react";
import { fetchTgAssistantGuide, fetchTgAssistantStatus } from "../services/api";
import { streamTgAssistant, type TgAssistantSseEvent } from "../services/tgAssistantStream";
import type {
  TgAssistantGuideResponse,
  TgAssistantOperation,
  TgAssistantPageContext,
  TgAssistantStatusResponse
} from "../types";

const SESSION_KEY = "nexpoly.assistant.tg.session.v2";
const LEGACY_SESSION_KEY = "nexpoly.assistant.tg.session.v1";
const CONSENT_KEY = "nexpoly.assistant.tg.page-context-consent.v1";
const MAX_STORED_SESSION_CHARACTERS = 512 * 1024;

function id() {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export type TgAssistantMessageItem = {
  kind: "message";
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: string;
  status: "done" | "understanding" | "streaming" | "stopped" | "error";
  userItemId?: string;
  image?: { name: string; size: number; type: string };
  error?: { code: string; message: string; retryable: boolean };
  processing?: {
    currentStage: TgAssistantProcessStage | null;
    stages: TgAssistantProcessStage[];
    intentSummary: string;
    answerSummary: string;
    intentSummaryDone: boolean;
    answerSummaryDone: boolean;
    warning?: string;
  };
  meta?: {
    requestId?: string;
    contextTrimmed: string[];
    contextAttached: boolean;
    imageAttached?: boolean;
    imageCount?: number;
    canvasImageAttached?: boolean;
    userImageAttached?: boolean;
  };
};

export type TgAssistantProcessStage =
  | "capturing_canvas"
  | "routing_request"
  | "validating_decision"
  | "analyzing_images"
  | "composing_answer"
  | "writing_answer"
  | "transport_fallback";

export type TgAssistantActionItem = {
  kind: "action";
  id: string;
  proposalId: string;
  basisRevision: string;
  operations: TgAssistantOperation[];
  previousParameters: {
    target_tg: number | null;
    similarity_threshold: number | null;
    candidate_size: number | null;
  } | null;
  previousStructure?: string | null;
  sourceText: string;
  createdAt: string;
  status: "pending" | "applying" | "applied" | "rejected" | "expired" | "failed";
  detail?: string;
};

export type TgAssistantNavigationItem = {
  kind: "navigation";
  id: string;
  target: "parameters" | "results";
  basisRevision: string;
  createdAt: string;
};

export type TgAssistantDividerItem = {
  kind: "divider";
  id: string;
  text: string;
  createdAt: string;
};

export type TgAssistantItem =
  | TgAssistantMessageItem
  | TgAssistantActionItem
  | TgAssistantNavigationItem
  | TgAssistantDividerItem;

export type TgAssistantPageAdapter = {
  captureContext: () => Promise<TgAssistantPageContext>;
  captureCanvasImage?: (signal?: AbortSignal) => Promise<Blob | null>;
  getRevision: () => string;
  getDraftParameters: () => {
    target_tg: number | null;
    similarity_threshold: number | null;
    candidate_size: number | null;
  };
  getStructureSmiles: () => string | null;
  navigate: (target: "parameters" | "results") => void;
  applyOperations: (
    operations: TgAssistantOperation[],
    basisRevision: string
  ) => Promise<{ status: "applied" | "expired" | "failed"; detail?: string }>;
};

type StoredSession = { version: 2; items: TgAssistantItem[] };

const PROCESS_STAGES: TgAssistantProcessStage[] = [
  "capturing_canvas",
  "routing_request",
  "validating_decision",
  "analyzing_images",
  "composing_answer",
  "writing_answer",
  "transport_fallback"
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, allowed: string[]) {
  return Object.keys(value).every((key) => allowed.includes(key));
}

function validFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function validTgAssistantOperations(value: unknown): value is TgAssistantOperation[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 2) return false;
  const types: string[] = [];
  for (const operation of value) {
    if (!isRecord(operation) || typeof operation.type !== "string") return false;
    types.push(operation.type);
    if (operation.type === "run_search") {
      if (!hasExactKeys(operation, ["type"])) return false;
      continue;
    }
    if (operation.type === "set_structure") {
      if (
        !hasExactKeys(operation, ["type", "smiles"]) ||
        typeof operation.smiles !== "string" ||
        operation.smiles.trim().length < 1 ||
        operation.smiles.length > 8000
      ) return false;
      continue;
    }
    if (operation.type !== "set_parameters" || !hasExactKeys(operation, ["type", "parameters"])) {
      return false;
    }
    if (!isRecord(operation.parameters)) return false;
    const entries = Object.entries(operation.parameters);
    if (entries.length === 0) return false;
    for (const [key, parameter] of entries) {
      if (!["target_tg", "similarity_threshold", "candidate_size"].includes(key)) return false;
      if (!validFiniteNumber(parameter)) return false;
      if (key === "similarity_threshold" && (parameter < 0 || parameter > 1)) return false;
      if (key === "candidate_size" && (!Number.isInteger(parameter) || parameter < 1 || parameter > 200)) {
        return false;
      }
    }
  }
  return (
    types.join(",") === "set_parameters" ||
    types.join(",") === "run_search" ||
    types.join(",") === "set_parameters,run_search" ||
    types.join(",") === "set_structure"
  );
}

function validPreviousParameters(value: unknown) {
  if (value === null) return true;
  if (!isRecord(value) || !hasExactKeys(value, ["target_tg", "similarity_threshold", "candidate_size"])) {
    return false;
  }
  return ["target_tg", "similarity_threshold", "candidate_size"].every((key) =>
    value[key] === null || validFiniteNumber(value[key])
  );
}

function validProcessing(value: unknown) {
  if (value === undefined) return true;
  if (!isRecord(value) || !hasExactKeys(value, [
    "currentStage", "stages", "intentSummary", "answerSummary",
    "intentSummaryDone", "answerSummaryDone", "warning"
  ])) return false;
  return (value.currentStage === null || PROCESS_STAGES.includes(value.currentStage as TgAssistantProcessStage)) &&
    Array.isArray(value.stages) && value.stages.every((stage) => PROCESS_STAGES.includes(stage as TgAssistantProcessStage)) &&
    typeof value.intentSummary === "string" && value.intentSummary.length <= 4000 &&
    typeof value.answerSummary === "string" && value.answerSummary.length <= 4000 &&
    typeof value.intentSummaryDone === "boolean" && typeof value.answerSummaryDone === "boolean" &&
    (value.warning === undefined || typeof value.warning === "string");
}

function isStoredItem(value: unknown): value is TgAssistantItem {
  if (!isRecord(value) || typeof value.id !== "string" || typeof value.createdAt !== "string") {
    return false;
  }
  if (value.kind === "message") {
    const validError = value.error === undefined || (
      isRecord(value.error) &&
      hasExactKeys(value.error, ["code", "message", "retryable"]) &&
      typeof value.error.code === "string" &&
      typeof value.error.message === "string" &&
      typeof value.error.retryable === "boolean"
    );
    const validMeta = value.meta === undefined || (
      isRecord(value.meta) &&
      hasExactKeys(value.meta, [
        "requestId", "contextTrimmed", "contextAttached", "imageAttached", "imageCount",
        "canvasImageAttached", "userImageAttached"
      ]) &&
      (value.meta.requestId === undefined || typeof value.meta.requestId === "string") &&
      Array.isArray(value.meta.contextTrimmed) &&
      value.meta.contextTrimmed.every((item) => typeof item === "string") &&
      typeof value.meta.contextAttached === "boolean" &&
      (value.meta.imageAttached === undefined || typeof value.meta.imageAttached === "boolean") &&
      (value.meta.imageCount === undefined || (Number.isInteger(value.meta.imageCount) && Number(value.meta.imageCount) >= 0 && Number(value.meta.imageCount) <= 2)) &&
      (value.meta.canvasImageAttached === undefined || typeof value.meta.canvasImageAttached === "boolean") &&
      (value.meta.userImageAttached === undefined || typeof value.meta.userImageAttached === "boolean")
    );
    const validImage = value.image === undefined || (
      isRecord(value.image) &&
      hasExactKeys(value.image, ["name", "size", "type"]) &&
      typeof value.image.name === "string" &&
      validFiniteNumber(value.image.size) &&
      value.image.size >= 0 &&
      typeof value.image.type === "string"
    );
    return hasExactKeys(value, [
      "kind", "id", "role", "content", "createdAt", "status", "userItemId", "image", "error", "processing", "meta"
    ]) &&
      (value.role === "user" || value.role === "assistant") &&
      typeof value.content === "string" && value.content.length <= 8000 &&
      ["done", "understanding", "streaming", "stopped", "error"].includes(String(value.status)) &&
      (value.userItemId === undefined || typeof value.userItemId === "string") &&
      validError && validMeta && validImage && validProcessing(value.processing);
  }
  if (value.kind === "action") {
    return hasExactKeys(value, [
      "kind", "id", "proposalId", "basisRevision", "operations", "previousParameters",
      "previousStructure", "sourceText", "createdAt", "status", "detail"
    ]) &&
      typeof value.proposalId === "string" && typeof value.basisRevision === "string" &&
      typeof value.sourceText === "string" && value.sourceText.length <= 8000 &&
      ["pending", "applying", "applied", "rejected", "expired", "failed"].includes(String(value.status)) &&
      (value.detail === undefined || typeof value.detail === "string") &&
      (value.previousStructure === undefined || value.previousStructure === null || typeof value.previousStructure === "string") &&
      validPreviousParameters(value.previousParameters) && validTgAssistantOperations(value.operations);
  }
  return false;
}

function trimItems(items: TgAssistantItem[]) {
  let messages = 0;
  const kept: TgAssistantItem[] = [];
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.kind === "message") {
      if (messages >= 30) continue;
      messages += 1;
    }
    kept.push(item);
    if (kept.length >= 80) break;
  }
  return kept.reverse();
}

function loadSession(): { items: TgAssistantItem[]; warning: string | null } {
  try {
    const currentRaw = sessionStorage.getItem(SESSION_KEY);
    const raw = currentRaw ?? sessionStorage.getItem(LEGACY_SESSION_KEY);
    if (!raw) return { items: [], warning: null };
    if (raw.length > MAX_STORED_SESSION_CHARACTERS) throw new Error("session is too large");
    const parsed = JSON.parse(raw) as StoredSession;
    if (
      !isRecord(parsed) ||
      !hasExactKeys(parsed, ["version", "items"]) ||
      ![1, 2].includes(Number(parsed.version)) ||
      !Array.isArray(parsed.items) ||
      !parsed.items.every(isStoredItem)
    ) {
      throw new Error("invalid session");
    }
    const items = parsed.items.flatMap((item) => {
      if (item.kind === "action" && !["applied", "rejected", "expired"].includes(item.status)) {
        return [];
      }
      if (item.kind === "message" && (item.status === "streaming" || item.status === "understanding")) {
        return [{ ...item, status: "stopped" as const }];
      }
      return [item];
    });
    return { items: trimItems(items), warning: null };
  } catch {
    try {
      sessionStorage.removeItem(SESSION_KEY);
      sessionStorage.removeItem(LEGACY_SESSION_KEY);
    } catch {
      // The warning below covers unavailable storage.
    }
    return { items: [], warning: "本标签页的 AI 对话无法持久化。" };
  }
}

function loadConsent(): "unknown" | "granted" | "denied" {
  try {
    const value = localStorage.getItem(CONSENT_KEY);
    return value === "granted" || value === "denied" ? value : "unknown";
  } catch {
    return "unknown";
  }
}

export function useTgAssistant() {
  const [initial] = useState(loadSession);
  const [items, setItems] = useState<TgAssistantItem[]>(initial.items);
  const itemsRef = useRef(items);
  const [storageWarning, setStorageWarning] = useState<string | null>(initial.warning);
  const [consent, setConsentState] = useState(loadConsent);
  const [status, setStatus] = useState<TgAssistantStatusResponse | null>(null);
  const [guide, setGuide] = useState<TgAssistantGuideResponse | null>(null);
  const [metadataLoading, setMetadataLoading] = useState(false);
  const [metadataError, setMetadataError] = useState<string | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const metadataLoadedRef = useRef(false);
  const controllerRef = useRef<AbortController | null>(null);
  const imageFilesRef = useRef(new Map<string, File>());
  const stopRequestedRef = useRef(false);
  const adapterRef = useRef<TgAssistantPageAdapter | null>(null);

  const updateItems = useCallback((updater: (current: TgAssistantItem[]) => TgAssistantItem[]) => {
    const next = trimItems(updater(itemsRef.current));
    const retainedImageIds = new Set(next.flatMap((item) =>
      item.kind === "message" && item.role === "user" && item.image ? [item.id] : []
    ));
    for (const userItemId of imageFilesRef.current.keys()) {
      if (!retainedImageIds.has(userItemId)) imageFilesRef.current.delete(userItemId);
    }
    itemsRef.current = next;
    setItems(next);
  }, []);

  useEffect(() => {
    try {
      const persisted = items.filter((item) =>
        item.kind === "message" ||
        (item.kind === "action" && ["applied", "rejected", "expired"].includes(item.status))
      );
      const serialized = JSON.stringify({ version: 2, items: persisted });
      if (serialized.length > MAX_STORED_SESSION_CHARACTERS) {
        throw new Error("session is too large");
      }
      sessionStorage.setItem(SESSION_KEY, serialized);
      sessionStorage.removeItem(LEGACY_SESSION_KEY);
    } catch {
      setStorageWarning("本标签页的 AI 对话无法持久化。");
    }
  }, [items]);

  useEffect(() => () => {
    controllerRef.current?.abort();
    imageFilesRef.current.clear();
  }, []);

  const loadMetadata = useCallback(async () => {
    if (metadataLoadedRef.current || metadataLoading) return;
    metadataLoadedRef.current = true;
    setMetadataLoading(true);
    setMetadataError(null);
    const [statusResult, guideResult] = await Promise.allSettled([
      fetchTgAssistantStatus(),
      fetchTgAssistantGuide()
    ]);
    if (statusResult.status === "fulfilled") setStatus(statusResult.value);
    if (guideResult.status === "fulfilled") setGuide(guideResult.value);
    if (statusResult.status === "rejected" || guideResult.status === "rejected") {
      setMetadataError("AI 助手状态或使用指南加载失败。");
    }
    setMetadataLoading(false);
  }, [metadataLoading]);

  const setConsent = useCallback((next: "granted" | "denied" | "unknown") => {
    setConsentState(next);
    try {
      if (next === "unknown") localStorage.removeItem(CONSENT_KEY);
      else localStorage.setItem(CONSENT_KEY, next);
    } catch {
      setStorageWarning("页面上下文授权无法在浏览器中保存。此标签页仍可继续使用。");
    }
  }, []);

  const addDivider = useCallback((text: string) => {
    const last = itemsRef.current.at(-1);
    if (last?.kind === "divider" && last.text === text) return;
    updateItems((current) => [...current, { kind: "divider", id: id(), text, createdAt: new Date().toISOString() }]);
  }, [updateItems]);

  const registerAdapter = useCallback((adapter: TgAssistantPageAdapter) => {
    adapterRef.current = adapter;
    return () => {
      if (adapterRef.current === adapter) adapterRef.current = null;
      queueMicrotask(() => {
        if (adapterRef.current !== null) return;
        updateItems((current) => current.map((item) =>
          item.kind === "action" && item.status === "pending"
            ? { ...item, status: "expired", detail: "页面已重建，请重新生成操作。" }
            : item
        ));
      });
    };
  }, [updateItems]);

  const requestMessages = useCallback((source: TgAssistantItem[]) => source.flatMap((item) => {
    if (item.kind !== "message") return [];
    if (item.role === "assistant" && item.status !== "done") return [];
    return [{ role: item.role, content: item.content }];
  }).slice(-30), []);

  const runRequest = useCallback(async (
    userItem: TgAssistantMessageItem,
    attachContext: boolean,
    appendUser: boolean,
    image?: File
  ) => {
    if (controllerRef.current) return;
    const controller = new AbortController();
    controllerRef.current = controller;
    stopRequestedRef.current = false;
    setIsStreaming(true);
    const releaseController = () => {
      if (controllerRef.current === controller) {
        controllerRef.current = null;
        setIsStreaming(false);
      }
    };
    const assistantItem: TgAssistantMessageItem = {
      kind: "message",
      id: id(),
      role: "assistant",
      content: "",
      createdAt: new Date().toISOString(),
      status: "understanding",
      userItemId: userItem.id,
      processing: {
        currentStage: attachContext ? "capturing_canvas" : "routing_request",
        stages: [attachContext ? "capturing_canvas" : "routing_request"],
        intentSummary: "",
        answerSummary: "",
        intentSummaryDone: false,
        answerSummaryDone: false
      }
    };
    const currentItems = itemsRef.current;
    const userIndex = currentItems.findIndex((item) => item.id === userItem.id);
    const historyItems = appendUser
      ? [...currentItems, userItem]
      : userIndex >= 0
        ? currentItems.slice(0, userIndex + 1)
        : [userItem];
    const displayItems = appendUser ? [...currentItems, userItem] : currentItems;
    updateItems(() => [...displayItems, assistantItem]);

    let pageContext: TgAssistantPageContext | undefined;
    let canvasImage: Blob | undefined;
    if (attachContext) {
      try {
        if (!adapterRef.current) throw new Error("Tg 页面当前不可用。");
        const adapter = adapterRef.current;
        const [contextResult, imageResult] = await Promise.allSettled([
          adapter.captureContext(),
          adapter.captureCanvasImage?.(controller.signal) ?? Promise.resolve(null)
        ]);
        if (contextResult.status === "rejected") throw contextResult.reason;
        pageContext = contextResult.value;
        if (imageResult.status === "fulfilled") {
          canvasImage = imageResult.value ?? undefined;
        } else if (!controller.signal.aborted) {
          updateItems((current) => current.map((item) => item.kind === "message" && item.id === assistantItem.id
            ? {
                ...item,
                processing: item.processing
                  ? { ...item.processing, warning: "画板快照生成失败，本轮已使用 SMILES 兜底。" }
                  : item.processing
              }
            : item));
        }
      } catch (error) {
        if (controller.signal.aborted) {
          if (stopRequestedRef.current) {
            updateItems((current) => current.map((item) => item.kind === "message" && item.id === assistantItem.id
              ? { ...item, status: "stopped" }
              : item));
          }
          releaseController();
          return;
        }
        const message = error instanceof Error ? error.message : "无法读取当前页面状态。";
        updateItems((current) => current.map((item) => item.kind === "message" && item.id === assistantItem.id
          ? { ...item, status: "error", error: { code: "context_error", message, retryable: true } }
          : item));
        releaseController();
        return;
      }
    }
    if (controller.signal.aborted) {
      if (stopRequestedRef.current) {
        updateItems((current) => current.map((item) => item.kind === "message" && item.id === assistantItem.id
          ? {
              ...item,
              status: "stopped",
              processing: item.processing ? { ...item.processing, currentStage: null } : item.processing
            }
          : item));
      }
      releaseController();
      return;
    }

    const pendingSummaries: Record<"intent" | "answer", string> = { intent: "", answer: "" };
    let summaryFlushQueued = false;
    const flushSummaries = () => {
      summaryFlushQueued = false;
      const intent = pendingSummaries.intent;
      const answer = pendingSummaries.answer;
      pendingSummaries.intent = "";
      pendingSummaries.answer = "";
      if (!intent && !answer) return;
      updateItems((current) => current.map((item) => {
        if (item.kind !== "message" || item.id !== assistantItem.id || !item.processing) return item;
        return {
          ...item,
          processing: {
            ...item.processing,
            intentSummary: (item.processing.intentSummary + intent).slice(0, 4000),
            answerSummary: (item.processing.answerSummary + answer).slice(0, 4000)
          }
        };
      }));
    };
    const queueSummary = (phase: "intent" | "answer", content: string) => {
      pendingSummaries[phase] += content;
      if (summaryFlushQueued) return;
      summaryFlushQueued = true;
      queueMicrotask(flushSummaries);
    };

    const handleEvent = (event: TgAssistantSseEvent) => {
      if (event.event === "meta") {
        const trimmed = Array.isArray(event.data.context_trimmed)
          ? event.data.context_trimmed.filter((item): item is string => typeof item === "string")
          : [];
        updateItems((current) => current.map((item) => item.kind === "message" && item.id === assistantItem.id
          ? {
              ...item,
              meta: {
                requestId: typeof event.data.request_id === "string" ? event.data.request_id : undefined,
                contextTrimmed: trimmed,
                contextAttached: event.data.context_attached === true,
                imageAttached: event.data.image_attached === true,
                imageCount: typeof event.data.image_count === "number" ? event.data.image_count : undefined,
                canvasImageAttached: event.data.canvas_image_attached === true,
                userImageAttached: event.data.user_image_attached === true
              }
            }
          : item));
        return;
      }
      if (event.event === "stage" && typeof event.data.code === "string") {
        const stage = event.data.code as TgAssistantProcessStage;
        if (!PROCESS_STAGES.includes(stage)) return;
        updateItems((current) => current.map((item) => {
          if (item.kind !== "message" || item.id !== assistantItem.id || !item.processing) return item;
          return {
            ...item,
            processing: {
              ...item.processing,
              currentStage: stage,
              stages: item.processing.stages.includes(stage)
                ? item.processing.stages
                : [...item.processing.stages, stage]
            }
          };
        }));
        return;
      }
      if (
        event.event === "reasoning_summary_delta" &&
        (event.data.phase === "intent" || event.data.phase === "answer") &&
        typeof event.data.content === "string"
      ) {
        queueSummary(event.data.phase, event.data.content);
        return;
      }
      if (
        event.event === "reasoning_summary_done" &&
        (event.data.phase === "intent" || event.data.phase === "answer")
      ) {
        flushSummaries();
        const phase = event.data.phase;
        updateItems((current) => current.map((item) => {
          if (item.kind !== "message" || item.id !== assistantItem.id || !item.processing) return item;
          return {
            ...item,
            processing: {
              ...item.processing,
              ...(phase === "intent" ? { intentSummaryDone: true } : { answerSummaryDone: true })
            }
          };
        }));
        return;
      }
      if (event.event === "token" && typeof event.data.content === "string") {
        const token = event.data.content;
        updateItems((current) => current.map((item) => item.kind === "message" && item.id === assistantItem.id
          ? { ...item, status: "streaming", content: item.content + token }
          : item));
        return;
      }
      if (event.event === "navigation") {
        const target = event.data.target;
        const basis = event.data.basis_revision;
        if ((target === "parameters" || target === "results") && typeof basis === "string") {
          updateItems((current) => [...current, {
            kind: "navigation",
            id: id(),
            target,
            basisRevision: basis,
            createdAt: new Date().toISOString()
          }]);
        }
        return;
      }
      if (event.event === "action_proposal") {
        const proposalId = event.data.proposal_id;
        const basis = event.data.basis_revision;
        const operations = event.data.operations;
        if (
          event.data.requires_confirmation === true &&
          typeof proposalId === "string" &&
          typeof basis === "string" &&
          validTgAssistantOperations(operations)
        ) {
          const adapter = adapterRef.current;
          const stillCurrent = adapter !== null && adapter.getRevision() === basis;
          updateItems((current) => [...current, {
            kind: "action",
            id: id(),
            proposalId,
            basisRevision: basis,
            operations,
            previousParameters: stillCurrent ? adapter.getDraftParameters() : null,
            previousStructure: stillCurrent ? adapter.getStructureSmiles() : null,
            sourceText: userItem.content,
            createdAt: new Date().toISOString(),
            status: stillCurrent ? "pending" : "expired",
            ...(!stillCurrent ? { detail: "页面已离开或状态已变化，请重新生成操作。" } : {})
          }]);
        }
        return;
      }
      if (event.event === "error") {
        flushSummaries();
        updateItems((current) => current.map((item) => item.kind === "message" && item.id === assistantItem.id
          ? {
              ...item,
              status: "error",
              processing: item.processing ? { ...item.processing, currentStage: null } : item.processing,
              error: {
                code: typeof event.data.code === "string" ? event.data.code : "stream_error",
                message: typeof event.data.message === "string" ? event.data.message : "AI 请求失败。",
                retryable: event.data.retryable === true
              }
            }
          : item));
        return;
      }
      if (event.event === "done") {
        flushSummaries();
        updateItems((current) => current.map((item) => item.kind === "message" && item.id === assistantItem.id
          ? {
              ...item,
              status: "done",
              processing: item.processing ? { ...item.processing, currentStage: null } : item.processing
            }
          : item));
      }
    };

    try {
      await streamTgAssistant(
        { messages: requestMessages(historyItems), ...(pageContext ? { page_context: pageContext } : {}) },
        handleEvent,
        controller.signal,
        {
          ...(canvasImage ? { canvasImage } : {}),
          ...(image ? { userImage: image } : {})
        }
      );
    } catch (error) {
      if (controller.signal.aborted) {
        if (stopRequestedRef.current) {
          updateItems((current) => current.map((item) => item.kind === "message" && item.id === assistantItem.id
            ? {
                ...item,
                status: "stopped",
                processing: item.processing ? { ...item.processing, currentStage: null } : item.processing
              }
            : item));
        }
      } else {
        updateItems((current) => current.map((item) => item.kind === "message" && item.id === assistantItem.id
          ? {
              ...item,
              status: "error",
              processing: item.processing ? { ...item.processing, currentStage: null } : item.processing,
              error: {
                code: "network_error",
                message: error instanceof Error ? error.message : "AI 网络请求失败。",
                retryable: true
              }
            }
          : item));
      }
    } finally {
      flushSummaries();
      releaseController();
    }
  }, [requestMessages, updateItems]);

  const send = useCallback(async (content: string, attachContext: boolean, image?: File) => {
    const normalized = content.trim();
    if (!normalized || normalized.length > 8000 || controllerRef.current) return false;
    const userItem: TgAssistantMessageItem = {
      kind: "message",
      id: id(),
      role: "user",
      content: normalized,
      createdAt: new Date().toISOString(),
      status: "done",
      ...(image ? { image: { name: image.name, size: image.size, type: image.type } } : {})
    };
    if (image) imageFilesRef.current.set(userItem.id, image);
    await runRequest(userItem, attachContext, true, image);
    return true;
  }, [runRequest]);

  const retry = useCallback(async (assistantItemId: string, attachContext: boolean) => {
    const assistant = itemsRef.current.find(
      (item): item is TgAssistantMessageItem => item.kind === "message" && item.id === assistantItemId
    );
    if (!assistant?.userItemId || controllerRef.current) return;
    const user = itemsRef.current.find(
      (item): item is TgAssistantMessageItem => item.kind === "message" && item.id === assistant.userItemId
    );
    if (!user) return;
    const image = imageFilesRef.current.get(user.id);
    if (user.image && !image) {
      updateItems((current) => current.map((item) => item.kind === "message" && item.id === assistantItemId
        ? {
            ...item,
            status: "error",
            error: {
              code: "image_unavailable",
              message: "图片附件已不可用，请重新上传图片后发送。",
              retryable: false
            }
          }
        : item));
      return;
    }
    await runRequest(user, attachContext, false, image);
  }, [runRequest, updateItems]);

  const stop = useCallback(() => {
    stopRequestedRef.current = true;
    controllerRef.current?.abort();
  }, []);

  const newConversation = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setIsStreaming(false);
    itemsRef.current = [];
    imageFilesRef.current.clear();
    setItems([]);
    try {
      sessionStorage.removeItem(SESSION_KEY);
      sessionStorage.removeItem(LEGACY_SESSION_KEY);
    } catch {
      setStorageWarning("本标签页的 AI 对话无法持久化。");
    }
  }, []);

  const rejectAction = useCallback((itemId: string) => {
    updateItems((current) => current.map((item) => item.kind === "action" && item.id === itemId
      ? { ...item, status: "rejected" }
      : item));
  }, [updateItems]);

  const applyAction = useCallback(async (itemId: string) => {
    const item = itemsRef.current.find(
      (candidate): candidate is TgAssistantActionItem => candidate.kind === "action" && candidate.id === itemId
    );
    if (!item || item.status !== "pending") return;
    const adapter = adapterRef.current;
    if (!adapter || adapter.getRevision() !== item.basisRevision) {
      updateItems((current) => current.map((candidate) => candidate.id === itemId
        ? { ...candidate, status: "expired", detail: "页面状态已变化，请重新生成操作。" } as TgAssistantActionItem
        : candidate));
      return;
    }
    updateItems((current) => current.map((candidate) => candidate.id === itemId
      ? { ...candidate, status: "applying" } as TgAssistantActionItem
      : candidate));
    let result: { status: "applied" | "expired" | "failed"; detail?: string };
    try {
      result = await adapter.applyOperations(item.operations, item.basisRevision);
    } catch {
      result = { status: "failed", detail: "页面操作失败，请检查当前状态后重试。" };
    }
    updateItems((current) => current.map((candidate) => candidate.id === itemId
      ? { ...candidate, status: result.status, detail: result.detail } as TgAssistantActionItem
      : candidate));
  }, [updateItems]);

  const runNavigation = useCallback((item: TgAssistantNavigationItem) => {
    const adapter = adapterRef.current;
    if (!adapter || adapter.getRevision() !== item.basisRevision) return false;
    adapter.navigate(item.target);
    return true;
  }, []);

  return {
    items,
    status,
    guide,
    metadataLoading,
    metadataError,
    storageWarning,
    consent,
    isStreaming,
    loadMetadata,
    setConsent,
    addDivider,
    registerAdapter,
    send,
    retry,
    stop,
    newConversation,
    rejectAction,
    applyAction,
    runNavigation
  };
}

export type TgAssistantSession = ReturnType<typeof useTgAssistant>;
