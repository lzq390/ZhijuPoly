/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { PrivateAgentFrame } from "./PrivateAgentFrame";

const issue = vi.hoisted(() => vi.fn());
vi.mock("../auth/session", async importOriginal => ({ ...await importOriginal<object>(), privateFetch: issue }));
vi.mock("../auth/authMutationLock", () => ({ withAuthMutationLock: (work: () => Promise<unknown>) => work() }));
const origin = "https://agent.example.test";
const ticket = () => Response.json({ ticket: "ticket", handoff_id: "current", exchange_url: origin + "/_nexpoly/auth/exchange" });
afterEach(() => { cleanup(); vi.restoreAllMocks(); issue.mockReset(); localStorage.clear(); });

it("navigates only after the expected window, source and handoff agree, and clears a revoked frame", async () => {
  issue.mockResolvedValue(ticket());
  const submitted = vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(function (this: HTMLFormElement) {
    expect(this.action).toBe(origin + "/_nexpoly/auth/exchange");
    expect(new FormData(this).get("ticket")).toBe("ticket");
    expect(new FormData(this).get("browser_id")).toBeTruthy();
  });
  const unavailable = vi.fn();
  render(<PrivateAgentFrame url={origin + "/"} onUnavailable={unavailable} />);
  const frame = screen.getByTitle("智聚万物智能体工作台") as HTMLIFrameElement;
  await waitFor(() => expect(submitted).toHaveBeenCalledOnce());
  const message = (handoff_id: string, targetOrigin = origin, source = frame.contentWindow) =>
    fireEvent(window, new MessageEvent("message", { origin: targetOrigin, source,
      data: { type: "nexpoly:openscience:ready", handoff_id, page_context: "page" } }));
  message("previous");
  message("current", "https://another.test");
  message("current", origin, window);
  expect(frame.src).toBe("about:blank");
  message("current");
  expect(frame.src).toBe(origin + "/?np_page=page");
  fireEvent(window, new MessageEvent("message", {origin, source: frame.contentWindow,
    data: {type: "nexpoly:openscience:expired", page_context: "page"}}));
  expect(unavailable).toHaveBeenCalledOnce();
  expect(screen.queryByTitle("智聚万物智能体工作台")).toBeNull();
});

it("drops a ticket response arriving after the signed-in frame unmounts", async () => {
  let complete!: (response: Response) => void;
  issue.mockReturnValue(new Promise<Response>(resolve => { complete = resolve; }));
  const submit = vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(() => {});
  const view = render(<PrivateAgentFrame url={origin} />);
  await waitFor(() => expect(issue).toHaveBeenCalledOnce());
  view.unmount();
  complete(ticket());
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(submit).not.toHaveBeenCalled();
});

it("shows a retryable capacity refusal before opening a workspace", async () => {
  issue.mockResolvedValue(ticket());
  const submitted = vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(() => {});
  render(<PrivateAgentFrame url={origin} />);
  const frame = screen.getByTitle("智聚万物智能体工作台") as HTMLIFrameElement;
  await waitFor(() => expect(submitted).toHaveBeenCalledOnce());
  fireEvent(window, new MessageEvent("message", { origin, source: frame.contentWindow,
    data: { type: "nexpoly:openscience:error", handoff_id: "current", reason: "workspace_capacity" } }));
  expect(screen.getByText("工作区容量已满，请稍后重试。")).toBeTruthy();
  expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
  expect(screen.queryByTitle("智聚万物智能体工作台")).toBeNull();
});

it("preserves a cached tab and releases its own page on unload once", async () => {
  issue.mockResolvedValue(ticket());
  const submitted = vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(() => {});
  const beacon = vi.fn(() => true);
  vi.stubGlobal("navigator", Object.assign(Object.create(window.navigator), { sendBeacon: beacon }));
  try {
    const view = render(<PrivateAgentFrame url={origin} />);
    await waitFor(() => expect(submitted).toHaveBeenCalledOnce());
    const frame = screen.getByTitle("智聚万物智能体工作台") as HTMLIFrameElement;
    fireEvent(window, new MessageEvent("message", { origin, source: frame.contentWindow,
      data: {type: "nexpoly:openscience:ready", handoff_id: "current", page_context: "tab-a"} }));
    fireEvent(window, new PageTransitionEvent("pagehide", {persisted: true}));
    expect(beacon).not.toHaveBeenCalled();
    fireEvent(window, new PageTransitionEvent("pagehide", {persisted: false}));
    view.unmount();
    expect(beacon).toHaveBeenCalledExactlyOnceWith(origin + "/_nexpoly/auth/leave",
      new URLSearchParams({page_context: "tab-a"}));
  } finally { vi.unstubAllGlobals(); }
});
