// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StructureEditorHandle } from "./editor";
import { createKetcherAdapter } from "./editor";
import { StructureWorkspace } from "./workspace";

function ket(smiles: string, x = 1, text = "") {
  return JSON.stringify({ root: { nodes: smiles || text ? [{ $ref: "mol0" }] : [] },
    mol0: { atoms: smiles ? [{ label: "C", location: [x, 0, 0], selected: true }] : [], text }, smiles });
}
function editorFixture() {
  let smiles = "";
  let document = ket("");
  const listeners = new Set<() => void>();
  const editor: StructureEditorHandle = {
    getSmiles: vi.fn(async () => smiles),
    getKet: vi.fn(async () => document),
    getMolfile: vi.fn(async () => "molfile"),
    setMolecule: vi.fn(async (source) => {
      if (source.startsWith("{")) { document = source; smiles = JSON.parse(source).smiles; }
      else { smiles = source; document = ket(source); }
      listeners.forEach((listener) => listener());
    }),
    clear: vi.fn(async () => {
      smiles = ""; document = ket(""); listeners.forEach((listener) => listener());
    }),
    generateImage: vi.fn(async () => new Blob()),
    subscribeChange: vi.fn((listener) => {
      listeners.add(listener); return () => { listeners.delete(listener); };
    }),
    settle: async () => {}
  };
  return { editor, listeners,
    edit(nextSmiles: string, x = 1, text = "") {
      smiles = nextSmiles; document = ket(smiles, x, text); listeners.forEach((listener) => listener());
    }
  };
}
async function mounted(smiles = "CC", standardize?: (smiles: string) => Promise<string>) {
  const workspace = new StructureWorkspace(smiles, standardize);
  const fixture = editorFixture();
  const lease = workspace.mountEditor();
  await lease.initialize(fixture.editor);
  await workspace.saveSnapshot();
  return { workspace, lease, ...fixture };
}
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe("shared structure documents", () => {
  it("leaves a restoring owner with the saved layout and invalid draft without exporting or remounting", async () => {
    const { workspace, lease, editor, edit } = await mounted();
    edit("CC", 19, "saved annotation");
    await workspace.saveSnapshot();
    lease.dispose();
    const next = workspace.mountEditor();
    workspace.setDraft("CC(", "invalid");
    const before = workspace.getSnapshot();
    vi.mocked(editor.getKet).mockClear();
    expect(await workspace.saveForNavigation()).toEqual({ status: "saved" });
    expect(workspace.getSnapshot()).toEqual(before);
    expect(workspace.getSnapshot().status).toBe("loading");
    expect(editor.getKet).not.toHaveBeenCalled();
    expect(workspace.getSnapshot().notice).toBeNull();
    next.dispose();
  });

  it("leaves a pristine loading document without confusing it with an unconfirmed clear", async () => {
    const workspace = new StructureWorkspace();
    const lease = workspace.mountEditor();
    expect(await workspace.saveForNavigation()).toEqual({ status: "saved" });
    expect(workspace.getSnapshot()).toMatchObject({ status: "loading", ket: null, notice: null, mountKey: 0 });
    workspace.commitSmiles("CC");
    expect((await workspace.saveForNavigation()).status).toBe("failed");
    lease.dispose();
  });

  it("preserves an externally validated write that arrives during destination initialization", async () => {
    const { workspace, lease } = await mounted();
    lease.dispose();
    const next = workspace.mountEditor();
    workspace.setSmiles("*CO*");
    await Promise.resolve();
    expect(await workspace.saveForNavigation()).toEqual({ status: "saved" });
    expect(workspace.getSnapshot()).toMatchObject({ smiles: "*CO*", ket: null, draft: "*CO*", notice: null });
    next.dispose();
    const fixture = editorFixture();
    const destination = workspace.mountEditor();
    await destination.initialize(fixture.editor);
    expect(await fixture.editor.getSmiles()).toBe("*CO*");
    destination.dispose();
  });

  it("retains a loading draft separately and clears its informational notice after acceptance", async () => {
    const workspace = new StructureWorkspace("CC");
    const lease = workspace.mountEditor();
    workspace.setDraft("CO");
    expect((await workspace.saveForNavigation()).status).toBe("saved");
    expect(workspace.getSnapshot()).toMatchObject({ smiles: "CC", draft: "CO", notice: "文本草稿已保留，画板就绪后将继续同步。" });
    workspace.setSmiles("CO");
    await Promise.resolve();
    expect(workspace.getSnapshot().notice).toBeNull();
    lease.dispose();
  });

  it("reuses a clean snapshot for navigation but captures a new layout at the same SMILES", async () => {
    const { workspace, editor, edit, lease } = await mounted();
    vi.mocked(editor.getKet).mockClear();
    vi.mocked(editor.getSmiles).mockClear();
    expect((await workspace.saveForNavigation()).status).toBe("saved");
    expect(editor.getKet).not.toHaveBeenCalled();
    edit("CC", 47, "new annotation");
    expect((await workspace.saveForNavigation()).status).toBe("saved");
    expect(editor.getKet).toHaveBeenCalledOnce();
    expect(editor.getSmiles).toHaveBeenCalledOnce();
    expect(JSON.parse(workspace.getSnapshot().ket!).mol0).toMatchObject({ text: "new annotation", atoms: [{ location: [47, 0, 0] }] });
    lease.dispose();
  });

  it("shares an in-flight autosave with navigation without duplicate SDK exports", async () => {
    vi.useFakeTimers();
    const { workspace, editor, edit, lease } = await mounted();
    let release!: (value: string) => void;
    vi.mocked(editor.getKet).mockClear().mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
    vi.mocked(editor.getSmiles).mockClear();
    edit("CO", 31, "latest layout");
    await vi.advanceTimersByTimeAsync(300);
    const first = workspace.saveSnapshot();
    const navigation = workspace.saveForNavigation();
    expect(navigation).toBe(first);
    expect(editor.getKet).toHaveBeenCalledOnce();
    expect(editor.getSmiles).toHaveBeenCalledOnce();
    release(ket("CO", 31, "latest layout"));
    expect((await navigation).status).toBe("saved");
    expect(workspace.getSnapshot()).toMatchObject({ smiles: "CO", dirty: false });
    lease.dispose();
  });

  it("does not reuse a snapshot while a mutation has begun but has not emitted an edit", async () => {
    vi.useFakeTimers();
    const { workspace, editor, edit, lease } = await mounted();
    const finish = workspace.beginMutation();
    let complete = false;
    const navigation = workspace.saveForNavigation().then(result => { complete = true; return result; });
    await vi.advanceTimersByTimeAsync(200);
    expect(complete).toBe(false);
    edit("");
    finish();
    await vi.advanceTimersByTimeAsync(40);
    expect((await navigation).status).toBe("saved");
    expect(await editor.getSmiles()).toBe("");
    expect(workspace.getSnapshot()).toMatchObject({ smiles: "", dirty: false });
    lease.dispose();
  });

  it("still establishes the first full empty snapshot through autosave", async () => {
    vi.useFakeTimers();
    const workspace = new StructureWorkspace();
    const fixture = editorFixture();
    const lease = workspace.mountEditor();
    await lease.initialize(fixture.editor);
    await vi.advanceTimersByTimeAsync(300);
    expect(workspace.getSnapshot().ket).not.toBeNull();
    expect(JSON.parse(workspace.getSnapshot().ket!).root.nodes).toEqual([]);
    lease.dispose();
  });

  it("saves layout-only edits and restores KET including labels across owners", async () => {
    vi.useFakeTimers();
    const { workspace, editor, edit, lease, listeners } = await mounted();
    edit("CC", 17, "repeat unit");
    expect(workspace.getSnapshot().dirty).toBe(true);
    await vi.advanceTimersByTimeAsync(299);
    expect(JSON.parse(workspace.getSnapshot().ket!).mol0.atoms[0].location[0]).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    const saved = workspace.getSnapshot();
    expect(saved.smiles).toBe("CC");
    expect(JSON.parse(saved.ket!).mol0).toEqual({ atoms: [{ label: "C", location: [17, 0, 0] }], text: "repeat unit" });
    lease.dispose();
    expect(listeners.size).toBe(0);
    const next = workspace.mountEditor();
    await next.initialize(editor);
    expect(editor.setMolecule).toHaveBeenLastCalledWith(saved.ket);
    next.dispose();
  });

  it("does not persist transient empty KET alongside populated SMILES", async () => {
    vi.useFakeTimers();
    const { workspace, editor, lease } = await mounted();
    vi.mocked(editor.getKet).mockResolvedValueOnce(ket(""));
    const pending = workspace.saveSnapshot();
    await vi.advanceTimersByTimeAsync(80);
    expect(await pending).toEqual({ status: "saved" });
    expect(workspace.getSnapshot().smiles).toBe("CC");
    expect(JSON.parse(workspace.getSnapshot().ket!).root.nodes).toHaveLength(1);
    lease.dispose();
  });

  it("retries a read if the document changed during export", async () => {
    vi.useFakeTimers();
    const { workspace, editor, edit, lease } = await mounted();
    let release!: (ket: string) => void;
    vi.mocked(editor.getKet).mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
    const pending = workspace.saveSnapshot();
    edit("CO", 23);
    release(ket("CC"));
    await vi.advanceTimersByTimeAsync(80);
    expect((await pending).status).toBe("saved");
    expect(workspace.getSnapshot().smiles).toBe("CO");
    expect(JSON.parse(workspace.getSnapshot().ket!).mol0.atoms[0].location[0]).toBe(23);
    lease.dispose();
  });

  it("retries transient empty SMILES without discarding populated KET", async () => {
    vi.useFakeTimers();
    const { workspace, editor, lease } = await mounted();
    vi.mocked(editor.getSmiles).mockResolvedValueOnce("");
    const pending = workspace.saveSnapshot();
    await vi.advanceTimersByTimeAsync(80);
    expect((await pending).status).toBe("saved");
    expect(workspace.getSnapshot().smiles).toBe("CC");
    lease.dispose();
  });

  it("does not interpret an empty export without a change event as deletion", async () => {
    vi.useFakeTimers();
    const { workspace, editor, lease } = await mounted();
    const previous = workspace.getSnapshot().ket;
    vi.mocked(editor.getSmiles).mockResolvedValue("");
    vi.mocked(editor.getKet).mockResolvedValue(ket(""));
    const pending = workspace.saveSnapshot();
    await vi.advanceTimersByTimeAsync(800);
    expect((await pending).status).toBe("failed");
    expect(workspace.getSnapshot()).toMatchObject({ smiles: "CC", ket: previous });
    lease.dispose();
  });

  it("rejects a late save from an unmounted owner", async () => {
    const { workspace, editor, lease } = await mounted();
    let release!: (ket: string) => void;
    vi.mocked(editor.getKet).mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
    const pending = workspace.saveSnapshot();
    lease.dispose();
    workspace.commitSmiles("CO");
    release(ket("CC"));
    expect((await pending).status).toBe("failed");
    expect(workspace.getSnapshot().smiles).toBe("CO");
    expect(workspace.getSnapshot().ket).toBeNull();
  });

  it("navigation fallback restores a coherent snapshot, keeps draft, and expires the editor", async () => {
    const { workspace, editor, edit, lease, listeners } = await mounted();
    edit("CC", 3);
    await workspace.saveSnapshot();
    const saved = workspace.getSnapshot().ket;
    const oldEditor = workspace.getEditor()!;
    edit("CO", 9);
    workspace.setDraft("unfinished(", "SMILES 无效");
    const key = workspace.getSnapshot().mountKey;
    workspace.recoverForNavigation("timeout");
    expect(workspace.getSnapshot()).toMatchObject({ smiles: "CC", ket: saved, draft: "unfinished(", mountKey: key + 1 });
    expect(workspace.getSnapshot().notice).toContain("上一次保存");
    expect((await workspace.saveForNavigation()).status).toBe("saved");
    expect(workspace.getSnapshot().mountKey).toBe(key + 1);
    expect(listeners.size).toBe(0);
    await expect(oldEditor.setMolecule("CCC")).rejects.toMatchObject({ name: "AbortError" });
    expect(editor.setMolecule).not.toHaveBeenCalledWith("CCC");
    lease.dispose();
  });

  it("explicit deletion saves an empty document and cannot resurrect old SMILES", async () => {
    const { workspace, edit, editor, lease } = await mounted();
    edit("");
    await workspace.saveSnapshot();
    expect(workspace.getSnapshot().smiles).toBe("");
    lease.dispose();
    const next = workspace.mountEditor();
    await next.initialize(editor);
    expect(await editor.getSmiles()).toBe("");
    next.dispose();
  });

  it("reports when navigation has no recoverable snapshot without discarding the draft", () => {
    const workspace = new StructureWorkspace();
    workspace.setDraft("CC(", "invalid");
    workspace.recoverForNavigation("timeout");
    expect(workspace.getSnapshot()).toMatchObject({ smiles: "", ket: null, draft: "CC(" });
    expect(workspace.getSnapshot().notice).toContain("暂无可恢复");
  });

  it("retries an initial import that resolves before the document is available", async () => {
    vi.useFakeTimers();
    const workspace = new StructureWorkspace("CC");
    const { editor } = editorFixture();
    vi.mocked(editor.setMolecule).mockImplementationOnce(async () => {});
    const lease = workspace.mountEditor();
    const pending = lease.initialize(editor);
    await vi.advanceTimersByTimeAsync(2500);
    await pending;
    expect(workspace.getSnapshot().status).toBe("ready");
    expect(editor.setMolecule).toHaveBeenCalledTimes(2);
    expect(await editor.getSmiles()).toBe("CC");
    lease.dispose();
  });

  it("a failed export preserves the previous document, including its KET", async () => {
    const { workspace, editor, lease } = await mounted();
    const before = workspace.getSnapshot();
    vi.mocked(editor.getSmiles).mockRejectedValueOnce(new Error("export failed"));
    expect((await workspace.saveSnapshot()).status).toBe("failed");
    expect(workspace.getSnapshot()).toMatchObject({ smiles: before.smiles, ket: before.ket });
    lease.dispose();
  });

  it("programmatic mutations suppress autosave until complete", async () => {
    vi.useFakeTimers();
    const { workspace, edit, editor, lease } = await mounted();
    vi.mocked(editor.getKet).mockClear();
    const finish = workspace.beginMutation();
    edit("");
    await vi.advanceTimersByTimeAsync(600);
    expect(editor.getKet).not.toHaveBeenCalled();
    edit("CO", 4);
    workspace.commitSmiles("CO");
    finish();
    await vi.advanceTimersByTimeAsync(300);
    expect(workspace.getSnapshot()).toMatchObject({ smiles: "CO", dirty: false });
    lease.dispose();
  });

  it("external text invalidates old KET only after validation, and ignores superseded responses", async () => {
    const complete = new Map<string, (value: string) => void>();
    const { workspace, lease } = await mounted("CC", (source) => new Promise((resolve) => { complete.set(source, resolve); }));
    lease.dispose();
    const saved = workspace.getSnapshot().ket;
    workspace.setSmiles("CO");
    workspace.setSmiles("CN");
    expect(workspace.getSnapshot()).toMatchObject({ smiles: "CC", ket: saved, draft: "CN" });
    complete.get("CN")!("CN");
    await Promise.resolve();
    complete.get("CO")!("CO");
    await Promise.resolve();
    expect(workspace.getSnapshot()).toMatchObject({ smiles: "CN", ket: null, draft: "CN" });
  });

  it("invalid external text preserves valid canvas and remains a visible draft", async () => {
    const { workspace, lease } = await mounted("CC", async () => { throw new Error("invalid"); });
    lease.dispose();
    const saved = workspace.getSnapshot().ket;
    workspace.setSmiles("CC(");
    await Promise.resolve();
    expect(workspace.getSnapshot()).toMatchObject({ smiles: "CC", ket: saved, draft: "CC(" });
    expect(workspace.getSnapshot().draftError).toContain("无效");
  });

  it("accepts a pending external write across destination mounting and fences it after an edit", async () => {
    let release!: (smiles: string) => void;
    const workspace = new StructureWorkspace("CC", () => new Promise((resolve) => { release = resolve; }));
    workspace.setSmiles("CO");
    const fixture = editorFixture();
    const lease = workspace.mountEditor();
    await lease.initialize(fixture.editor);
    release("CO");
    await Promise.resolve();
    expect(workspace.getSnapshot()).toMatchObject({ smiles: "CO", ket: null, draft: "CO" });
    lease.dispose();
    const next = workspace.mountEditor();
    await next.initialize(fixture.editor);
    workspace.setSmiles("CN");
    fixture.edit("CCC");
    release("CN");
    await Promise.resolve();
    expect(workspace.getSnapshot().smiles).toBe("CO");
    await workspace.saveSnapshot();
    expect(workspace.getSnapshot().smiles).toBe("CCC");
    next.dispose();
  });

  it("expires pending exports and their timeout timers on disposal", async () => {
    vi.useFakeTimers();
    const { workspace, editor, lease } = await mounted();
    vi.mocked(editor.getKet).mockImplementation(() => new Promise(() => {}));
    const pending = workspace.saveSnapshot();
    lease.dispose();
    expect((await pending).status).toBe("failed");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("retires a timed-out write before later input can reuse its SDK instance", async () => {
    vi.useFakeTimers();
    const { workspace, editor, lease, listeners } = await mounted();
    const previous = workspace.getSnapshot();
    const old = workspace.getEditor()!;
    vi.mocked(editor.setMolecule).mockImplementationOnce(() => new Promise(() => {}));
    const pending = expect(old.setMolecule("CO")).rejects.toMatchObject({ name: "TimeoutError" });
    await vi.advanceTimersByTimeAsync(8000);
    await pending;
    expect(workspace.getSnapshot()).toMatchObject({ smiles: previous.smiles, ket: previous.ket, mountKey: previous.mountKey + 1 });
    expect(listeners.size).toBe(0);
    await expect(old.setMolecule("CN")).rejects.toMatchObject({ name: "AbortError" });
    lease.dispose();
  });

  it("keeps one subscription through ten mount/unmount cycles", async () => {
    const workspace = new StructureWorkspace("CC");
    const { editor, listeners } = editorFixture();
    for (let cycle = 0; cycle < 10; ++cycle) {
      const lease = workspace.mountEditor();
      await lease.initialize(editor);
      expect(listeners.size).toBe(1);
      lease.dispose();
      expect(listeners.size).toBe(0);
    }
  });

  it("protects polymer end groups on the shared consumer path", async () => {
    const { workspace, editor, lease } = await mounted("*CC*", async () => "CC");
    vi.mocked(editor.getSmiles).mockResolvedValue("CC");
    expect(await workspace.getCurrentSmiles()).toBe("*CC*");
    lease.dispose();
  });

  it("falls back from rejected KET to SMILES and retains a notice", async () => {
    const { workspace, lease } = await mounted();
    lease.dispose();
    const fixture = editorFixture();
    const set = fixture.editor.setMolecule;
    fixture.editor.setMolecule = vi.fn(async (value) => {
      if (value.startsWith("{")) throw new Error("unsupported KET");
      await set(value);
    });
    const next = workspace.mountEditor();
    await next.initialize(fixture.editor);
    expect(workspace.getSnapshot().status).toBe("ready");
    expect(workspace.getSnapshot().notice).toContain("布局恢复失败");
    expect(await fixture.editor.getSmiles()).toBe("CC");
    next.dispose();
  });

  it("leaves the snapshot and draft intact when both restoration formats fail", async () => {
    const { workspace, lease } = await mounted();
    lease.dispose();
    const previous = workspace.getSnapshot().ket;
    workspace.setDraft("CC(", "invalid");
    const fixture = editorFixture();
    fixture.editor.setMolecule = vi.fn().mockRejectedValue(new Error("load failed"));
    const next = workspace.mountEditor();
    await next.initialize(fixture.editor);
    expect(workspace.getSnapshot()).toMatchObject({ status: "error", ket: previous, draft: "CC(" });
    next.dispose();
  });

  it("initialization after disposal cannot register listeners or mark a new owner ready", async () => {
    const workspace = new StructureWorkspace("CC");
    const fixture = editorFixture();
    let release!: () => void;
    fixture.editor.settle = () => new Promise((resolve) => { release = resolve; });
    const lease = workspace.mountEditor();
    const pending = lease.initialize(fixture.editor);
    await Promise.resolve(); await Promise.resolve();
    lease.dispose();
    release();
    await pending;
    expect(workspace.getSnapshot().status).toBe("unmounted");
    expect(fixture.listeners.size).toBe(0);
  });

  it("removes the exact adapter change handler on unsubscribe", () => {
    const handlers = new Set<() => void>();
    const adapter = createKetcherAdapter({ getSmiles: async () => "", changeEvent: {
      add: (handler) => handlers.add(handler), remove: (handler) => handlers.delete(handler)
    } });
    const listener = vi.fn();
    const off = adapter.subscribeChange(listener);
    handlers.forEach((handler) => handler());
    off();
    handlers.forEach((handler) => handler());
    expect(listener).toHaveBeenCalledOnce();
    expect(handlers.size).toBe(0);
  });

  it("uses explicit Molfile formats and rejects non-SMILES serializer output", async () => {
    const raw = {
      getSmiles: vi.fn(async () => "CC"),
      getMolfile: vi.fn(async () => "V2000 molfile")
    };
    const editor = createKetcherAdapter(raw);
    expect(await editor.getMolfile()).toBe("V2000 molfile");
    expect(raw.getMolfile).toHaveBeenCalledWith("v2000");
    raw.getMolfile.mockRejectedValueOnce(new Error("too many atoms")).mockResolvedValueOnce("V3000 molfile");
    expect(await editor.getMolfile()).toBe("V3000 molfile");
    expect(raw.getMolfile).toHaveBeenLastCalledWith("v3000");
    raw.getSmiles.mockResolvedValueOnce("header\n  1  0 V2000\nM END");
    await expect(editor.getSmiles()).rejects.toThrow("未返回有效的 SMILES");
    raw.getSmiles.mockResolvedValueOnce('{"root":{"nodes":[]}}');
    await expect(editor.getSmiles()).rejects.toThrow("未返回有效的 SMILES");
  });
});
