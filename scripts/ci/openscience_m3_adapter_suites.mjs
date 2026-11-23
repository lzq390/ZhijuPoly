// Keep evidence from both independent adapters. The complete run still needs a single database-wide reconciliation.
export async function verifyAdapterSuites(report, kinds, verify, reconcile) {
  const failures = [];
  for (const kind of kinds) {
    const evidence = report.adapters[kind] = { status: "failed" };
    try {
      await verify(kind, evidence);
      evidence.status = "passed";
    } catch (error) {
      evidence.failure = String(error.message ?? error).replace(/np_page=[^ &"]+/g, "np_page=[redacted]");
      failures.push(kind + ": " + evidence.failure);
    }
    console.log(kind, evidence.status, evidence.failure ?? "");
  }
  if (failures.length) throw new Error(failures.join("\n"));
  report.ledger = await reconcile();
}
