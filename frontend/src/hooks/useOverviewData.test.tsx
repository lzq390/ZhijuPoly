// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GUEST_SESSION, installSession, retireSession, type AuthSession } from "../auth/session";
import type { OverviewData } from "../types/overview";
import { useOverviewData } from "./useOverviewData";

const mocks = vi.hoisted(() => ({ read: vi.fn(), auth: null as null | { status: string; session: AuthSession } }));
vi.mock("../services/overviewData", () => ({ fetchOverviewData: mocks.read }));
vi.mock("../auth/AuthProvider", () => ({ useAuth: () => mocks.auth }));

const member = (id: string): AuthSession => ({ authenticated: true, user: { id, username: id, must_change_password: false }, session_id: `session-${id}`, csrf_token: "test-csrf", capabilities: {}, account_type: "member" });
function snapshot(asOf: string): OverviewData {
  return {
    asOf,
    structures: { status: "ready", data: [] }, taskSources: [],
    knowledge: { status: "ready", data: { sources: [] } },
    knowledgeActivity: { status: "ready", data: { dailyAvailable: true, recentAvailable: true, daily: [], recent: [], warnings: [] } },
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(complete => { resolve = complete; });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.read.mockReset();
  mocks.auth = null;
  retireSession();
  installSession(member("a"));
  mocks.read.mockResolvedValue(snapshot("first"));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

describe("useOverviewData access and refresh", () => {
  it("does not request private data for guests or restricted accounts", () => {
    installSession(GUEST_SESSION);
    const { result, rerender } = renderHook(() => useOverviewData());
    expect(result.current.access).toBe("guest");
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(false);
    act(() => result.current.refresh());
    expect(mocks.read).not.toHaveBeenCalled();
    act(() => { retireSession(); installSession({ ...member("free"), account_type: "free" }); });
    rerender();
    expect(result.current.access).toBe("restricted");
    act(() => result.current.refresh());
    expect(mocks.read).not.toHaveBeenCalled();
  });

  it("waits until the auth context confirms the installed member session", async () => {
    mocks.auth = { status: "loading", session: member("a") };
    const { result, rerender } = renderHook(() => useOverviewData());
    expect(result.current.access).toBe("checking");
    expect(mocks.read).not.toHaveBeenCalled();
    mocks.auth = { status: "authenticated", session: member("a") };
    rerender();
    await waitFor(() => expect(result.current.data?.asOf).toBe("first"));
    expect(result.current.access).toBe("member");
  });

  it("retains the same member's last snapshot on a failed refresh and permits retry", async () => {
    const { result } = renderHook(() => useOverviewData());
    await waitFor(() => expect(result.current.data?.asOf).toBe("first"));
    mocks.read.mockRejectedValueOnce(new Error("暂时离线"));
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.error).toBe("暂时离线"));
    expect(result.current.data?.asOf).toBe("first");
    expect(result.current.refreshing).toBe(false);
    mocks.read.mockResolvedValueOnce(snapshot("retried"));
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.data?.asOf).toBe("retried"));
    expect(result.current.error).toBeNull();
  });

  it("clears retired data immediately and rejects late work from the previous account", async () => {
    const oldRefresh = deferred<OverviewData>();
    const newRead = deferred<OverviewData>();
    mocks.read.mockResolvedValueOnce(snapshot("account-a")).mockReturnValueOnce(oldRefresh.promise).mockReturnValueOnce(newRead.promise);
    const { result, rerender } = renderHook(() => useOverviewData());
    await waitFor(() => expect(result.current.data?.asOf).toBe("account-a"));
    act(() => result.current.refresh());
    const oldSignal = mocks.read.mock.calls[1][0] as AbortSignal;
    act(() => retireSession());
    expect(result.current.data).toBeNull();
    expect(result.current.access).toBe("checking");
    expect(oldSignal.aborted).toBe(true);
    installSession(member("b"));
    rerender();
    await waitFor(() => expect(mocks.read).toHaveBeenCalledTimes(3));
    await act(async () => newRead.resolve(snapshot("account-b")));
    expect(result.current.data?.asOf).toBe("account-b");
    await act(async () => oldRefresh.resolve(snapshot("late-account-a")));
    expect(result.current.data?.asOf).toBe("account-b");
  });

  it("aborts pending reads on unmount", async () => {
    const pending = deferred<OverviewData>();
    mocks.read.mockReturnValue(pending.promise);
    const { unmount } = renderHook(() => useOverviewData());
    const signal = mocks.read.mock.calls[0][0] as AbortSignal;
    unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve(snapshot("after-unmount")));
  });

  it("restarts an aborted StrictMode mount without publishing its obsolete response", async () => {
    const first = deferred<OverviewData>();
    mocks.read.mockReturnValueOnce(first.promise).mockResolvedValueOnce(snapshot("strict-current"));
    const { result } = renderHook(() => useOverviewData(), { reactStrictMode: true });
    await waitFor(() => expect(result.current.data?.asOf).toBe("strict-current"));
    expect(mocks.read).toHaveBeenCalledTimes(2);
    expect((mocks.read.mock.calls[0][0] as AbortSignal).aborted).toBe(true);
    await act(async () => first.resolve(snapshot("strict-retired")));
    expect(result.current.data?.asOf).toBe("strict-current");
  });

  it("polls only visible pages, refreshes on return, and never overlaps reads", async () => {
    vi.useFakeTimers();
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    const slow = deferred<OverviewData>();
    mocks.read.mockResolvedValueOnce(snapshot("initial")).mockReturnValueOnce(slow.promise).mockResolvedValue(snapshot("returned"));
    const { result } = renderHook(() => useOverviewData());
    await act(async () => {});
    expect(result.current.data?.asOf).toBe("initial");
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(mocks.read).toHaveBeenCalledTimes(2);
    expect(result.current.refreshing).toBe(true);
    act(() => { window.dispatchEvent(new Event("focus")); result.current.refresh(); });
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(mocks.read).toHaveBeenCalledTimes(2);
    await act(async () => slow.resolve(snapshot("polled")));
    visibility.mockReturnValue("hidden");
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    act(() => window.dispatchEvent(new Event("focus")));
    expect(mocks.read).toHaveBeenCalledTimes(2);
    visibility.mockReturnValue("visible");
    await act(async () => document.dispatchEvent(new Event("visibilitychange")));
    expect(mocks.read).toHaveBeenCalledTimes(3);
    expect(result.current.data?.asOf).toBe("returned");
  });
});
