import { describe, expect, it } from "vitest";
import legacyV1ResultFixture from "../../../backend/tests/fixtures/monomer_dft_scientific_result_v1_legacy.json";
import type { MonomerDftArtifact, MonomerDftProgressStage, MonomerDftResult } from "../types";
import {
  formatMonomerDftRdkitPreparation,
  effectiveMonomerDftArtifactsState,
  hasAvailableMonomerDftArtifacts,
  isMonomerDftArtifactAvailable,
  labelMonomerDftStage,
  lowestMonomerDftFrequency,
  resolveMonomerDftRdkitPreparation,
  selectMonomerDftDisplayTimings,
  userFacingMonomerDftMessage
} from "./monomerDftPresentation";

const artifact = (available: boolean): MonomerDftArtifact => ({
  artifact_id: "scientific_result",
  name: "scientific_result.json",
  media_type: "application/json",
  size_bytes: 42,
  sha256: "a".repeat(64),
  available
});

describe("monomer DFT presentation", () => {
  it.each<[MonomerDftProgressStage, string]>([
    ["pending", "等待调度"],
    ["queued", "任务排队"],
    ["validating", "输入校验"],
    ["conformer", "准备三维构型"],
    ["single_point", "计算能量与原子性质"],
    ["optimization", "几何优化"],
    ["hessian", "计算二阶力常数"],
    ["frequency", "计算振动频率"],
    ["artifacts", "整理结果与文件"],
    ["running", "计算中"],
    ["dispatch_retry", "正在恢复计算连接"],
    ["dispatch_failed", "计算任务启动失败"],
    ["cancel_requested", "等待取消"],
    ["worker_failed", "计算未能完成"],
    ["completed", "计算完成"],
    ["failed", "计算失败"],
    ["cancelled", "任务已取消"]
  ])("labels stage %s in Chinese", (stage, label) => {
    expect(labelMonomerDftStage(stage)).toBe(label);
  });

  it("does not leak an unknown backend stage as raw English", () => {
    expect(labelMonomerDftStage("future_worker_phase")).toBe("处理中");
  });

  it("translates the single-conformer warning into user-facing Chinese", () => {
    expect(userFacingMonomerDftMessage(
      "Only one deterministic local conformer was evaluated."
    )).toBe("本次仅评估了一个自动生成的初始构型。");
  });

  it("translates charge warnings as complete sentences", () => {
    expect(userFacingMonomerDftMessage(
      "Explicit net_charge overrides SMILES charge inference and differs from the encoded formal charge.",
      { code: "net_charge_override" }
    )).toBe("已使用手动设置的总电荷，该值与结构中标注的形式电荷不同。");
    expect(userFacingMonomerDftMessage(
      "Explicit net_charge overrides SMILES charge inference and matches the encoded formal charge.",
      { code: "net_charge_override" }
    )).toBe("已使用手动设置的总电荷，该值与结构中标注的形式电荷一致。");
  });

  it("uses stable codes without exposing service implementation terms", () => {
    expect(userFacingMonomerDftMessage(
      "monomer DFT Unix socket is not configured",
      { code: "worker_socket_not_configured" }
    )).toBe("计算服务暂不可用，请联系管理员。");
    expect(userFacingMonomerDftMessage(
      "The CUDA context entered an unsafe state and the worker must restart.",
      { code: "cuda_fatal" }
    )).toBe("计算资源发生异常，本次任务未能完成，请稍后重试。");
  });

  it("uses a Chinese fallback for unknown English service messages", () => {
    expect(userFacingMonomerDftMessage("Failed to fetch", {
      fallback: "读取服务状态失败。"
    })).toBe("读取服务状态失败。");
    expect(userFacingMonomerDftMessage("请检查输入结构。")).toBe("请检查输入结构。");
    expect(userFacingMonomerDftMessage("服务端 Worker unavailable", {
      fallback: "读取服务状态失败。"
    })).toBe("读取服务状态失败。");
  });

  it("uses artifact availability for individual files and aggregate actions", () => {
    const unavailable = artifact(false);
    const available = artifact(true);
    const job = { artifacts: [unavailable, available], artifacts_deleted: false, artifacts_state: "available" as const };
    expect(isMonomerDftArtifactAvailable(job, unavailable)).toBe(false);
    expect(isMonomerDftArtifactAvailable(job, available)).toBe(true);
    expect(hasAvailableMonomerDftArtifacts(job)).toBe(true);
  });

  it("keeps all artifact actions disabled after the canonical post-delete reload", () => {
    const retainedDescriptor = artifact(false);
    const reloadedJob = { artifacts: [retainedDescriptor], artifacts_deleted: true, artifacts_state: "deleted" as const };
    expect(isMonomerDftArtifactAvailable(reloadedJob, retainedDescriptor)).toBe(false);
    expect(hasAvailableMonomerDftArtifacts(reloadedJob)).toBe(false);

    const staleDescriptor = artifact(true);
    expect(isMonomerDftArtifactAvailable({ artifacts_deleted: true, artifacts_state: "deleted" }, staleDescriptor)).toBe(false);
    expect(hasAvailableMonomerDftArtifacts({ artifacts: [staleDescriptor], artifacts_deleted: true, artifacts_state: "deleted" })).toBe(false);
  });

  it("disables artifact access while asynchronous deletion is pending", () => {
    const available = artifact(true);
    const pending = { artifacts: [available], artifacts_deleted: false, artifacts_state: "delete_requested" as const };
    expect(effectiveMonomerDftArtifactsState(pending)).toBe("delete_requested");
    expect(isMonomerDftArtifactAvailable(pending, available)).toBe(false);
    expect(hasAvailableMonomerDftArtifacts(pending)).toBe(false);
  });

  it.each([
    [{ seed: 1, force_field: "ETKDG-only", optimization_performed: false, optimization_status: -1, optimization_state: "not_performed" }, "ETKDG-only · 未执行 MMFF/UFF 力场优化"],
    [{ seed: 1, force_field: "MMFF94", optimization_performed: true, optimization_status: 0, optimization_state: "converged" }, "MMFF94 · 力场优化已收敛"],
    [{ seed: 1, force_field: "UFF", optimization_performed: true, optimization_status: 1, optimization_state: "not_converged" }, "UFF · 力场优化未收敛"]
  ] satisfies Array<[MonomerDftResult["rdkit"], string]>)("formats RDKit preparation state %#", (rdkit, label) => {
    expect(formatMonomerDftRdkitPreparation(rdkit)).toBe(label);
  });

  it("renders the shared legacy V1 fixture without treating missing fields as false", () => {
    type V1Rdkit = Extract<MonomerDftResult, { schema_version: 1 }>["rdkit"];
    const rdkit: V1Rdkit = legacyV1ResultFixture.rdkit;

    expect("optimization_performed" in legacyV1ResultFixture.rdkit).toBe(false);
    expect("optimization_state" in legacyV1ResultFixture.rdkit).toBe(false);
    expect(resolveMonomerDftRdkitPreparation(rdkit)).toEqual({
      state: "converged",
      inferredFromLegacyFields: true
    });
    expect(formatMonomerDftRdkitPreparation(rdkit)).toBe(
      "MMFF94 · 力场优化已收敛（旧版结果推断）"
    );
  });

  it("keeps ambiguous legacy RDKit preparation explicitly unknown", () => {
    type V1Rdkit = Extract<MonomerDftResult, { schema_version: 1 }>["rdkit"];
    const rdkit: V1Rdkit = { seed: 1, force_field: "MMFF94", optimization_status: -1 };

    expect(resolveMonomerDftRdkitPreparation(rdkit).state).toBe("unknown");
    expect(formatMonomerDftRdkitPreparation(rdkit)).toContain("状态未知");
  });

  it("uses backend job timings even when Worker result timings differ", () => {
    const jobTimings = { queue_wait_ms: 125, total_ms: 250 };
    const workerResultTimings = { queue_wait_ms: 0, total_ms: 90 };
    expect(selectMonomerDftDisplayTimings(
      { timings: jobTimings },
      { timings: workerResultTimings }
    )).toBe(jobTimings);
  });

  it("does not render Infinity for an atom with no vibrational modes", () => {
    expect(lowestMonomerDftFrequency([])).toBeNull();
    expect(lowestMonomerDftFrequency([3651.5, -42.25, 1630.4])).toBe(-42.25);
  });
});
