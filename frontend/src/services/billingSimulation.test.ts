import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { confirmSimulationCheckout, readSimulationCheckout, simulationCheckoutUrl, simulationTokenFromHash } from "./billingSimulation";
const token = `${"a".repeat(32)}.${"b".repeat(43)}`;
const checkout = { simulation: true, provider: "wechat", amount_fen: 9900, currency: "CNY", period: "month", status: "pending", order_suffix: "abc123", expires_at: "2027-01-01T00:00:00Z", server_time: "2026-11-01T00:00:00Z", access_ends_at: null };
const fetchMock = vi.fn();
beforeEach(() => { fetchMock.mockReset().mockResolvedValue(new Response(JSON.stringify(checkout))); vi.stubGlobal("fetch", fetchMock); });
afterEach(() => vi.unstubAllGlobals());
it("keeps the token in the current-origin fragment and rejects missing/duplicate/query-style tokens", () => {
  expect(simulationCheckoutUrl(token, "https://nexpoly.example:9001")).toBe(`https://nexpoly.example:9001/billing/simulate-pay#token=${token}`);
  expect(simulationTokenFromHash(`#token=${token}`)).toBe(token);
  expect(simulationTokenFromHash(`#token=${token}&token=${token}`)).toBeNull();
  expect(simulationTokenFromHash("#token=invalid")).toBeNull(); expect(simulationTokenFromHash("")).toBeNull();
  expect(simulationCheckoutUrl("invalid", "https://nexpoly.example")).toBeNull();
});
it.each([["checkout", readSimulationCheckout], ["confirm", confirmSimulationCheckout]] as const)("POSTs %s without cookies or a referrer, sending the token only in JSON", async (action, call) => {
  const signal = new AbortController().signal;
  await expect(call(token, signal)).resolves.toEqual(checkout);
  expect(fetchMock).toHaveBeenCalledExactlyOnceWith(`/api/v1/billing/simulation/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }), signal, credentials: "omit", referrerPolicy: "no-referrer", cache: "no-store" });
});
it.each([[404, "invalid"], [410, "expired"], [409, "closed"], [403, "unavailable"], [503, "unavailable"]])("maps HTTP %s to a safe UI state", async (status, kind) => {
  fetchMock.mockResolvedValue(new Response("", { status: Number(status) }));
  await expect(readSimulationCheckout(token)).rejects.toMatchObject({ kind });
});
it.each([{ simulation: false }, { status: "unknown" }, { amount_fen: -1 }, { expires_at: "invalid" }, { order_suffix: "full-private-order-id" }])("fails closed on an unexpected checkout payload %o", async override => {
  fetchMock.mockResolvedValue(new Response(JSON.stringify({ ...checkout, ...override })));
  await expect(readSimulationCheckout(token)).rejects.toMatchObject({ kind: "unavailable" });
});
