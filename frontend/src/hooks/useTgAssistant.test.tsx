// @vitest-environment jsdom

import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TgAssistantPageContext } from "../types";
import {
  useTgAssistant,
  validTgAssistantOperations,
  type TgAssistantPageAdapter
} from "./useTgAssistant";

const mocks = vi.hoisted(() => ({
  stream: vi.fn(),
  status: vi.fn(),
  guide: vi.fn(),
  previewSave: vi.fn(),
  previewLoad: vi.fn(),
  previewDelete: vi.fn(),
  previewClear: vi.fn(),
  previewPrune: vi.fn()
}));

vi.mock("../services/api", () => ({
  fetchTgAssistantStatus: mocks.status,
  fetchTgAssistantGuide: mocks.guide
}));

vi.mock("../services/tgAssistantStream", () => ({
  streamTgAssistant: mocks.stream
}));

vi.mock("../services/tgAssistantImagePreviews", () => ({
  saveTgAssistantImagePreview: mocks.previewSave,
  loadTgAssistantImagePreview: mocks.previewLoad,
  deleteTgAssistantImagePreview: mocks.previewDelete,
  clearTgAssistantImagePreviews: mocks.previewClear,
  pruneExpiredTgAssistantImagePreviews: mocks.previewPrune
}));

const SESSION_KEY = "nexpoly.assistant.tg.session.v2";
const LEGACY_SESSION_KEY = "nexpoly.assistant.tg.session.v1";
const IMAGE_PREVIEW_SESSION_KEY = "nexpoly.assistant.tg.image-preview-session.v1";

function pageContext(revision = "revision-1"): TgAssistantPageContext {
  return {
    type: "tg_reverse_design",
    version: 1,
    captured_at: "2026-08-24T09:30:00+08:00",
    action_context_revision: revision,
    structure: {
      smiles: "*CC*",
      canvas_dirty: false,
      editor_ready: true,
      view_mode: "2d",
      busy: false
    },
    draft_parameters: {
      target_tg: 450,
      similarity_threshold: 0.7,
      candidate_size: 200
    },
    submitted_request: null,
    parameters_dirty: false,
    validation_error: null,
    job: null,
    result_view: null,
    error: null
  };
}

function adapter(overrides: Partial<TgAssistantPageAdapter> = {}): TgAssistantPageAdapter {
  return {
    captureContext: vi.fn().mockResolvedValue(pageContext()),
    getRevision: () => "revision-1",
    getDraftParameters: () => ({
      target_tg: 450,
      similarity_threshold: 0.7,
      candidate_size: 200
    }),
    getStructureSmiles: () => "*CC*",
    navigate: vi.fn(),
    applyOperations: vi.fn().mockResolvedValue({ status: "applied" }),
    ...overrides
  };
}

beforeEach(() => {
  window.sessionStorage.clear();
  window.localStorage.clear();
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:conversation-preview")
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn()
  });
  mocks.previewSave.mockReset().mockResolvedValue(undefined);
  mocks.previewLoad.mockReset().mockResolvedValue(null);
  mocks.previewDelete.mockReset().mockResolvedValue(undefined);
  mocks.previewClear.mockReset().mockResolvedValue(undefined);
  mocks.previewPrune.mockReset().mockResolvedValue(undefined);
  mocks.stream.mockReset();
  mocks.status.mockReset().mockResolvedValue({
    enabled: true,
    configured: true,
    image: {
      supported: true,
      max_files: 2,
      max_canvas_snapshots: 1,
      max_user_upload_files: 1,
      max_bytes: 5 * 1024 * 1024,
      max_total_bytes: 10 * 1024 * 1024,
      accepted_mime_types: ["image/png", "image/jpeg", "image/webp"]
    }
  });
  mocks.guide.mockReset().mockResolvedValue({
    module: "reverseDesign",
    version: 3,
    language: "zh-CN",
    defaults: {},
    sections: []
  });
  mocks.stream.mockImplementation(async (_payload, onEvent) => {
    onEvent({ event: "meta", data: { request_id: "r1", context_trimmed: [], context_attached: true } });
    onEvent({ event: "token", data: { content: "答复" } });
    onEvent({ event: "done", data: { message: "答复" } });
  });
});

