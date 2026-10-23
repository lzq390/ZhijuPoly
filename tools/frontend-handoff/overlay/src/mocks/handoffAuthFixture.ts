/** Synthetic auth-only fixture. No backend calls or password storage. */
export const guestHandoffSession = () => ({ authenticated: false as const, user: null, session_id: null, csrf_token: null, capabilities: {} });
export function memberHandoffSession(username = "handoff", generation = 1) {
  return { authenticated: true as const, account_type: "member" as const,
    user: { id: "synthetic-handoff-user", username, must_change_password: false },
    session_id: `synthetic-session-${generation}`, csrf_token: `synthetic-csrf-${generation}`,
    capabilities: { dev_gpu_control: false, "openscience.use": true } };
}
type Session = ReturnType<typeof guestHandoffSession> | ReturnType<typeof memberHandoffSession>;
const objectBody = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
async function requestJson(input: RequestInfo | URL, init?: RequestInit): Promise<Record<string, unknown>> {
  try {
    if (typeof init?.body === "string") return objectBody(JSON.parse(init.body));
    if (init?.body instanceof FormData) return Object.fromEntries(init.body.entries());
    if (input instanceof Request) return objectBody(await input.clone().json());
  } catch { /* Malformed input is an empty body, as in the original fixture. */ }
  return {};
}
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
/** Return undefined for non-auth requests so an explicit scenario provider can
 * handle them. This handler is not a scientific/business fixture replacement. */
export function createHandoffAuthFixture(options: { pageUrl?: string; guest?: boolean } = {}) {
  const page = new URL(options.pageUrl ?? "http://handoff.local/");
  let session: Session = options.guest ? guestHandoffSession() : memberHandoffSession();
  let generation = 1;
  return async (input: RequestInfo | URL, init?: RequestInit): Promise<Response | undefined> => {
    const url = new URL(typeof input === "string" || input instanceof URL ? String(input) : input.url, page);
    if (url.origin !== page.origin || !url.pathname.startsWith("/api/v1/auth/")) return undefined;
    const path = url.pathname.slice("/api/v1".length);
    const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (path === "/auth/session" && method === "GET") return json(session);
    const body = await requestJson(input, init);
    if (path === "/auth/login" && method === "POST") {
      if (!String(body.username ?? "").trim() || !String(body.password ?? "")) return json({ detail: "Enter a demonstration username and password." }, 400);
      session = memberHandoffSession(String(body.username).trim(), ++generation); return json(session);
    }
    if (path === "/auth/logout" && method === "POST") { session = guestHandoffSession(); return new Response(null, { status: 204 }); }
    if (path === "/auth/password" && method === "POST") {
      if (!session.authenticated) return json({ detail: "Demonstration login required." }, 401);
      if (!String(body.current_password ?? "") || String(body.new_password ?? "").length < 12) return json({ detail: "Demonstration password requires at least 12 characters." }, 400);
      session = guestHandoffSession(); return new Response(null, { status: 204 });
    }
    return json({ code: "HANDOFF_AUTH_NOT_IMPLEMENTED", synthetic: true }, 501);
  };
}
