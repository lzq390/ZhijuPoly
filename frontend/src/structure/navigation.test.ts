// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { waitForGuard } from "../hooks/useGuardedNavigation";
import { syncStructureForNavigation } from "./navigation";
import { StructureWorkspace, type StructureSyncResult } from "./workspace";

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe("structure navigation transaction", () => {
  it("cancels the navigation wait immediately without retiring the retained editor or rolling back later edits", async () => {
    vi.useFakeTimers();
    const workspace = new StructureWorkspace("CC");
    const lease = workspace.mountEditor();
    const current = workspace.guard();
    const recover = vi.spyOn(workspace, "recoverForNavigation");
    let release!: (value: StructureSyncResult) => void;
    let saving!: Promise<void>;
    const wait = waitForGuard(signal => saving = syncStructureForNavigation(workspace,
      () => new Promise(resolve => { release = resolve; }), signal));
    wait.cancel();
    expect(await wait.promise).toBe(false);
    await saving;
    workspace.commitSmiles("CO");
    await vi.advanceTimersByTimeAsync(1900);
    release({ status: "failed" });
    await Promise.resolve();
    expect(recover).not.toHaveBeenCalled();
    expect(current()).toBe(true);
    expect(workspace.getSnapshot()).toMatchObject({ smiles: "CO", notice: null, mountKey: 0 });
    expect(vi.getTimerCount()).toBe(0);
    lease.dispose();
  });

  it("retires the old session at the single 1.5s deadline before permitting the route commit", async () => {
    vi.useFakeTimers();
    const workspace = new StructureWorkspace("CC");
    const lease = workspace.mountEditor();
    const current = workspace.guard();
    const retire = vi.fn();
    lease.onRetire(retire);
    workspace.commitSmiles("CO");
    workspace.setDraft("CO");
    const recover = vi.spyOn(workspace, "recoverForNavigation");
    const wait = waitForGuard(signal => syncStructureForNavigation(workspace, () => new Promise(() => {}), signal));
    const committed = wait.promise.then(allowed => {
      expect(allowed).toBe(true);
      expect(current()).toBe(false);
      expect(retire).toHaveBeenCalledOnce();
      expect(workspace.getSnapshot()).toMatchObject({ smiles: "CC", draft: "CO" });
    });
    expect(vi.getTimerCount()).toBe(1);
    await vi.advanceTimersByTimeAsync(1499);
    expect(recover).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    await committed;
    expect(recover).toHaveBeenCalledExactlyOnceWith("timeout");
    expect(workspace.getSnapshot().notice).toContain("最新修改未能同步");
    expect(vi.getTimerCount()).toBe(0);
    lease.dispose();
  });

  it.each(["failed", "timeout"] as const)("an old owner's %s cannot recover a newly mounted editor", async reason => {
    vi.useFakeTimers();
    const workspace = new StructureWorkspace("CC");
    const old = workspace.mountEditor();
    const recover = vi.spyOn(workspace, "recoverForNavigation");
    let release!: (value: StructureSyncResult) => void;
    const wait = waitForGuard(signal => syncStructureForNavigation(workspace,
      () => new Promise(resolve => { release = resolve; }), signal));
    old.dispose();
    const next = workspace.mountEditor();
    const current = workspace.guard();
    if (reason === "failed") release({ status: "failed" });
    else await vi.advanceTimersByTimeAsync(1500);
    expect(await wait.promise).toBe(true);
    expect(recover).not.toHaveBeenCalled();
    expect(current()).toBe(true);
    expect(workspace.getSnapshot().notice).toBeNull();
    next.dispose();
  });

  it("a successful save removes the deadline and a later cancellation cannot undo it", async () => {
    vi.useFakeTimers();
    const workspace = new StructureWorkspace("CC");
    const recover = vi.spyOn(workspace, "recoverForNavigation");
    const wait = waitForGuard(signal => syncStructureForNavigation(workspace, async () => ({ status: "saved" }), signal));
    expect(await wait.promise).toBe(true);
    wait.cancel();
    await vi.advanceTimersByTimeAsync(2000);
    expect(recover).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("recovers a real save failure once and still releases navigation", async () => {
    const workspace = new StructureWorkspace("CC");
    const lease = workspace.mountEditor();
    workspace.commitSmiles("CO");
    const recover = vi.spyOn(workspace, "recoverForNavigation");
    const wait = waitForGuard(signal => syncStructureForNavigation(workspace, async () => ({ status: "failed" }), signal));
    expect(await wait.promise).toBe(true);
    expect(recover).toHaveBeenCalledExactlyOnceWith("failed");
    expect(workspace.getSnapshot()).toMatchObject({ smiles: "CC", mountKey: 1 });
    lease.dispose();
  });
});
