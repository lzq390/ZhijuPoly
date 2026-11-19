import { describe, expect, it, vi } from "vitest";
import { enlargeImportedStructure, type KetcherInstance } from "./editor";

function fakeKetcher(box: { w: number; h: number }, zoom = 1) {
  let current = zoom;
  const centerStruct = vi.fn();
  const ketcher = {
    editor: {
      struct: () => ({ getCoordBoundingBox: () => ({ min: { x: 0, y: 0 }, max: { x: box.w, y: box.h } }) }),
      zoom: (value?: number) => { if (value !== undefined) current = value; return current; },
      centerStruct,
      render: { options: { microModeScale: 40 }, clientArea: { getBoundingClientRect: () => ({ width: 1000, height: 800 }) } }
    }
  } as unknown as KetcherInstance;
  return { ketcher, zoom: () => current, centerStruct };
}

describe("enlargeImportedStructure", () => {
  it("把小结构放大到画板的可读比例，并居中", () => {
    const view = fakeKetcher({ w: 3, h: 2 });
    enlargeImportedStructure(view.ketcher);
    // 0.4 * 1000 / (3 * 40) = 3.33 and 0.45 * 800 / (2 * 40) = 4.5, both capped at 2.
    expect(view.zoom()).toBe(2);
    expect(view.centerStruct).toHaveBeenCalledOnce();
  });

  it("按较紧的方向取比例", () => {
    const view = fakeKetcher({ w: 8, h: 2 });
    enlargeImportedStructure(view.ketcher);
    expect(view.zoom()).toBeCloseTo((0.4 * 1000) / (8 * 40));
  });

  it("不缩小已经足够大的结构", () => {
    const view = fakeKetcher({ w: 30, h: 20 });
    enlargeImportedStructure(view.ketcher);
    expect(view.zoom()).toBe(1);
    expect(view.centerStruct).not.toHaveBeenCalled();
  });

  it("缺少渲染信息时不做任何事", () => {
    expect(() => enlargeImportedStructure({ editor: {} } as unknown as KetcherInstance)).not.toThrow();
  });
});
