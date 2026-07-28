import { useCallback, useEffect, useRef, useState } from "react";
import { Cpu, LoaderCircle, Power, TriangleAlert } from "lucide-react";
import {
  ApiRequestError,
  fetchDevGpuSessionStatus,
  recoverDevGpuSession
} from "../services/api";
import type { DevGpuSessionPhase, DevGpuSessionStatusResponse } from "../types";


const ACTIVE_POLL_MS = 1_500;
const IDLE_POLL_MS = 5_000;
const REQUEST_TIMEOUT_MS = 8_000;

const unavailableStatus = (message: string): DevGpuSessionStatusResponse => ({
  schema_version: 1,
  operator_available: false,
  phase: "unavailable",
  controller_status: "unavailable",
  can_recover: false,
  operation_id: null,
  message,
  source_sha: null,
  source_tree: null,
  updated_at: null
});

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "GPU 控制服务不可用";
}

async function fetchStatusWithTimeout(signal?: AbortSignal): Promise<DevGpuSessionStatusResponse> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  const timeout = window.setTimeout(abort, REQUEST_TIMEOUT_MS);
  try {
    return await fetchDevGpuSessionStatus(controller.signal);
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
}

export type DevGpuSessionControl = {
  status: DevGpuSessionStatusResponse | null;
  actionPending: boolean;
  feedbackVisible: boolean;
  recover: () => Promise<void>;
};

export function useDevGpuSessionControl(enabled: boolean): DevGpuSessionControl {
  const [serviceStatus, setServiceStatus] = useState<DevGpuSessionStatusResponse | null>(null);
  const [isActionPending, setIsActionPending] = useState(false);
  const [showFeedback, setShowFeedback] = useState(false);
  const actionInFlight = useRef(false);
  const actionController = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!enabled) {
      setServiceStatus(null);
      return;
    }
    let disposed = false;
    let timer: number | undefined;
    let controller: AbortController | null = null;

    const poll = async () => {
      controller?.abort();
      controller = new AbortController();
      let nextStatus: DevGpuSessionStatusResponse;
      try {
        nextStatus = await fetchStatusWithTimeout(controller.signal);
      } catch (error) {
        if (controller.signal.aborted || disposed) {
          return;
        }
        nextStatus = unavailableStatus(errorMessage(error));
      }
      if (disposed) {
        return;
      }
      setServiceStatus(nextStatus);
      const active = ["recovering", "queued", "starting"].includes(nextStatus.phase);
      timer = window.setTimeout(poll, active ? ACTIVE_POLL_MS : IDLE_POLL_MS);
    };

    void poll();
    return () => {
      disposed = true;
      controller?.abort();
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [enabled]);

  useEffect(() => {
    if (
      !showFeedback ||
      serviceStatus?.phase === "recovering" ||
      serviceStatus?.phase === "queued" ||
      serviceStatus?.phase === "starting"
    ) {
      return;
    }
    const timer = window.setTimeout(() => setShowFeedback(false), 4_000);
    return () => window.clearTimeout(timer);
  }, [serviceStatus?.phase, showFeedback]);

  useEffect(
    () => () => {
      actionController.current?.abort();
    },
    []
  );

  const recover = useCallback(async () => {
    if (!enabled || actionInFlight.current) {
      return;
    }
    actionInFlight.current = true;
    setIsActionPending(true);
    setShowFeedback(true);
    const controller = new AbortController();
    actionController.current?.abort();
    actionController.current = controller;
    let recoverRequested = false;
    let currentStatus: DevGpuSessionStatusResponse | null = null;
    try {
      currentStatus = await fetchStatusWithTimeout(controller.signal);
      setServiceStatus(currentStatus);
      if (currentStatus.phase === "ready" || !currentStatus.can_recover) {
        return;
      }
      const optimisticPhase =
        currentStatus.phase === "stopped" || currentStatus.phase === "failed"
          ? "starting"
          : "queued";
      setServiceStatus({
        ...currentStatus,
        phase: optimisticPhase,
        can_recover: false,
        message:
          optimisticPhase === "queued"
            ? "恢复请求已排队，正在等待当前 GPU1 session 安全回收"
            : "恢复请求已发送，正在启动 GPU1 相关服务"
      });
      recoverRequested = true;
      const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
      try {
        setServiceStatus(await recoverDevGpuSession(controller.signal));
      } finally {
        window.clearTimeout(timeout);
      }
    } catch (error) {
      if (controller.signal.aborted) {
        return;
      }
      if (recoverRequested && !(error instanceof ApiRequestError)) {
        const queued = currentStatus?.phase !== "stopped";
        setServiceStatus({
          ...(currentStatus ?? unavailableStatus("正在重新连接 Backend")),
          operator_available: true,
          phase: queued ? "queued" : "starting",
          can_recover: false,
          message: queued
            ? "恢复请求已发送，正在等待 Backend 和 GPU1 controller"
            : "恢复请求已发送，正在等待 Backend 重新连接"
        });
      } else {
        setServiceStatus({
          ...(currentStatus ?? unavailableStatus(errorMessage(error))),
          operator_available: true,
          phase: "failed",
          can_recover: true,
          message: errorMessage(error)
        });
      }
    } finally {
      if (actionController.current === controller) {
        actionController.current = null;
      }
      actionInFlight.current = false;
      setIsActionPending(false);
    }
  }, [enabled]);

  return {
    status: serviceStatus,
    actionPending: isActionPending,
    feedbackVisible:
      showFeedback ||
      serviceStatus?.phase === "recovering" ||
      serviceStatus?.phase === "queued" ||
      serviceStatus?.phase === "starting",
    recover
  };
}

