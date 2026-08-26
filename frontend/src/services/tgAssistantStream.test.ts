// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  parseTgAssistantSseBlock,
  streamTgAssistant,
  type TgAssistantSseEvent
} from "./tgAssistantStream";

function chunkedResponse(source: string, cutPoints: number[]) {
  const bytes = new TextEncoder().encode(source);
  let start = 0;
  const chunks = [...cutPoints, bytes.length].map((end) => {
    const chunk = bytes.slice(start, end);
    start = end;
    return chunk;
  });
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(chunk));
      controller.close();
    }
  }), { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Tg assistant SSE parser", () => {
  it("parses CRLF and multiline data while ignoring comments", () => {
    expect(parseTgAssistantSseBlock(": heartbeat\r\n")).toBeNull();
    expect(parseTgAssistantSseBlock(
      "event: token\r\ndata: {\"content\":\r\ndata: \"你好\"}\r\n"
    )).toEqual({ event: "token", data: { content: "你好" } });
  });

  it("handles arbitrary byte chunks, UTF-8 splits, comments, and unknown events", async () => {
    const body = [
      "event: meta\r\ndata: {\"request_id\":\"r1\",\"context_trimmed\":[]}\r\n\r\n",
      ": heartbeat\r\n\r\n",
      "event: future_event\r\ndata: {\"ignored\":true}\r\n\r\n",
      "event: token\r\ndata: {\"content\":\"聚合物\"}\r\n\r\n",
      "event: done\r\ndata: {\"message\":\"聚合物\"}\r\n\r\n"
    ].join("");
    const encodedLength = new TextEncoder().encode(body).length;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      chunkedResponse(body, [1, 7, 39, 80, Math.floor(encodedLength / 2), encodedLength - 3])
    ));
    const events: TgAssistantSseEvent[] = [];

    await streamTgAssistant(
      { messages: [{ role: "user", content: "hello" }] },
      (event) => events.push(event),
      new AbortController().signal
    );

    expect(events.map((event) => event.event)).toEqual([
      "meta", "future_event", "token", "done"
    ]);
    expect(events[2].data.content).toBe("聚合物");
  });

  it("stops consuming after the first terminal event", async () => {
    const body = [
      "event: error\ndata: {\"code\":\"provider_error\"}\n\n",
      "event: done\ndata: {\"message\":\"must be ignored\"}\n\n"
    ].join("");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(chunkedResponse(body, [])));
    const events: TgAssistantSseEvent[] = [];

    await streamTgAssistant(
      { messages: [{ role: "user", content: "hello" }] },
      (event) => events.push(event),
      new AbortController().signal
    );

    expect(events.map((event) => event.event)).toEqual(["error"]);
  });

  it("rejects a stream that ends without done or error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(chunkedResponse(
      "event: token\ndata: {\"content\":\"partial\"}\n\n",
      [3, 11]
    )));

    await expect(streamTgAssistant(
      { messages: [{ role: "user", content: "hello" }] },
      () => undefined,
      new AbortController().signal
    )).rejects.toThrow("before a terminal event");
  });

  it("surfaces a structured HTTP failure before opening the stream", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: "Tg assistant is disabled" }),
      { status: 503, headers: { "Content-Type": "application/json" } }
    )));

    await expect(streamTgAssistant(
      { messages: [{ role: "user", content: "hello" }] },
      () => undefined,
      new AbortController().signal
    )).rejects.toMatchObject({ status: 503, message: "Tg assistant is disabled" });
  });

  it("uses multipart for an image without setting Content-Type manually", async () => {
    const fetchMock = vi.fn().mockResolvedValue(chunkedResponse(
      "event: done\ndata: {\"message\":\"ok\"}\n\n",
      []
    ));
    vi.stubGlobal("fetch", fetchMock);
    const image = new File(["image"], "structure.webp", { type: "image/webp" });

    await streamTgAssistant(
      { messages: [{ role: "user", content: "analyze" }] },
      () => undefined,
      new AbortController().signal,
      { userImage: image }
    );

    expect(fetchMock.mock.calls[0][0]).toContain("/assistant/tg/chat/image-stream");
    const options = fetchMock.mock.calls[0][1] as RequestInit;
    expect(options.headers).toBeUndefined();
    expect(options.body).toBeInstanceOf(FormData);
    const form = options.body as FormData;
    expect(form.get("image")).toBe(image);
    expect(JSON.parse(String(form.get("payload")))).toEqual({
      messages: [{ role: "user", content: "analyze" }]
    });
  });

  it("orders a canvas snapshot before the optional user image", async () => {
    const fetchMock = vi.fn().mockResolvedValue(chunkedResponse(
      "event: done\ndata: {\"message\":\"ok\"}\n\n",
      []
    ));
    vi.stubGlobal("fetch", fetchMock);
    const canvasImage = new Blob(["canvas"], { type: "image/png" });
    const userImage = new File(["reference"], "reference.png", { type: "image/png" });

    await streamTgAssistant(
      { messages: [{ role: "user", content: "compare" }] },
      () => undefined,
      new AbortController().signal,
      { canvasImage, userImage }
    );

    const form = (fetchMock.mock.calls[0][1] as RequestInit).body as FormData;
    expect((form.get("canvas_image") as File).name).toBe("tg-canvas.png");
    expect(form.get("image")).toBe(userImage);
  });
});
