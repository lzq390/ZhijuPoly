// Use real platform logins, private November instances, MCP gateway and native business endpoints.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as sleep } from "node:timers/promises";
import { developmentBrowser } from "./openscience_dev_browser.mjs";

function option(name) {
  const i = process.argv.indexOf(name);
  assert.ok(i > 0 && process.argv[i + 1]);
  return resolve(process.argv[i + 1]);
}
const state = option("--state"),
  output = option("--output");
const stack = JSON.parse(
  await readFile(resolve(state, "running.json"), "utf8"),
);
assert.match(stack.gateway_image, /^sha256:[a-f0-9]{64}$/);
const { browser, device, app, login, logout, workbench } =
  await developmentBrowser(state, stack);
const report = {
  environment: "dev-m1",
  start: new Date().toISOString(),
  browser: browser.version(),
  gateway_base:
    "sha256:fb7fdd70e46d7544d0816f042bd0a4324cd7a6c1e86a4a4606031f5108199df0",
  gateway_image: stack.gateway_image,
  users: {},
  status: "failed",
};
const profiles = [];
const directory = "/openscience/files/general";
const ttl = 65000;
function ledger(extra = {}) {
  const path = fileURLToPath(
    new URL("openscience_m3_ledger.py", import.meta.url),
  );
  return JSON.parse(
    execFileSync("python3", [path], {
      input: JSON.stringify({
        pg: stack.pg,
        deployment: stack.deployment,
        ...extra,
      }),
      encoding: "utf8",
    }),
  );
}
async function client(tag) {
  const c = await device(),
    p = await app(c),
    name = "test-user-" + tag;
  const identity = await login(p, name);
  profiles.push({ p, name });
  const f = await workbench(p);
  return { f, p, c, tag, ...identity };
}
async function api(u, path, method = "GET", body) {
  return u.f.evaluate(
    async ({ path, method, body, directory }) => {
      const url = new URL("/openscience-api" + path, location.origin);
      url.searchParams.set("directory", directory);
      const r = await fetch(url, {
        method,
        ...(body
          ? {
              headers: { "content-type": "application/json" },
              body: JSON.stringify(body),
            }
          : {}),
      });
      const text = await r.text();
      return { status: r.status, value: text ? JSON.parse(text) : null };
    },
    { path, method, body, directory },
  );
}
async function turn(u, tool, args, { confirmFrom, expectError = false } = {}) {
  const path = "/session/" + u.conversation + "/message";
  const before = await api(u, path);
  assert.equal(before.status, 200);
  const previous = new Set(
    before.value.flatMap((m) =>
      m.parts.filter((p) => p.type === "tool").map((p) => p.id),
    ),
  );
  const response = api(u, path, "POST", {
    model: { providerID: "m0fixture", modelID: "m0" },
    parts: [
      {
        type: "text",
        text: "M3-TEST-CALL " + JSON.stringify({ tool, arguments: args }),
      },
    ],
  });
  if (tool === "dft_job_submit") {
    const deadline = Date.now() + 30000;
    let pending;
    while (Date.now() < deadline && !pending) {
      const permissions = await api(u, "/permission");
      pending = permissions.value?.find(
        (p) =>
          p.sessionID === u.conversation &&
          p.patterns.includes("capability_gateway_dft_job_submit"),
      );
      if (!pending) {
        const early = await Promise.race([
          response,
          sleep(200).then(() => null),
        ]);
        if (early)
          throw Error(
            "DFT turn ended before permission: " +
              early.status +
              " " +
              JSON.stringify(early.value?.info?.error ?? {}).slice(0, 300),
          );
      }
    }
    assert.ok(pending, "DFT must wait for permission");
    if (confirmFrom) {
      const cross = await api(
        confirmFrom,
        "/permission/" + pending.id + "/reply",
        "POST",
        { reply: "once" },
      );
      assert.equal(cross.status, 403);
      report.wrong_device_confirmation_status = cross.status;
    }
    assert.equal(
      (
        await api(u, "/permission/" + pending.id + "/reply", "POST", {
          reply: "once",
        })
      ).status,
      200,
    );
  }
  const r = await response;
  assert.equal(r.status, 200);
  const messages = (await api(u, path)).value;
  const part = messages
    .flatMap((m) => m.parts)
    .find(
      (p) =>
        p.type === "tool" &&
        p.tool === "capability_gateway_" + tool &&
        !previous.has(p.id),
    );
  if (!part)
    throw Error(
      "No persisted tool invocation for " +
        tool +
        "; assistant error: " +
        JSON.stringify(messages.at(-1)?.info?.error ?? {}).slice(0, 500),
    );
  const value = part.state.output || part.state.error || "";
  if (expectError) {
    assert.match(
      value,
      /^(?:Job not found|Native request refused \(HTTP 404\))$/,
      "expected the other test job to be private",
    );
  } else {
    if (part.state.status !== "completed" || !value.trim().startsWith("{"))
      throw Error("Tool " + tool + " failed: " + value.slice(0, 450));
  }
  u.calls.push({ tool, call_id: part.callID, allowed: !expectError });
  console.log(u.tag, tool, expectError ? "foreign refused" : "owner ok");
  return expectError ? null : JSON.parse(value);
}
async function download(u, job, artifact) {
  return u.p.evaluate(
    async ({ job, artifact }) => {
      const session = await (await fetch("/api/v1/auth/session")).json();
      const path =
        "/api/v1/monomer-dft/jobs/" +
        job +
        "/" +
        (artifact ? "artifacts/" + artifact : "bundle");
      const r = await fetch(path, {
        headers: { "X-Session-Context": session.session_id },
      });
      const bytes = await r.arrayBuffer();
      let archive;
      if (r.ok && !artifact) {
        if (bytes.byteLength > 2 * 1024 * 1024) throw Error("Unexpected development bundle size");
        archive = new Uint8Array(bytes).toBase64();
      }
      return {
        ...(archive ? { archive } : {}),
        status: r.status,
        size: r.ok ? bytes.byteLength : 0,
        sha256: r.ok
          ? Array.from(
              new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
              (v) => v.toString(16).padStart(2, "0"),
            ).join("")
          : null,
      };
    },
    { job, artifact },
  );
}
async function seedToolActivity(u, secondDevice) {
  u.calls = [];
  const created = await api(u, "/session", "POST", {
    title: "M3 six tools " + report.start,
  });
  assert.equal(created.status, 200);
  u.conversation = created.value.id;
  const p = await turn(u, "predict_polymer_properties", {
    smiles: u.tag === "a" ? "*CC*" : "*CCC*",
    properties: ["Glass transition temperature"],
  });
  assert.ok(Number.isFinite(p.predictions["Glass transition temperature"]));
  const poly = await turn(u, "polymerize_monomers", {
    monomer_a_smiles: u.tag === "a" ? "C=C" : "C=CC",
    target_class: "polyolefin",
    max_results: 2,
  });
  assert.ok(poly.results.length > 0);
  const candidates = poly.results.map((row) => row.polymer_smiles);
  assert.ok(
    candidates.every(
      (smiles) => typeof smiles === "string" && smiles.length > 0,
    ),
    "expected actual polymer SMILES for every returned candidate",
  );
  u.observed = {
    tg_c: p.predictions["Glass transition temperature"],
    polymer_smiles: candidates,
    polymer_total: poly.total,
  };
  const job = await turn(
    u,
    "dft_job_submit",
    {
      calculation_type: "single_point",
      smiles: u.tag === "a" ? "CCO" : "O",
      properties: ["energy", "forces"],
    },
    { confirmFrom: secondDevice },
  );
  assert.match(job.job_id, /^[0-9a-f-]{36}$/);
  u.job = job.job_id;
  report.users[u.tag] = {
    workspace_session: u.conversation,
    job_id: u.job,
    predicted_tg_c: p.predictions["Glass transition temperature"],
    polymer_candidates: poly.total,
  };
}
async function verifyReads(u, other) {
  const list = await turn(u, "dft_job_list", { page_size: 50 });
  assert.ok(list.jobs.some((j) => j.job_id === u.job));
  assert.ok(!list.jobs.some((j) => j.job_id === other.job));
  u.observed.dft_page = list.jobs.map((j) => j.job_id);
  u.observed.dft_total = list.total_count;
  assert.equal(
    list.jobs.length,
    Math.min(50, list.total_count),
    "DFT tool returned an incomplete first page",
  );
  let status;
  const deadline = Date.now() + ttl;
  do {
    status = await turn(u, "dft_job_status", { job_id: u.job });
    if (status.status === "completed") break;
    assert.ok(!["failed", "cancelled"].includes(status.status));
    await sleep(1000);
  } while (Date.now() < deadline);
  assert.equal(status.status, "completed");
  const result = await turn(u, "dft_job_result", { job_id: u.job });
  assert.equal(result.status, "completed");
  assert.ok(result.result && result.artifacts.length > 0);
  u.observed.fixture_energy = result.result.properties?.energy?.value_eV;
  assert.ok(
    Number.isFinite(u.observed.fixture_energy),
    "DFT tool omitted the single-point fixture energy",
  );
  assert.ok(
    result.download_hint.includes(stack.parent + "/monomer-dft?job=" + u.job),
  );
  await turn(u, "dft_job_status", { job_id: other.job }, { expectError: true });
  await turn(u, "dft_job_result", { job_id: other.job }, { expectError: true });
  const files = [];
  for (const f of result.artifacts) {
    const own = await download(u, u.job, f.id),
      cross = await download(other, u.job, f.id);
    assert.deepEqual(own, {
      status: 200,
      size: f.size_bytes,
      sha256: f.sha256,
    });
    assert.equal(cross.status, 404);
    files.push({
      name: f.name,
      bytes: own.size,
      sha256: own.sha256,
      other_http: cross.status,
    });
  }
  const bundle = await download(u, u.job),
    cross = await download(other, u.job);
  assert.equal(bundle.status, 200);
  assert.ok(bundle.size > 0);
  assert.equal(cross.status, 404);
  const { archive, ...bundleSummary } = bundle;
  u.downloads = { archive, files };
  Object.assign(report.users[u.tag], {
    status: status.status,
    own_job_listed: true,
    foreign_job_listed: false,
    foreign_status: 404,
    foreign_result: 404,
    files,
    bundle: { ...bundleSummary, other_http: cross.status },
  });
}
try {
  const baseline = ledger();
  const [a, b] = await Promise.all([client("a"), client("b")]);
  const a2 = await client("a");
  assert.notEqual(a.session_id, a2.session_id);
  await seedToolActivity(a, a2);
  await seedToolActivity(b);
  await verifyReads(a, b);
  await verifyReads(b, a);
  assert.notEqual(
    a.observed.tg_c,
    b.observed.tg_c,
    "The two prediction inputs must yield distinguishable Tg results",
  );
  assert.notDeepEqual(
    a.observed.polymer_smiles,
    b.observed.polymer_smiles,
    "The two monomers must yield distinguishable polymer SMILES",
  );
  assert.notEqual(
    a.observed.fixture_energy,
    b.observed.fixture_energy,
    "The two DFT fixture molecules must yield distinguishable energies",
  );
  a2.conversation = a.conversation;
  a2.job = a.job;
  a2.calls = a.calls;
  const second = await turn(a2, "dft_job_result", { job_id: a.job });
  assert.equal(second.status, "completed");
  report.same_owner_second_login = true;
  const users = {};
  for (const u of [a, b])
    users[u.tag] = {
      id: u.user_id,
      session_ids: u === a ? [a.session_id, a2.session_id] : [b.session_id],
      workspace_session: u.conversation,
      job_id: u.job,
      calls: u.calls,
      observed: u.observed,
      downloads: u.downloads,
    };
  const retried = JSON.parse(await readFile(stack.retry_evidence, "utf8"));
  assert.ok(!retried.failed);
  assert.deepEqual(retried.job_ids.slice().sort(), [a.job, b.job].sort());
  assert.equal(retried.retries, 2);
  assert.deepEqual(retried.sync_retries, {
    predict_polymer_properties: 2,
    polymerize_monomers: 2,
  });
  report.replayed_writes = retried;
  report.ledger = ledger({ baseline, users });
  report.distinct_browser_results = {
    predicted_tg: true,
    polymer_smiles: true,
    dft_fixture_energy: true,
  };
  report.status = "passed";
} catch (e) {
  console.error(String(e).replace(/np_page=[^ &"]+/g, "np_page=[redacted]"));
  process.exitCode = 1;
} finally {
  for (const { p, name } of profiles) await logout(p, name).catch(() => {});
  await browser.close();
  report.end = new Date().toISOString();
  await writeFile(output, JSON.stringify(report, null, 2) + "\n", {
    flag: "wx",
    mode: 0o600,
  });
  console.log("M3 verification", report.status);
}
