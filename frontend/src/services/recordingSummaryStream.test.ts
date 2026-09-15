import { afterEach, expect, it, vi } from "vitest";
import { streamRecordingSummary } from "./recordingSummaryStream";

const summary = { recording_id: "one", summary: "聚合物总结", generated: true };
const frame = (event: string, data: object) => `event: ${event}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`;
function response(source: string) {
  const bytes = new TextEncoder().encode(source);
  return new Response(new ReadableStream({ start(controller) {
    // Split every boundary, including CRLF and multibyte Chinese characters.
    for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
    controller.close();
  } }), { headers: { "Content-Type": "text/event-stream" } });
}
afterEach(() => vi.unstubAllGlobals());

it("逐段解码 UTF-8 / CRLF，跳过心跳，并等待 done 才完成", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(": heartbeat\r\n\r\n" +
    frame("delta", { text: "聚合" }) + frame("delta", { text: "物总结" }) + frame("done", summary))));
  const onPartial = vi.fn();
  expect(await streamRecordingSummary("one", onPartial)).toEqual(summary);
  expect(onPartial.mock.calls).toEqual([["聚合"], ["聚合物总结"]]);
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/one/summary"), expect.objectContaining({ headers: expect.objectContaining({ Accept: "text/event-stream" }) }));
});

it("收到部分正文时立即通知界面，不等连接结束", async () => {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(new ReadableStream({ start(value) { controller = value; } }), {
    headers: { "Content-Type": "text/event-stream" }
  })));
  const onPartial = vi.fn();
  const pending = streamRecordingSummary("one", onPartial);
  controller.enqueue(new TextEncoder().encode(frame("delta", { text: "聚合物" })));
  await vi.waitFor(() => expect(onPartial).toHaveBeenCalledWith("聚合物"));
  controller.enqueue(new TextEncoder().encode(frame("done", summary)));
  controller.close();
  expect(await pending).toEqual(summary);
});

it("断流和错误事件保留已通知的正文，但不能当成完整总结", async () => {
  for (const ending of ["", frame("error", { detail: "模型响应超时" })]) {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(frame("delta", { text: "未完成" }) + ending)));
    const onPartial = vi.fn();
    await expect(streamRecordingSummary("one", onPartial)).rejects.toThrow(ending ? "模型响应超时" : "总结连接中断");
    expect(onPartial).toHaveBeenCalledWith("未完成");
  }
});

it("校验终态记录 ID，忽略 done 之后的事件，并兼容旧 JSON 响应", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(frame("done", { ...summary, recording_id: "other" }))));
  await expect(streamRecordingSummary("one", vi.fn())).rejects.toThrow("未返回完整结果");
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(frame("done", summary) + frame("error", { detail: "ignored" }))));
  expect(await streamRecordingSummary("one", vi.fn())).toEqual(summary);
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(summary)));
  expect(await streamRecordingSummary("one", vi.fn())).toEqual(summary);
});

it("开始响应为 HTTP 错误时沿用服务端说明", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ detail: "记录已失效" }, { status: 410 })));
  await expect(streamRecordingSummary("one", vi.fn())).rejects.toMatchObject({ status: 410, message: "记录已失效" });
});
