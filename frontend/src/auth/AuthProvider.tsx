import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { BarChart3, ChevronUp, KeyRound, LogIn, LogOut, UserRound, X, Crown } from "lucide-react";
import { openSubscription } from "./access";
import { fetchBillingPlans } from "../services/billing";
import { authRequest } from "./api";
import { GUEST_SESSION, getSessionEpoch, installSession, onSessionInvalidated, retireSession, type AuthSession } from "./session";
import { AUTH_OWNER_KEY, AUTH_SHARED_OWNER_KEY, AUTH_SYNC_KEY, clearPrivateBrowserData, clearPrivateSessionStorage } from "./storage";
import { randomId } from "./randomId";
import { acceptGuestDraft } from "./guestDraft";
import { isRegistrationEnabled, isSubscriptionEnabled } from "../accountFeatures";
import "./auth.css";

type AuthState = { status: "loading" | "guest" | "authenticated" | "error"; session: AuthSession; error: string | null };
type AuthContextValue = AuthState & { login: () => void; register: () => void; refreshSession: () => Promise<void>; logout: () => Promise<void>; changePassword: () => void; guestDraft: string; registerGuestCapture: (capture: (() => Promise<string>) | null) => void };
const AuthContext = createContext<AuthContextValue | null>(null);
export const useAuth = () => useContext(AuthContext);
const initial: AuthState = { status: "loading", session: GUEST_SESSION, error: null };
function ownerKey(session: AuthSession) { return session.authenticated ? `${session.user!.id}:${session.session_id}` : ""; }

