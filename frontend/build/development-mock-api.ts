import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import type { Plugin } from "vite";

/**
 * Frontend preview mode (`npm run dev:mock`): the dev server answers `/api/v1/*` itself so the
 * interface can be viewed and adjusted without a backend. Only `vite serve --mode mock` loads it;
 * nothing here reaches built or preview assets.
 *
 * Every value is a stand-in: predictions are derived from SMILES text, Tg candidates come from a
 * fixed fixture (similarities to the built-in Tg demo structure), and the Tg assistant replies by
 * rule. Endpoints not listed answer 503 with a readable reason.
 */

type Json = Record<string, unknown>;
type Fixture = {
  candidates: Array<{ pi_id: number; smiles: string; tg: number; similarity: number; monomer_a: string; monomer_b: string; svg: string }>;
  monomers: Array<{ smiles: string; iupac: string; svg: string }>;
};
type Job = { id: string; started: number; request: Json };
type SseEvent = [string, Json];

const API = "/api/v1";
const JOB_SECONDS = 2.4;
const PREDICTABLE = [
  "Glass transition temperature", "Melting temperature", "Thermal decomposition temperature",
  "Thermal decomposition weight loss", "Elongation at break", "Tensile stress strength at break",
  "O2 Permeability Barrer", "Co2 Permeability Barrer", "H2 Permeability Barrer"
];

const GUIDE = {
  module: "reverseDesign", version: 3, language: "zh-CN",
  defaults: { target_tg: 450, similarity_threshold: 0.7, candidate_size: 200 },
  sections: [
    { id: "start", title: "开始", content: ["先在画板中绘制或导入参考结构，再打开“搜索参数”设置目标 Tg、相似度阈值和候选数量。"] },
    { id: "results", title: "结果", content: ["结果按与目标 Tg 的差距和结构相似度排序，可在结果抽屉中查看单体信息。"] },
    { id: "assistant", title: "AI 助手", content: ["勾选“附带当前页面”后，助手可以读取结构、参数和当前页候选，并给出可确认的操作。",
      "前端预览模式中的回答由规则生成，不调用真实模型。"] }
  ]
};

function stable(text: string, lo: number, hi: number) {
  let hash = 2166136261;
  for (const char of text) hash = Math.imul(hash ^ char.codePointAt(0)!, 16777619) >>> 0;
  return lo + ((hash % 10007) / 10007) * (hi - lo);
}

/** Rough, deterministic features read from SMILES text; enough to make values move with the structure. */
function features(smiles: string) {
  const text = smiles.replace(/\s+\|[^|]*\|\s*$/, "").replace(/\[[^\]]*\]/g, "C");
  const aromatic = (text.match(/[cnos]/g) ?? []).length;
  const aliphatic = (text.match(/Cl|Br|[CNOSPFI]/g) ?? []).length;
  const hetero = (text.match(/Cl|Br|[NOSFI]|[nos]/g) ?? []).length;
  const halo = (text.match(/Cl|Br|F|I/g) ?? []).length;
  return { aromaticRings: Math.round(aromatic / 5.5), heavy: aromatic + aliphatic, hetero, halo, rotatable: Math.max(0, Math.round((text.match(/C/g) ?? []).length / 2)) };
}

export function mockPredictions(smiles: string, properties: unknown) {
  const f = features(smiles);
  const rigid = f.aromaticRings * 28 - f.rotatable * 9;
  const mw = f.heavy * 13;
  const base: Record<string, number> = {
    "Glass transition temperature": 20 + rigid * 2.5 + 0.3 * mw + 6 * f.hetero,
    "Melting temperature": 150 + rigid * 3 + 0.4 * mw,
    "Thermal decomposition temperature": 330 + f.aromaticRings * 22 - f.rotatable * 4,
    "Thermal decomposition weight loss": Math.max(0.5, 6 - f.aromaticRings * 0.6 + f.rotatable * 0.3),
    "Elongation at break": Math.max(1.5, 380 - rigid * 6 - mw),
    "Tensile stress strength at break": 25 + f.aromaticRings * 6 + f.hetero * 2,
    "O2 Permeability Barrer": Math.max(0.05, 3.5 - f.hetero * 0.4 + f.halo * 0.8),
    "Co2 Permeability Barrer": Math.max(0.2, 14 - f.hetero * 1.6 + f.halo * 3),
    "H2 Permeability Barrer": Math.max(1, 12 + f.aromaticRings * 4 - f.hetero * 1.2 + f.halo * 2)
  };
  const requested = Array.isArray(properties) ? properties.filter((name): name is string => typeof name === "string") : PREDICTABLE;
  return Object.fromEntries(requested.filter((name) => name in base)
    .map((name) => [name, Math.round((base[name] + stable(smiles + name, -3, 3)) * 1000) / 1000]));
}

