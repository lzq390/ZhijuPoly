// @vitest-environment node
import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { URL as NodeURL } from "node:url";

// The canvas toolbar's Zoom in / Zoom out items run Ketcher's own zoom actions.
// Exercise the installed, patched action table of both module formats.
type PointerEvent = { altKey: boolean };
type ZoomEditor = {
  lastEvent?: PointerEvent;
  zoom(value?: number, event?: unknown): number;
  tool(): { mousemove(event?: PointerEvent): void };
};
type ZoomActions = {
  zoom: { action(editor: ZoomEditor): void };
  "zoom-in": { action(event?: unknown): (editor: ZoomEditor) => void };
  "zoom-out": { action(event?: unknown): (editor: ZoomEditor) => void };
};

function zoomActions(bundle: string) {
  const source = readFileSync(new NodeURL(`../../node_modules/ketcher-react/dist/${bundle}`, import.meta.url), "utf8");
  const start = source.indexOf("var zoomList = [");
  const end = source.indexOf("\nvar openHelpLink = ", start);
  if (start < 0 || end < 0) throw new Error("Locate the zoom actions after updating ketcher-react");
  return new Function(`
    const findIndex = (predicate, list) => list.findIndex(value => predicate(value));
    const findLastIndex = (predicate, list) => list.findLastIndex(value => predicate(value));
    const fp = { findIndex, findLastIndex };
    const isHidden = () => false;
    ${source.slice(start, end)}
    return zoom;
  `)() as ZoomActions;
}

// Like Ketcher's select tool, the active tool reads modifier keys from the
// pointer event that each zoom action replays to refresh the hover.
function editorAt(level: number, lastEvent?: PointerEvent) {
  const mousemove = vi.fn((event?: PointerEvent) => { void event!.altKey; });
  const editor: ZoomEditor = {
    lastEvent,
    zoom(...args: unknown[]) {
      if (args.length) level = args[0] as number;
      return level;
    },
    tool: () => ({ mousemove })
  };
  return { editor, mousemove };
}

describe.each(["index.js", "cjs/index.js"])("Ketcher %s zoom actions", (bundle) => {
  const actions = zoomActions(bundle);

  // The editor only receives pointer events that land on its own canvas, so a
  // user who loads a structure from the SMILES box and opens the zoom menu has
  // produced none yet.
  it("step the zoom before any pointer event has reached the canvas", () => {
    const { editor, mousemove } = editorAt(1);
    actions["zoom-in"].action()(editor);
    expect(editor.zoom()).toBe(1.1);
    actions["zoom-in"].action()(editor);
    expect(editor.zoom()).toBe(1.2);
    actions["zoom-out"].action()(editor);
    expect(editor.zoom()).toBe(1.1);
    actions.zoom.action(editor);
    expect(editor.zoom()).toBe(1);
    expect(mousemove).not.toHaveBeenCalled();
  });

  it("step from a fitted level that is not on Ketcher's zoom list", () => {
    for (const [fitted, zoomedIn, zoomedOut] of [[0.73, 0.8, 0.7], [1.94, 2, 1.7]]) {
      const zoomIn = editorAt(fitted);
      actions["zoom-in"].action()(zoomIn.editor);
      expect(zoomIn.editor.zoom()).toBe(zoomedIn);
      const zoomOut = editorAt(fitted);
      actions["zoom-out"].action()(zoomOut.editor);
      expect(zoomOut.editor.zoom()).toBe(zoomedOut);
    }
  });

  it("still refresh the hover under a pointer that has reached the canvas", () => {
    const pointer = { altKey: false };
    const { editor, mousemove } = editorAt(1, pointer);
    actions["zoom-in"].action()(editor);
    actions["zoom-out"].action()(editor);
    actions.zoom.action(editor);
    expect(editor.zoom()).toBe(1);
    expect(mousemove.mock.calls).toEqual([[pointer], [pointer], [pointer]]);
  });
});
