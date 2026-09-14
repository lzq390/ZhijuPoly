// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { NativeEditorSession, type NativeKetcher } from "./nativeSession";
import { StructureWorkspace } from "./workspace";
import { createKetcherAdapter } from "./editor";

const empty = JSON.stringify({ root: { nodes: [] } });
const filled = JSON.stringify({ root: { nodes: [{ $ref: "mol0" }] }, mol0: { atoms: [{ label: "C" }] } });
function fixture() {
  const failures = new Set<() => void>();
  const changes = new Set<() => void>();
  let document = empty;
  const sdk: NativeKetcher = {
    retire: vi.fn(() => { sdk.disposed = true; }),
    getSmiles: vi.fn(async () => document === empty ? "" : "C"),
    getKet: vi.fn(async () => document),
    setMolecule: vi.fn(async value => { document = value ? filled : empty; }),
    changeEvent: { add: listener => changes.add(listener), remove: listener => changes.delete(listener) },
    eventBus: { on: (_event, listener) => failures.add(listener), off: (_event, listener) => failures.delete(listener) }
  };
  const fatal = vi.fn();
  const session = new NativeEditorSession(fatal);
  const service = { disposed: false, destroy: vi.fn(() => { service.disposed = true; }) };
  session.ownService(service);
  const editor = session.attach(sdk);
  return { sdk, session, service, editor, fatal, failures };
}

describe("native editor session boundaries", () => {
  it("does not commit a KET decode that finishes after its owner expires", async () => {
    let current = true;
    let finish!: () => void;
    const { sdk, session } = fixture();
    const commit = vi.fn();
    const decoded = { initHalfBonds: vi.fn(), initNeighbors: vi.fn(), setImplicitHydrogen: vi.fn(),
      setStereoLabelsToAtoms: vi.fn(), markFragments: vi.fn() };
    sdk.editor = { struct: commit };
    sdk.formatterFactory = { create: () => ({ getStructureFromStringAsync: () => new Promise(resolve => { finish = () => resolve(decoded); }) }) };
    const editor = createKetcherAdapter(sdk, undefined, () => current);
    const pending = editor.setMolecule(filled);
    current = false;
    finish();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(commit).not.toHaveBeenCalled();
    expect(sdk.setMolecule).not.toHaveBeenCalled();
    session.dispose();
  });
  it("rejects a swallowed SDK failure and removes its temporary subscription", async () => {
    const { sdk, editor, failures, session } = fixture();
    vi.mocked(sdk.setMolecule!).mockImplementation(async () => { failures.forEach(listener => listener()); });
    await expect(editor.setMolecule("CC")).rejects.toThrow();
    expect(failures.size).toBe(0);
    session.dispose();
  });

  it("rejects malformed exports and nonempty imports that leave an empty canvas", async () => {
    const { sdk, editor, session } = fixture();
    vi.mocked(sdk.getKet!).mockResolvedValueOnce("{}");
    await expect(editor.getKet()).rejects.toThrow("KET");
    vi.mocked(sdk.setMolecule!).mockResolvedValue(undefined);
    await expect(editor.setMolecule("CC")).rejects.toThrow("有效画板");
    await expect(editor.clear()).resolves.toBeUndefined();
    session.dispose();
  });

  it("settles in-flight adapter reads immediately on retirement and ignores late results", async () => {
    const { sdk, editor, service, session } = fixture();
    let resolve!: (value: string) => void;
    vi.mocked(sdk.getSmiles).mockReturnValue(new Promise(done => { resolve = done; }));
    const pending = editor.getSmiles();
    const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    session.dispose();
    await rejected;
    resolve("LATE");
    await expect(editor.getSmiles()).rejects.toMatchObject({ name: "AbortError" });
    session.dispose();
    expect(service.destroy).toHaveBeenCalledOnce();
    expect(sdk.retire).toHaveBeenCalledOnce();
  });

  it("turns a terminal service error into a recoverable editor error", async () => {
    const { sdk, editor, service, session, fatal } = fixture();
    vi.mocked(sdk.getSmiles).mockImplementation(async () => { service.disposed = true; throw new Error("worker failed"); });
    await expect(editor.getSmiles()).rejects.toThrow("worker failed");
    expect(session.signal.aborted).toBe(true);
    expect(fatal).toHaveBeenCalledOnce();
  });

  it("retires the service synchronously on navigation timeout and keeps the last good document", async () => {
    const { editor, sdk, service } = fixture();
    const workspace = new StructureWorkspace();
    const lease = workspace.mountEditor();
    await lease.initialize(editor);
    await editor.setMolecule("C");
    await workspace.saveSnapshot();
    workspace.setDraft("CC(", "Invalid draft");
    workspace.recoverForNavigation("timeout");
    expect(service.destroy).toHaveBeenCalledOnce();
    expect(sdk.retire).toHaveBeenCalledOnce();
    expect(workspace.getSnapshot()).toMatchObject({ smiles: "C", draft: "CC(", status: "loading", mountKey: 1 });
    lease.dispose();
  });

  it("destroys a service created after an initialization was cancelled", () => {
    const { session } = fixture();
    session.dispose();
    const late = { destroy: vi.fn() };
    expect(() => session.ownService(late)).toThrow("会话已结束");
    expect(late.destroy).toHaveBeenCalledOnce();
  });

  it("navigation can retire initialization before SDK onInit provides a handle", () => {
    const workspace = new StructureWorkspace("CC");
    const lease = workspace.mountEditor();
    const session = new NativeEditorSession(vi.fn());
    const service = { destroy: vi.fn() };
    session.ownService(service);
    lease.onRetire(session.dispose);
    workspace.recoverForNavigation("timeout");
    expect(session.signal.aborted).toBe(true);
    expect(service.destroy).toHaveBeenCalledOnce();
    lease.dispose();
  });

  it("keeps polymer wildcard SMILES stable when Indigo adds empty CX atom labels", async () => {
    const { sdk, editor, session } = fixture();
    vi.mocked(sdk.getSmiles).mockResolvedValueOnce("*CC* |$;;;$|").mockResolvedValueOnce("*CC* |$R1;;;R2$|");
    expect(await editor.getSmiles()).toBe("*CC*");
    // Do not silently discard meaningful extensions or stereochemistry.
    expect(await editor.getSmiles()).toBe("*CC* |$R1;;;R2$|");
    session.dispose();
  });
});