export function mockAssistantEvents(messages: unknown, context: Json | null): SseEvent[] {
  const list = Array.isArray(messages) ? messages as Array<{ role?: string; content?: string }> : [];
  const text = [...list].reverse().find((message) => message.role === "user")?.content ?? "";
  const revision = typeof context?.action_context_revision === "string" ? context.action_context_revision : null;
  const number = text.match(/(-?\d+(?:\.\d+)?)/);
  const wantsSearch = ["搜索", "运行", "查找", "找"].some((word) => text.includes(word));
  const events: SseEvent[] = [["stage", { code: "routing_request" }]];
  let reply: string;
  let proposal: Json | null = null;
  let navigation: Json | null = null;
  if (!context) {
    reply = "我还看不到当前页面。勾选输入框下方的“附带当前页面”后再问，我可以根据当前结构、参数和候选回答，也可以替你设置参数并发起搜索。";
  } else if (/Tg|目标|温度/.test(text) && number && revision) {
    const value = Number(number[1]);
    const operations: Json[] = [{ type: "set_parameters", parameters: { target_tg: value } }];
    if (wantsSearch) operations.push({ type: "run_search" });
    proposal = { proposal_id: randomUUID(), basis_revision: revision, requires_confirmation: true, operations };
    reply = `可以。我准备把目标 Tg 设为 ${value} °C${wantsSearch ? "，并立即用当前结构搜索。" : "。"}请在下方确认后执行。`;
  } else if (/相似度|阈值/.test(text) && number && revision) {
    const value = Math.max(0, Math.min(1, Number(number[1])));
    proposal = { proposal_id: randomUUID(), basis_revision: revision, requires_confirmation: true,
      operations: [{ type: "set_parameters", parameters: { similarity_threshold: value } }] };
    reply = `我准备把相似度阈值改为 ${value}。阈值越低，候选越多，但与参考结构的差异也越大。请确认后执行。`;
  } else if (text.includes("参数") && revision) {
    const draft = (context.draft_parameters ?? {}) as Json;
    navigation = { target: "parameters", basis_revision: revision };
    reply = `当前参数：目标 Tg ${draft.target_tg} °C，相似度阈值 ${draft.similarity_threshold}，候选数量 ${draft.candidate_size}。`;
  } else if (/结果|候选|解释|分析/.test(text)) {
    const view = context.result_view as { total?: number; visible_candidates?: Array<{ rank: number; tg_value: number; tg_difference: number; similarity_score: number }> } | null;
    const rows = view?.visible_candidates ?? [];
    if (!rows.length) {
      reply = "当前还没有搜索结果。可以先运行搜索，或告诉我目标 Tg，我帮你设置后再搜索。";
    } else {
      const best = rows.reduce((a, b) => (Math.abs(b.tg_difference) < Math.abs(a.tg_difference) ? b : a));
      reply = `当前共有 ${view?.total ?? rows.length} 个候选，这一页 ${rows.length} 个。与目标最接近的是第 ${best.rank} 名，Tg ${best.tg_value} °C（差 ${best.tg_difference > 0 ? "+" : ""}${best.tg_difference} °C），相似度 ${best.similarity_score.toFixed(2)}。`;
    }
  } else {
    reply = "这是前端预览模式的演示助手，回答由规则生成。可以试试：“把目标 Tg 设为 420 并搜索”、“相似度阈值改为 0.6”、“解释一下当前结果”。";
  }
  events.push(["stage", { code: "composing_answer" }], ["stage", { code: "writing_answer" }]);
  for (let index = 0; index < reply.length; index += 3) events.push(["token", { content: reply.slice(index, index + 3) }]);
  if (navigation) events.push(["navigation", navigation]);
  if (proposal) events.push(["action_proposal", proposal]);
  events.push(["done", {}]);
  return events;
}

