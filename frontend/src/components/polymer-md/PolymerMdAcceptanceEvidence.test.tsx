// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { PolymerMdCapabilities } from "../../types/polymerMd";
import fixture from "./fixtures/acceptance-summary.synthetic.json";
import { PolymerMdAcceptanceEvidence, polymerMdAcceptanceView } from "./PolymerMdAcceptanceEvidence";
import { PolymerMdProtocolInfo } from "./PolymerMdProtocolInfo";

// Generated through the backend registry loader with synthetic private evidence.
// This fixture is a contract regression, never an actual scientific approval.
const capabilities = () => structuredClone(fixture) as unknown as PolymerMdCapabilities;
afterEach(cleanup);

function expand(container: HTMLElement) {
  for (const details of container.querySelectorAll<HTMLDetailsElement>("details")) details.open = true;
}

describe("independent scientific acceptance display", () => {
  it("shows the backend-produced scoped review while retaining pending sealed metadata", () => {
    const value = capabilities();
    const view = render(<PolymerMdAcceptanceEvidence capabilities={value} />);
    const scopes = value.acceptance_summary!.accepted_scopes!;
    const parsed = polymerMdAcceptanceView(value);
    expect(parsed.accepted).toBe(true);
    expect(parsed.count).toBe(value.acceptance_summary!.accepted_protocol_count);
    expect(view.container.textContent).toContain(`已验收协议：${parsed.count} 项`);
    expect(screen.getByText(/仅适用于下列精确请求范围/)).toBeTruthy();
    expect(screen.getByText(/相同模型或力场名称不表示其他材料/)).toBeTruthy();
    fireEvent.click(screen.getByText("查看验收绑定、适用范围与审阅证据"));
    expand(view.container);
    expect(screen.getAllByText(value.runtime!.runtime_fingerprint!).length).toBeGreaterThan(0);
    for (const [index, scope] of scopes.entries()) {
      const region = within(screen.getByRole("region", { name: `验收范围 ${index + 1}` }));
      expect(region.getByText(`${scope.material_label} · ${scope.protocol_id}`)).toBeTruthy();
      expect(region.getByText(scope.request_sha256)).toBeTruthy();
      expect(region.getByText(scope.reviewer_ref)).toBeTruthy();
      expect(region.getAllByText(scope.decision_sha256).length).toBeGreaterThan(0);
      for (const evidence of scope.evidence) expect(region.getAllByText(evidence.sha256).length).toBeGreaterThan(0);
      expect(region.getByText("几何优化主基组")).toBeTruthy();
      expect(region.getByText("目标温度")).toBeTruthy();
    }
    expect(screen.getByText(/封存时状态；当前正式验收以独立登记为准/)).toBeTruthy();
    expect(screen.getAllByText("封存时未登记通过").length).toBeGreaterThan(0);
  });

  it.each(["unconfigured", "missing", "invalid", "mismatch", "revoked"] as const)("withholds claims and scope evidence for registry status %s", status => {
    const value = capabilities();
    value.acceptance_summary!.registry_status = status;
    // Even stale accepted data in an error response must not grant approval.
    const view = render(<PolymerMdAcceptanceEvidence capabilities={value} />);
    expect(polymerMdAcceptanceView(value).accepted).toBe(false);
    expect(view.container.textContent).toContain("已验收协议：0 项");
    expect(screen.queryByText("查看验收绑定、适用范围与审阅证据")).toBeNull();
    expect(view.container.textContent).not.toContain(value.acceptance_summary!.accepted_scopes![0].request_sha256);
    expect(screen.queryByText(/独立人工科学审阅已登记/)).toBeNull();
  });

  it.each(["runtime_fingerprint", "source_sha", "patch_version"] as const)("requires the registry's %s to match this response's runtime", key => {
    const value = capabilities();
    value.runtime![key] = key === "patch_version" ? "different-adapter" : "f".repeat(key === "source_sha" ? 40 : 64);
    const view = render(<PolymerMdAcceptanceEvidence capabilities={value} />);
    expect(polymerMdAcceptanceView(value).accepted).toBe(false);
    expect(view.container.textContent).toContain("验收记录与当前运行时或协议绑定不匹配");
    expect(screen.queryByText(/独立人工科学审阅已登记/)).toBeNull();
  });

  it.each(["bad_count", "protocol_missing", "broad_scope", "bad_evidence", "review_missing", "unknown_qm", "bad_conditions", "bad_protocol_hash"])("rejects inconsistent public scope evidence: %s", kind => {
    const value = capabilities(), summary = value.acceptance_summary!, scope = summary.accepted_scopes![0];
    if (kind === "bad_count") summary.accepted_protocol_count++;
    if (kind === "protocol_missing") summary.accepted_protocols = ["another-protocol"];
    if (kind === "broad_scope") (scope as unknown as Record<string, unknown>).scope_policy = "all_forcefields_and_materials";
    if (kind === "bad_evidence") scope.evidence[0].sha256 = "/private/INVALID_EVIDENCE";
    if (kind === "review_missing") scope.evidence = scope.evidence.filter(item => item.label !== "review_report");
    if (kind === "unknown_qm") (scope.qm as unknown as Record<string, unknown>).opt_basis = "unknown-basis";
    if (kind === "bad_conditions") scope.conditions.temperature_k = Number.NaN;
    if (kind === "bad_protocol_hash") summary.binding!.scientific_protocol_sha256 = "invalid";
    const view = render(<PolymerMdAcceptanceEvidence capabilities={value} />);
    expect(polymerMdAcceptanceView(value).accepted).toBe(false);
    expect(view.container.textContent).toContain("已验收协议：0 项");
    expect(screen.queryByText(/独立人工科学审阅已登记/)).toBeNull();
  });

  it("never promotes missing or legacy summaries from a sealed runtime's passed flags", () => {
    const value = capabilities();
    value.runtime = { ...value.runtime, scientific_acceptance: "passed", adapter_scientific_validation: "passed", accepted_protocols: ["legacy-global"] };
    delete value.acceptance_summary;
    const view = render(<PolymerMdAcceptanceEvidence capabilities={value} />);
    expect(polymerMdAcceptanceView(value).count).toBe(0);
    expect(view.container.textContent).toContain("当前独立科学验收尚未核验通过");
    expect(view.container.textContent).not.toContain("legacy-global");
    view.rerender(<PolymerMdAcceptanceEvidence capabilities={{ ...value, acceptance_summary: {
      scientific_acceptance: "passed", adapter_scientific_validation: "passed", accepted_protocols: ["legacy-global"], accepted_protocol_count: 1
    } }} />);
    expect(view.container.textContent).toContain("已验收协议：0 项");
  });

  it("projects public evidence without exposing extra private identity or chemistry fields", () => {
    const value = capabilities(), secret = "PRIVATE_ATTESTATION_CANARY";
    Object.assign(value.acceptance_summary!, { owner_user_id: secret, registry_path: `/private/${secret}` });
    Object.assign(value.acceptance_summary!.accepted_scopes![0], { smiles: secret, components: [{ smiles: secret }], job_id: secret, reviewer: { name: secret }, seed: secret });
    Object.assign(value.acceptance_summary!.accepted_scopes![0].evidence[0], { path: `/private/${secret}`, storage_ref: secret });
    const view = render(<PolymerMdAcceptanceEvidence capabilities={value} />);
    expand(view.container);
    expect(polymerMdAcceptanceView(value).accepted).toBe(true);
    expect(view.container.textContent).not.toContain(secret);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("does not infer individual properties for a legacy request without saved property selections", () => {
    const value = capabilities();
    value.acceptance_summary!.accepted_scopes![0].properties = [];
    const view = render(<PolymerMdAcceptanceEvidence capabilities={value} />);
    expand(view.container);
    expect(polymerMdAcceptanceView(value).accepted).toBe(true);
    expect(screen.getByText(/历史请求未保存逐项性质；不据当前目录推定单项性质已验收/)).toBeTruthy();
  });

  it("uses the independent summary for the protocol badge instead of sealed passed metadata", () => {
    const value = capabilities();
    const view = render(<PolymerMdProtocolInfo capabilities={value} />);
    expect(within(screen.getByLabelText("只读科学协议")).getByText("查看验收状态")).toBeTruthy();
    value.acceptance_summary!.registry_status = "revoked";
    value.runtime!.scientific_acceptance = "passed";
    view.rerender(<PolymerMdProtocolInfo capabilities={value} />);
    expect(within(screen.getByLabelText("只读科学协议")).getByText("待科学验收")).toBeTruthy();
  });
});
