import { useEffect, useState } from "react";
import { getSession, sessionIsReady } from "../../auth/session";
import { ApiRequestError, fetchStructureDescriptors } from "../../services/api";
import type { StructureDescriptorResponse } from "../../types";

export type StructureDescriptorState =
  | { status: "idle" }
  | { status: "loading"; previous: StructureDescriptorResponse | null }
  | { status: "ready"; descriptors: StructureDescriptorResponse }
  | { status: "invalid" }
  | { status: "unavailable" }
  /** Signed out or without service access: show nothing rather than an error. */
  | { status: "no-access" };

const DEBOUNCE_MS = 350;
const CACHE_LIMIT = 40;
const cache = new Map<string, StructureDescriptorResponse>();

function remember(smiles: string, descriptors: StructureDescriptorResponse) {
  cache.delete(smiles);
  cache.set(smiles, descriptors);
  if (cache.size > CACHE_LIMIT) cache.delete(cache.keys().next().value as string);
}

/** Background requests never open the login or membership prompt; signed-out users simply see no facts. */
function canRequestSilently() {
  if (!sessionIsReady()) return false;
  const session = getSession();
  return session.authenticated && session.account_type !== "free";
}

/** Descriptors of the committed structure, refreshed shortly after it changes. */
export function useStructureDescriptors(smiles: string): StructureDescriptorState {
  const value = smiles.trim();
  const [state, setState] = useState<StructureDescriptorState>(() => {
    const cached = value ? cache.get(value) : undefined;
    return cached ? { status: "ready", descriptors: cached } : { status: "idle" };
  });

  useEffect(() => {
    if (!value) {
      setState({ status: "idle" });
      return;
    }
    const cached = cache.get(value);
    if (cached) {
      setState({ status: "ready", descriptors: cached });
      return;
    }
    if (!canRequestSilently()) {
      setState({ status: "no-access" });
      return;
    }
    setState((current) => ({
      status: "loading",
      previous: current.status === "ready" ? current.descriptors : current.status === "loading" ? current.previous : null
    }));
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      fetchStructureDescriptors(value, controller.signal)
        .then((descriptors) => {
          remember(value, descriptors);
          if (!controller.signal.aborted) setState({ status: "ready", descriptors });
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) return;
          setState({ status: error instanceof ApiRequestError && error.status === 422 ? "invalid" : "unavailable" });
        });
    }, DEBOUNCE_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [value]);

  return state;
}

/** Test hook: descriptor results are cached per page load. */
export function clearStructureDescriptorCache() {
  cache.clear();
}