function loadFixture(): Fixture {
  return JSON.parse(readFileSync(new URL("../scripts/fixtures/dev-mock-reverse-design.json", import.meta.url), "utf8"));
}

export function mockReverseDesignResults(fixture: Fixture, request: Json) {
  const target = Number(request.target_tg);
  const threshold = Number(request.similarity_threshold);
  const size = Number(request.candidate_size);
  const monomers = new Map(fixture.monomers.map((monomer) => [monomer.smiles, monomer]));
  return fixture.candidates
    .filter((candidate) => candidate.similarity >= threshold)
    .sort((a, b) => Math.abs(a.tg - target) - Math.abs(b.tg - target) || b.similarity - a.similarity)
    .slice(0, size)
    .map((candidate, index) => {
      const a = monomers.get(candidate.monomer_a);
      const b = monomers.get(candidate.monomer_b);
      return {
        rank: index + 1, pi_id: candidate.pi_id, polymer_smiles: candidate.smiles, canonical_polym: candidate.smiles,
        monomer_a_smiles: candidate.monomer_a, monomer_b_smiles: candidate.monomer_b,
        monomer_a_iupac: a?.iupac ?? null, monomer_b_iupac: b?.iupac ?? null,
        monomer_a_structure_svg: a?.svg ?? null, monomer_b_structure_svg: b?.svg ?? null,
        tg_value: candidate.tg, tg_unit: "°C", tg_difference: Math.round((candidate.tg - target) * 10) / 10,
        similarity_score: candidate.similarity, structure_svg: candidate.svg, knowledge_available: false
      };
    });
}

function readBody(request: IncomingMessage) {
  return new Promise<Buffer>((resolve, reject) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => resolve(Buffer.concat(chunks)));
    request.on("error", reject);
  });
}

function sendJson(response: ServerResponse, body: unknown, status = 200) {
  response.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  response.end(JSON.stringify(body));
}

function unavailable(response: ServerResponse, detail = "前端预览模式未提供该接口。") {
  sendJson(response, { detail }, 503);
}

