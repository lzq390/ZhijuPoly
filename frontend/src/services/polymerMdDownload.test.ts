// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installSession, retireSession } from "../auth/session";
import { downloadPolymerMdArtifact, POLYMER_MD_MEMORY_DOWNLOAD_LIMIT } from "./polymerMdApi";

const session = { authenticated: true, session_id: "test-session", csrf_token: "test-csrf", user: { id: "test-user", username: "test", must_change_password: false }, capabilities: {} };
beforeEach(() => installSession(session));
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); installSession(session); });

describe("formal artifact download", () => {
  it("opens the save picker before fetching and writes artifact chunks incrementally", async () => {
    const order: string[] = [], write = vi.fn(async () => {}), close = vi.fn(async () => {}), abort = vi.fn(async () => {});
    vi.stubGlobal("showSaveFilePicker", vi.fn(() => { order.push("picker"); return Promise.resolve({ createWritable: async () => ({ write, close, abort }) }); }));
    const stream = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new Uint8Array([1, 2])); controller.enqueue(new Uint8Array([3])); controller.close(); } });
    vi.stubGlobal("fetch", vi.fn(async () => { order.push("fetch"); return new Response(stream); }));
    await downloadPolymerMdArtifact("job", "artifact", "trajectory.xtc", 3);
    expect(order).toEqual(["picker", "fetch"]); expect(write).toHaveBeenCalledTimes(2); expect(close).toHaveBeenCalledOnce(); expect(abort).not.toHaveBeenCalled();
  });
  it("rejects a large file before fetching when streaming file save is unavailable", async () => {
    vi.stubGlobal("showSaveFilePicker", undefined); const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    await expect(downloadPolymerMdArtifact("job", "artifact", "large.xtc", POLYMER_MD_MEMORY_DOWNLOAD_LIMIT + 1)).rejects.toThrow("256 MiB");
    expect(fetch).not.toHaveBeenCalled();
  });
  it("aborts the open writer on account retirement and never commits the file", async () => {
    const abort = vi.fn(async () => {}), close = vi.fn(async () => {}), write = vi.fn(async () => { retireSession(); });
    vi.stubGlobal("showSaveFilePicker", vi.fn(async () => ({ createWritable: async () => ({ write, close, abort }) })));
    vi.stubGlobal("fetch", vi.fn(async () => new Response(new Uint8Array([1, 2, 3]))));
    await expect(downloadPolymerMdArtifact("job", "artifact", "trajectory.xtc", 3)).rejects.toMatchObject({ name: "AbortError" });
    expect(write).toHaveBeenCalledOnce(); expect(abort).toHaveBeenCalled(); expect(close).not.toHaveBeenCalled();
  });
  it("does not commit an incomplete artifact", async () => {
    const abort = vi.fn(async () => {}), close = vi.fn(async () => {}), write = vi.fn(async () => {});
    vi.stubGlobal("showSaveFilePicker", vi.fn(async () => ({ createWritable: async () => ({ write, close, abort }) })));
    vi.stubGlobal("fetch", vi.fn(async () => new Response(new Uint8Array([1, 2]))));
    await expect(downloadPolymerMdArtifact("job", "artifact", "trajectory.xtc", 3)).rejects.toThrow("未完整接收");
    expect(abort).toHaveBeenCalledOnce(); expect(close).not.toHaveBeenCalled();
  });
  it("downloads a small file through a bounded Blob fallback", async () => {
    vi.stubGlobal("showSaveFilePicker", undefined);
    const create = vi.fn(() => "blob:test"), revoke = vi.fn();
    class DownloadURL extends URL { static createObjectURL = create; static revokeObjectURL = revoke; }
    vi.stubGlobal("URL", DownloadURL);
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    vi.stubGlobal("fetch", vi.fn(async () => new Response(new Uint8Array([1, 2, 3]))));
    await downloadPolymerMdArtifact("job", "artifact", "summary.json", 3);
    expect(create).toHaveBeenCalledOnce(); expect(revoke).toHaveBeenCalledWith("blob:test");
  });
});
