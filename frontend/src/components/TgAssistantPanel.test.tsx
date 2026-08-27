// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { TgAssistantMessageItem, TgAssistantSession } from "../hooks/useTgAssistant";
import { TgAssistantPanel } from "./TgAssistantPanel";

function assistantSession(overrides: Partial<TgAssistantSession> = {}): TgAssistantSession {
  return {
    items: [],
    status: {
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
    },
    guide: {
      module: "reverseDesign",
      version: 3,
      language: "zh-CN",
      defaults: {},
      sections: []
    },
    metadataLoading: false,
    metadataError: null,
    storageWarning: null,
    consent: "denied",
    isStreaming: false,
    loadMetadata: vi.fn().mockResolvedValue(undefined),
    setConsent: vi.fn(),
    addDivider: vi.fn(),
    registerAdapter: vi.fn(() => vi.fn()),
    send: vi.fn().mockResolvedValue(true),
    getImagePreviewUrl: vi.fn(() => null),
    isImagePreviewRestoring: vi.fn(() => false),
    retry: vi.fn(),
    stop: vi.fn(),
    newConversation: vi.fn(),
    rejectAction: vi.fn(),
    applyAction: vi.fn(),
    runNavigation: vi.fn(),
    ...overrides
  } as unknown as TgAssistantSession;
}

function renderPanel(assistant: TgAssistantSession) {
  return render(
    <TgAssistantPanel
      assistant={assistant}
      onClose={vi.fn()}
      contextLabels={["当前结构已连接", "Tg 450 °C"]}
      localDiagnostic="本地阶段诊断"
      contextualSuggestions={["建议一", "建议二", "建议三"]}
    />
  );
}

afterEach(() => cleanup());

