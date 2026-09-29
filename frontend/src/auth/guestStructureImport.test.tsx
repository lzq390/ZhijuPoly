// @vitest-environment jsdom
import { useEffect, useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AccountMenu, AuthProvider, useAuth } from "./AuthProvider";
import { GUEST_SESSION, type AuthSession } from "./session";
import { useStructureWorkspace } from "../hooks/useStructureWorkspace";
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
  const structure = useStructureWorkspace({ guest: isGuest, initialSmiles: isGuest ? auth?.guestDraft : undefined });
  const register = auth?.registerGuestCapture;
  useEffect(() => { mounted(); return () => unmounted(); }, []);
  useEffect(() => {
    if (!isGuest || !register) return;
    register(guest.capture ?? (() => Promise.resolve(draft)));
    return () => register(null);
  }, [isGuest, register, draft]);
  return <div>{isGuest ? <section>共享游客页面<span data-testid="guest-draft">{draft}</span><button onClick={() => setDraft("CCO")}>准备游客结构</button></section> : <>私人页面<output data-testid="imported-structure">{structure.smiles}</output><iframe title="OpenScience" /></>}<AccountMenu /></div>;
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
describe("guest structure login handoff", () => {
  it("hands a confirmed guest structure to the actual authenticated workspace", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response(GUEST_SESSION)).mockResolvedValueOnce(response(account("a")));
    vi.stubGlobal("fetch", fetch); page();
    fireEvent.click(await screen.findByRole("button", { name: "准备游客结构" }));
    openGuestLogin();
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "a" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "password" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await screen.findByRole("heading", { name: "导入本次游客画板" });
    fireEvent.click(screen.getByRole("button", { name: "导入结构" }));
    await screen.findByText("私人页面");
    expect(screen.getByTestId("imported-structure").textContent).toBe("CCO");
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("keeps the guest workspace mounted when capture fails before login", async () => {
    guest.capture = vi.fn().mockRejectedValue(new Error("画板尚未同步"));
    const fetch = vi.fn().mockResolvedValue(response(GUEST_SESSION));
    vi.stubGlobal("fetch", fetch); page();
    fireEvent.click(await screen.findByRole("button", { name: "准备游客结构" }));
    const original = screen.getByTestId("guest-draft");
    openGuestLogin();
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "a" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "password" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    expect((await screen.findByRole("alert")).textContent).toBe("画板尚未同步");
    expect(screen.getByTestId("guest-draft")).toBe(original);
    expect(original.textContent).toBe("CCO");
    expect(fetch).toHaveBeenCalledOnce();
    expect(unmounted).not.toHaveBeenCalled();
  });
});
