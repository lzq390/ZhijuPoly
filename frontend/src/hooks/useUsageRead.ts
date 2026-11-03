import { useCallback, useEffect, useRef, useState } from "react";

type ReadState<T> = {
  key: string;
  revision: number;
  attempt: number;
  data?: T;
  error?: unknown;
  loading: boolean;
};

/** Query identity fences results; refreshing the same query retains its last success. */
export function useUsageRead<T>(
  key: string,
  read: (signal: AbortSignal) => Promise<T>,
  revision = 0,
  enabled = true,
) {
  const readRef = useRef(read);
  readRef.current = read;
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<ReadState<T>>({ key, revision, attempt, loading: enabled });
  const retry = useCallback(() => setAttempt(value => value + 1), []);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    const request = readRef.current;
    setState(previous => ({ key, revision, attempt, data: previous.key === key ? previous.data : undefined, loading: true }));
    Promise.resolve().then(() => request(controller.signal)).then(data => {
      if (!controller.signal.aborted) setState({ key, revision, attempt, data, loading: false });
    }).catch(error => {
      if (!controller.signal.aborted) setState(previous => ({ ...previous, error, loading: false }));
    });
    return () => controller.abort();
  }, [key, revision, attempt, enabled]);

  const matching = state.key === key;
  const pending = !matching || state.revision !== revision || state.attempt !== attempt;
  return {
    data: matching ? state.data : undefined,
    error: matching && !pending ? state.error : undefined,
    loading: enabled && (pending || state.loading),
    retry,
  };
}
