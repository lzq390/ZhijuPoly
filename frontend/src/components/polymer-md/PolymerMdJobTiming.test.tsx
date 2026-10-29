// @vitest-environment jsdom
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PolymerMdJob } from "../../types/polymerMd";
import { PolymerMdJobTiming, polymerMdJobTiming, polymerMdTimingIsLive, usePolymerMdClock } from "./PolymerMdJobTiming";

type JobTiming = Pick<PolymerMdJob, "status" | "started_at" | "finished_at">;
const start = "2026-10-28T17:01:02Z";
const running: JobTiming = { status: "running", started_at: start };
const now = Date.parse("2026-10-29T19:04:06Z");

afterEach(() => { cleanup(); vi.useRealTimers(); });

function TimingList({ jobs }: { jobs: JobTiming[] }) {
  const clock = usePolymerMdClock(jobs.some(polymerMdTimingIsLive));
  return <>{jobs.map((job, index) => <PolymerMdJobTiming key={index} job={job} now={clock} />)}</>;
}

describe("polymer MD task execution times", () => {
  it("shows explicit Beijing calendar dates and exact elapsed days/hours/minutes/seconds", () => {
    expect(polymerMdJobTiming(running, now)).toEqual({ started: "2026-10-29 01:01:02", duration: "1 天 2 小时 3 分 4 秒" });
    expect(polymerMdJobTiming({ ...running, started_at: "2026-10-29T01:01:02+08:00" }, now)).toEqual(polymerMdJobTiming(running, now));
    const view = render(<PolymerMdJobTiming job={running} now={now} />);
    expect(screen.getByText("开始时间")).toBeTruthy();
    expect(screen.getByText("运行时长")).toBeTruthy();
    expect(screen.getByText("北京时间")).toBeTruthy();
    expect(view.container.querySelector("time")?.getAttribute("datetime")).toBe(start);
    const duration = screen.getByText("运行时长").nextElementSibling!;
    expect(duration.textContent).toBe("1 天 2 小时 3 分 4 秒");
    expect([...duration.querySelectorAll(".np-pmd-numeric-value")].map(element => element.textContent)).toEqual(["1", "2", "3", "4"]);
    expect(duration.children).toHaveLength(1);
  });

  it.each(["completed", "failed", "cancelled"] as const)("freezes %s execution at the recorded finish time", status => {
    const job = { ...running, status, finished_at: "2026-10-28T18:02:05Z" };
    expect(polymerMdJobTiming(job, now).duration).toBe("1 小时 1 分 3 秒");
    expect(polymerMdJobTiming(job, now + 86400000).duration).toBe("1 小时 1 分 3 秒");
    expect(polymerMdTimingIsLive(job)).toBe(false);
  });

  it.each(["pending", "queued"] as const)("identifies %s tasks without execution timestamps as not started", status => {
    expect(polymerMdJobTiming({ status }, now)).toEqual({ started: "尚未开始", duration: "尚未开始" });
    expect(polymerMdTimingIsLive({ status })).toBe(false);
  });

  it("does not infer missing or invalid execution times from other job metadata", () => {
    expect(polymerMdJobTiming({ status: "running" }, now)).toEqual({ started: "未记录", duration: "未记录" });
    expect(polymerMdJobTiming({ ...running, started_at: "invalid" }, now)).toEqual({ started: "未记录", duration: "未记录" });
    expect(polymerMdJobTiming({ ...running, status: "completed" }, now).duration).toBe("结束时间未记录");
    expect(polymerMdJobTiming({ ...running, finished_at: "invalid" }, now).duration).toBe("时间数据异常");
    expect(polymerMdJobTiming({ ...running, finished_at: "2026-10-28T17:01:01Z" }, now).duration).toBe("时间数据异常");
    expect(polymerMdJobTiming(running, Date.parse(start) - 1000).duration).toBe("时间数据异常");
  });

  it("uses one shared timer for multiple running tasks and stops when all end or the view unmounts", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse(start) + 1000);
    const view = render(<TimingList jobs={[running, { ...running, status: "cancel_requested" }]} />);
    expect(vi.getTimerCount()).toBe(1);
    expect(screen.getAllByText("运行时长").map(label => label.nextElementSibling?.textContent)).toEqual(["1 秒", "1 秒"]);
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getAllByText("运行时长").map(label => label.nextElementSibling?.textContent)).toEqual(["2 秒", "2 秒"]);
    const finished = { ...running, status: "completed" as const, finished_at: "2026-10-28T17:01:04Z" };
    view.rerender(<TimingList jobs={[finished, { ...finished, status: "cancelled" }]} />);
    expect(vi.getTimerCount()).toBe(0);
    act(() => vi.advanceTimersByTime(20000));
    for (const timing of screen.getAllByLabelText("任务执行时间")) expect(within(timing).getByText("运行时长").nextElementSibling?.textContent).toBe("2 秒");
    view.rerender(<TimingList jobs={[running]} />);
    expect(vi.getTimerCount()).toBe(1);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});
