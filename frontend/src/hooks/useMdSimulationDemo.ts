import { useCallback, useEffect, useRef, useState } from "react";
import { getSessionEpoch, onSessionRetired } from "../auth/session";
import {
  calculateMdDemoAtomDistance,
  fetchMdDemoDefaults,
  isApiRequestError,
  runMdDemo
} from "../services/api";
import type {
  MdDemoAtomDistanceRequest,
  MdDemoAtomDistanceResponse,
  MdDemoDefaultsResponse,
  MdDemoRunRequest,
  MdDemoRunResponse
} from "../types";

const PROGRESS_KEYFRAMES = [
  { time: 0, value: 1 },
  { time: 280, value: 6 },
  { time: 640, value: 8 },
  { time: 1050, value: 8 },
  { time: 1500, value: 24 },
  { time: 1850, value: 28 },
  { time: 2350, value: 28 },
  { time: 2920, value: 44 },
  { time: 3320, value: 52 },
  { time: 3920, value: 68 },
  { time: 4320, value: 68 },
  { time: 4860, value: 86 },
  { time: 5220, value: 93 },
  { time: 5600, value: 99 }
] as const;
const RESULT_REVEAL_AT_MS = 7600;

type MdSimulationDemoState = {
  defaults: MdDemoDefaultsResponse | null;
  defaultsLoading: boolean;
  defaultsError: string | null;
  data: MdDemoRunResponse | null;
  runLoading: boolean;
  runError: string | null;
  progress: number;
  distance: MdDemoAtomDistanceResponse | null;
  distanceLoading: boolean;
  distanceError: string | null;
};

function isAbortError(error: unknown) {
  return error instanceof DOMException
    ? error.name === "AbortError"
    : error instanceof Error && error.name === "AbortError";
}

function networkLike(error: unknown) {
  return error instanceof TypeError ||
    (error instanceof Error && /Failed to fetch|NetworkError|Load failed/i.test(error.message));
}

function defaultsErrorMessage(error: unknown) {
  if (networkLike(error)) return "暂时无法加载模拟配置，请检查网络后重试。";
  if (isApiRequestError(error)) return "模拟配置加载失败，请稍后刷新重试。";
  return "无法加载模拟配置。";
}

function runErrorMessage(error: unknown) {
  if (isApiRequestError(error, 422)) return "模拟参数不符合要求，请检查输入后重试。";
  if (networkLike(error)) return "MD 模拟失败，请检查网络后重试。";
  return "MD 模拟失败，请稍后重试。";
}

