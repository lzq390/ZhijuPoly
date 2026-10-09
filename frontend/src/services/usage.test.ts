import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GUEST_SESSION, installSession, retireSession } from "../auth/session";
import { fetchUsageCatalog, fetchUsageRecords, fetchUsageSummary } from "./usage";

function account(id: string) {
  installSession({ authenticated: true, user: { id, username: id, must_change_password: false }, session_id: `session-${id}`, csrf_token: "fixture-csrf", capabilities: {} });
}
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
beforeEach(() => { retireSession(); account("a"); });
afterEach(() => { retireSession(); vi.unstubAllGlobals(); });
describe("private usage transport", () => {
  it("only sends allowed GET filters with the current session and no caching", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ items: [] })); vi.stubGlobal("fetch", fetch);
    await fetchUsageRecords({ start: "2026-09-01T00:00:00.000Z", end: "2026-10-01T00:00:00.000Z", module: "md", page: 2, page_size: 25, status: "failed", include_children: true, ...{ owner_user_id: "foreign" } });
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
  it("does not call fetch as a guest", async () => {
    installSession(GUEST_SESSION); const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    await expect(fetchUsageCatalog()).rejects.toThrow("请登录"); expect(fetch).not.toHaveBeenCalled();
  });
  it("preserves the disabled service code without changing the session", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ code: "usage_metering_disabled", detail: "尚未启用" }, 503)));
    await expect(fetchUsageSummary({ start: "a", end: "b" })).rejects.toMatchObject({ status: 503, code: "usage_metering_disabled" });
  });
  it("rejects a late response from the account retired while reading usage", async () => {
    let finish!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(resolve => { finish = resolve; })));
    const pending = fetchUsageCatalog();
    retireSession(); account("b"); finish(response({ enabled: true }));
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });
});
