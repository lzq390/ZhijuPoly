// @vitest-environment jsdom
import { execFileSync } from "node:child_process";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installSession, retireSession } from "../auth/session";
import type { PolymerMdArtifact } from "../types/polymerMd";
import { createPolymerMdZip64Entry, downloadPolymerMdArtifacts } from "./polymerMdArtifactBundle";

// Exercise both hard limits with small streams; the production constant is 256 MiB.
vi.mock("./polymerMdApi", async importOriginal => ({ ...await importOriginal<typeof import("./polymerMdApi")>(), POLYMER_MD_MEMORY_DOWNLOAD_LIMIT: 1024 }));
const session = { authenticated: true, session_id: "bundle-session", csrf_token: "bundle-csrf", user: { id: "bundle-user", username: "test", must_change_password: false }, capabilities: {} };
const artifact = (id: string, size: number, filename = `${id}.bin`, stage = "eq_1"): PolymerMdArtifact => ({ artifact_id: id, stage_run_id: stage, kind: "analysis", filename, media_type: "application/octet-stream", size_bytes: size, sha256: "" });
const concat = (chunks: Uint8Array[]) => Buffer.concat(chunks.map(chunk => Buffer.from(chunk)));
function inspectZip(bytes: Uint8Array) {
  // Python's independent ZIP implementation verifies CRC and decodes ZIP64, names,
  // descriptors and the central-directory offsets, rather than mirroring the writer.
  return JSON.parse(execFileSync("python3", ["-c", "import sys,io,zipfile,json,base64; z=zipfile.ZipFile(io.BytesIO(sys.stdin.buffer.read())); assert z.testzip() is None; print(json.dumps([{'name':i.filename,'body':base64.b64encode(z.read(i)).decode(),'method':i.compress_type,'flags':i.flag_bits,'version':i.extract_version} for i in z.infolist()]))"], { input: bytes, encoding: "utf8" }));
}
function streamingWriter(onWrite?: (chunk: Uint8Array) => void | Promise<void>) {
  const chunks: Uint8Array[] = [];
  const write = vi.fn(async (chunk: Uint8Array) => { chunks.push(Uint8Array.from(chunk)); await onWrite?.(chunk); });
  const close = vi.fn(async () => {}), abort = vi.fn(async () => {});
  const picker = vi.fn(async () => ({ createWritable: async () => ({ write, close, abort }) }));
  vi.stubGlobal("showSaveFilePicker", picker);
  return { chunks, write, close, abort, picker };
}
async function blobBytes(blob: Blob) {
  return new Uint8Array(await new Promise<ArrayBuffer>((resolve, reject) => {
    const reader = new FileReader(); reader.onload = () => resolve(reader.result as ArrayBuffer); reader.onerror = reject; reader.readAsArrayBuffer(blob);
  }));
}
beforeEach(() => installSession(session));
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); installSession(session); });

