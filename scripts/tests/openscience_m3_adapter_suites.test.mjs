import assert from "node:assert/strict";
import test from "node:test";
import {verifyAdapterSuites} from "../ci/openscience_m3_adapter_suites.mjs";

test("a failed adapter keeps its partial evidence and a second adapter can finish with both owners", async () => {
  const report = {adapters:{}}, calls = [];
  await assert.rejects(verifyAdapterSuites(report, ["polytao", "retrosynthesis"], (kind, evidence) => {
    calls.push(kind);
    evidence.a = {charged:1};
    if (kind === "polytao") throw Error("private draft read timed out");
    evidence.b = {charged:1};
  }, () => assert.fail("A failed adapter cannot pass combined reconciliation")), /polytao: private draft/);
  assert.deepEqual(calls, ["polytao", "retrosynthesis"]);
  assert.deepEqual(report.adapters.polytao.a, {charged:1});
  assert.equal(report.adapters.polytao.status, "failed");
  assert.deepEqual(report.adapters.retrosynthesis, {status:"passed", a:{charged:1}, b:{charged:1}});
  assert.equal(report.ledger, undefined);
});

test("both successful adapters require database-wide reconciliation exactly once", async () => {
  const report = {adapters:{}}, calls = [];
  await verifyAdapterSuites(report, ["polytao", "retrosynthesis"], kind => calls.push(kind), () => {
    calls.push("combined ledger");
    return {new_native_operations:4};
  });
  assert.deepEqual(calls, ["polytao","retrosynthesis","combined ledger"]);
  assert.equal(report.ledger.new_native_operations, 4);
  await assert.rejects(verifyAdapterSuites({adapters:{}}, ["polytao","retrosynthesis"], () => {},
    () => {throw Error("unexpected fifth charge")}), /unexpected fifth charge/);
});

test("an explicit adapter run reports only the selected module's two owners", async () => {
  const report = {adapters:{}};
  await verifyAdapterSuites(report, ["retrosynthesis"], (kind, evidence) => {
    assert.equal(kind, "retrosynthesis");
    evidence.ledger = {new_native_operations:2,users:{a:{},b:{}}};
  }, () => report.adapters.retrosynthesis.ledger);
  assert.deepEqual(Object.keys(report.adapters), ["retrosynthesis"]);
  assert.equal(report.ledger.new_native_operations, 2);
});
