import { useEffect, useState } from "react";
import type { PolymerMdJob } from "../../types/polymerMd";
import { terminalJob } from "./config";
import { PolymerMdNumericText } from "./PolymerMdNumericText";

type JobTiming = Pick<PolymerMdJob, "status" | "started_at" | "finished_at">;
const beijingTime = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"
});
function timestamp(value: string | null | undefined): number | null {
  const parsed = value ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) ? parsed : null;
}
export function polymerMdTimingIsLive(job: JobTiming): boolean {
  return !terminalJob(job.status) && timestamp(job.started_at) !== null && !job.finished_at;
}
/** One display clock per visible list or result view; never polls the API. */
export function usePolymerMdClock(active: boolean): number {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}
export function polymerMdJobTiming(job: JobTiming, now: number) {
  const start = timestamp(job.started_at);
  const waiting = ["pending", "queued"].includes(job.status) && !job.started_at;
  if (start === null) return {
    started: waiting ? "尚未开始" : "未记录",
    duration: waiting ? "尚未开始" : "未记录"
  };
  const parts = Object.fromEntries(beijingTime.formatToParts(start).map(part => [part.type, part.value]));
  const started = `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
  const finish = timestamp(job.finished_at);
  // Historical terminal jobs may lack an end time. Do not keep their clocks
  // running or substitute creation/update times for execution timestamps.
  if (!job.finished_at && terminalJob(job.status)) return { started, duration: "结束时间未记录" };
  const end = job.finished_at ? finish : now;
  if (end === null || !Number.isFinite(end) || end < start) return { started, duration: "时间数据异常" };
  const seconds = Math.floor((end - start) / 1000);
  const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600), minutes = Math.floor(seconds % 3600 / 60);
  const duration = [days ? `${days} 天` : null, days || hours ? `${hours} 小时` : null,
    days || hours || minutes ? `${minutes} 分` : null, `${seconds % 60} 秒`].filter(Boolean).join(" ");
  return { started, duration };
}

export function PolymerMdJobTiming({ job, now }: { job: JobTiming; now: number }) {
  const timing = polymerMdJobTiming(job, now);
  const hasStart = timestamp(job.started_at) !== null;
  return <dl className="np-pmd-job-timing" aria-label="任务执行时间">
    <div><dt>开始时间</dt><dd>{hasStart ? <><time dateTime={job.started_at!}>{timing.started}</time><span className="np-pmd-time-zone">北京时间</span></> : timing.started}</dd></div>
    <div><dt>运行时长</dt><dd><PolymerMdNumericText>{timing.duration}</PolymerMdNumericText></dd></div>
  </dl>;
}
