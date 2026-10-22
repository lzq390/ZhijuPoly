// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { createElement } from "react";
import App from "../App";
import { AuthProvider } from "../auth/AuthProvider";
import { handoffPageReady } from "../../scripts/handoff-readiness.mjs";
import { createHandoffMockFetch } from "./handoffMockFetch";

function pageState(fonts: "loaded" | "loading", visiblePanels: boolean[]) {
  return {
    fonts: { status: fonts },
    querySelectorAll: () => visiblePanels.map(visible => ({ checkVisibility: () => visible })),
  } as unknown as Pick<Document, "fonts" | "querySelectorAll">;
}

describe("handoff page readiness", () => {
  it("accepts a loaded page without an authentication gate", () => {
    expect(handoffPageReady(pageState("loaded", []))).toBe(true);
  });
  it("retains hidden workspace membership notices without blocking the visible page", () => {
    expect(handoffPageReady(pageState("loaded", [false]))).toBe(true);
  });
  it("still blocks visible login, password, error or loading screens", () => {
    expect(handoffPageReady(pageState("loaded", [true]))).toBe(false);
    expect(handoffPageReady(pageState("loaded", [false, true]))).toBe(false);
  });
  it("waits for active font loading even when no authentication screen is visible", () => {
    expect(handoffPageReady(pageState("loading", []))).toBe(false);
    expect(handoffPageReady(pageState("loading", [false]))).toBe(false);
  });
});

it("uses the current member identity contract for the synthetic authenticated session", async () => {
  const mock = createHandoffMockFetch({ pageUrl: "http://127.0.0.1/knowledge?mockAuth=authenticated" });
  const response = await mock("http://127.0.0.1/api/v1/auth/session");
  const session = await response.json();
  expect(session.authenticated).toBe(true);
  expect(session.account_type).toBe("member");
  expect(session.capabilities["openscience.use"]).toBe(true);
  expect(session.capabilities.dev_gpu_control).toBe(false);
});

it("preserves the synthetic guest identity", async () => {
  const mock = createHandoffMockFetch({ pageUrl: "http://127.0.0.1/knowledge?mockAuth=guest" });
  const response = await mock("http://127.0.0.1/api/v1/auth/session");
  const session = await response.json();
  expect(session.authenticated).toBe(false);
  expect(session.user).toBeNull();
  expect(session.capabilities["openscience.use"]).not.toBe(true);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

function homePage(sessionOverride?: Record<string, unknown>, guest = false) {
  window.history.replaceState({}, "", "/");
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.stubEnv("VITE_AGENT_WORKSPACE_URL", "");
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  const transport = createHandoffMockFetch({ pageUrl: "http://127.0.0.1:5173/" + (guest ? "?mockAuth=guest" : "") });
  const fetcher = vi.fn<typeof fetch>(async (input, init) => {
    const path = new URL(typeof input === "string" || input instanceof URL ? String(input) : input.url, window.location.href).pathname;
    if (sessionOverride && path === "/api/v1/auth/session") {
      const base = await (await transport(input, init)).json();
      return new Response(JSON.stringify({ ...base, ...sessionOverride }), { headers: { "Content-Type": "application/json" } });
    }
    return transport(input, init);
  });
  vi.stubGlobal("fetch", fetcher);
  render(createElement(AuthProvider, null, createElement(App)));
  return fetcher;
}

describe("handoff home uses the production capability boundary", () => {
  it("renders only the local placeholder for the synthetic member", async () => {
    homePage();
    expect(await screen.findByText("智能体工作区：交付包本地占位", { exact: true })).toBeTruthy();
    expect(document.querySelector("iframe")).toBeNull();
    expect(screen.queryByText("共享工作空间需要会员权限")).toBeNull();
  });

  it("keeps a member session without the workspace capability denied", async () => {
    const fetcher = homePage({ capabilities: { dev_gpu_control: false, "openscience.use": false } });
    expect(await screen.findByText("共享工作空间需要会员权限", { exact: true })).toBeTruthy();
    expect(screen.queryByText("智能体工作区：交付包本地占位")).toBeNull();
    expect(document.querySelector("iframe")).toBeNull();
    expect(fetcher.mock.calls.every(([input]) =>
      new URL(typeof input === "string" || input instanceof URL ? String(input) : input.url, window.location.href).pathname === "/api/v1/auth/session"
    )).toBe(true);
  });

  it("keeps guest home local and does not acquire member capability", async () => {
    const fetcher = homePage(undefined, true);
    expect(await screen.findByRole("button", { name: "游客账号菜单" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "对话", level: 1 })).toBeTruthy();
    expect(document.querySelector("iframe")).toBeNull();
    expect(fetcher.mock.calls.every(([input]) =>
      new URL(typeof input === "string" || input instanceof URL ? String(input) : input.url, window.location.href).pathname === "/api/v1/auth/session"
    )).toBe(true);
  });
});
