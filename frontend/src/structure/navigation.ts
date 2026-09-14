import type { StructureSyncResult, StructureWorkspace } from "./workspace";

/** The navigation guard owns the only deadline. Cancelling its wait does not
 * cancel a shared autosave or a text edit that still belongs to this page. */
export function syncStructureForNavigation(
  workspace: StructureWorkspace,
  save: () => Promise<StructureSyncResult>,
  signal: AbortSignal
): Promise<void> {
  const current = workspace.guard();
  return new Promise((resolve) => {
    let settled = false;
    const finish = (result?: StructureSyncResult) => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", abort);
      if (result && result.status !== "saved" && current()) {
        workspace.recoverForNavigation(result.status === "timeout" ? "timeout" : "failed");
      }
      resolve();
    };
    const abort = () => finish(signal.reason?.name === "TimeoutError" ? { status: "timeout" } : undefined);
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) { abort(); return; }
    try { save().then(finish, () => finish({ status: "failed" })); }
    catch { finish({ status: "failed" }); }
  });
}