describe("Tg assistant operation validation", () => {
  it("accepts only the fixed operation shapes and strict numeric values", () => {
    expect(validTgAssistantOperations([
      { type: "set_parameters", parameters: { similarity_threshold: 0.65, candidate_size: 50 } },
      { type: "run_search" }
    ])).toBe(true);
    expect(validTgAssistantOperations([
      { type: "run_search" },
      { type: "set_parameters", parameters: { candidate_size: 50 } }
    ])).toBe(false);
    expect(validTgAssistantOperations([
      { type: "set_parameters", parameters: { candidate_size: 50.5 } }
    ])).toBe(false);
    expect(validTgAssistantOperations([
      { type: "set_parameters", parameters: { similarity_threshold: "0.7" } }
    ])).toBe(false);
    expect(validTgAssistantOperations([
      { type: "run_search", url: "/internal" }
    ])).toBe(false);
    expect(validTgAssistantOperations([
      { type: "set_structure", smiles: "*CCO*" }
    ])).toBe(true);
    expect(validTgAssistantOperations([
      { type: "set_structure", smiles: "CC", url: "/internal" }
    ])).toBe(false);
    expect(validTgAssistantOperations([
      { type: "set_structure", smiles: "CC" },
      { type: "run_search" }
    ])).toBe(false);
  });
});

