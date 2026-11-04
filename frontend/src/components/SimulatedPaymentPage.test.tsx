// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SimulationCheckoutError, type SimulationCheckout } from "../services/billingSimulation";
const mocks = vi.hoisted(() => ({ read: vi.fn(), confirm: vi.fn() }));
vi.mock("../services/billingSimulation", async original => ({ ...await original<typeof import("../services/billingSimulation")>(), readSimulationCheckout: mocks.read, confirmSimulationCheckout: mocks.confirm }));
import { SimulatedPaymentPage } from "./SimulatedPaymentPage";
const token = `${"a".repeat(32)}.${"b".repeat(43)}`;
const pending: SimulationCheckout = { simulation: true, provider: "wechat", amount_fen: 9900, currency: "CNY", period: "month", status: "pending", order_suffix: "abc123", expires_at: new Date(Date.now() + 600000).toISOString(), server_time: new Date().toISOString(), access_ends_at: null };
beforeEach(() => { vi.clearAllMocks(); window.history.replaceState({}, "", `/billing/simulate-pay#token=${token}`); mocks.read.mockResolvedValue(pending); });
afterEach(() => { cleanup(); vi.useRealTimers(); });
it.each(["wechat", "alipay"] as const)("shows %s order facts without confirming on open/remount; requires an explicit click", async provider => {
  mocks.read.mockResolvedValue({ ...pending, provider });
  let finish!: (value: SimulationCheckout) => void;
  mocks.confirm.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  let view = render(<SimulatedPaymentPage />);
  await screen.findByRole("button", { name: "确认模拟支付（不会扣款）" });
  expect(screen.getByText("¥99.00")).toBeTruthy(); expect(screen.getByText("abc123")).toBeTruthy(); expect(screen.getByText("月费会员 · 1 个月")).toBeTruthy();
  expect(mocks.confirm).not.toHaveBeenCalled(); view.unmount(); view = render(<SimulatedPaymentPage />);
  const confirm = await screen.findByRole("button", { name: "确认模拟支付（不会扣款）" });
  expect(mocks.confirm).not.toHaveBeenCalled(); fireEvent.click(confirm); fireEvent.click(confirm);
  expect(mocks.confirm).toHaveBeenCalledExactlyOnceWith(token, expect.any(AbortSignal));
  await act(async () => finish({ ...pending, status: "paid", access_ends_at: "2027-01-01T00:00:00Z" }));
  expect(await screen.findByRole("heading", { name: "模拟支付已确认" })).toBeTruthy();
  expect(screen.queryByRole("button")).toBeNull(); view.unmount();
  mocks.read.mockResolvedValue({ ...pending, status: "paid", expires_at: "2020-01-01T00:00:00Z" }); render(<SimulatedPaymentPage />);
  await screen.findByRole("heading", { name: "模拟支付已确认" }); expect(mocks.confirm).toHaveBeenCalledOnce();
});
it("does not accept tokens in query strings or read an invalid fragment", async () => {
  window.history.replaceState({}, "", `/billing/simulate-pay?token=${token}`); render(<SimulatedPaymentPage />);
  expect(screen.getByRole("heading", { name: "模拟链接无效" })).toBeTruthy(); expect(mocks.read).not.toHaveBeenCalled(); expect(mocks.confirm).not.toHaveBeenCalled();
});
it.each([["invalid", "模拟链接无效"], ["expired", "模拟链接已过期"], ["closed", "模拟订单已关闭"]] as const)("does not offer confirmation for %s", async (kind, title) => {
  mocks.read.mockRejectedValue(new SimulationCheckoutError(kind)); render(<SimulatedPaymentPage />);
  await screen.findByRole("heading", { name: title }); expect(screen.queryByRole("button")).toBeNull(); expect(mocks.confirm).not.toHaveBeenCalled();
});
it("uses server time to prevent confirmation after expiry", async () => {
  mocks.read.mockResolvedValue({ ...pending, server_time: "2028-01-01T00:00:00Z", expires_at: "2027-01-01T00:00:00Z" }); render(<SimulatedPaymentPage />);
  await screen.findByRole("heading", { name: "模拟链接已过期" }); expect(screen.queryByRole("button")).toBeNull(); expect(mocks.confirm).not.toHaveBeenCalled();
});
it("reads again after an uncertain confirmation instead of retrying the payment", async () => {
  mocks.confirm.mockRejectedValue(new Error("network interrupted")); render(<SimulatedPaymentPage />);
  fireEvent.click(await screen.findByRole("button", { name: "确认模拟支付（不会扣款）" }));
  const read = await screen.findByRole("button", { name: "重新读取" }); expect(screen.queryByRole("button", { name: "确认模拟支付（不会扣款）" })).toBeNull();
  mocks.read.mockResolvedValue({ ...pending, status: "paid" }); fireEvent.click(read);
  await screen.findByRole("heading", { name: "模拟支付已确认" }); expect(mocks.confirm).toHaveBeenCalledOnce();
});
it("aborts a confirmation when a different token is opened and reads only the new token", async () => {
  mocks.confirm.mockImplementation(() => new Promise(() => {})); render(<SimulatedPaymentPage />);
  fireEvent.click(await screen.findByRole("button", { name: "确认模拟支付（不会扣款）" }));
  const signal = mocks.confirm.mock.calls[0][1] as AbortSignal;
  const next = `${"c".repeat(32)}.${"d".repeat(43)}`;
  await act(async () => { window.history.replaceState({}, "", `/billing/simulate-pay#token=${next}`); window.dispatchEvent(new Event("hashchange")); });
  await waitFor(() => expect(mocks.read).toHaveBeenLastCalledWith(next, expect.any(AbortSignal)));
  expect(signal.aborted).toBe(true); expect(mocks.confirm).toHaveBeenCalledOnce();
});
