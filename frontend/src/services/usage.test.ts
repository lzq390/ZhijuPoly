import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GUEST_SESSION, installSession, retireSession } from "../auth/session";
import { fetchUsageActivity, fetchUsageActivityByGroup, fetchUsageCatalog, fetchUsageRecords, fetchUsageSummary } from "./usage";
import { fetchUsageOperations, fetchUsageOperationsByGroup } from "./entitlements";

function account(id: string) {
  installSession({ authenticated: true, user: { id, username: id, must_change_password: false }, session_id: `session-${id}`, csrf_token: "fixture-csrf", capabilities: {} });
}
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
beforeEach(() => { retireSession(); account("a"); });
afterEach(() => { retireSession(); vi.unstubAllGlobals(); });
describe("private usage transport", () => {
  it("only sends allowed GET filters with the current session and no caching", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ items: [] })); vi.stubGlobal("fetch", fetch);
    await fetchUsageRecords({ start: "2026-09-01T00:00:00.000Z", end: "2026-10-01T00:00:00.000Z", module: "monomer_md", page: 2, page_size: 25, status: "failed", include_children: true, ...{ owner_user_id: "foreign" } });
    const [input, init] = fetch.mock.calls[0]; const url = new URL(input, "http://localhost");
    expect(url.pathname).toBe("/api/v1/usage/records");
    expect(url.searchParams.get("owner_user_id")).toBeNull();
    expect(url.searchParams.get("page")).toBe("2");
    expect(url.searchParams.get("status")).toBe("failed");
    expect(url.searchParams.get("include_children")).toBe("true");
    expect(init.method ?? "GET").toBe("GET");
    expect(init.credentials).toBe("same-origin"); expect(init.cache).toBe("no-store");
    expect(init.headers.get("X-Session-Context")).toBe("session-a");
  });
  it("requests Beijing daily buckets only for summary while preserving absolute record bounds", async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(response({}))); vi.stubGlobal("fetch", fetch);
    const period = { start: "2026-09-30T16:00:00Z", end: "2026-10-01T16:00:00Z", timezone: "Asia/Shanghai" as const };
    await fetchUsageSummary(period);
    expect(new URL(fetch.mock.calls[0][0], "http://localhost").searchParams.get("timezone")).toBe("Asia/Shanghai");
    await fetchUsageRecords({ ...period, page: 1, page_size: 25, include_children: false });
    const params = new URL(fetch.mock.calls[1][0], "http://localhost").searchParams;
    expect(params.has("timezone")).toBe(false);
    expect(params.get("start")).toBe(period.start); expect(params.get("end")).toBe(period.end);
  });
  it("reads independent activity with only offset, limit and module through the private GET transport", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ mode: "activity_only", items: [] })); vi.stubGlobal("fetch", fetch);
    const controller = new AbortController();
    await fetchUsageActivity(25, "monomer_md", controller.signal);
    const [input, init] = fetch.mock.calls[0]; const url = new URL(input, "http://localhost");
    expect(url.pathname).toBe("/api/v1/usage/activity");
    expect(Object.fromEntries(url.searchParams)).toEqual({ offset: "25", limit: "25", module: "monomer_md" });
    expect(init.method ?? "GET").toBe("GET"); expect(init.cache).toBe("no-store");
    expect(init.headers.get("X-Session-Context")).toBe("session-a");
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });
  it("does not call fetch as a guest", async () => {
    installSession(GUEST_SESSION); const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    await expect(fetchUsageCatalog()).rejects.toThrow("请登录");
    await expect(fetchUsageActivity(0)).rejects.toThrow("请登录"); expect(fetch).not.toHaveBeenCalled();
    await expect(fetchUsageActivityByGroup(0, "mdSimulationDemo")).rejects.toThrow("请登录");
    await expect(fetchUsageOperationsByGroup(0, "monomerDft")).rejects.toThrow("请登录"); expect(fetch).not.toHaveBeenCalled();
  });
  it("preserves the disabled service code without changing the session", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ code: "usage_metering_disabled", detail: "尚未启用" }, 503)));
    await expect(fetchUsageSummary({ start: "a", end: "b" })).rejects.toMatchObject({ status: 503, code: "usage_metering_disabled" });
  });
  it("rejects a late response from the account retired while reading usage", async () => {
    let finish!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(resolve => { finish = resolve; })));
    const pending = fetchUsageActivity(0);
    retireSession(); account("b"); finish(response({ enabled: true }));
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });
  it("uses one server group filter for activity and receipts without changing exact module reads", async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(response({ items: [] }))); vi.stubGlobal("fetch", fetch);
    await fetchUsageActivityByGroup(25, "monomerDft");
    await fetchUsageOperationsByGroup(50, "mdSimulationDemo");
    await fetchUsageOperations(25, "monomer_dft");
    expect(fetch.mock.calls.map(([input]) => Object.fromEntries(new URL(input, "http://localhost").searchParams))).toEqual([
      { offset: "25", limit: "25", module_group: "monomerDft" },
      { offset: "50", limit: "25", module_group: "mdSimulationDemo" },
      { offset: "25", limit: "25", module: "monomer_dft" },
    ]);
    for (const [, init] of fetch.mock.calls) {
      expect(init.method ?? "GET").toBe("GET"); expect(init.cache).toBe("no-store");
      expect(init.headers.get("X-Session-Context")).toBe("session-a");
    }
  });
  it("sends the same group to summary and records and rejects mixed filter semantics", async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(response({}))); vi.stubGlobal("fetch", fetch);
    const period = { start: "2026-09-30T16:00:00Z", end: "2026-10-01T16:00:00Z", module_group: "monomerDft" };
    await fetchUsageSummary(period);
    await fetchUsageRecords({ ...period, page: 2, page_size: 25, include_children: false });
    for (const [input] of fetch.mock.calls) {
      const params = new URL(input, "http://localhost").searchParams;
      expect(params.get("module_group")).toBe("monomerDft"); expect(params.has("module")).toBe(false);
    }
    expect(() => fetchUsageSummary({ ...period, module: "monomer_dft" })).toThrow("不能同时筛选");
    expect(() => fetchUsageRecords({ ...period, module: "dft_history", page: 1, page_size: 25, include_children: false })).toThrow("不能同时筛选");
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
