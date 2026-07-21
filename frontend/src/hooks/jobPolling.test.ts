import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiRequestError } from "../services/api";
import { JOB_POLL_BACKOFF_MS, pollJobWithBackoff } from "./jobPolling";

async function flush() {
  await Promise.resolve();
  await Promise.resolve();
}

describe("pollJobWithBackoff", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("uses 1.5/3/6/10 second transient-error backoff without retrying task creation", async () => {
    const controller = new AbortController();
    const terminalJob = { status: "completed" };
    const fetchJob = vi.fn()
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockRejectedValueOnce(new ApiRequestError(500, "temporary"))
      .mockRejectedValueOnce(new ApiRequestError(401, "temporary auth propagation"))
      .mockRejectedValueOnce(new Error("connection reset"))
      .mockResolvedValueOnce(terminalJob);
    const onJob = vi.fn();

    const polling = pollJobWithBackoff({
      signal: controller.signal,
      fetchJob,
      isTerminal: (job) => job.status === "completed",
      onJob,
      onExpired: vi.fn(),
      intervalMs: 1_000
    });
    await flush();
    expect(fetchJob).toHaveBeenCalledTimes(1);

    for (let index = 0; index < JOB_POLL_BACKOFF_MS.length; index += 1) {
      const delay = JOB_POLL_BACKOFF_MS[index];
      await vi.advanceTimersByTimeAsync(delay - 1);
      expect(fetchJob).toHaveBeenCalledTimes(index + 1);
      await vi.advanceTimersByTimeAsync(1);
      expect(fetchJob).toHaveBeenCalledTimes(index + 2);
    }

    await expect(polling).resolves.toBe("terminal");
    expect(onJob).toHaveBeenCalledWith(terminalJob);
  });

  it("resets the backoff after a successful status read", async () => {
    const controller = new AbortController();
    const fetchJob = vi.fn()
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValueOnce({ status: "running" })
      .mockRejectedValueOnce(new TypeError("offline again"))
      .mockResolvedValueOnce({ status: "completed" });

    const polling = pollJobWithBackoff({
      signal: controller.signal,
      fetchJob,
      isTerminal: (job) => job.status === "completed",
      onJob: vi.fn(),
      onExpired: vi.fn(),
      intervalMs: 500
    });
    await flush();
    await vi.advanceTimersByTimeAsync(1_500);
    await vi.advanceTimersByTimeAsync(500);
    expect(fetchJob).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(1_499);
    expect(fetchJob).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(1);

    await expect(polling).resolves.toBe("terminal");
    expect(fetchJob).toHaveBeenCalledTimes(4);
  });

  it.each([404, 410])("stops immediately when a job read returns HTTP %s", async (status) => {
    const controller = new AbortController();
    const error = new ApiRequestError(status, "gone");
    const fetchJob = vi.fn().mockRejectedValue(error);
    const onExpired = vi.fn();

    const result = await pollJobWithBackoff({
      signal: controller.signal,
      fetchJob,
      isTerminal: () => false,
      onJob: vi.fn(),
      onExpired,
      intervalMs: 1_000
    });

    expect(result).toBe("expired");
    expect(fetchJob).toHaveBeenCalledOnce();
    expect(onExpired).toHaveBeenCalledWith(error);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetchJob).toHaveBeenCalledOnce();
  });

  it("passes an AbortSignal to the active GET and exits when it is aborted", async () => {
    const controller = new AbortController();
    let observedSignal: AbortSignal | null = null;
    const fetchJob = vi.fn((signal: AbortSignal) => {
      observedSignal = signal;
      return new Promise<never>((_resolve, reject) => {
        signal.addEventListener(
          "abort",
          () => reject(new DOMException("aborted", "AbortError")),
          { once: true }
        );
      });
    });

    const polling = pollJobWithBackoff({
      signal: controller.signal,
      fetchJob,
      isTerminal: () => false,
      onJob: vi.fn(),
      onExpired: vi.fn(),
      intervalMs: 1_000
    });
    await flush();
    controller.abort();

    await expect(polling).resolves.toBe("aborted");
    expect(observedSignal).toBe(controller.signal);
    expect(controller.signal.aborted).toBe(true);
  });
});