describe("useTgAssistant session and request lifecycle", () => {
  it("migrates the version-1 storage key without adding binary data", () => {
    window.sessionStorage.setItem(LEGACY_SESSION_KEY, JSON.stringify({
      version: 1,
      items: [{
        kind: "message", id: "legacy", role: "user", content: "旧消息",
        createdAt: "2026-08-24T00:00:00.000Z", status: "done"
      }]
    }));

    const { result } = renderHook(() => useTgAssistant());

    expect(result.current.items).toEqual([expect.objectContaining({ id: "legacy" })]);
    expect(window.sessionStorage.getItem(SESSION_KEY)).toContain('"version":2');
    expect(window.sessionStorage.getItem(LEGACY_SESSION_KEY)).toBeNull();
  });

  it("restores validated messages, drops pending actions, and limits history to 30 messages", () => {
    const messages = Array.from({ length: 31 }, (_, index) => ({
      kind: "message",
      id: `message-${index}`,
      role: index % 2 ? "assistant" : "user",
      content: `message ${index}`,
      createdAt: "2026-08-24T00:00:00.000Z",
      status: "done"
    }));
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({
      version: 1,
      items: [
        ...messages,
        {
          kind: "action",
          id: "pending-action",
          proposalId: "proposal-1",
          basisRevision: "revision-1",
          operations: [{ type: "run_search" }],
          previousParameters: {
            target_tg: 450,
            similarity_threshold: 0.7,
            candidate_size: 200
          },
          sourceText: "重新搜索",
          createdAt: "2026-08-24T00:00:00.000Z",
          status: "pending"
        }
      ]
    }));

    const { result } = renderHook(() => useTgAssistant());

    expect(result.current.items).toHaveLength(30);
    expect(result.current.items.some((item) => item.kind === "action")).toBe(false);
    expect(result.current.items[0]).toMatchObject({ id: "message-1" });
  });

  it("discards damaged storage and reports in-memory fallback", () => {
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({
      version: 1,
      items: [{ kind: "navigation", id: "unsafe", createdAt: "now", target: "results" }]
    }));

    const { result } = renderHook(() => useTgAssistant());

    expect(result.current.items).toEqual([]);
    expect(result.current.storageWarning).toContain("无法持久化");
    expect(window.sessionStorage.getItem(SESSION_KEY) ?? "").not.toContain("unsafe");
  });

  it("keeps complete text history when a later turn omits fresh page context", async () => {
    const payloads: Array<Record<string, unknown>> = [];
    mocks.stream.mockImplementation(async (payload, onEvent) => {
      payloads.push(payload);
      onEvent({ event: "token", data: { content: "答复" } });
      onEvent({ event: "done", data: { message: "答复" } });
    });
    const { result } = renderHook(() => useTgAssistant());
    act(() => result.current.registerAdapter(adapter()));

    await act(async () => {
      await result.current.send("第一问", true);
      await result.current.send("第二问", false);
    });

    expect(payloads[0].page_context).toMatchObject({ type: "tg_reverse_design" });
    expect(payloads[1]).not.toHaveProperty("page_context");
    expect(payloads[1].messages).toEqual([
      { role: "user", content: "第一问" },
      { role: "assistant", content: "答复" },
      { role: "user", content: "第二问" }
    ]);
  });

  it("routes one image through multipart without putting binary bytes in sessionStorage", async () => {
    const file = new File(["private-binary-content"], "structure.png", { type: "image/png" });
    window.sessionStorage.setItem(IMAGE_PREVIEW_SESSION_KEY, "preview-session-1");
    const { result } = renderHook(() => useTgAssistant());

    await act(async () => {
      await result.current.send("分析这张图", false, file);
    });

    expect(mocks.stream.mock.calls[0][3]).toEqual({ userImage: file });
    const userItem = result.current.items.find(
      (item) => item.kind === "message" && item.role === "user"
    );
    expect(userItem).toBeTruthy();
    expect(result.current.getImagePreviewUrl(userItem!.id)).toBe("blob:conversation-preview");
    expect(mocks.previewSave).toHaveBeenCalledWith("preview-session-1", userItem!.id, file);
    const stored = window.sessionStorage.getItem(SESSION_KEY) || "";
    expect(stored).toContain("structure.png");
    expect(stored).not.toContain("private-binary-content");
    expect(stored).not.toContain("blob:conversation-preview");

    act(() => result.current.newConversation());
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:conversation-preview");
    await waitFor(() => expect(mocks.previewClear).toHaveBeenCalledWith("preview-session-1"));
  });

  it("restores a persisted thumbnail after refresh without restoring the original file", async () => {
    const thumbnail = new Blob(["persisted-thumbnail"], { type: "image/webp" });
    window.sessionStorage.setItem(IMAGE_PREVIEW_SESSION_KEY, "preview-session-restored");
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({
      version: 2,
      items: [{
        kind: "message",
        id: "restored-image-user",
        role: "user",
        content: "分析图片",
        image: { name: "restored.png", size: 1024, type: "image/png" },
        createdAt: "2026-08-24T00:00:00.000Z",
        status: "done"
      }]
    }));
    mocks.previewLoad.mockResolvedValue(thumbnail);

    const { result } = renderHook(() => useTgAssistant());

    expect(result.current.isImagePreviewRestoring("restored-image-user")).toBe(true);
    await waitFor(() => {
      expect(result.current.getImagePreviewUrl("restored-image-user")).toBe("blob:conversation-preview");
    });
    expect(result.current.isImagePreviewRestoring("restored-image-user")).toBe(false);
    expect(mocks.previewLoad).toHaveBeenCalledWith("preview-session-restored", "restored-image-user");
    expect(mocks.stream).not.toHaveBeenCalled();
  });

  it("deletes a stored thumbnail when its message falls out of retained history", async () => {
    window.sessionStorage.setItem(IMAGE_PREVIEW_SESSION_KEY, "preview-session-trimmed");
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({
      version: 2,
      items: [
        {
          kind: "message",
          id: "old-image-user",
          role: "user",
          content: "旧图片",
          image: { name: "old.png", size: 100, type: "image/png" },
          createdAt: "2026-08-24T00:00:00.000Z",
          status: "done"
        },
        ...Array.from({ length: 29 }, (_, index) => ({
          kind: "message",
          id: `history-${index}`,
          role: index % 2 === 0 ? "assistant" : "user",
          content: `历史 ${index}`,
          createdAt: "2026-08-24T00:00:00.000Z",
          status: "done"
        }))
      ]
    }));
    const { result } = renderHook(() => useTgAssistant());

    await act(async () => {
      await result.current.send("新问题", false);
    });

    await waitFor(() => expect(mocks.previewDelete).toHaveBeenCalledWith(
      "preview-session-trimmed",
      "old-image-user"
    ));
    expect(result.current.items.some((item) => item.id === "old-image-user")).toBe(false);
  });

  it("captures a canvas image with context, sends two sources, and stores only stage summaries", async () => {
    const canvasImage = new Blob(["private-canvas-binary"], { type: "image/png" });
    const userImage = new File(["private-user-binary"], "reference.webp", { type: "image/webp" });
    mocks.stream.mockImplementation(async (_payload, onEvent) => {
      onEvent({
        event: "meta",
        data: {
          request_id: "r-images",
          context_trimmed: [],
          context_attached: true,
          image_attached: true,
          image_count: 2,
          canvas_image_attached: true,
          user_image_attached: true
        }
      });
      onEvent({ event: "stage", data: { code: "routing_request" } });
      onEvent({ event: "reasoning_summary_delta", data: { phase: "intent", content: "正在判断请求" } });
      onEvent({ event: "reasoning_summary_done", data: { phase: "intent" } });
      onEvent({ event: "stage", data: { code: "analyzing_images" } });
      onEvent({ event: "reasoning_summary_delta", data: { phase: "answer", content: "正在比较两张图" } });
      onEvent({ event: "reasoning_summary_done", data: { phase: "answer" } });
      onEvent({ event: "token", data: { content: "完成" } });
      onEvent({ event: "done", data: { message: "完成" } });
    });
    const captureCanvasImage = vi.fn().mockResolvedValue(canvasImage);
    const { result } = renderHook(() => useTgAssistant());
    act(() => result.current.registerAdapter(adapter({ captureCanvasImage })));

    await act(async () => {
      await result.current.send("比较结构", true, userImage);
    });

    expect(captureCanvasImage).toHaveBeenCalledOnce();
    expect(mocks.stream.mock.calls[0][3]).toEqual({ canvasImage, userImage });
    const answer = result.current.items.find(
      (item) => item.kind === "message" && item.role === "assistant"
    );
    expect(answer).toMatchObject({
      status: "done",
      meta: {
        imageCount: 2,
        canvasImageAttached: true,
        userImageAttached: true
      },
      processing: {
        currentStage: null,
        intentSummary: "正在判断请求",
        answerSummary: "正在比较两张图",
        intentSummaryDone: true,
        answerSummaryDone: true
      }
    });
    const stored = window.sessionStorage.getItem(SESSION_KEY) ?? "";
    expect(stored).toContain("正在比较两张图");
    expect(stored).not.toContain("private-canvas-binary");
    expect(stored).not.toContain("private-user-binary");
    expect(stored).not.toContain("base64");
  });

  it("continues with SMILES context when canvas rendering fails", async () => {
    const { result } = renderHook(() => useTgAssistant());
    act(() => result.current.registerAdapter(adapter({
      captureCanvasImage: vi.fn().mockRejectedValue(new Error("render failed"))
    })));

    await act(async () => {
      await result.current.send("分析当前画板", true);
    });

    expect(mocks.stream.mock.calls[0][3]).toEqual({});
    expect(result.current.items.find(
      (item) => item.kind === "message" && item.role === "assistant"
    )).toMatchObject({ processing: { warning: expect.stringContaining("SMILES 兜底") } });
  });

  it("requires re-upload when retrying a persisted image turn after refresh", async () => {
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({
      version: 1,
      items: [
        {
          kind: "message", id: "image-user", role: "user", content: "分析图片",
          image: { name: "structure.png", size: 100, type: "image/png" },
          createdAt: "2026-08-24T00:00:00.000Z", status: "done"
        },
        {
          kind: "message", id: "image-answer", role: "assistant", content: "",
          createdAt: "2026-08-24T00:00:01.000Z", status: "error", userItemId: "image-user",
          error: { code: "provider_error", message: "失败", retryable: true }
        }
      ]
    }));
    const { result } = renderHook(() => useTgAssistant());

    await act(async () => {
      await result.current.retry("image-answer", false);
    });

    expect(mocks.stream).not.toHaveBeenCalled();
    expect(result.current.items.find((item) => item.id === "image-answer")).toMatchObject({
      error: { code: "image_unavailable", retryable: false }
    });
  });

  it("locks the tab stream while the page snapshot is still being captured", async () => {
    let resolveCapture: ((context: TgAssistantPageContext) => void) | undefined;
    const capture = vi.fn(() => new Promise<TgAssistantPageContext>((resolve) => {
      resolveCapture = resolve;
    }));
    const { result } = renderHook(() => useTgAssistant());
    act(() => result.current.registerAdapter(adapter({ captureContext: capture })));

    let firstRequest: Promise<boolean> | undefined;
    act(() => {
      firstRequest = result.current.send("第一问", true);
    });
    await waitFor(() => expect(result.current.isStreaming).toBe(true));

    let secondAccepted = true;
    await act(async () => {
      secondAccepted = await result.current.send("第二问", false);
    });
    expect(secondAccepted).toBe(false);
    expect(mocks.stream).not.toHaveBeenCalled();

    resolveCapture?.(pageContext());
    await act(async () => {
      await firstRequest;
    });
    expect(mocks.stream).toHaveBeenCalledOnce();
  });

  it("creates a one-shot action card, records old values, and prevents double apply", async () => {
    let resolveApply: ((value: { status: "applied" }) => void) | undefined;
    const applyOperations = vi.fn(() => new Promise<{ status: "applied" }>((resolve) => {
      resolveApply = resolve;
    }));
    const pageAdapter = adapter({ applyOperations });
    mocks.stream.mockImplementation(async (_payload, onEvent) => {
      onEvent({
        event: "action_proposal",
        data: {
          proposal_id: "proposal-1",
          basis_revision: "revision-1",
          requires_confirmation: true,
          operations: [{ type: "set_parameters", parameters: { candidate_size: 50 } }]
        }
      });
      onEvent({ event: "done", data: { message: "" } });
    });
    const { result } = renderHook(() => useTgAssistant());
    act(() => result.current.registerAdapter(pageAdapter));
    await act(async () => {
      await result.current.send("候选数量改成 50", true);
    });
    const actionItem = result.current.items.find((item) => item.kind === "action");
    expect(actionItem).toMatchObject({
      status: "pending",
      previousParameters: { candidate_size: 200 }
    });

    let firstApply: Promise<void> | undefined;
    act(() => {
      firstApply = result.current.applyAction(actionItem!.id);
    });
    await waitFor(() => expect(
      result.current.items.find((item) => item.id === actionItem!.id)
    ).toMatchObject({ status: "applying" }));
    await act(async () => {
      await result.current.applyAction(actionItem!.id);
    });
    expect(applyOperations).toHaveBeenCalledOnce();

    resolveApply?.({ status: "applied" });
    await act(async () => {
      await firstApply;
    });
    expect(result.current.items.find((item) => item.id === actionItem!.id)).toMatchObject({ status: "applied" });
  });

  it("expires a proposal when the page revision changed", async () => {
    let revision = "revision-1";
    const applyOperations = vi.fn();
    mocks.stream.mockImplementation(async (_payload, onEvent) => {
      onEvent({
        event: "action_proposal",
        data: {
          proposal_id: "proposal-1",
          basis_revision: "revision-1",
          requires_confirmation: true,
          operations: [{ type: "run_search" }]
        }
      });
      onEvent({ event: "done", data: { message: "" } });
    });
    const { result } = renderHook(() => useTgAssistant());
    act(() => result.current.registerAdapter(adapter({
      getRevision: () => revision,
      applyOperations
    })));
    await act(async () => {
      await result.current.send("重新搜索", true);
    });
    const actionItem = result.current.items.find((item) => item.kind === "action");
    revision = "revision-2";

    await act(async () => {
      await result.current.applyAction(actionItem!.id);
    });

    expect(applyOperations).not.toHaveBeenCalled();
    expect(result.current.items.find((item) => item.id === actionItem!.id)).toMatchObject({ status: "expired" });
  });

  it("expires an action that arrives after the Tg page adapter is gone", async () => {
    let release: (() => void) | undefined;
    mocks.stream.mockImplementation(async (_payload, onEvent) => {
      await new Promise<void>((resolve) => { release = resolve; });
      onEvent({
        event: "action_proposal",
        data: {
          proposal_id: "proposal-late",
          basis_revision: "revision-1",
          requires_confirmation: true,
          operations: [{ type: "run_search" }]
        }
      });
      onEvent({ event: "done", data: { message: "" } });
    });
    const { result } = renderHook(() => useTgAssistant());
    const unregister = result.current.registerAdapter(adapter());

    let request: Promise<boolean> | undefined;
    act(() => { request = result.current.send("重新搜索", true); });
    await waitFor(() => expect(result.current.isStreaming).toBe(true));
    act(() => unregister());
    await act(async () => {
      release?.();
      await request;
    });

    expect(result.current.items.find((item) => item.kind === "action")).toMatchObject({
      status: "expired",
      previousParameters: null
    });
  });

  it("retries an older failure with history ending at its original user turn", async () => {
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({
      version: 1,
      items: [
        {
          kind: "message", id: "u1", role: "user", content: "第一问",
          createdAt: "2026-08-24T00:00:00.000Z", status: "done"
        },
        {
          kind: "message", id: "a1", role: "assistant", content: "部分片段",
          createdAt: "2026-08-24T00:00:01.000Z", status: "error", userItemId: "u1",
          error: { code: "provider_error", message: "失败", retryable: true }
        },
        {
          kind: "message", id: "u2", role: "user", content: "第二问",
          createdAt: "2026-08-24T00:00:02.000Z", status: "done"
        },
        {
          kind: "message", id: "a2", role: "assistant", content: "第二答",
          createdAt: "2026-08-24T00:00:03.000Z", status: "done", userItemId: "u2"
        }
      ]
    }));
    const payloads: Array<Record<string, unknown>> = [];
    mocks.stream.mockImplementation(async (payload, onEvent) => {
      payloads.push(payload);
      onEvent({ event: "token", data: { content: "重试答复" } });
      onEvent({ event: "done", data: { message: "重试答复" } });
    });
    const { result } = renderHook(() => useTgAssistant());

    await act(async () => {
      await result.current.retry("a1", false);
    });

    expect(payloads[0].messages).toEqual([{ role: "user", content: "第一问" }]);
    expect(result.current.items).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "u2", content: "第二问" }),
      expect.objectContaining({ id: "a2", content: "第二答" })
    ]));
  });

  it("aborts and clears the active stream when starting a new conversation", async () => {
    mocks.stream.mockImplementation((_payload, _onEvent, signal: AbortSignal) => new Promise<void>((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
    }));
    const { result } = renderHook(() => useTgAssistant());

    act(() => {
      void result.current.send("持续生成", false);
    });
    await waitFor(() => expect(result.current.isStreaming).toBe(true));
    act(() => result.current.newConversation());

    expect(result.current.items).toEqual([]);
    expect(result.current.isStreaming).toBe(false);
  });
});
