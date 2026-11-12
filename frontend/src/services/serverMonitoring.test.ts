import { afterEach, expect, it, vi } from "vitest";
import { installSession, privateFetch } from "../auth/session";
import { fetchMonitoringAccess, fetchMonitoringSnapshot } from "./serverMonitoring";
import { monitoringHostInfo, monitoringSnapshot } from "../test/serverMonitoringFixture";

afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });
it("lets authorized free accounts ask for access without widening unrelated or mutating routes", async () => {
  vi.stubEnv("VITE_SERVER_MONITORING_ENABLED", "true");
  installSession({ authenticated: true, user: { id: "free", username: "free", must_change_password: false }, session_id: "free-session", csrf_token: "csrf", account_type: "free", capabilities: {} });
  const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response('{"can_read":true}', { status: 200 })); vi.stubGlobal("fetch", fetcher);
  expect(await fetchMonitoringAccess(new AbortController().signal)).toBe(true);
  for (const path of ["snapshot", "history", "gpu-tasks"]) await (await privateFetch(`/api/v1/server-monitoring/${path}`)).json();
  expect(fetcher).toHaveBeenCalledTimes(4);
  for (const [path, method] of [["/server-monitoring/snapshot", "POST"], ["/server-monitoring/gpu-tasks", "POST"], ["/server-monitoring/gpu-tasks/other", "GET"], ["/server-monitoring/other", "GET"], ["/server-monitoring/history/delete", "GET"], ["/monomer-dft/jobs", "GET"]]) {
    await expect(privateFetch(`/api/v1${path}`, { method })).rejects.toThrow();
  }
  expect(fetcher).toHaveBeenCalledTimes(4);
  const init = fetcher.mock.calls[0]?.[1] as RequestInit | undefined;
  expect(new Headers(init?.headers).get("X-Session-Context")).toBe("free-session");
});

it.each([undefined, null, monitoringHostInfo({ nvidia_driver_version: null })])("reads a snapshot with legacy, unavailable or partially collected host metadata", async host_info => {
  vi.stubEnv("VITE_SERVER_MONITORING_ENABLED", "true");
  installSession({ authenticated: true, user: { id: "host-reader", username: "host-reader", must_change_password: false }, session_id: "host-reader-session", csrf_token: "csrf", account_type: "free", capabilities: {} });
  const snapshot = monitoringSnapshot({ host_info });
  vi.stubGlobal("fetch", vi.fn(async () => Response.json(snapshot)));
  const actual = await fetchMonitoringSnapshot(new AbortController().signal);
  expect(actual.host_info).toEqual(host_info);
  expect(actual.logical_cpu_count).toBe(64);
  expect(actual.disks).toEqual(snapshot.disks);
});