export function AuthProvider({ children }: { children: ReactNode }) {
  const registrationEnabled = isRegistrationEnabled();
  const subscriptionEnabled = isSubscriptionEnabled();
  const [state, setState] = useState<AuthState>(initial);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [loginOpen, setLoginOpen] = useState(false);
  const [registerMode, setRegisterMode] = useState(false);
  const [registration, setRegistration] = useState<"loading" | "enabled" | "disabled" | "error">("loading");
  useEffect(() => {
    if (!registrationEnabled || !loginOpen || !registerMode) return;
    const controller = new AbortController(); setRegistration("loading");
    fetchBillingPlans(controller.signal).then(plans => { if (!controller.signal.aborted) setRegistration(plans.registration_enabled ? "enabled" : "disabled"); })
      .catch(() => { if (!controller.signal.aborted) setRegistration("error"); });
    return () => controller.abort();
  }, [loginOpen, registerMode, registrationEnabled]);
  const [membershipOpen, setMembershipOpen] = useState(false);
  useEffect(() => {
    const show = () => setMembershipOpen(true);
    window.addEventListener("nexpoly:membership-required", show);
    return () => window.removeEventListener("nexpoly:membership-required", show);
  }, []);
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
  const refresh = useCallback(async (invalidate = false, retainOnFailure = false) => {
    if (mutating.current && !invalidate) return;
    const requestGeneration = invalidate ? begin() : ++generation.current;
    pending.current?.abort();
    const controller = new AbortController(); pending.current = controller;
    try {
      const session = await authRequest("session", undefined, controller.signal);
      if (session) await apply(session, requestGeneration, Boolean(sessionRef.current) || !session.authenticated);
    } catch (error) { if (!controller.signal.aborted) { if (retainOnFailure) throw error; fail(error, requestGeneration); } }
  }, [apply, begin, fail]);
  useEffect(() => {
    void refresh(true);
    const invalidated = onSessionInvalidated(() => { broadcast(); void refresh(true); });
    const requestLogin = () => { setRegisterMode(false); setState(previous => ({ ...previous, error: "请登录账号。" })); setLoginOpen(true); };
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
    if (registerMode && !registrationEnabled) return;
    if (mutating.current) return;
    mutating.current = true;
    const expected = getSessionEpoch();
    let requestGeneration = generation.current;
    try {
      const draft = guestCapture.current ? await guestCapture.current() : guestDraft;
      if (expected !== getSessionEpoch()) return;
      setGuestDraft(draft);
      requestGeneration = begin();
      const session = await authRequest(registerMode ? "register" : "login", { username, password });
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
      const requestGeneration = begin(); setPasswordOpen(false); setRegisterMode(false); setLoginOpen(true);
      await apply(GUEST_SESSION, requestGeneration, true); broadcast();
    } finally { mutating.current = false; }
  };
  const refreshSession = useCallback(() => refresh(false, true), [refresh]);
  const content = state.status === "loading" ? <main className="np-auth-center" role="status">正在确认登录状态…</main>
    : state.status === "error" ? <main className="np-auth-center"><h1>暂时无法确认登录状态</h1><p role="alert">{state.error}</p><button onClick={() => void refresh(true)}>重试</button></main>
    : state.status === "authenticated" && (state.session.user!.must_change_password || passwordOpen) ? <main className="np-auth-center"><CredentialsForm mode="password" submit={password} /><button type="button" onClick={() => void logout()}>退出登录</button>{!state.session.user!.must_change_password && <button type="button" onClick={() => setPasswordOpen(false)}>返回工作空间</button>}</main>
    : state.status === "authenticated" && importPending ? <main className="np-auth-center"><h1>导入本次游客画板</h1><p>是否将本次游客体验的结构带入工作空间？导入不会自动查询或计算。</p><button type="button" onClick={() => { acceptGuestDraft(guestDraft); setGuestDraft(""); setImportPending(false); }}>导入结构</button><button type="button" onClick={() => { setGuestDraft(""); setImportPending(false); }}>从空白开始</button></main>
    : <div key={`${state.session.session_id ?? "guest"}:${getSessionEpoch()}`} className="np-auth-workspace">{children}</div>;
  return <AuthContext.Provider value={{ ...state, login: () => { setRegisterMode(false); setLoginOpen(true); }, register: () => { setRegisterMode(registrationEnabled); setLoginOpen(true); }, refreshSession, logout, changePassword: () => setPasswordOpen(true), guestDraft, registerGuestCapture: setGuestCapture }}>
    {content}
    {state.status === "authenticated" && state.session.account_type === "free" && membershipOpen && <LoginDialog label={subscriptionEnabled ? "会员功能说明" : "功能权限说明"} onClose={() => setMembershipOpen(false)}><div className="np-auth-dialog-message"><h2>{subscriptionEnabled ? "开通会员以使用此功能" : "当前账号暂无此功能权限"}</h2><p>{subscriptionEnabled ? "普通账号可使用本地画板和公开示例。会员可使用科研服务与工作空间。" : "你仍可使用本地画板和公开示例。如需使用此功能，请联系管理员。"}</p><div className="np-auth-dialog-actions">{subscriptionEnabled && <button className="is-primary" type="button" onClick={() => { setMembershipOpen(false); openSubscription(); }}>查看会员订阅</button>}<button type="button" onClick={() => setMembershipOpen(false)}>{subscriptionEnabled ? "暂不开通" : "关闭说明"}</button></div></div></LoginDialog>}
    {state.status === "guest" && loginOpen && <LoginDialog label={registerMode && registrationEnabled ? "注册账号" : "登录工作空间"} onClose={() => setLoginOpen(false)}><>{!registerMode || !registrationEnabled || registration === "enabled" ? <CredentialsForm key={registerMode && registrationEnabled ? "register" : "login"} mode={registerMode && registrationEnabled ? "register" : "login"} error={state.error} submit={login} /> : <p className="np-auth-dialog-notice" role="status">{registration === "loading" ? "正在确认注册服务…" : registration === "disabled" ? "自助注册暂未开放，请使用已有账号登录。" : "暂时无法确认注册服务，请稍后重试。"}</p>}{registrationEnabled && <div className="np-auth-dialog-switch"><span>{registerMode ? "已有账号？" : "还没有账号？"}</span><button type="button" onClick={() => { setRegisterMode(value => !value); setState(previous => ({ ...previous, error: null })); }}>{registerMode ? "返回登录" : "注册账号"}</button></div>}</></LoginDialog>}
  </AuthContext.Provider>;
}

function LoginDialog({ children, onClose, label = "登录工作空间" }: { children: ReactNode; onClose: () => void; label?: string }) {
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
    <div className="np-auth-dialog" ref={dialog} role="dialog" aria-modal="true" aria-label={label}>
      <button className="np-auth-dialog-close" type="button" aria-label={label === "注册账号" ? "关闭注册" : label === "登录工作空间" ? "关闭登录" : "关闭说明"} onClick={onClose}><X aria-hidden="true" /></button>
      {children}
    </div>
  </div>;
}
function CredentialsForm({ mode, submit, error: externalError }: {
  mode: "login" | "register" | "password"; submit: (first: string, second: string) => Promise<void>; error?: string | null;
}) {
  const [first, setFirst] = useState(""); const [second, setSecond] = useState(""); const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  const login = mode !== "password";
  const registering = mode === "register";
  const passwordId = useId();
  const passwordHintId = `${passwordId}-hint`;
  async function handle(event: FormEvent) {
    event.preventDefault(); if (busy) return;
    if ((!login || registering) && second !== confirm) { setError("两次输入的新密码不一致。"); return; }
    setBusy(true); setError(null);
    try { await submit(first, second); } catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败，请重试。"); }
    finally { setBusy(false); }
  }
  return <form className={`np-auth-form${registering ? " np-auth-form--register" : ""}`} onSubmit={event => void handle(event)}>
    {registering ? <header className="np-auth-form__register-header"><h2>注册账号</h2><p>{isSubscriptionEnabled() ? "注册后可使用本地画板和公开示例。会员服务需另行开通。" : "注册后可使用本地画板和公开示例。科研服务权限请联系管理员。"}</p></header> : <><h2>{login ? "登录工作空间" : "修改密码"}</h2><p>{login ? "使用账号和密码登录。" : "修改成功后，请使用新密码重新登录。"}</p></>}
    <label>{login ? "用户名" : "当前密码"}<input required maxLength={login ? 64 : 256} autoComplete={login ? "username" : "current-password"} type={login ? "text" : "password"} value={first} onChange={event => setFirst(event.target.value)} /></label>
    {registering ? <div className="np-auth-form__password-field"><label htmlFor={passwordId}>密码</label><span className="np-auth-form__field-hint" id={passwordHintId}>至少 12 位字符</span><input id={passwordId} aria-describedby={passwordHintId} required autoComplete="new-password" type="password" maxLength={256} minLength={12} value={second} onChange={event => setSecond(event.target.value)} /></div> : <label>{login ? "密码" : "新密码"}<input required autoComplete={login ? "current-password" : "new-password"} type="password" maxLength={256} minLength={login ? 1 : 12} value={second} onChange={event => setSecond(event.target.value)} /></label>}
    {(!login || registering) && <label>{registering ? "确认密码" : "确认新密码"}<input required type="password" autoComplete="new-password" maxLength={256} value={confirm} onChange={event => setConfirm(event.target.value)} /></label>}
    {(error || externalError) && <p role="alert">{error || externalError}</p>}
    <button type="submit" disabled={busy}>{busy ? "处理中…" : registering ? "创建账号" : login ? "登录" : "修改密码并重新登录"}</button>
  </form>;
}
export function AccountMenu({ onOpenUsage, onOpenSubscription }: { onOpenUsage?: () => void; onOpenSubscription?: () => void } = {}) {
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
      <div className="np-account-menu__summary"><strong>{username}</strong><span>{signedIn ? auth.session.account_type === "free" ? isSubscriptionEnabled() ? "普通账号 · 可开通会员" : "普通账号" : "个人工作空间" : "登录后保存和管理科研任务"}</span></div>
      {isSubscriptionEnabled() && onOpenSubscription && <button type="button" role="menuitem" onClick={() => act(onOpenSubscription)}><Crown aria-hidden="true" />会员订阅</button>}
      {signedIn ? <>
        {onOpenUsage && <button type="button" role="menuitem" onClick={() => act(onOpenUsage)}><BarChart3 aria-hidden="true" />我的用量</button>}
        <button type="button" role="menuitem" onClick={() => act(auth.changePassword)}><KeyRound aria-hidden="true" />修改密码</button>
        <button type="button" role="menuitem" onClick={() => act(() => void auth.logout())}><LogOut aria-hidden="true" />退出登录</button>
      </> : <button type="button" role="menuitem" onClick={() => act(auth.login)}><LogIn aria-hidden="true" />登录</button>}
    </div>}
    <button className="np-account-menu__trigger" ref={trigger} type="button" aria-label={signedIn ? `账号菜单：${username}` : "游客账号菜单"} aria-haspopup="menu" aria-expanded={open} aria-controls={open ? menuId : undefined} onClick={() => setOpen(value => !value)} onKeyDown={event => {
      if (event.key === "ArrowUp" || event.key === "ArrowDown") { event.preventDefault(); setOpen(true); }
    }}>
      <span className="np-account-menu__avatar" aria-hidden="true">{signedIn ? username.slice(0, 2).toUpperCase() : <UserRound />}</span>
      <span className="np-account-menu__identity"><span>{username}</span><small>{signedIn ? auth.session.account_type === "free" ? "普通账号" : auth.session.account_type === "member" ? "会员账号" : "个人账号" : "登录以使用完整功能"}</small></span>
      <ChevronUp className="np-account-menu__chevron" aria-hidden="true" />
    </button>
  </div>;
}
