import { afterEach, describe, expect, it, vi } from "vitest";
import { BatchArtifactError, downloadBatchArtifact } from "./polymerizationBatchApi";

afterEach(() => vi.unstubAllGlobals());

describe("batch artifact download", () => {
  it("returns the original file bytes after a successful response", async () => {
    const bytes = new Uint8Array([0x50, 0x4b, 3, 4, 0, 255]);
    const fetch = vi.fn().mockResolvedValue(new Response(bytes, { headers: { "Content-Type": "application/zip" } }));
    vi.stubGlobal("fetch", fetch);
    const signal = new AbortController().signal;
    const blob = await downloadBatchArtifact("a".repeat(32), "results.zip", signal);
    expect(new Uint8Array(await blob.arrayBuffer())).toEqual(bytes);
    expect(blob.type).toBe("application/zip");
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/artifacts/results.zip"), { cache: "no-store", signal });
  });

  it("rejects a 410 JSON error instead of treating it as a downloadable file", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: "expired", message: "文件已过期。" }), { status: 410 })));
    const result = downloadBatchArtifact("a".repeat(32), "results.xlsx");
    await expect(result).rejects.toBeInstanceOf(BatchArtifactError);
    await expect(result).rejects.toMatchObject({ status: 410, code: "expired", message: "文件已过期。" });
  });

  it("provides a retryable message when the proxy returns HTML or the transfer breaks", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response("<html>Unavailable</html>", { status: 503 }))
      .mockResolvedValueOnce({ ok: true, blob: () => Promise.reject(new TypeError("connection closed")) });
    vi.stubGlobal("fetch", fetch);
    await expect(downloadBatchArtifact("a".repeat(32), "results.zip")).rejects.toMatchObject({ status: 503, message: "下载失败 (503)，请稍后重试。" });
    await expect(downloadBatchArtifact("a".repeat(32), "results.zip")).rejects.toThrow("connection closed");
  });
});