describe("complete polymer MD artifact archive", () => {
  it("opens one picker immediately, streams sequential authenticated GETs, and produces an independently readable ZIP64", async () => {
    const events: string[] = [], save = streamingWriter(() => { events.push("write"); });
    save.picker.mockImplementation(async () => { events.push("picker"); return { createWritable: async () => save }; });
    save.close.mockImplementation(async () => { events.push("close"); });
    const payloads = [new Uint8Array([0, 255, 10, 13]), new TextEncoder().encode("真实字节"), new Uint8Array()];
    const files = [artifact("a", payloads[0].length, "../轨迹.bin", "../eq_1"), artifact("b", payloads[1].length, "../轨迹.bin", "../eq_1"), artifact("c", 0, "empty.txt", "analysis")];
    const original = files.map(file => ({ ...file }));
    const fetch = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      const index = fetch.mock.calls.length - 1;
      events.push(`fetch:${original[index].artifact_id}`);
      expect(new Headers(init?.headers).get("X-Session-Context")).toBe("bundle-session");
      expect(init?.method ?? "GET").toBe("GET");
      if (index > 0) expect(events.slice(0, -1)).toContain("write");
      // Mutating the caller's array after the click cannot change the batch.
      files[1].artifact_id = "different"; files[1].filename = "changed.bin";
      return new Response(payloads[index]);
    });
    vi.stubGlobal("fetch", fetch);
    const progress = vi.fn((done: number, total: number) => { events.push(`progress:${done}/${total}`); });
    const result = downloadPolymerMdArtifacts("job / fixture", files, progress);
    expect(events).toEqual(["picker"]);
    await result;
    expect(save.picker).toHaveBeenCalledOnce(); expect(fetch).toHaveBeenCalledTimes(3);
    expect(fetch.mock.calls.map(call => String(call[0]))).toEqual(original.map(file => `/api/v1/polymer-md/jobs/job%20%2F%20fixture/artifacts/${file.artifact_id}`));
    expect(save.close).toHaveBeenCalledOnce(); expect(save.abort).not.toHaveBeenCalled();
    expect(events.at(-2)).toBe("close"); expect(events.at(-1)).toBe("progress:3/3");
    expect(progress.mock.calls).toEqual([[0, 3], [1, 3], [2, 3], [3, 3]]);
    const entries = inspectZip(concat(save.chunks));
    expect(entries.map((entry: { name: string }) => entry.name)).toEqual(["_eq_1/_轨迹.bin", "_eq_1/_轨迹 (2).bin", "analysis/empty.txt"]);
    expect(entries.map((entry: { body: string }) => entry.body)).toEqual(payloads.map(value => Buffer.from(value).toString("base64")));
    for (const entry of entries) { expect(entry.method).toBe(0); expect(entry.flags & 0x0808).toBe(0x0808); expect(entry.version).toBe(45); }
    expect(save.write.mock.calls.some(call => call[0].length === payloads[1].length)).toBe(true);
  });

  it("downloads one bounded fallback Blob with complete ZIP metadata and releases its URL", async () => {
    vi.stubGlobal("showSaveFilePicker", undefined);
    let saved: Blob | undefined;
    const create = vi.fn((blob: Blob) => { saved = blob; return "blob:bundle"; }), revoke = vi.fn();
    class DownloadURL extends URL { static createObjectURL = create; static revokeObjectURL = revoke; }
    vi.stubGlobal("URL", DownloadURL);
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    vi.stubGlobal("fetch", vi.fn(async () => new Response(new TextEncoder().encode("abc"))));
    await downloadPolymerMdArtifacts("job", [artifact("a", 3)]);
    expect(click).toHaveBeenCalledOnce(); expect(create).toHaveBeenCalledOnce(); expect(revoke).toHaveBeenCalledWith("blob:bundle");
    expect(saved?.type).toBe("application/zip"); expect(saved!.size).toBeLessThan(1024);
    expect(inspectZip(await blobBytes(saved!))).toMatchObject([{ name: "eq_1/a.bin", body: "YWJj" }]);
    expect(document.querySelector("a[download]")).toBeNull();
  });

  it("preflights the aggregate size including ZIP overhead before any GET", async () => {
    vi.stubGlobal("showSaveFilePicker", undefined); const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    // Each file fits the fallback; their total plus directory records does not.
    await expect(downloadPolymerMdArtifacts("job", [artifact("a", 450), artifact("b", 450)])).rejects.toThrow("256 MiB");
    // Even a payload below the limit can exceed it once ZIP headers are included.
    await expect(downloadPolymerMdArtifacts("job", [artifact("a", 950)])).rejects.toThrow("256 MiB");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("enforces the actual whole-archive memory limit against an oversized response", async () => {
    vi.stubGlobal("showSaveFilePicker", undefined);
    const cancel = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array(1025)); }, cancel }))));
    const progress = vi.fn();
    await expect(downloadPolymerMdArtifacts("job", [artifact("a", 1)], progress)).rejects.toThrow("256 MiB");
    expect(cancel).toHaveBeenCalled(); expect(progress).not.toHaveBeenCalledWith(1, 1);
  });

  it.each([NaN, -1, Number.MAX_SAFE_INTEGER + 1])("rejects invalid recorded size %s before picker or network", async size => {
    const save = streamingWriter(), fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    await expect(downloadPolymerMdArtifacts("job", [artifact("a", size)])).rejects.toThrow("大小记录不完整");
    expect(save.picker).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled();
  });

  it("does not start GETs when the picker is cancelled", async () => {
    vi.stubGlobal("showSaveFilePicker", vi.fn(async () => { throw new DOMException("Cancelled", "AbortError"); }));
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    await expect(downloadPolymerMdArtifacts("job", [artifact("a", 1)])).rejects.toMatchObject({ name: "AbortError" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each([
    ["incomplete", () => new Response(new Uint8Array([1, 2])), "未完整接收"],
    ["too long", () => new Response(new Uint8Array([1, 2, 3, 4])), "大小与产物记录不符"],
    ["wrong Content-Length", () => new Response(new Uint8Array([1, 2, 3]), { headers: { "Content-Length": "4" } }), "大小与产物记录不符"],
    ["HTTP failure", () => new Response("gone", { status: 404 }), "产物下载失败"],
    ["stream failure", () => new Response(new ReadableStream({ pull(controller) { controller.error(new Error("stream disconnected")); } })), "stream disconnected"]
  ] as const)("aborts the complete archive on %s without reporting success or fetching the next file", async (_name, response, message) => {
    const save = streamingWriter(), fetch = vi.fn(async () => response()), progress = vi.fn(); vi.stubGlobal("fetch", fetch);
    await expect(downloadPolymerMdArtifacts("job", [artifact("a", 3), artifact("b", 2)], progress)).rejects.toThrow(message);
    expect(fetch).toHaveBeenCalledOnce(); expect(save.abort).toHaveBeenCalledOnce(); expect(save.close).not.toHaveBeenCalled(); expect(progress).not.toHaveBeenCalledWith(2, 2);
  });

  it.each(["write", "close"] as const)("does not report success if the file writer cannot %s", async method => {
    const save = streamingWriter(), progress = vi.fn();
    save[method].mockRejectedValueOnce(new Error("disk unavailable"));
    vi.stubGlobal("fetch", vi.fn(async () => new Response(new Uint8Array([1]))));
    await expect(downloadPolymerMdArtifacts("job", [artifact("a", 1)], progress)).rejects.toThrow("disk unavailable");
    expect(save.abort).toHaveBeenCalledOnce(); expect(progress).not.toHaveBeenCalledWith(1, 1);
  });

  it("cancels a pending stream on external abort and does not commit the archive", async () => {
    const save = streamingWriter(), source = new AbortController(), cancel = vi.fn();
    let started!: () => void; const ready = new Promise<void>(resolve => { started = resolve; });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(new ReadableStream({ pull() { started(); }, cancel }, { highWaterMark: 0 }))));
    const download = downloadPolymerMdArtifacts("job", [artifact("a", 1)], undefined, source.signal);
    const rejected = expect(download).rejects.toMatchObject({ name: "AbortError" });
    await ready; source.abort(); await rejected;
    expect(cancel).toHaveBeenCalled(); expect(save.abort).toHaveBeenCalledOnce(); expect(save.close).not.toHaveBeenCalled();
  });

  it("retires a session during a blocked write without waiting for it or requesting a second file", async () => {
    let started!: () => void; const ready = new Promise<void>(resolve => { started = resolve; });
    const save = streamingWriter(async () => { started(); await new Promise<void>(() => {}); });
    const fetch = vi.fn(async () => new Response(new Uint8Array([1]))); vi.stubGlobal("fetch", fetch);
    const download = downloadPolymerMdArtifacts("job", [artifact("a", 1), artifact("b", 1)]);
    const rejected = expect(download).rejects.toMatchObject({ name: "AbortError" });
    await ready; retireSession(); await rejected;
    expect(save.abort).toHaveBeenCalledOnce(); expect(save.close).not.toHaveBeenCalled(); expect(fetch).toHaveBeenCalledOnce();
  });

  it("retries abort after an in-flight native-style write unlocks the file stream", async () => {
    let unlock!: () => void, started!: () => void;
    const ready = new Promise<void>(resolve => { started = resolve; });
    const save = streamingWriter(async () => { started(); await new Promise<void>(resolve => { unlock = resolve; }); });
    let locked = true;
    save.abort.mockImplementation(async () => { if (locked) throw new TypeError("stream is locked"); });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(new Uint8Array([1]))));
    const source = new AbortController(), download = downloadPolymerMdArtifacts("job", [artifact("a", 1)], undefined, source.signal);
    const rejected = expect(download).rejects.toMatchObject({ name: "AbortError" });
    await ready; source.abort(); await rejected;
    expect(save.abort).toHaveBeenCalled();
    const attempts = save.abort.mock.calls.length;
    locked = false; unlock();
    await vi.waitFor(() => expect(save.abort.mock.calls.length).toBeGreaterThan(attempts));
    expect(save.close).not.toHaveBeenCalled();
  });

  it("ignores a late picker after cancellation and aborts a writable that opens late", async () => {
    let choose!: (value: { createWritable: () => Promise<{ write: ReturnType<typeof vi.fn>; close: ReturnType<typeof vi.fn>; abort: ReturnType<typeof vi.fn> }> }) => void;
    vi.stubGlobal("showSaveFilePicker", vi.fn(() => new Promise(resolve => { choose = resolve; })));
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    const source = new AbortController(), createWritable = vi.fn();
    const result = downloadPolymerMdArtifacts("job", [artifact("a", 1)], undefined, source.signal);
    const rejection = expect(result).rejects.toMatchObject({ name: "AbortError" });
    source.abort(); await rejection; choose({ createWritable }); await Promise.resolve();
    expect(createWritable).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled();

    const save = streamingWriter(); let opened!: (value: typeof save) => void;
    let opening!: () => void; const ready = new Promise<void>(resolve => { opening = resolve; });
    save.picker.mockImplementation(async () => ({ createWritable: () => { opening(); return new Promise(resolve => { opened = resolve; }); } }));
    const nextSource = new AbortController(), next = downloadPolymerMdArtifacts("job", [artifact("a", 1)], undefined, nextSource.signal);
    const nextRejection = expect(next).rejects.toMatchObject({ name: "AbortError" });
    await ready; nextSource.abort(); await nextRejection; opened(save); await Promise.resolve(); await Promise.resolve();
    expect(save.abort).toHaveBeenCalledOnce(); expect(save.close).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled();
  });

  it("represents file sizes and local offsets beyond 4 GiB exactly without allocating their payloads", () => {
    const size = 0x100000000n + 17n, offset = 0x200000000n + 29n, crc = 0x12345678;
    const entry = createPolymerMdZip64Entry("stage/data.bin", size, offset, crc);
    const local = new DataView(entry.local.buffer), central = new DataView(entry.central.buffer), descriptor = new DataView(entry.descriptor.buffer);
    const localExtra = 30 + local.getUint16(26, true), centralExtra = 46 + central.getUint16(28, true);
    expect(local.getUint32(18, true)).toBe(0xffffffff); expect(local.getBigUint64(localExtra + 4, true)).toBe(size);
    expect(central.getUint32(42, true)).toBe(0xffffffff); expect(central.getBigUint64(centralExtra + 4, true)).toBe(size); expect(central.getBigUint64(centralExtra + 20, true)).toBe(offset);
    expect(descriptor.getUint32(4, true)).toBe(crc); expect(descriptor.getBigUint64(8, true)).toBe(size); expect(descriptor.getBigUint64(16, true)).toBe(size);
    expect(() => createPolymerMdZip64Entry("x", 1n << 64n, 0n, 0)).toThrow("ZIP");
  });
});
