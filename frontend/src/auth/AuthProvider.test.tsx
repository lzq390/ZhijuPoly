// @vitest-environment jsdom
import { useEffect, useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AccountMenu, AuthProvider, useAuth } from "./AuthProvider";
import { AUTH_OWNER_KEY, AUTH_SHARED_OWNER_KEY, AUTH_SYNC_KEY } from "./storage";
import * as browserStorage from "./storage";
import { GUEST_SESSION, type AuthSession } from "./session";
const mounted = vi.fn(); const unmounted = vi.fn();
const guest = vi.hoisted(() => ({ capture: null as (() => Promise<string>) | null }));
function account(id: string, mustChange = false): AuthSession {
  return { authenticated: true, user: { id, username: id, must_change_password: mustChange }, session_id: `session-${id}`, csrf_token: `csrf-${id}`, capabilities: {} };
}
const response = (value: unknown) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
function PrivateTree() {
  const auth = useAuth();
  const isGuest = auth?.status === "guest";
  const [draft, setDraft] = useState(auth?.guestDraft ?? "");
  const register = auth?.registerGuestCapture;
  useEffect(() => { mounted(); return () => unmounted(); }, []);
  useEffect(() => {
    if (!isGuest || !register) return;
    register(guest.capture ?? (() => Promise.resolve(draft)));
    return () => register(null);
  }, [isGuest, register, draft]);
  return <div>{isGuest ? <section>共享游客页面<span data-testid="guest-draft">{draft}</span><button onClick={() => setDraft("CCO")}>准备游客结构</button></section> : <>私人页面<iframe title="OpenScience" /></>}<AccountMenu /></div>;
}
function page() { return render(<AuthProvider><PrivateTree /></AuthProvider>); }
function openGuestLogin() {
  fireEvent.click(screen.getByRole("button", { name: "游客账号菜单" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "登录" }));
}
beforeEach(() => {
  guest.capture = null;
  localStorage.clear(); sessionStorage.clear(); mounted.mockClear(); unmounted.mockClear(); window.history.replaceState(null, "", "/?job_id=old-private");
  vi.stubGlobal("navigator", { locks: { request: (_name: string, _options: unknown, callback: () => Promise<unknown>) => callback() } });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
describe("identity boundary", () => {
  it("shows a guest sidebar account entry and only opens login on request", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(GUEST_SESSION)));
    page(); await screen.findByText("共享游客页面");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByLabelText("用户名")).toBeNull();
    openGuestLogin();
    expect(screen.getByRole("dialog", { name: "登录工作空间" })).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByLabelText("用户名"));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "游客账号菜单" }));
    expect(mounted).toHaveBeenCalledOnce();
  });
  it("shows normal login fields when Web Locks is unavailable on public HTTP", async () => {
    vi.stubGlobal("navigator", {});
    const fetch = vi.fn().mockResolvedValue(response(GUEST_SESSION));
    vi.stubGlobal("fetch", fetch);
    page(); await screen.findByText("共享游客页面");
    openGuestLogin();
    expect(screen.getByLabelText("用户名")).toBeTruthy();
    expect(screen.getByLabelText("密码")).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByLabelText("用户名"));
    expect(screen.queryByText(/SSH 隧道/)).toBeNull();
    expect(fetch).toHaveBeenCalledOnce();
  });
  it("opens the shared login form with a precise guest service prompt", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(GUEST_SESSION)));
    page(); await screen.findByText("共享游客页面");
    act(() => window.dispatchEvent(new Event("nexpoly:login-required")));
    expect(screen.getByRole("dialog", { name: "登录工作空间" })).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toBe("请登录账号。");
  });
  it("supports keyboard navigation and dismissal in the account menu", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(account("a"))));
    page(); await screen.findByText("私人页面");
    const trigger = screen.getByRole("button", { name: "账号菜单：a" });
    fireEvent.keyDown(trigger, { key: "ArrowUp" });
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "修改密码" }));
    fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "退出登录" }));
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger);
    fireEvent.click(trigger);
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("menu")).toBeNull();
  });
  it("waits for failed-session cleanup before reopening the same account on retry", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response(account("a")))
      .mockRejectedValueOnce(new Error("service unavailable"))
      .mockResolvedValueOnce(response(account("a")));
    vi.stubGlobal("fetch", fetch);
    page(); await screen.findByText("私人页面");
    let finishCleanup!: () => void;
    const clear = vi.spyOn(browserStorage, "clearPrivateBrowserData")
      .mockImplementationOnce(() => new Promise(resolve => { finishCleanup = resolve; }));
    fireEvent.focus(window);
    await screen.findByRole("alert");
    await waitFor(() => expect(clear).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
    expect(screen.queryByText("私人页面")).toBeNull();
    await act(async () => { finishCleanup(); });
    await screen.findByText("私人页面");
    expect(clear).toHaveBeenCalledOnce();
  });
  it("does not submit an old login after a delayed guest capture and cross-tab account change", async () => {
    let complete!: (value: string) => void;
    const capture = vi.fn(() => new Promise<string>(resolve => { complete = resolve; }));
    guest.capture = capture;
    const fetch = vi.fn().mockResolvedValueOnce(response(GUEST_SESSION)).mockResolvedValueOnce(response(account("b")));
    vi.stubGlobal("fetch", fetch);
    page(); await screen.findByText("共享游客页面");
    openGuestLogin();
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "a" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "password" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    expect(capture).toHaveBeenCalledOnce();
    fireEvent(window, new StorageEvent("storage", { key: AUTH_SYNC_KEY, newValue: "changed" }));
    await screen.findByText("b");
    await act(async () => { complete("old-guest-draft"); });
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(screen.getByText("b")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "导入本次游客画板" })).toBeNull();
  });
  it("waits for identity before mounting the shared guest tree without OpenScience", async () => {
    let resolve!: (response: Response) => void;
    const fetch = vi.fn((_url: RequestInfo | URL) => new Promise<Response>(done => { resolve = done; })); vi.stubGlobal("fetch", fetch);
    page(); expect(mounted).not.toHaveBeenCalled(); expect(document.querySelector("iframe")).toBeNull();
    await act(async () => resolve(response(GUEST_SESSION)));
    expect(await screen.findByText("共享游客页面")).toBeTruthy(); expect(mounted).toHaveBeenCalledOnce();
    expect(document.querySelector("iframe")).toBeNull();
    expect(fetch).toHaveBeenCalledOnce(); expect(String(fetch.mock.calls[0][0])).toBe("/api/v1/auth/session");
    expect(window.location.search).toBe("");
  });
  it("clears legacy unowned storage before allowing an authenticated workspace", async () => {
    localStorage.setItem("polyprop.pdfSimilarityDemo.uploadHistory", "private"); sessionStorage.setItem("nexpoly:md-simulation:draft", "private"); localStorage.setItem("unrelated", "keep");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(account("a")))); page();
    await screen.findByText("私人页面"); expect(localStorage.getItem("polyprop.pdfSimilarityDemo.uploadHistory")).toBeNull();
    expect(sessionStorage.getItem("nexpoly:md-simulation:draft")).toBeNull(); expect(localStorage.getItem("unrelated")).toBe("keep");
  });
  it("preserves verified shared history in a new tab only after the same HTTP session is confirmed", async () => {
    let finish!: (value: Response) => void;
    const fetch = vi.fn().mockResolvedValueOnce(response(account("a")))
      .mockImplementationOnce(() => new Promise<Response>(resolve => { finish = resolve; }));
    vi.stubGlobal("fetch", fetch);
    const first = page(); await screen.findByText("私人页面");
    expect(localStorage.getItem(AUTH_SHARED_OWNER_KEY)).toBe("a:session-a");
    localStorage.setItem("polyprop.pdfSimilarityDemo.uploadHistory", "a-private-history");
    first.unmount(); sessionStorage.clear();
    sessionStorage.setItem("nexpoly:md-simulation:draft", "unowned-tab-draft");
    sessionStorage.setItem("unrelated", "keep");
    const clear = vi.spyOn(browserStorage, "clearPrivateBrowserData");
    page();
    expect(screen.queryByText("私人页面")).toBeNull();
    await act(async () => { finish(response(account("a"))); });
    await screen.findByText("私人页面");
    expect(clear).not.toHaveBeenCalled();
    expect(localStorage.getItem("polyprop.pdfSimilarityDemo.uploadHistory")).toBe("a-private-history");
    expect(sessionStorage.getItem("nexpoly:md-simulation:draft")).toBeNull();
    expect(sessionStorage.getItem("unrelated")).toBe("keep");
    expect(sessionStorage.getItem(AUTH_OWNER_KEY)).toBe("a:session-a");
  });
  it.each([
    { name: "different account", next: account("b"), tabOwner: null, sharedOwner: "a:session-a" },
    { name: "same account with a new session", next: { ...account("a"), session_id: "session-a-new" }, tabOwner: null, sharedOwner: "a:session-a" },
    { name: "guest", next: GUEST_SESSION, tabOwner: null, sharedOwner: "a:session-a" },
    { name: "conflicting tab owner", next: account("a"), tabOwner: "b:session-b", sharedOwner: "a:session-a" },
    { name: "known guest tab", next: account("a"), tabOwner: "", sharedOwner: "a:session-a" },
    { name: "missing shared proof", next: account("a"), tabOwner: null, sharedOwner: null },
    { name: "malformed shared proof", next: account("a"), tabOwner: null, sharedOwner: "a" },
  ])("clears uncertain or changed shared ownership: $name", async ({ next, tabOwner, sharedOwner }) => {
    if (tabOwner !== null) sessionStorage.setItem(AUTH_OWNER_KEY, tabOwner);
    if (sharedOwner !== null) localStorage.setItem(AUTH_SHARED_OWNER_KEY, sharedOwner);
    localStorage.setItem("polyprop.pdfSimilarityDemo.uploadHistory", "old-private-history");
    sessionStorage.setItem("nexpoly:md-simulation:draft", "old-private-draft");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(next)));
    const clear = vi.spyOn(browserStorage, "clearPrivateBrowserData");
    page(); await screen.findByText(next.authenticated ? "私人页面" : "共享游客页面");
    expect(clear).toHaveBeenCalledOnce();
    expect(localStorage.getItem("polyprop.pdfSimilarityDemo.uploadHistory")).toBeNull();
    expect(sessionStorage.getItem("nexpoly:md-simulation:draft")).toBeNull();
    expect(localStorage.getItem(AUTH_SHARED_OWNER_KEY)).toBe(next.authenticated ? `${next.user!.id}:${next.session_id}` : null);
  });
  it("removes shared ownership proof when verification fails before a new tab opens", async () => {
    localStorage.setItem(AUTH_SHARED_OWNER_KEY, "a:session-a");
    localStorage.setItem("polyprop.pdfSimilarityDemo.uploadHistory", "a-private-history");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("service unavailable")));
    page(); await screen.findByRole("alert");
    await waitFor(() => expect(localStorage.getItem(AUTH_SHARED_OWNER_KEY)).toBeNull());
    expect(localStorage.getItem("polyprop.pdfSimilarityDemo.uploadHistory")).toBeNull();
    expect(screen.queryByText("私人页面")).toBeNull();
  });
  it("unmounts all private state and clears drafts on logout before showing guest", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response(account("a"))).mockResolvedValueOnce(new Response(null, { status: 204 })); vi.stubGlobal("fetch", fetch);
    page(); await screen.findByText("私人页面"); sessionStorage.setItem("nexpoly.assistant.tg.session.v2", "a-secret");
    fireEvent.click(screen.getByRole("button", { name: "账号菜单：a" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "退出登录" }));
    await screen.findByText("共享游客页面"); expect(unmounted).toHaveBeenCalledOnce(); expect(document.querySelector("iframe")).toBeNull();
    expect(sessionStorage.getItem("nexpoly.assistant.tg.session.v2")).toBeNull(); expect(window.location.search).toBe("");
    const init = fetch.mock.calls[1][1]; expect(init.headers.get("X-CSRF-Token")).toBe("csrf-a");
  });
  it("handles a different account from another tab by remounting a clean private tree", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response(account("a"))).mockResolvedValueOnce(response(account("b"))); vi.stubGlobal("fetch", fetch);
    page(); await screen.findByText("a"); await waitFor(() => expect(mounted).toHaveBeenCalledOnce()); sessionStorage.setItem("nexpoly:monomer-md-simulation:draft", "a-secret");
    fireEvent(window, new StorageEvent("storage", { key: AUTH_SYNC_KEY, newValue: "changed" }));
    await screen.findByText("b"); await waitFor(() => expect(mounted).toHaveBeenCalledTimes(2)); expect(unmounted).toHaveBeenCalledOnce();
    expect(sessionStorage.getItem("nexpoly:monomer-md-simulation:draft")).toBeNull();
  });
  it("requires initial password change before mounting any private page", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(account("a", true)))); page();
    await screen.findByRole("heading", { name: "修改密码" }); expect(mounted).not.toHaveBeenCalled(); expect(document.querySelector("iframe")).toBeNull();
  });
  it("keeps the workspace closed on authentication service failure and clears private storage", async () => {
    sessionStorage.setItem("nexpoly:md-simulation:draft", "private");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("service unavailable"))); page();
    await screen.findByRole("alert"); expect(mounted).not.toHaveBeenCalled();
    await waitFor(() => expect(sessionStorage.getItem("nexpoly:md-simulation:draft")).toBeNull());
  });
  it("preserves the guest draft after an unsuccessful login", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(response(GUEST_SESSION)).mockResolvedValueOnce(new Response('{"detail":"账号或密码错误"}', { status: 401 }))); page();
    fireEvent.click(await screen.findByRole("button", { name: "准备游客结构" }));
    openGuestLogin();
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "a" } }); fireEvent.change(screen.getByLabelText("密码"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await screen.findByRole("alert"); expect(screen.getByTestId("guest-draft").textContent).toBe("CCO"); expect(mounted).toHaveBeenCalledTimes(2);
  });
  it("waits for the next login before offering draft import after mandatory password change", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(response(GUEST_SESSION)).mockResolvedValueOnce(response(account("a", true))).mockResolvedValueOnce(new Response(null, { status: 204 })));
    page(); fireEvent.click(await screen.findByRole("button", { name: "准备游客结构" }));
    openGuestLogin();
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "a" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "initial-password" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await screen.findByRole("heading", { name: "修改密码" });
    fireEvent.change(screen.getByLabelText("当前密码"), { target: { value: "initial-password" } });
    fireEvent.change(screen.getByLabelText("新密码"), { target: { value: "new-password-long" } });
    fireEvent.change(screen.getByLabelText("确认新密码"), { target: { value: "new-password-long" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码并重新登录" }));
    await screen.findByText("共享游客页面");
    expect(screen.queryByRole("heading", { name: "导入本次游客画板" })).toBeNull();
    expect(screen.getByTestId("guest-draft").textContent).toBe("CCO");
    expect(screen.getByLabelText("用户名")).toBeTruthy();
  });
  it("requires explicit confirmation before carrying a guest structure into an account", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(response(GUEST_SESSION)).mockResolvedValueOnce(response(account("a")))); page();
    fireEvent.click(await screen.findByRole("button", { name: "准备游客结构" }));
    openGuestLogin();
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "a" } }); fireEvent.change(screen.getByLabelText("密码"), { target: { value: "password" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await screen.findByRole("heading", { name: "导入本次游客画板" }); expect(mounted).toHaveBeenCalledOnce(); expect(unmounted).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "从空白开始" })); await screen.findByText("私人页面");
  });
});