describe("TgAssistantPanel", () => {
  it("labels the pre-response phase as thinking", () => {
    renderPanel(assistantSession({
      items: [{
        kind: "message",
        id: "assistant-thinking",
        role: "assistant",
        content: "",
        createdAt: "2026-08-24T00:00:00.000Z",
        status: "understanding"
      }]
    }));

    expect(screen.getByText("正在思考中")).toBeTruthy();
    expect(screen.queryByText("正在理解请求…")).toBeNull();
  });

  it("shows real processing stages and summary text, then folds completed work", () => {
    const item: TgAssistantMessageItem = {
      kind: "message",
      id: "assistant-process",
      role: "assistant",
      content: "最终回答",
      createdAt: "2026-08-24T00:00:00.000Z",
      status: "done",
      processing: {
        currentStage: null,
        stages: ["capturing_canvas", "routing_request", "analyzing_images", "writing_answer"],
        intentSummary: "用户要求比较当前画板。",
        answerSummary: "结合两张图片组织差异说明。",
        intentSummaryDone: true,
        answerSummaryDone: true
      }
    };
    const view = renderPanel(assistantSession({ items: [item] }));
    const details = view.container.querySelector(".tg-assistant-process") as HTMLDetailsElement;

    expect(details.open).toBe(false);
    fireEvent.click(details.querySelector("summary")!);
    expect(screen.getByText("思考过程（摘要）")).toBeTruthy();
    expect(screen.getByText("用户要求比较当前画板。")).toBeTruthy();
    expect(screen.getByText("结合两张图片组织差异说明。")).toBeTruthy();
    expect(view.container.textContent).not.toContain("原始思维链");
  });

  it("renders only the Markdown whitelist and never creates links or raw HTML", () => {
    const content = [
      "# 页面状态",
      "**粗体** 与 `代码`",
      "[外部链接](https://example.test/private)",
      "<a href=\"https://example.test\">raw html</a>",
      "```",
      "block code",
      "```"
    ].join("\n");
    const assistant = assistantSession({
      items: [{
        kind: "message",
        id: "assistant-1",
        role: "assistant",
        content,
        createdAt: "2026-08-24T00:00:00.000Z",
        status: "done"
      }]
    });
    const view = renderPanel(assistant);

    expect(view.container.querySelector("a")).toBeNull();
    expect(view.container.querySelector("pre code")?.textContent).toBe("block code");
    expect(screen.getByText("粗体").tagName).toBe("STRONG");
    expect(view.container.textContent).toContain("[外部链接](https://example.test/private)");
    expect(view.container.textContent).toContain("<a href=\"https://example.test\">raw html</a>");
  });

  it("supports Enter send, Shift+Enter newline, and Chinese IME composition", async () => {
    const assistant = assistantSession();
    renderPanel(assistant);
    const input = screen.getByRole("textbox", { name: "发送给 AI 助手的消息" });
    fireEvent.change(input, { target: { value: "解释当前结果" } });

    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(assistant.send).not.toHaveBeenCalled();
    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(assistant.send).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(assistant.send).toHaveBeenCalledWith("解释当前结果", false));
    expect((input as HTMLTextAreaElement).value).toBe("");
  });

  it("asks once before page-context authorization and keeps generic chat available after denial", async () => {
    const assistant = assistantSession({ consent: "unknown" });
    const view = renderPanel(assistant);
    const toggle = screen.getByRole("checkbox", { name: "附带当前页面" });

    fireEvent.click(toggle);
    expect(screen.getByRole("dialog", { name: "是否附带当前页面？" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "仅发送问题" }));
    expect(assistant.setConsent).toHaveBeenCalledWith("denied");
    expect(assistant.addDivider).toHaveBeenCalledWith("本次未附带页面上下文");

    const input = screen.getByRole("textbox", { name: "发送给 AI 助手的消息" });
    fireEvent.change(input, { target: { value: "只问通用问题" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(assistant.send).toHaveBeenCalledWith("只问通用问题", false));
    expect(view.container.textContent).not.toContain("完整对话历史");
    expect(view.container.textContent).not.toContain("外部 AI 服务处理");
  });

  it("shows user-facing errors without internal codes or context-trimming diagnostics", () => {
    const assistant = assistantSession({
      items: [{
        kind: "message",
        id: "assistant-error",
        role: "assistant",
        content: "",
        createdAt: "2026-08-24T00:00:00.000Z",
        status: "error",
        error: { code: "provider_timeout", message: "请求超时，请重试。", retryable: true },
        meta: {
          contextAttached: true,
          contextTrimmed: ["candidate_iupac", "monomer_smiles"]
        }
      }]
    });
    const view = renderPanel(assistant);

    expect(view.container.textContent).toContain("请求超时，请重试。");
    expect(view.container.textContent).not.toContain("provider_timeout");
    expect(view.container.textContent).not.toContain("上下文已精简");
    expect(view.container.textContent).not.toContain("candidate_iupac");
  });

  it("keeps suggestions visible and switches them with the per-turn page-context toggle", () => {
    const assistant = assistantSession({
      consent: "granted",
      items: [{
        kind: "divider",
        id: "context-divider",
        text: "页面上下文已开启",
        createdAt: "2026-08-24T00:00:00.000Z"
      }]
    });
    renderPanel(assistant);

    expect(screen.getByRole("button", { name: "建议一" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "怎样从目标 Tg 反推聚合物骨架设计？" })).toBeNull();

    const contextToggle = screen.getByRole("checkbox", { name: "附带当前页面" });
    fireEvent.click(contextToggle);

    expect(screen.getByRole("button", { name: "怎样从目标 Tg 反推聚合物骨架设计？" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "建议一" })).toBeNull();
  });

  it("labels the header reset control as irreversible conversation clearing", () => {
    const assistant = assistantSession();
    renderPanel(assistant);

    const clear = screen.getByRole("button", { name: "清空当前对话（不可恢复）" });
    expect(clear.getAttribute("title")).toBe("清空当前对话（不可恢复）");
    expect(screen.queryByRole("button", { name: "新建对话" })).toBeNull();

    fireEvent.click(clear);
    expect(assistant.newConversation).toHaveBeenCalledOnce();
  });

  it("uses the lower-left plus control for one image and supports image-only sending", async () => {
    const createObjectURL = vi.fn(() => "blob:preview");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });
    const assistant = assistantSession();
    renderPanel(assistant);
    const file = new File(["image"], "structure.png", { type: "image/png" });

    const upload = screen.getByRole("button", { name: "上传图片给 AI 分析" });
    const picker = screen.getByLabelText("选择供 AI 分析的图片");
    expect(upload.closest(".tg-assistant-input-actions")).toBeTruthy();
    fireEvent.change(picker, { target: { files: [file] } });

    expect(screen.getByAltText("待发送图片预览")).toBeTruthy();
    expect(screen.getByText("structure.png")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(assistant.send).toHaveBeenCalledWith("请分析这张图片", false, file));
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith("blob:preview"));
  });

  it("renders an uploaded image inside its user message instead of only showing the file name", () => {
    const getImagePreviewUrl = vi.fn(() => "blob:conversation-preview");
    renderPanel(assistantSession({
      getImagePreviewUrl,
      items: [{
        kind: "message",
        id: "image-user",
        role: "user",
        content: "替换到画板中",
        image: { name: "structure.png", size: 128, type: "image/png" },
        createdAt: "2026-08-24T00:00:00.000Z",
        status: "done"
      }]
    }));

    const preview = screen.getByRole("img", { name: "上传的图片：structure.png" });
    expect(preview.getAttribute("src")).toBe("blob:conversation-preview");
    expect(screen.getByText("structure.png").tagName).toBe("FIGCAPTION");
    expect(getImagePreviewUrl).toHaveBeenCalledWith("image-user");
  });

  it("falls back cleanly when a live conversation thumbnail cannot be decoded", () => {
    renderPanel(assistantSession({
      getImagePreviewUrl: vi.fn(() => "blob:broken-preview"),
      items: [{
        kind: "message",
        id: "broken-image-user",
        role: "user",
        content: "分析图片",
        image: { name: "broken.png", size: 128, type: "image/png" },
        createdAt: "2026-08-24T00:00:00.000Z",
        status: "done"
      }]
    }));

    fireEvent.error(screen.getByRole("img", { name: "上传的图片：broken.png" }));
    expect(screen.getByRole("img", { name: "图片预览已失效" })).toBeTruthy();
  });

  it("shows a restoring state while a persisted thumbnail is loading", () => {
    renderPanel(assistantSession({
      isImagePreviewRestoring: vi.fn(() => true),
      items: [{
        kind: "message",
        id: "restoring-image-user",
        role: "user",
        content: "分析图片",
        image: { name: "restoring.png", size: 128, type: "image/png" },
        createdAt: "2026-08-24T00:00:00.000Z",
        status: "done"
      }]
    }));

    expect(screen.getByRole("img", { name: "正在恢复图片预览" })).toBeTruthy();
    expect(screen.queryByRole("img", { name: "图片预览已失效" })).toBeNull();
  });

  it("shows an explicit placeholder for persisted image metadata after the binary is gone", () => {
    renderPanel(assistantSession({
      items: [{
        kind: "message",
        id: "persisted-image-user",
        role: "user",
        content: "分析图片",
        image: { name: "history.webp", size: 256, type: "image/webp" },
        createdAt: "2026-08-24T00:00:00.000Z",
        status: "done"
      }]
    }));

    expect(screen.getByRole("img", { name: "图片预览已失效" })).toBeTruthy();
    expect(screen.getByText("history.webp")).toBeTruthy();
  });

  it("replaces and removes the pending image while releasing both preview URLs", async () => {
    const createObjectURL = vi.fn()
      .mockReturnValueOnce("blob:first")
      .mockReturnValueOnce("blob:second");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });
    renderPanel(assistantSession());
    const picker = screen.getByLabelText("选择供 AI 分析的图片");

    fireEvent.change(picker, {
      target: { files: [new File(["first"], "first.png", { type: "image/png" })] }
    });
    fireEvent.change(picker, {
      target: { files: [new File(["second"], "second.webp", { type: "image/webp" })] }
    });

    expect(screen.queryByText("first.png")).toBeNull();
    expect(screen.getByText("second.webp")).toBeTruthy();
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith("blob:first"));
    fireEvent.click(screen.getByRole("button", { name: "删除待发送图片" }));
    expect(screen.queryByAltText("待发送图片预览")).toBeNull();
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith("blob:second"));
  });

  it("shows Chinese validation errors for unsupported and oversized images", () => {
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:unused") });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    const assistant = assistantSession({
      status: {
        enabled: true,
        configured: true,
        image: {
          supported: true,
          max_files: 2,
          max_canvas_snapshots: 1,
          max_user_upload_files: 1,
          max_bytes: 4,
          max_total_bytes: 8,
          accepted_mime_types: ["image/png", "image/jpeg", "image/webp"]
        }
      }
    });
    renderPanel(assistant);
    const picker = screen.getByLabelText("选择供 AI 分析的图片");

    fireEvent.change(picker, {
      target: { files: [new File(["text"], "notes.txt", { type: "text/plain" })] }
    });
    expect(screen.getByRole("alert").textContent).toContain("仅支持 PNG、JPEG 或 WebP");
    fireEvent.change(picker, {
      target: { files: [new File(["12345"], "large.png", { type: "image/png" })] }
    });
    expect(screen.getByRole("alert").textContent).toContain("图片不能超过");
    expect(screen.queryByAltText("待发送图片预览")).toBeNull();
  });

  it("keeps a selected image while waiting for page-context consent", async () => {
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:consent") });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    const assistant = assistantSession({ consent: "unknown" });
    const view = renderPanel(assistant);
    const file = new File(["image"], "molecule.webp", { type: "image/webp" });
    fireEvent.change(screen.getByLabelText("选择供 AI 分析的图片"), { target: { files: [file] } });
    const contextToggle = screen.getByRole("checkbox", { name: "附带当前页面" });
    fireEvent.click(contextToggle);

    expect(screen.getByText("molecule.webp")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "同意并附带" }));
    view.rerender(
      <TgAssistantPanel
        assistant={{ ...assistant, consent: "granted" }}
        onClose={vi.fn()}
        contextLabels={["当前结构已连接", "Tg 450 °C"]}
        localDiagnostic="本地阶段诊断"
        contextualSuggestions={["建议一", "建议二", "建议三"]}
      />
    );
    await waitFor(() => expect((contextToggle as HTMLInputElement).checked).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(assistant.send).toHaveBeenCalledWith("请分析这张图片", true, file));
  });

  it("shows old-to-new values and never treats an action card as an Enter shortcut", () => {
    const assistant = assistantSession({
      items: [{
        kind: "action",
        id: "action-1",
        proposalId: "proposal-1",
        basisRevision: "revision-1",
        operations: [
          { type: "set_parameters", parameters: { similarity_threshold: 0.65 } },
          { type: "run_search" }
        ],
        previousParameters: {
          target_tg: 450,
          similarity_threshold: 0.7,
          candidate_size: 200
        },
        sourceText: "阈值改成 0.65 并重新搜索",
        createdAt: "2026-08-24T00:00:00.000Z",
        status: "pending"
      }]
    });
    renderPanel(assistant);

    expect(screen.getByText("0.7 → 0.65")).toBeTruthy();
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
    expect(assistant.applyAction).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "应用并搜索" }));
    expect(assistant.applyAction).toHaveBeenCalledWith("action-1");
  });

  it("keeps local stage diagnostics visible when the model is unavailable", () => {
    const assistant = assistantSession({
      status: {
        enabled: false,
        configured: false,
        image: {
          supported: true,
          max_files: 2,
          max_canvas_snapshots: 1,
          max_user_upload_files: 1,
          max_bytes: 5 * 1024 * 1024,
          max_total_bytes: 10 * 1024 * 1024,
          accepted_mime_types: ["image/png", "image/jpeg", "image/webp"]
        }
      },
      items: [{
        kind: "message",
        id: "history-1",
        role: "assistant",
        content: "历史答复",
        createdAt: "2026-08-24T00:00:00.000Z",
        status: "done"
      }]
    });

    renderPanel(assistant);

    expect(screen.getByText("本地阶段诊断")).toBeTruthy();
    expect(screen.getByText("AI 助手暂不可用，请稍后重试。")).toBeTruthy();
  });
});
