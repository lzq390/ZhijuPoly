import { describe, expect, it } from "vitest";
import type { PolymerMdCapabilities } from "../../types/polymerMd";
import { polymerMdServiceState } from "./serviceState";

const ready = { enabled: true, schema_ready: true, worker_ready: true, submit_enabled: true } as PolymerMdCapabilities;
describe("polymer MD service admission presentation", () => {
  it.each([
    ["maintenance", "计算服务正在维护"], ["cleanup_unconfirmed", "确认旧进程清理"],
    ["recovering", "恢复任务状态"], ["submission_disabled", "暂停受理新计算"],
    ["runtime_not_ready", "科学运行环境尚未就绪"], ["authorization_unavailable", "启动授权服务暂不可用"]
  ])("explains the authoritative %s block without claiming capacity is full", (reason, text) => {
    const result = polymerMdServiceState(false, null, ready, { ...ready, worker_ready: false, admission_reason: reason });
    expect(result.ready).toBe(false); expect(result.reason).toContain(text); expect(result.reason).not.toContain("容量已满");
  });
  it("fails closed on an unknown admission reason and on incomplete or contradictory status", () => {
    expect(polymerMdServiceState(false, null, ready, { ...ready, admission_reason: "new_reason" }).ready).toBe(false);
    expect(polymerMdServiceState(false, null, null, ready).ready).toBe(false);
    expect(polymerMdServiceState(false, null, { ...ready, enabled: false }, ready).ready).toBe(false);
  });
  it("keeps old API flags usable without inventing resource or acceptance data", () => {
    expect(polymerMdServiceState(false, null, ready, ready).ready).toBe(true);
    expect(polymerMdServiceState(false, null, ready, { ...ready, submit_enabled: false }).reason).toContain("暂停受理");
    expect(polymerMdServiceState(true, null, ready, ready).ready).toBe(false);
    expect(polymerMdServiceState(false, "读取失败", ready, ready).reason).toBe("读取失败");
  });
});