function sendSse(request: IncomingMessage, response: ServerResponse, meta: Json, events: SseEvent[]) {
  response.writeHead(200, { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache", "X-Accel-Buffering": "no" });
  const queue: SseEvent[] = [["meta", meta], ...events];
  let closed = false;
  request.on("close", () => { closed = true; });
  const next = () => {
    if (closed) return;
    const event = queue.shift();
    if (!event) { response.end(); return; }
    response.write(`event: ${event[0]}\ndata: ${JSON.stringify(event[1])}\n\n`);
    setTimeout(next, event[0] === "token" ? 30 : 250);
  };
  next();
}

/** Extracts the JSON `payload` field of a multipart assistant request. */
export function multipartPayload(body: Buffer, contentType: string) {
  const boundary = contentType.match(/boundary=(?:"([^"]+)"|([^;]+))/i);
  if (!boundary) return { payload: null as Json | null, hasUserImage: false };
  const parts = body.toString("utf8").split(`--${boundary[1] ?? boundary[2]}`);
  const payloadPart = parts.find((part) => /name="payload"/.test(part));
  const json = payloadPart?.split(/\r?\n\r?\n/).slice(1).join("\n\n").replace(/\r?\n$/, "");
  return { payload: json ? JSON.parse(json) as Json : null, hasUserImage: parts.some((part) => /name="image"/.test(part)) };
}

export function developmentMockApi(): Plugin {
  let fixture: Fixture | null = null;
  const jobs = new Map<string, Job>();
  return {
    name: "nexpoly-development-mock-api",
    apply: "serve",
    configureServer(server) {
      server.config.logger.info("  ➜  前端预览模式：/api/v1 由本地模拟数据响应，不连接后端。");
      server.middlewares.use(async (request, response, next) => {
        const url = new URL(request.url ?? "/", "http://localhost");
        if (!url.pathname.startsWith(`${API}/`)) return next();
        const path = url.pathname.slice(API.length);
        const method = request.method ?? "GET";
        try {
          if (method === "GET" && path === "/auth/session") {
            return sendJson(response, { authenticated: true, user: { id: "frontend-preview", username: "前端预览", must_change_password: false },
              session_id: "frontend-preview", csrf_token: "frontend-preview", capabilities: {} });
          }
          if (method === "GET" && path === "/usage/entitlements") return sendJson(response, { enabled: false, mode: "not_enabled", policy: null, pools: [] });
          if (method === "GET" && path === "/assistant/tg/status") {
            return sendJson(response, { enabled: true, configured: true, image: { supported: true, max_files: 2, max_canvas_snapshots: 1,
              max_user_upload_files: 1, max_bytes: 5_000_000, max_total_bytes: 8_000_000, accepted_mime_types: ["image/png", "image/jpeg", "image/webp"] } });
          }
          if (method === "GET" && path === "/assistant/tg/guide") return sendJson(response, GUIDE);
          const job = path.match(/^\/reverse-design\/tg\/jobs\/([\w-]+)$/);
          if (method === "GET" && job) {
            const entry = jobs.get(job[1]);
            if (!entry) return sendJson(response, { detail: "任务不存在。" }, 404);
            fixture ??= loadFixture();
            const results = mockReverseDesignResults(fixture, entry.request);
            const progress = Math.min(1, (Date.now() - entry.started) / 1000 / JOB_SECONDS);
            const done = progress >= 1;
            const now = new Date().toISOString();
            return sendJson(response, {
              job_id: entry.id,
              status: done ? (results.length >= Number(entry.request.candidate_size) ? "found_enough" : "exhausted") : "running",
              target_tg: entry.request.target_tg, similarity_threshold: entry.request.similarity_threshold,
              created_at: now, updated_at: now, started_at: now, finished_at: done ? now : null,
              scanned_rows: Math.round(fixture.candidates.length * progress), matched_count: Math.round(results.length * progress),
              current_tg_radius: 30, best_similarity_score: results.length ? Math.max(...results.map((item) => item.similarity_score)) : null,
              message: null, error: null,
              result: done ? { target_tg: entry.request.target_tg, query_time_ms: JOB_SECONDS * 1000, candidate_pool_size: fixture.candidates.length,
                sampled_candidate_count: fixture.candidates.length, total: results.length, data_source: "pi_reverse_design", results } : null
            });
          }
          if (method !== "POST") return unavailable(response);

          const raw = await readBody(request);
          const contentType = request.headers["content-type"] ?? "";
          const body = contentType.includes("application/json") && raw.length ? JSON.parse(raw.toString("utf8")) as Json : {};
          if (path === "/structure/standardize-smiles") {
            const smiles = String(body.smiles ?? "");
            if (!smiles.trim()) return sendJson(response, { detail: "SMILES 不能为空。" }, 422);
            return sendJson(response, { input_smiles: smiles, standardized_smiles: smiles, changed: false, query_time_ms: 1 });
          }
          if (path === "/structure/3d") return unavailable(response, "前端预览模式不生成三维构象。");
          if (path === "/structure/recognize-image") return unavailable(response, "前端预览模式不识别图片中的结构。");
          if (path === "/predict") {
            return sendJson(response, { predictions: mockPredictions(String(body.smiles ?? ""), body.properties), query_time_ms: 120 });
          }
          if (path === "/reverse-design/tg/jobs") {
            const id = randomUUID();
            jobs.set(id, { id, started: Date.now(), request: body });
            return sendJson(response, { job_id: id, status: "pending" }, 202);
          }
          if (path === "/assistant/tg/chat/stream" || path === "/assistant/tg/chat/image-stream") {
            const image = path.endsWith("image-stream");
            const parsed = image ? multipartPayload(raw, contentType) : { payload: body, hasUserImage: false };
            const payload = parsed.payload ?? {};
            const context = (payload.page_context as Json | undefined) ?? null;
            const events = mockAssistantEvents(payload.messages, context);
            if (parsed.hasUserImage) events.splice(1, 0, ["token", { content: "（前端预览模式不分析你上传的图片。）" }]);
            return sendSse(request, response, { request_id: randomUUID(), context_attached: Boolean(context), image_attached: image,
              image_count: image ? 1 : 0, context_trimmed: [] }, events);
          }
          return unavailable(response);
        } catch (error) {
          return sendJson(response, { detail: `前端预览模式处理请求失败：${error instanceof Error ? error.message : String(error)}` }, 500);
        }
      });
    }
  };
}
