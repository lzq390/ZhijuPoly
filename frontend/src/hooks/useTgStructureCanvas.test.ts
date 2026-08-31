// @vitest-environment jsdom

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { StructureWorkspaceContext } from "../types";
import {
  adoptKetcherPng,
  isProtectedCanvasConsistent,
  isEmptyKetcherDocument,
  shouldAdoptEditorSmiles,
  stripKetcherSelectedFields,
  useTgStructureCanvas,
  wildcardCount
} from "./useTgStructureCanvas";

const apiMocks = vi.hoisted(() => ({
  standardizeSmiles: vi.fn(),
  recognizeStructureImage: vi.fn()
}));

vi.mock("../services/api", () => apiMocks);

beforeEach(() => {
  apiMocks.standardizeSmiles.mockReset().mockImplementation(async ({ smiles }: { smiles: string }) => ({
    input_smiles: smiles,
    standardized_smiles: smiles
  }));
  apiMocks.recognizeStructureImage.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Tg structure canvas wildcard protection", () => {
  it("adopts a valid PNG Blob-like value from a different iframe realm", async () => {
    const bytes = Uint8Array.from([
      0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00
    ]);
    const iframeBlob = {
      size: bytes.byteLength,
      type: "image/png",
      arrayBuffer: vi.fn(async () => bytes.buffer.slice(0))
    };

    expect(iframeBlob instanceof Blob).toBe(false);
    const adopted = await adoptKetcherPng(iframeBlob);

    expect(adopted).toBeInstanceOf(Blob);
    expect(adopted.type).toBe("image/png");
    expect(adopted.size).toBe(bytes.byteLength);
    expect(iframeBlob.arrayBuffer).toHaveBeenCalledOnce();
  });

  it("counts polymer end groups and rejects a Ketcher value that loses them", () => {
    expect(wildcardCount("*CC(*)C*")).toBe(3);
    expect(shouldAdoptEditorSmiles("*CC*", "CCC")).toBe(false);
  });

  it("accepts editor SMILES when wildcard preservation is reliable", () => {
    expect(shouldAdoptEditorSmiles("*CC*", "*C(C)*")).toBe(true);
    expect(shouldAdoptEditorSmiles("CC", "CCC")).toBe(true);
    expect(shouldAdoptEditorSmiles("CC", "")).toBe(false);
  });

  it("treats an exact endpoint-stripped Ketcher representation as synchronized", () => {
    expect(isProtectedCanvasConsistent("*CC*", "CC")).toBe(true);
    expect(isProtectedCanvasConsistent("*CC*", "CCC")).toBe(false);
    expect(isProtectedCanvasConsistent("CC", "CCC")).toBe(false);
    expect(isProtectedCanvasConsistent("", "")).toBe(true);
  });

  it("restores the original Ketcher snapshot when loading a new structure fails", async () => {
    const setMolecule = vi.fn(async (source: string) => {
      if (source !== "old-molfile") throw new Error("Ketcher rejected structure");
    });
    const ketcher = {
      clear: vi.fn().mockResolvedValue(undefined),
      getSmiles: vi.fn().mockResolvedValue("*CC*"),
      getMolfile: vi.fn().mockResolvedValue("old-molfile"),
      setMolecule
    };
    const frameWindow = {
      ketcher,
      Event,
      dispatchEvent: vi.fn(),
      scrollTo: vi.fn()
    };
    const structure = {
      smiles: "*CC*",
      setSmiles: vi.fn(),
      iframeRef: { current: { contentWindow: frameWindow } },
      setIsReady: vi.fn(),
      getCurrentSmiles: vi.fn().mockResolvedValue("*CC*")
    } as unknown as StructureWorkspaceContext;
    const onStructureChanged = vi.fn();
    const { result } = renderHook(() => useTgStructureCanvas({ structure, onStructureChanged }));

    let loaded = true;
    await act(async () => {
      loaded = await result.current.loadStructure("*CO*");
    });

    expect(loaded).toBe(false);
    expect(setMolecule).toHaveBeenCalledWith("*CO*");
    expect(setMolecule).toHaveBeenCalledWith("CO");
    expect(setMolecule).toHaveBeenLastCalledWith("old-molfile");
    expect(structure.setSmiles).toHaveBeenCalledWith("*CC*");
    expect(onStructureChanged).not.toHaveBeenCalled();
  });

  it("removes only selected flags and recognizes an empty KET document", () => {
    const cleaned = stripKetcherSelectedFields({
      selected: true,
      root: {
        nodes: [{ selected: false, type: "molecule", data: { selected: true, charge: 1 } }],
        connections: []
      },
      highlight: true
    });

    expect(JSON.stringify(cleaned)).not.toContain("selected");
    expect(cleaned).toMatchObject({ root: { nodes: [{ data: { charge: 1 } }] }, highlight: true });
    expect(isEmptyKetcherDocument({ root: { nodes: [], connections: [] } })).toBe(true);
    expect(isEmptyKetcherDocument({ root: { nodes: [{ type: "molecule" }], connections: [] } })).toBe(false);
  });

  it("renders a white PNG from cleaned KET and reuses the one-entry cache", async () => {
    const getKet = vi.fn().mockResolvedValue(JSON.stringify({
      root: { nodes: [{ type: "molecule", selected: true }], connections: [] }
    }));
    const pngBytes = Uint8Array.from([
      0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00
    ]);
    const generateImage = vi.fn().mockResolvedValue({
      size: pngBytes.byteLength,
      type: "image/png",
      arrayBuffer: vi.fn(async () => pngBytes.buffer.slice(0))
    });
    const bitmapClose = vi.fn();
    vi.stubGlobal("createImageBitmap", vi.fn().mockResolvedValue({
      width: 120,
      height: 60,
      close: bitmapClose
    }));
    const drawImage = vi.fn();
    const fillRect = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      fillStyle: "",
      fillRect,
      drawImage
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((callback) => {
      callback(new Blob(["normalized"], { type: "image/png" }));
    });
    const frameWindow = {
      ketcher: { getSmiles: vi.fn().mockResolvedValue("CC"), getKet, generateImage },
      Event,
      dispatchEvent: vi.fn(),
      scrollTo: vi.fn()
    };
    const structure = {
      smiles: "CC",
      setSmiles: vi.fn(),
      iframeRef: { current: { contentWindow: frameWindow } },
      setIsReady: vi.fn(),
      getCurrentSmiles: vi.fn().mockResolvedValue("CC")
    } as unknown as StructureWorkspaceContext;
    const { result } = renderHook(() => useTgStructureCanvas({
      structure,
      onStructureChanged: vi.fn()
    }));

    let first: Blob | null = null;
    let second: Blob | null = null;
    await act(async () => {
      first = await result.current.captureCanvasImage();
      second = await result.current.captureCanvasImage();
    });

    expect(first).toBe(second);
    expect(getKet).toHaveBeenCalledTimes(2);
    expect(generateImage).toHaveBeenCalledTimes(1);
    expect(generateImage.mock.calls[0][0]).not.toContain("selected");
    expect(generateImage.mock.calls[0][1]).toEqual({
      outputFormat: "png",
      backgroundColor: "255, 255, 255",
      "image-resolution": 144
    });
    expect(fillRect).toHaveBeenCalled();
    expect(drawImage).toHaveBeenCalled();
    expect(bitmapClose).toHaveBeenCalledOnce();
  });

  it("retries a transiently empty KET before deciding that the populated canvas is empty", async () => {
    const emptyKet = JSON.stringify({ root: { nodes: [], connections: [] } });
    const populatedKet = JSON.stringify({
      root: { nodes: [{ type: "molecule", selected: true }], connections: [] }
    });
    const getKet = vi.fn()
      .mockResolvedValueOnce(emptyKet)
      .mockResolvedValue(populatedKet);
    const pngBytes = Uint8Array.from([
      0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00
    ]);
    const generateImage = vi.fn().mockResolvedValue({
      size: pngBytes.byteLength,
      type: "image/png",
      arrayBuffer: vi.fn(async () => pngBytes.buffer.slice(0))
    });
    vi.stubGlobal("createImageBitmap", vi.fn().mockResolvedValue({
      width: 120,
      height: 60,
      close: vi.fn()
    }));
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      fillStyle: "",
      fillRect: vi.fn(),
      drawImage: vi.fn()
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((callback) => {
      callback(new Blob(["normalized"], { type: "image/png" }));
    });
    const frameWindow = {
      ketcher: { getSmiles: vi.fn().mockResolvedValue("CC"), getKet, generateImage },
      Event,
      dispatchEvent: vi.fn(),
      scrollTo: vi.fn()
    };
    const structure = {
      smiles: "CC",
      setSmiles: vi.fn(),
      iframeRef: { current: { contentWindow: frameWindow } },
      setIsReady: vi.fn(),
      getCurrentSmiles: vi.fn().mockResolvedValue("CC")
    } as unknown as StructureWorkspaceContext;
    const { result } = renderHook(() => useTgStructureCanvas({
      structure,
      onStructureChanged: vi.fn()
    }));

    let image: Blob | null = null;
    await act(async () => {
      image = await result.current.captureCanvasImage();
    });

    expect(image).toBeInstanceOf(Blob);
    expect(getKet).toHaveBeenCalledTimes(2);
    expect(generateImage).toHaveBeenCalledOnce();
    expect(generateImage.mock.calls[0][0]).toContain("molecule");
    expect(generateImage.mock.calls[0][0]).not.toContain("selected");
  });

  it("renders a populated canvas from its local Molfile when KET remains transiently empty", async () => {
    const emptyKet = JSON.stringify({ root: { nodes: [], connections: [] } });
    const molfile = [
      "fallback",
      "  Ketcher",
      "",
      "  2  1  0  0  0  0            999 V2000",
      "    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0",
      "    1.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0",
      "  1  2  1  0  0  0  0",
      "M  END"
    ].join("\n");
    const getKet = vi.fn().mockResolvedValue(emptyKet);
    const pngBytes = Uint8Array.from([
      0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00
    ]);
    const generateImage = vi.fn().mockResolvedValue({
      size: pngBytes.byteLength,
      type: "image/png",
      arrayBuffer: vi.fn(async () => pngBytes.buffer.slice(0))
    });
    vi.stubGlobal("createImageBitmap", vi.fn().mockResolvedValue({
      width: 120,
      height: 60,
      close: vi.fn()
    }));
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      fillStyle: "",
      fillRect: vi.fn(),
      drawImage: vi.fn()
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((callback) => {
      callback(new Blob(["normalized"], { type: "image/png" }));
    });
    const getMolfile = vi.fn().mockResolvedValue(molfile);
    const frameWindow = {
      ketcher: {
        getSmiles: vi.fn().mockResolvedValue("CC"),
        getMolfile,
        getKet,
        generateImage
      },
      Event,
      dispatchEvent: vi.fn(),
      scrollTo: vi.fn()
    };
    const structure = {
      smiles: "CC",
      setSmiles: vi.fn(),
      iframeRef: { current: { contentWindow: frameWindow } },
      setIsReady: vi.fn(),
      getCurrentSmiles: vi.fn().mockResolvedValue("CC")
    } as unknown as StructureWorkspaceContext;
    const { result } = renderHook(() => useTgStructureCanvas({
      structure,
      onStructureChanged: vi.fn()
    }));

    let image: Blob | null = null;
    await act(async () => {
      image = await result.current.captureCanvasImage();
    });

    expect(image).toBeInstanceOf(Blob);
    expect(getKet.mock.calls.length).toBeGreaterThan(1);
    expect(getMolfile).toHaveBeenCalledOnce();
    expect(generateImage).toHaveBeenCalledWith(molfile, expect.objectContaining({ outputFormat: "png" }));
  });

  it("does not render a genuinely empty canvas", async () => {
    const generateImage = vi.fn();
    const getKet = vi.fn().mockResolvedValue(JSON.stringify({ root: { nodes: [], connections: [] } }));
    const ketcher = {
      getSmiles: vi.fn().mockResolvedValue(""),
      getKet,
      generateImage
    };
    const frameWindow = { ketcher, Event, dispatchEvent: vi.fn(), scrollTo: vi.fn() };
    const structure = {
      smiles: "",
      setSmiles: vi.fn(),
      iframeRef: { current: { contentWindow: frameWindow } },
      setIsReady: vi.fn(),
      getCurrentSmiles: vi.fn().mockResolvedValue("")
    } as unknown as StructureWorkspaceContext;
    const { result } = renderHook(() => useTgStructureCanvas({ structure, onStructureChanged: vi.fn() }));

    await expect(result.current.captureCanvasImage()).resolves.toBeNull();
    expect(generateImage).not.toHaveBeenCalled();
  });

  it("canonicalizes equivalent SMILES before marking the canvas dirty", async () => {
    const ketcher = { getSmiles: vi.fn().mockResolvedValue("CCO") };
    const frameWindow = { ketcher, Event, dispatchEvent: vi.fn(), scrollTo: vi.fn() };
    const structure = {
      smiles: "C(C)O",
      setSmiles: vi.fn(),
      iframeRef: { current: { contentWindow: frameWindow } },
      setIsReady: vi.fn(),
      getCurrentSmiles: vi.fn().mockResolvedValue("CCO")
    } as unknown as StructureWorkspaceContext;
    apiMocks.standardizeSmiles.mockResolvedValue({
      input_smiles: "CCO",
      standardized_smiles: "CCO"
    });
    const { result } = renderHook(() => useTgStructureCanvas({ structure, onStructureChanged: vi.fn() }));

    await expect(result.current.peekCanvasState()).resolves.toMatchObject({ canvasDirty: false });
    expect(apiMocks.standardizeSmiles).toHaveBeenCalledTimes(2);
  });

  it("marks the canvas dirty when Ketcher state cannot be read", async () => {
    const ketcher = { getSmiles: vi.fn().mockRejectedValue(new Error("iframe unavailable")) };
    const frameWindow = { ketcher, Event, dispatchEvent: vi.fn(), scrollTo: vi.fn() };
    const structure = {
      smiles: "CCO",
      setSmiles: vi.fn(),
      iframeRef: { current: { contentWindow: frameWindow } },
      setIsReady: vi.fn(),
      getCurrentSmiles: vi.fn().mockResolvedValue("CCO")
    } as unknown as StructureWorkspaceContext;
    const { result } = renderHook(() => useTgStructureCanvas({
      structure,
      onStructureChanged: vi.fn()
    }));

    await expect(result.current.peekCanvasState()).resolves.toMatchObject({
      smiles: "CCO",
      canvasDirty: true
    });
  });

  it("在文本模式标准化并应用结构，同时保留聚合物端基", async () => {
    const structure = {
      smiles: "",
      setSmiles: vi.fn(),
      iframeRef: { current: null },
      setIsReady: vi.fn(),
      getCurrentSmiles: vi.fn().mockResolvedValue("")
    } as StructureWorkspaceContext;
    apiMocks.standardizeSmiles.mockResolvedValue({
      input_smiles: "*CC*",
      standardized_smiles: "CC"
    });
    const onStructureChanged = vi.fn();
    const { result } = renderHook(() => useTgStructureCanvas({ structure, onStructureChanged }));

    await expect(result.current.applyTextStructure(" *CC* ")).resolves.toEqual({
      applied: true,
      smiles: "*CC*"
    });
    expect(structure.setSmiles).toHaveBeenCalledWith("*CC*");
    expect(onStructureChanged).toHaveBeenCalledOnce();
  });

  it("将可编辑 SMILES 防抖校验后写入 Ketcher 画板", async () => {
    let editorSmiles = "*CC*";
    const clear = vi.fn(async () => {
      editorSmiles = "";
    });
    const setMolecule = vi.fn(async (value: string) => {
      editorSmiles = value;
    });
    const ketcher = {
      clear,
      setMolecule,
      getSmiles: vi.fn(async () => editorSmiles),
      getMolfile: vi.fn().mockResolvedValue("old-molfile")
    };
    const structure = {
      smiles: "*CC*",
      setSmiles: vi.fn(),
      iframeRef: {
        current: {
          contentWindow: { ketcher, Event, dispatchEvent: vi.fn(), scrollTo: vi.fn() }
        }
      },
      setIsReady: vi.fn(),
      getCurrentSmiles: vi.fn().mockResolvedValue("*CC*")
    } as unknown as StructureWorkspaceContext;
    const onStructureChanged = vi.fn();
    const { result } = renderHook(() => useTgStructureCanvas({ structure, onStructureChanged }));

    act(() => result.current.updateSmilesDraft("*CO*"));
    expect(result.current.smilesDraftState).toBe("pending");
    expect(apiMocks.standardizeSmiles).not.toHaveBeenCalled();

    await vi.waitFor(() => expect(result.current.smilesDraftState).toBe("synced"), {
      timeout: 2000
    });

    expect(apiMocks.standardizeSmiles).toHaveBeenCalledWith({ smiles: "*CO*" });
    expect(clear).toHaveBeenCalledOnce();
    expect(setMolecule).toHaveBeenCalledWith("*CO*");
    expect(structure.setSmiles).toHaveBeenCalledWith("*CO*");
    expect(onStructureChanged).toHaveBeenCalledOnce();
    expect(result.current.smilesDraft).toBe("*CO*");
    expect(result.current.smilesDraftState).toBe("synced");
  });

  it("无效 SMILES 保留原画板并显示可恢复错误", async () => {
    const structure = {
      smiles: "*CC*",
      setSmiles: vi.fn(),
      iframeRef: { current: null },
      setIsReady: vi.fn(),
      getCurrentSmiles: vi.fn().mockResolvedValue("*CC*")
    } as StructureWorkspaceContext;
    apiMocks.standardizeSmiles.mockRejectedValue(new Error("invalid smiles"));
    const { result } = renderHook(() => useTgStructureCanvas({
      structure,
      onStructureChanged: vi.fn()
    }));

    act(() => result.current.updateSmilesDraft("*C("));
    let applied = true;
    await act(async () => {
      applied = await result.current.flushSmilesDraft();
    });

    expect(applied).toBe(false);
    expect(structure.setSmiles).not.toHaveBeenCalled();
    expect(result.current.smilesDraft).toBe("*C(");
    expect(result.current.smilesDraftState).toBe("error");
    expect(result.current.smilesDraftError).toContain("原画板未修改");
  });

  it("忽略已失效的文本校验结果，只应用最后一次输入", async () => {
    let resolveFirst: ((value: { input_smiles: string; standardized_smiles: string }) => void) | null = null;
    apiMocks.standardizeSmiles
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveFirst = resolve;
      }))
      .mockResolvedValueOnce({ input_smiles: "*CN*", standardized_smiles: "*CN*" });
    const structure = {
      smiles: "*CC*",
      setSmiles: vi.fn(),
      iframeRef: { current: null },
      setIsReady: vi.fn(),
      getCurrentSmiles: vi.fn().mockResolvedValue("*CC*")
    } as StructureWorkspaceContext;
    const { result } = renderHook(() => useTgStructureCanvas({
      structure,
      onStructureChanged: vi.fn()
    }));

    act(() => result.current.updateSmilesDraft("*CO*"));
    let firstSync: Promise<boolean> | null = null;
    act(() => {
      firstSync = result.current.flushSmilesDraft();
    });
    await vi.waitFor(() => expect(apiMocks.standardizeSmiles).toHaveBeenCalledTimes(1));

    act(() => result.current.updateSmilesDraft("*CN*"));
    let secondSync: Promise<boolean> | null = null;
    act(() => {
      secondSync = result.current.flushSmilesDraft();
    });
    act(() => resolveFirst?.({ input_smiles: "*CO*", standardized_smiles: "*CO*" }));

    await act(async () => {
      expect(await firstSync).toBe(false);
      expect(await secondSync).toBe(true);
    });
    expect(structure.setSmiles).toHaveBeenCalledTimes(1);
    expect(structure.setSmiles).toHaveBeenCalledWith("*CN*");
    expect(result.current.smilesDraft).toBe("*CN*");
  });

  it("文本模式清空共享结构而不要求编辑器存在", async () => {
    const structure = {
      smiles: "CC",
      setSmiles: vi.fn(),
      iframeRef: { current: null },
      setIsReady: vi.fn(),
      getCurrentSmiles: vi.fn().mockResolvedValue("CC")
    } as StructureWorkspaceContext;
    const { result } = renderHook(() => useTgStructureCanvas({ structure, onStructureChanged: vi.fn() }));

    await expect(result.current.clearCanvas()).resolves.toBe(true);
    expect(structure.setSmiles).toHaveBeenCalledWith("");
  });

  it("延迟挂载 iframe 后重新启动就绪检测并恢复共享结构", async () => {
    const iframeRef: StructureWorkspaceContext["iframeRef"] = { current: null };
    const structure = {
      smiles: "CC",
      setSmiles: vi.fn(),
      iframeRef,
      setIsReady: vi.fn(),
      getCurrentSmiles: vi.fn().mockResolvedValue("CC")
    } as StructureWorkspaceContext;
    const setMolecule = vi.fn().mockResolvedValue(undefined);
    const { result } = renderHook(() => useTgStructureCanvas({ structure, onStructureChanged: vi.fn() }));

    iframeRef.current = {
      contentWindow: {
        ketcher: { getSmiles: vi.fn().mockResolvedValue(""), setMolecule },
        Event,
        dispatchEvent: vi.fn(),
        scrollTo: vi.fn()
      }
    } as unknown as HTMLIFrameElement;
    await act(async () => result.current.handleEditorLoad());

    await vi.waitFor(() => expect(result.current.isEditorReady).toBe(true));
    expect(setMolecule).toHaveBeenCalledWith("CC");
    expect(structure.setIsReady).toHaveBeenCalledWith(true);
  });
});
