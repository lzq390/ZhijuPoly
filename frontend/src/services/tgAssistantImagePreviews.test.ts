// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createTgAssistantImageThumbnail,
  loadTgAssistantImagePreview
} from "./tgAssistantImagePreviews";

describe("Tg assistant persisted image previews", () => {
  const bitmapClose = vi.fn();
  const fillRect = vi.fn();
  const drawImage = vi.fn();

  beforeEach(() => {
    bitmapClose.mockReset();
    fillRect.mockReset();
    drawImage.mockReset();
    vi.stubGlobal("createImageBitmap", vi.fn().mockResolvedValue({
      width: 1280,
      height: 640,
      close: bitmapClose
    }));
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      fillStyle: "",
      fillRect,
      drawImage
    } as unknown as CanvasRenderingContext2D);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("rasterizes a bounded white-background thumbnail and releases the decoder", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((callback, type) => {
      callback(new Blob(["thumbnail"], { type: type || "image/webp" }));
    });

    const thumbnail = await createTgAssistantImageThumbnail(
      new Blob(["source"], { type: "image/png" })
    );

    expect(thumbnail.type).toBe("image/webp");
    expect(fillRect).toHaveBeenCalledWith(0, 0, 640, 320);
    expect(drawImage).toHaveBeenCalledWith(expect.anything(), 0, 0, 640, 320);
    expect(bitmapClose).toHaveBeenCalledOnce();
  });

  it("reduces dimensions and quality until the thumbnail fits the storage bound", async () => {
    const oversized = new Blob([new Uint8Array(512 * 1024 + 1)], { type: "image/webp" });
    const encoded = vi.spyOn(HTMLCanvasElement.prototype, "toBlob")
      .mockImplementationOnce((callback) => callback(oversized))
      .mockImplementationOnce((callback) => callback(new Blob(["small"], { type: "image/webp" })));

    const thumbnail = await createTgAssistantImageThumbnail(
      new Blob(["source"], { type: "image/jpeg" })
    );

    expect(thumbnail.size).toBe(5);
    expect(encoded).toHaveBeenCalledTimes(2);
    expect(drawImage).toHaveBeenNthCalledWith(1, expect.anything(), 0, 0, 640, 320);
    expect(drawImage).toHaveBeenNthCalledWith(2, expect.anything(), 0, 0, 499, 250);
    expect(bitmapClose).toHaveBeenCalledOnce();
  });

  it("fails closed when IndexedDB is unavailable", async () => {
    vi.stubGlobal("indexedDB", undefined);

    await expect(loadTgAssistantImagePreview("session", "message")).rejects.toThrow(
      "IndexedDB is unavailable"
    );
  });
});
