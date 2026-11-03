// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useUsageRead } from "./useUsageRead";

afterEach(cleanup);

it("retains the last success during same-query refresh and exposes a failed refresh", async () => {
  let reject!: (error: Error) => void;
  const read = vi.fn().mockResolvedValueOnce(7).mockImplementationOnce(() => new Promise((_, fail) => { reject = fail; }));
  const { result, rerender } = renderHook(({ revision }) => useUsageRead("a", read, revision), { initialProps: { revision: 0 } });
  await waitFor(() => expect(result.current.data).toBe(7));
  rerender({ revision: 1 });
  expect(result.current.data).toBe(7);
  expect(result.current.loading).toBe(true);
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  await act(async () => reject(new Error("network unavailable")));
  expect(result.current.data).toBe(7);
  expect(result.current.error).toBeInstanceOf(Error);
  expect(result.current.loading).toBe(false);
});

it("clears results for a changed query and discards its predecessor's late response", async () => {
  let finishOld!: (value: string) => void;
  let finishNew!: (value: string) => void;
  const read = vi.fn().mockResolvedValueOnce("first page")
    .mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve; }))
    .mockImplementationOnce(() => new Promise(resolve => { finishNew = resolve; }));
  const { result, rerender } = renderHook(({ key, revision }) => useUsageRead(key, read, revision), { initialProps: { key: "a", revision: 0 } });
  await waitFor(() => expect(result.current.data).toBe("first page"));
  rerender({ key: "a", revision: 1 });
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  rerender({ key: "b", revision: 1 });
  expect(result.current.data).toBeUndefined();
  await waitFor(() => expect(read).toHaveBeenCalledTimes(3));
  expect(read.mock.calls[1][0].aborted).toBe(true);
  await act(async () => finishOld("retired page"));
  expect(result.current.data).toBeUndefined();
  await act(async () => finishNew("second page"));
  expect(result.current.data).toBe("second page");
});

it("retries independently and aborts on unmount", async () => {
  const read = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(3);
  const { result, unmount } = renderHook(() => useUsageRead("a", read));
  await waitFor(() => expect(result.current.error).toBeInstanceOf(Error));
  act(() => result.current.retry());
  await waitFor(() => expect(result.current.data).toBe(3));
  expect(result.current.error).toBeUndefined();
  unmount();
  expect(read.mock.calls[1][0].aborted).toBe(true);
});

it("does not read a disabled resource", async () => {
  const read = vi.fn().mockResolvedValue(2);
  const { result, rerender } = renderHook(({ enabled }) => useUsageRead("a", read, 0, enabled), { initialProps: { enabled: false } });
  expect(result.current.loading).toBe(false);
  expect(read).not.toHaveBeenCalled();
  rerender({ enabled: true });
  await waitFor(() => expect(result.current.data).toBe(2));
});
