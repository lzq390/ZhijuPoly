import { isApiRequestError } from "../services/api";

export const JOB_POLL_BACKOFF_MS = [1_500, 3_000, 6_000, 10_000] as const;

export type JobPollResult = "terminal" | "expired" | "aborted" | "timed_out";

type JobPollingOptions<T> = {
  signal: AbortSignal;
  fetchJob: (signal: AbortSignal) => Promise<T>;
  isTerminal: (job: T) => boolean;
  onJob: (job: T) => void;
  onExpired: (error: unknown) => void;
  intervalMs: number;
  initialDelayMs?: number;
  timeoutMs?: number;
  onTimeout?: () => void;
};

export function isAbortError(error: unknown): boolean {
  return typeof error === "object" && error !== null && "name" in error && error.name === "AbortError";
}

export function isExpiredJobError(error: unknown): boolean {
  return isApiRequestError(error, 404) || isApiRequestError(error, 410);
}

export function abortableDelay(ms: number, signal: AbortSignal): Promise<boolean> {
  if (signal.aborted) {
    return Promise.resolve(false);
  }

  return new Promise((resolve) => {
    const timer = globalThis.setTimeout(() => {
      signal.removeEventListener("abort", handleAbort);
      resolve(true);
    }, ms);
    const handleAbort = () => {
      globalThis.clearTimeout(timer);
      resolve(false);
    };
    signal.addEventListener("abort", handleAbort, { once: true });
  });
}

export async function pollJobWithBackoff<T>({
  signal,
  fetchJob,
  isTerminal,
  onJob,
  onExpired,
  intervalMs,
  initialDelayMs = 0,
  timeoutMs,
  onTimeout
}: JobPollingOptions<T>): Promise<JobPollResult> {
  const startedAt = Date.now();
  let transientFailures = 0;

  if (initialDelayMs > 0 && !(await abortableDelay(initialDelayMs, signal))) {
    return "aborted";
  }

  while (!signal.aborted) {
    if (timeoutMs !== undefined && Date.now() - startedAt >= timeoutMs) {
      onTimeout?.();
      return "timed_out";
    }

    let job: T;
    try {
      job = await fetchJob(signal);
    } catch (error) {
      if (signal.aborted || isAbortError(error)) {
        return "aborted";
      }
      if (isExpiredJobError(error)) {
        onExpired(error);
        return "expired";
      }

      transientFailures += 1;
      const backoffIndex = Math.min(transientFailures - 1, JOB_POLL_BACKOFF_MS.length - 1);
      if (!(await abortableDelay(JOB_POLL_BACKOFF_MS[backoffIndex], signal))) {
        return "aborted";
      }
      continue;
    }

    if (signal.aborted) {
      return "aborted";
    }

    transientFailures = 0;
    onJob(job);
    if (isTerminal(job)) {
      return "terminal";
    }
    if (!(await abortableDelay(intervalMs, signal))) {
      return "aborted";
    }
  }

  return "aborted";
}
