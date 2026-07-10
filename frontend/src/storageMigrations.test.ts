import { afterEach, describe, expect, it, vi } from "vitest";
import { runStorageMigrations } from "./storageMigrations";

const LEGACY_PDF_API_SETTINGS_STORAGE_KEY = "polyprop.pdfSimilarityDemo.apiSettings";

describe("runStorageMigrations", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("removes the legacy PDF API settings key", () => {
    const removeItem = vi.fn();
    vi.stubGlobal("window", { localStorage: { removeItem } });

    runStorageMigrations();

    expect(removeItem).toHaveBeenCalledOnce();
    expect(removeItem).toHaveBeenCalledWith(LEGACY_PDF_API_SETTINGS_STORAGE_KEY);
  });

  it("is safe to run repeatedly", () => {
    const removeItem = vi.fn();
    vi.stubGlobal("window", { localStorage: { removeItem } });

    runStorageMigrations();
    runStorageMigrations();

    expect(removeItem).toHaveBeenCalledTimes(2);
    expect(removeItem).toHaveBeenNthCalledWith(1, LEGACY_PDF_API_SETTINGS_STORAGE_KEY);
    expect(removeItem).toHaveBeenNthCalledWith(2, LEGACY_PDF_API_SETTINGS_STORAGE_KEY);
  });

  it("does not throw when storage access fails", () => {
    const blockedWindow = {};
    Object.defineProperty(blockedWindow, "localStorage", {
      get() {
        throw new Error("storage is unavailable");
      }
    });
    vi.stubGlobal("window", blockedWindow);

    expect(() => runStorageMigrations()).not.toThrow();
  });

  it("does not throw when removing the key fails", () => {
    vi.stubGlobal("window", {
      localStorage: {
        removeItem() {
          throw new Error("storage is read-only");
        }
      }
    });

    expect(() => runStorageMigrations()).not.toThrow();
  });
});