function distanceErrorMessage(error: unknown) {
  if (isApiRequestError(error, 404)) return "所选原子不在当前轨迹中，请重新选择。";
  if (isApiRequestError(error, 422)) return "请选择两个不同的有效原子。";
  if (networkLike(error)) return "距离计算失败，请检查网络后重试。";
  return "距离计算失败，请稍后重试。";
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function scriptedProgress(elapsed: number) {
  const first = PROGRESS_KEYFRAMES[0];
  if (elapsed <= first.time) return first.value;
  for (let index = 1; index < PROGRESS_KEYFRAMES.length; index += 1) {
    const previous = PROGRESS_KEYFRAMES[index - 1];
    const next = PROGRESS_KEYFRAMES[index];
    if (elapsed <= next.time) {
      if (previous.value === next.value) return next.value;
      const ratio = clamp((elapsed - previous.time) / (next.time - previous.time), 0, 1);
      const eased = 1 - Math.pow(1 - ratio, 2);
      return Math.round(previous.value + (next.value - previous.value) * eased);
    }
  }
  return 99;
}

function waitForReveal(delay: number, signal: AbortSignal) {
  if (signal.aborted) return Promise.reject(new DOMException("Aborted", "AbortError"));
  if (delay <= 0) return Promise.resolve();
  return new Promise<void>((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, delay);
    const abort = () => {
      window.clearTimeout(timeout);
      signal.removeEventListener("abort", abort);
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal.addEventListener("abort", abort, { once: true });
  });
}

export function useMdSimulationDemo(options: { resultRevealDelayMs?: number; enabled?: boolean } = {}) {
  const resultRevealDelayMs = options.resultRevealDelayMs ?? RESULT_REVEAL_AT_MS;
  const enabledRef = useRef(options.enabled !== false);
  enabledRef.current = options.enabled !== false;
  const mounted = useRef(true);
  const identity = useRef(getSessionEpoch());
  const valid = useCallback(() => mounted.current && enabledRef.current && identity.current === getSessionEpoch(), []);
  const [state, setState] = useState<MdSimulationDemoState>({
    defaults: null,
    defaultsLoading: options.enabled !== false,
    defaultsError: null,
    data: null,
    runLoading: false,
    runError: null,
    progress: 0,
    distance: null,
    distanceLoading: false,
    distanceError: null
  });
  const defaultsAbortRef = useRef<AbortController | null>(null);
  const runAbortRef = useRef<AbortController | null>(null);
  const distanceAbortRef = useRef<AbortController | null>(null);
  const defaultsRevisionRef = useRef(0);
  const runRevisionRef = useRef(0);
  const distanceRevisionRef = useRef(0);
  const progressTimerRef = useRef<number | null>(null);

  const stopProgress = useCallback(() => {
    if (progressTimerRef.current !== null) {
      window.clearInterval(progressTimerRef.current);
      progressTimerRef.current = null;
    }
  }, []);

  const refreshDefaults = useCallback(async () => {
    if (!valid()) return null;
    defaultsAbortRef.current?.abort();
    const controller = new AbortController();
    const revision = defaultsRevisionRef.current + 1;
    defaultsRevisionRef.current = revision;
    defaultsAbortRef.current = controller;
    setState((current) => ({ ...current, defaultsLoading: true, defaultsError: null }));
    try {
      const defaults = await fetchMdDemoDefaults(controller.signal);
      if (!valid() || controller.signal.aborted || defaultsRevisionRef.current !== revision) return null;
      setState((current) => ({
        ...current,
        defaults,
        defaultsLoading: false,
        defaultsError: null
      }));
      return defaults;
    } catch (error) {
      if (!valid() || controller.signal.aborted || defaultsRevisionRef.current !== revision || isAbortError(error)) return null;
      setState((current) => ({
        ...current,
        defaultsLoading: false,
        defaultsError: defaultsErrorMessage(error)
      }));
      return null;
    } finally {
      if (defaultsAbortRef.current === controller) defaultsAbortRef.current = null;
    }
  }, [valid]);

  const clearDistance = useCallback(() => {
    distanceRevisionRef.current += 1;
    distanceAbortRef.current?.abort();
    distanceAbortRef.current = null;
    setState((current) => ({
      ...current,
      distance: null,
      distanceLoading: false,
      distanceError: null
    }));
  }, []);

  const run = useCallback(async (request: MdDemoRunRequest) => {
    if (!valid()) return null;
    runRevisionRef.current += 1;
    runAbortRef.current?.abort();
    distanceRevisionRef.current += 1;
    distanceAbortRef.current?.abort();
    stopProgress();

    const controller = new AbortController();
    const revision = runRevisionRef.current;
    runAbortRef.current = controller;
    const startedAt = window.performance.now();
    setState((current) => ({
      ...current,
      runLoading: true,
      runError: null,
      progress: 1,
      distance: null,
      distanceLoading: false,
      distanceError: null
    }));
    progressTimerRef.current = window.setInterval(() => {
      if (!valid() || controller.signal.aborted || runRevisionRef.current !== revision) return;
      setState((current) => ({ ...current, progress: scriptedProgress(window.performance.now() - startedAt) }));
    }, 140);

    try {
      const data = await runMdDemo(request, controller.signal);
      const remaining = resultRevealDelayMs - (window.performance.now() - startedAt);
      await waitForReveal(remaining, controller.signal);
      if (!valid() || controller.signal.aborted || runRevisionRef.current !== revision) return null;
      stopProgress();
      setState((current) => ({
        ...current,
        data,
        runLoading: false,
        runError: null,
        progress: 100
      }));
      return data;
    } catch (error) {
      if (!valid() || controller.signal.aborted || runRevisionRef.current !== revision || isAbortError(error)) return null;
      stopProgress();
      setState((current) => ({
        ...current,
        runLoading: false,
        runError: runErrorMessage(error),
        progress: 0
      }));
      return null;
    } finally {
      if (runAbortRef.current === controller) runAbortRef.current = null;
    }
  }, [resultRevealDelayMs, stopProgress, valid]);

  const calculateDistance = useCallback(async (payload: MdDemoAtomDistanceRequest) => {
    if (!valid()) return null;
    distanceRevisionRef.current += 1;
    distanceAbortRef.current?.abort();
    const controller = new AbortController();
    const revision = distanceRevisionRef.current;
    distanceAbortRef.current = controller;
    setState((current) => ({
      ...current,
      distance: null,
      distanceLoading: true,
      distanceError: null
    }));
    try {
      const distance = await calculateMdDemoAtomDistance(payload, controller.signal);
      if (!valid() || controller.signal.aborted || distanceRevisionRef.current !== revision) return null;
      setState((current) => ({
        ...current,
        distance,
        distanceLoading: false,
        distanceError: null
      }));
      return distance;
    } catch (error) {
      if (!valid() || controller.signal.aborted || distanceRevisionRef.current !== revision || isAbortError(error)) return null;
      setState((current) => ({
        ...current,
        distance: null,
        distanceLoading: false,
        distanceError: distanceErrorMessage(error)
      }));
      return null;
    } finally {
      if (distanceAbortRef.current === controller) distanceAbortRef.current = null;
    }
  }, [valid]);

  const clearResults = useCallback(() => {
    runRevisionRef.current += 1;
    distanceRevisionRef.current += 1;
    runAbortRef.current?.abort();
    distanceAbortRef.current?.abort();
    runAbortRef.current = null;
    distanceAbortRef.current = null;
    stopProgress();
    setState((current) => ({
      ...current,
      data: null,
      runLoading: false,
      runError: null,
      progress: 0,
      distance: null,
      distanceLoading: false,
      distanceError: null
    }));
  }, [stopProgress]);

  const invalidateRequests = useCallback(() => {
    defaultsRevisionRef.current += 1;
    runRevisionRef.current += 1;
    distanceRevisionRef.current += 1;
    defaultsAbortRef.current?.abort();
    runAbortRef.current?.abort();
    distanceAbortRef.current?.abort();
    defaultsAbortRef.current = null;
    runAbortRef.current = null;
    distanceAbortRef.current = null;
    stopProgress();
  }, [stopProgress]);

  useEffect(() => {
    mounted.current = true;
    const retire = onSessionRetired(() => {
      invalidateRequests();
      setState({ defaults: null, defaultsLoading: false, defaultsError: null,
        data: null, runLoading: false, runError: null, progress: 0,
        distance: null, distanceLoading: false, distanceError: null });
    });
    return () => { mounted.current = false; retire(); invalidateRequests(); };
  }, [invalidateRequests]);

  useEffect(() => {
    if (options.enabled !== false) void refreshDefaults();
    else setState(current => ({ ...current, defaultsLoading: false, runLoading: false,
      distanceLoading: false, progress: current.data ? 100 : 0 }));
    return invalidateRequests;
  }, [options.enabled, refreshDefaults, invalidateRequests]);

  return {
    ...state,
    refreshDefaults,
    run,
    calculateDistance,
    clearDistance,
    clearResults
  };
}
