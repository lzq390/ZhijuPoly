import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ChevronUp, KeyRound, LogIn, LogOut, UserRound, X } from "lucide-react";
import { authRequest } from "./api";
import { GUEST_SESSION, getSessionEpoch, installSession, onSessionInvalidated, retireSession, type AuthSession } from "./session";
import { AUTH_OWNER_KEY, AUTH_SHARED_OWNER_KEY, AUTH_SYNC_KEY, clearPrivateBrowserData, clearPrivateSessionStorage } from "./storage";
import { randomId } from "./randomId";
import { acceptGuestDraft } from "./guestDraft";
import "./auth.css";

type AuthState = { status: "loading" | "guest" | "authenticated" | "error"; session: AuthSession; error: string | null };
type AuthContextValue = AuthState & { login: () => void; logout: () => Promise<void>; changePassword: () => void; guestDraft: string; registerGuestCapture: (capture: (() => Promise<string>) | null) => void };
const AuthContext = createContext<AuthContextValue | null>(null);
export const useAuth = () => useContext(AuthContext);
const initial: AuthState = { status: "loading", session: GUEST_SESSION, error: null };
function ownerKey(session: AuthSession) { return session.authenticated ? `${session.user!.id}:${session.session_id}` : ""; }

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>(initial);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [loginOpen, setLoginOpen] = useState(false);
  const [guestDraft, setGuestDraft] = useState("");
  const guestCapture = useRef<(() => Promise<string>) | null>(null);
  const setGuestCapture = useCallback((capture: (() => Promise<string>) | null) => { guestCapture.current = capture; }, []);
  const [importPending, setImportPending] = useState(false);
  const generation = useRef(0);
  const mutating = useRef(false);
  const pending = useRef<AbortController | null>(null);
  const channel = useRef<BroadcastChannel | null>(null);
  const sessionRef = useRef<AuthSession | null>(null);
  const cleanup = useRef<Promise<void>>(Promise.resolve());
  const broadcast = useCallback(() => {
    const event = { id: randomId(), timestamp: Date.now() };
    channel.current?.postMessage(event);
    try { localStorage.setItem(AUTH_SYNC_KEY, JSON.stringify(event)); } catch { /* BroadcastChannel remains available. */ }
  }, []);
  const begin = useCallback(() => {
    ++generation.current;
    pending.current?.abort();
    retireSession();
    setState(previous => ({ ...previous, status: "loading", error: null }));
    return generation.current;
  }, []);
  const apply = useCallback(async (session: AuthSession, requestGeneration: number, clearUrl: boolean) => {
    if (requestGeneration !== generation.current) return;
    const prior = sessionRef.current;
    let storedOwner: string | null = null;
    let sharedOwner: string | null = null;
    let tabOwnerReadable = false;
    try { storedOwner = sessionStorage.getItem(AUTH_OWNER_KEY); tabOwnerReadable = true; } catch { /* Clear on uncertain ownership. */ }
    try { sharedOwner = localStorage.getItem(AUTH_SHARED_OWNER_KEY); } catch { /* No shared ownership proof. */ }
    // A genuinely new tab has no tab owner. Preserve origin-shared data only
    // after the HTTP response confirms the exact owner/session already verified
    // by another tab. A prior owner, guest or new login session never qualifies.
    const sameSessionNewTab = !prior && tabOwnerReadable && storedOwner === null && session.authenticated && sharedOwner === ownerKey(session);
    if (!prior || ownerKey(prior) !== ownerKey(session)) {
      retireSession();
      setState(previous => ({ ...previous, status: "loading" }));
      if (!session.authenticated || storedOwner !== ownerKey(session)) {
        cleanup.current = cleanup.current.catch(() => {}).then(() => {
          // Unknown tab-local drafts must still be removed even though another
          // tab's verified shared history/previews belong to this same session.
          if (sameSessionNewTab) clearPrivateSessionStorage();
          else return clearPrivateBrowserData(clearUrl);
        });
        await cleanup.current;
      }
    }
    await cleanup.current;
    if (requestGeneration !== generation.current) return;
    installSession(session);
    sessionRef.current = session;
    try { sessionStorage.setItem(AUTH_OWNER_KEY, ownerKey(session)); } catch { /* Next startup will clear. */ }
    try {
      if (session.authenticated) localStorage.setItem(AUTH_SHARED_OWNER_KEY, ownerKey(session));
      else localStorage.removeItem(AUTH_SHARED_OWNER_KEY);
    } catch { /* New tabs cannot preserve shared state without this proof. */ }
    setState({ status: session.authenticated ? "authenticated" : "guest", session, error: null });
  }, []);
  const fail = useCallback((error: unknown, requestGeneration: number) => {
    if (requestGeneration !== generation.current) return;
    retireSession();
    // A retry must await cleanup even when it resolves to the same account.
    // Otherwise delayed IndexedDB clearing can erase the newly mounted tree.
    sessionRef.current = null;
    cleanup.current = cleanup.current.catch(() => {}).then(() => clearPrivateBrowserData());
    void cleanup.current.catch(() => {});
    setState({ status: "error", session: GUEST_SESSION, error: error instanceof Error ? error.message : "无法确认登录状态。" });
  }, []);
  const refresh = useCallback(async (invalidate = false) => {
    if (mutating.current && !invalidate) return;
    const requestGeneration = invalidate ? begin() : ++generation.current;
    pending.current?.abort();
    const controller = new AbortController(); pending.current = controller;
    try {
      const session = await authRequest("session", undefined, controller.signal);
      if (session) await apply(session, requestGeneration, Boolean(sessionRef.current) || !session.authenticated);
    } catch (error) { if (!controller.signal.aborted) fail(error, requestGeneration); }
  }, [apply, begin, fail]);
  useEffect(() => {
    void refresh(true);
    const invalidated = onSessionInvalidated(() => { broadcast(); void refresh(true); });
    const requestLogin = () => { setState(previous => ({ ...previous, error: "请登录账号。" })); setLoginOpen(true); };
    window.addEventListener("nexpoly:login-required", requestLogin);
    const received = new Set<string>();
    const synchronize = (event?: { id?: unknown }) => {
      if (typeof event?.id === "string") {
        if (received.has(event.id)) return;
        if (received.size >= 128) received.clear();
        received.add(event.id);
      }
      setGuestDraft(""); setImportPending(false); setLoginOpen(false); void refresh(true);
    };
    if (typeof BroadcastChannel !== "undefined") {
      channel.current = new BroadcastChannel("nexpoly-auth");
      channel.current.onmessage = event => synchronize(event.data);
    }
    const storageChanged = (event: StorageEvent) => {
      if (event.key !== AUTH_SYNC_KEY) return;
      try { synchronize(JSON.parse(event.newValue ?? "null")); } catch { synchronize(); }
    };
    const visible = () => { if (document.visibilityState === "visible") void refresh(); };
    const restored = () => { void refresh(); };
    window.addEventListener("storage", storageChanged);
    window.addEventListener("focus", restored);
    window.addEventListener("pageshow", restored);
    document.addEventListener("visibilitychange", visible);
    return () => {
      ++generation.current; pending.current?.abort(); retireSession(); invalidated();
      window.removeEventListener("nexpoly:login-required", requestLogin);
      channel.current?.close(); channel.current = null;
      window.removeEventListener("storage", storageChanged);
      window.removeEventListener("focus", restored);
      window.removeEventListener("pageshow", restored);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [broadcast, refresh]);
  const login = async (username: string, password: string) => {
    if (mutating.current) return;
    mutating.current = true;
    const expected = getSessionEpoch();
    let requestGeneration = generation.current;
    try {
      const draft = guestCapture.current ? await guestCapture.current() : guestDraft;
      if (expected !== getSessionEpoch()) return;
      setGuestDraft(draft);
      requestGeneration = begin();
      const session = await authRequest("login", { username, password });
      if (!session) throw new Error("登录服务未返回会话。");
      if (requestGeneration !== generation.current) return;
      setImportPending(Boolean(draft.trim()));
      setLoginOpen(false);
      await apply(session, requestGeneration, true); broadcast();
    } catch (error) {
      if (requestGeneration !== generation.current) return;
      installSession(GUEST_SESSION);
      setLoginOpen(true);
      setState({ status: "guest", session: GUEST_SESSION, error: error instanceof Error ? error.message : "登录失败" });
    } finally { mutating.current = false; }
  };
  const logout = async () => {
    if (mutating.current) return;
    mutating.current = true;
    const requestGeneration = begin(); setGuestDraft(""); setImportPending(false); setPasswordOpen(false); setLoginOpen(false);
    try {
      await Promise.all([clearPrivateBrowserData(), authRequest("logout", {})]);
      await apply(GUEST_SESSION, requestGeneration, true); broadcast();
    } catch (error) { fail(error, requestGeneration); } finally { mutating.current = false; }
  };
  const password = async (currentPassword: string, newPassword: string) => {
    if (mutating.current) return;
    mutating.current = true;
    const expected = getSessionEpoch();
    try {
      await authRequest("password", { current_password: currentPassword, new_password: newPassword });
      if (expected !== getSessionEpoch()) return;
      const requestGeneration = begin(); setPasswordOpen(false); setLoginOpen(true);
      await apply(GUEST_SESSION, requestGeneration, true); broadcast();
    } finally { mutating.current = false; }
  };
  const content = state.status === "loading" ? <main className="np-auth-center" role="status">正在确认登录状态…</main>
    : state.status === "error" ? <main className="np-auth-center"><h1>暂时无法确认登录状态</h1><p role="alert">{state.error}</p><button onClick={() => void refresh(true)}>重试</button></main>
    : state.status === "authenticated" && (state.session.user!.must_change_password || passwordOpen) ? <main className="np-auth-center"><CredentialsForm mode="password" submit={password} /><button type="button" onClick={() => void logout()}>退出登录</button>{!state.session.user!.must_change_password && <button type="button" onClick={() => setPasswordOpen(false)}>返回工作空间</button>}</main>
    : state.status === "authenticated" && importPending ? <main className="np-auth-center"><h1>导入本次游客画板</h1><p>是否将本次游客体验的结构带入工作空间？导入不会自动查询或计算。</p><button type="button" onClick={() => { acceptGuestDraft(guestDraft); setGuestDraft(""); setImportPending(false); }}>导入结构</button><button type="button" onClick={() => { setGuestDraft(""); setImportPending(false); }}>从空白开始</button></main>
    : <div key={`${state.session.session_id ?? "guest"}:${getSessionEpoch()}`} className="np-auth-workspace">{children}</div>;
  return <AuthContext.Provider value={{ ...state, login: () => setLoginOpen(true), logout, changePassword: () => setPasswordOpen(true), guestDraft, registerGuestCapture: setGuestCapture }}>
    {content}
    {state.status === "guest" && loginOpen && <LoginDialog onClose={() => setLoginOpen(false)}><CredentialsForm mode="login" error={state.error} submit={login} /></LoginDialog>}
  </AuthContext.Provider>;
}

function LoginDialog({ children, onClose }: { children: ReactNode; onClose: () => void }) {
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    (dialog.current?.querySelector<HTMLInputElement>("input") ?? dialog.current?.querySelector<HTMLButtonElement>("button"))?.focus();
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); onClose(); return; }
      if (event.key !== "Tab") return;
      const focusable = Array.from(dialog.current?.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), [tabindex='0']") ?? []);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", keyboard);
    return () => { document.removeEventListener("keydown", keyboard); if (previous?.isConnected) previous.focus(); };
  }, [onClose]);
  return <div className="np-auth-dialog-backdrop" onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="np-auth-dialog" ref={dialog} role="dialog" aria-modal="true" aria-label="登录工作空间">
      <button className="np-auth-dialog-close" type="button" aria-label="关闭登录" onClick={onClose}><X aria-hidden="true" /></button>
      {children}
    </div>
  </div>;
}
function CredentialsForm({ mode, submit, error: externalError }: {
  mode: "login" | "password"; submit: (first: string, second: string) => Promise<void>; error?: string | null;
}) {
  const [first, setFirst] = useState(""); const [second, setSecond] = useState(""); const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  const login = mode === "login";
  async function handle(event: FormEvent) {
    event.preventDefault(); if (busy) return;
    if (!login && second !== confirm) { setError("两次输入的新密码不一致。"); return; }
    setBusy(true); setError(null);
    try { await submit(first, second); } catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败，请重试。"); }
    finally { setBusy(false); }
  }
  return <form className="np-auth-form" onSubmit={event => void handle(event)}>
    <h2>{login ? "登录工作空间" : "修改密码"}</h2>
    <p>{login ? "使用运维人员提供的账号登录。" : "修改成功后，请使用新密码重新登录。"}</p>
    <label>{login ? "用户名" : "当前密码"}<input required maxLength={login ? 64 : 256} autoComplete={login ? "username" : "current-password"} type={login ? "text" : "password"} value={first} onChange={event => setFirst(event.target.value)} /></label>
    <label>{login ? "密码" : "新密码"}<input required autoComplete={login ? "current-password" : "new-password"} type="password" maxLength={256} minLength={login ? 1 : 12} value={second} onChange={event => setSecond(event.target.value)} /></label>
    {!login && <label>确认新密码<input required type="password" autoComplete="new-password" maxLength={256} value={confirm} onChange={event => setConfirm(event.target.value)} /></label>}
    {(error || externalError) && <p role="alert">{error || externalError}</p>}
    <button type="submit" disabled={busy}>{busy ? "处理中…" : login ? "登录" : "修改密码并重新登录"}</button>
  </form>;
}
export function AccountMenu() {
  const auth = useAuth();
  const [open, setOpen] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const menuId = useId();
  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const outside = (event: PointerEvent) => { if (!container.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  if (!auth || !["guest", "authenticated"].includes(auth.status)) return null;
  const signedIn = auth.session.authenticated;
  const username = signedIn ? auth.session.user!.username : "游客";
  const close = () => { setOpen(false); trigger.current?.focus(); };
  const act = (action: () => void) => { close(); action(); };
  return <div className="np-account-menu" ref={container} aria-label="当前账号" onBlur={event => {
    if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget as Node)) setOpen(false);
  }}>
    {open && <div className="np-account-menu__popover" role="menu" aria-label="账号操作" id={menuId} ref={menu} onKeyDown={event => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const items = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>("button") ?? []);
      const index = items.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items[next]?.focus();
    }}>
      <div className="np-account-menu__summary"><strong>{username}</strong><span>{signedIn ? "个人工作空间" : "登录后保存和管理科研任务"}</span></div>
      {signedIn ? <>
        <button type="button" role="menuitem" onClick={() => act(auth.changePassword)}><KeyRound aria-hidden="true" />修改密码</button>
        <button type="button" role="menuitem" onClick={() => act(() => void auth.logout())}><LogOut aria-hidden="true" />退出登录</button>
      </> : <button type="button" role="menuitem" onClick={() => act(auth.login)}><LogIn aria-hidden="true" />登录</button>}
    </div>}
    <button className="np-account-menu__trigger" ref={trigger} type="button" aria-label={signedIn ? `账号菜单：${username}` : "游客账号菜单"} aria-haspopup="menu" aria-expanded={open} aria-controls={open ? menuId : undefined} onClick={() => setOpen(value => !value)} onKeyDown={event => {
      if (event.key === "ArrowUp" || event.key === "ArrowDown") { event.preventDefault(); setOpen(true); }
    }}>
      <span className="np-account-menu__avatar" aria-hidden="true">{signedIn ? username.slice(0, 2).toUpperCase() : <UserRound />}</span>
      <span className="np-account-menu__identity"><span>{username}</span><small>{signedIn ? "个人账号" : "登录以使用完整功能"}</small></span>
      <ChevronUp className="np-account-menu__chevron" aria-hidden="true" />
    </button>
  </div>;
}