function phasePresentation(phase: DevGpuSessionPhase | "loading") {
  switch (phase) {
    case "ready":
      return {
        label: "GPU 服务已启动",
        className: "border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-50",
        dotClassName: "bg-emerald-500",
        Icon: Cpu,
        spinning: false
      };
    case "recovering":
    case "queued":
    case "starting":
      return {
        label:
          phase === "queued"
            ? "GPU 恢复已排队"
            : phase === "recovering"
              ? "GPU 服务回收中"
              : "GPU 服务启动中",
        className: "border-amber-200 bg-amber-50 text-amber-700",
        dotClassName: "bg-amber-500",
        Icon: LoaderCircle,
        spinning: true
      };
    case "failed":
      return {
        label: "GPU 服务恢复失败，点击重试",
        className: "border-rose-200 bg-rose-50 text-rose-700 hover:bg-rose-100",
        dotClassName: "bg-rose-500",
        Icon: TriangleAlert,
        spinning: false
      };
    case "stopped":
      return {
        label: "一键恢复 GPU 服务",
        className: "border-slate-200 bg-white text-slate-600 hover:border-teal-200 hover:bg-teal-50 hover:text-teal-700",
        dotClassName: "bg-slate-400",
        Icon: Power,
        spinning: false
      };
    case "unavailable":
      return {
        label: "GPU 控制服务不可用",
        className: "border-slate-200 bg-slate-100 text-slate-400",
        dotClassName: "bg-slate-300",
        Icon: TriangleAlert,
        spinning: false
      };
    default:
      return {
        label: "正在检测 GPU 服务",
        className: "border-slate-200 bg-white text-slate-500",
        dotClassName: "bg-slate-300",
        Icon: LoaderCircle,
        spinning: true
      };
  }
}

type GpuSessionButtonProps = {
  control: DevGpuSessionControl;
  statusId: string;
};

export function GpuSessionButton({ control, statusId }: GpuSessionButtonProps) {
  const phase = control.status?.phase ?? "loading";
  const presentation = phasePresentation(phase);
  const disabled =
    control.actionPending ||
    phase === "loading" ||
    phase === "ready" ||
    phase === "queued" ||
    phase === "starting" ||
    phase === "unavailable" ||
    control.status?.can_recover === false;
  const Icon = control.actionPending ? LoaderCircle : presentation.Icon;
  const message = control.status?.message ?? "正在检测 GPU 服务状态";
  const label = control.actionPending ? "正在提交 GPU 恢复请求" : presentation.label;

  return (
    <div className="group relative flex shrink-0 items-center">
      <button
        type="button"
        aria-label={label}
        aria-describedby={statusId}
        title={`${label}：${message}`}
        disabled={disabled}
        className={[
          "relative inline-flex h-9 w-9 items-center justify-center rounded-xl border shadow-sm transition",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500 focus-visible:ring-offset-2",
          "disabled:cursor-not-allowed disabled:opacity-80",
          presentation.className
        ].join(" ")}
        onClick={() => void control.recover()}
      >
        <Icon className={["h-4 w-4", control.actionPending || presentation.spinning ? "animate-spin" : ""].join(" ")} />
        <span
          aria-hidden="true"
          className={[
            "absolute bottom-1 right-1 h-1.5 w-1.5 rounded-full ring-2 ring-white",
            presentation.dotClassName
          ].join(" ")}
        />
      </button>
      <div
        id={statusId}
        role="status"
        aria-live="polite"
        className={[
          "pointer-events-none absolute right-0 top-11 z-50 w-64 rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs leading-5 text-slate-700 shadow-xl transition",
          control.feedbackVisible
            ? "visible translate-y-0 opacity-100"
            : "invisible -translate-y-1 opacity-0 group-hover:visible group-hover:translate-y-0 group-hover:opacity-100 group-focus-within:visible group-focus-within:translate-y-0 group-focus-within:opacity-100"
        ].join(" ")}
      >
        <div className="font-semibold text-slate-900">{label}</div>
        <div>{message}</div>
      </div>
    </div>
  );
}
