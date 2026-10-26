"""Synthetic local receipts exercise the real reader; no scientific approval."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.routers import polymer_md
from app.services import polymer_md_acceptance_registry as registry
from polymer_md.contracts import ScientificRequest


def seal(document, field="receipt_sha256"):
    value = deepcopy(document)
    value[field] = registry.canonical_sha256({k: v for k, v in value.items() if k != field})
    return value


def private_write(path, document):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        path.chmod(0o600)
    path.write_text(json.dumps(document, allow_nan=False))
    path.chmod(0o400)


@pytest.fixture
def example(tmp_path):
    root = tmp_path / "synthetic-registry"
    root.mkdir(mode=0o700)
    (root / "receipts").mkdir(mode=0o700)
    (root / "revocations").mkdir(mode=0o700)
    runtime = {"source_sha": "2" * 40, "patch_version": "synthetic-fixture-v1",
               "runtime_fingerprint": "1" * 64, "adapter_scientific_validation": "pending",
               "scientific_acceptance": "pending", "accepted_protocols": []}
    protocol = polymer_md._scientific_protocol()
    request = ScientificRequest(smiles="*CC*", seed=1729,
        native_configuration={"properties": ["density", "Cp"]},
        model_configuration={"mode": "homopolymer", "components": [{"id": "A", "smiles": "*CC*"}],
                             "chain_length": {"mode": "atoms", "value": 1000}}).model_dump(mode="json")
    evidence = []
    for label in sorted(registry.REQUIRED_EVIDENCE_LABELS):
        path = tmp_path / (label + ".synthetic.txt")
        path.write_text("Synthetic test evidence only; no material acceptance.\n")
        path.chmod(0o600)
        evidence.append({"label": label, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                         "size_bytes": path.stat().st_size})
    receipt = seal({"schema_version": 1, "kind": "approval", "receipt_id": "a" * 32,
        "protocol_id": "synthetic-exact-request-fixture", "binding": {
            **{key: runtime[key] for key in ("source_sha", "patch_version", "runtime_fingerprint")},
            "scientific_protocol_sha256": registry.canonical_sha256(protocol)},
        "review": {"decision": "approved", "reviewer": "PRIVATE synthetic reviewer private@example.invalid",
            "reviewed_at": "2026-10-19T00:00:00+00:00", "decision_sha256": "3" * 64},
        "scopes": [{"public_material_label": "Synthetic PE fixture", "scientific_request": request}],
        "evidence": evidence})
    path = root / "receipts" / (receipt["receipt_id"] + ".json")
    private_write(path, receipt)
    return root, runtime, protocol, receipt, path


def test_valid_synthetic_reader_projects_only_explicit_public_scope(example):
    root, runtime, protocol, receipt, _ = example
    original = deepcopy(runtime)
    value = registry.acceptance_summary(str(root), runtime, protocol)
    assert value["registry_status"] == "verified" and value["scientific_acceptance"] == "scoped_external_review"
    assert value["accepted_protocols"] == [receipt["protocol_id"]] and value["accepted_protocol_count"] == 1
    scope = value["accepted_scopes"][0]
    assert scope["material_label"] == "Synthetic PE fixture" and scope["model_mode"] == "homopolymer"
    assert scope["request_sha256"] == registry.canonical_sha256(receipt["scopes"][0]["scientific_request"])
    assert scope["conditions"]["seed"] == 1729 and scope["scope_policy"] == "exact_scientific_request_only"
    rendered = json.dumps(value)
    for private in ("*CC*", "PRIVATE", "private@example.invalid", str(root.parent), '"smiles"', '"components"', '"scientific_request"', '"path"'):
        assert private not in rendered
    assert runtime == original and value["adapter_scientific_validation"] == "pending"


def test_disabled_and_missing_registry_never_trust_runtime_accepted(example):
    root, runtime, protocol, _, _ = example
    runtime.update(scientific_acceptance="passed", accepted_protocols=["untrusted-old-seal"])
    for path, status in (("", "unconfigured"), (str(root / "absent"), "missing")):
        value = registry.acceptance_summary(path, runtime, protocol)
        assert value["registry_status"] == status and value["accepted_protocols"] == []
        assert value["scientific_acceptance"] == "pending"


@pytest.mark.parametrize("key,value", [("runtime_fingerprint", "4" * 64), ("source_sha", "5" * 40), ("patch_version", "other")])
def test_runtime_binding_changes_have_no_accepted_scope(example, key, value):
    root, runtime, protocol, _, _ = example
    runtime[key] = value
    result = registry.acceptance_summary(str(root), runtime, protocol)
    assert result["registry_status"] == "mismatch" and not result["accepted_scopes"]


def test_protocol_change_requires_new_review(example):
    root, runtime, protocol, _, _ = example
    protocol["rnemd"]["burn_in_ps"] = 801
    value = registry.acceptance_summary(str(root), runtime, protocol)
    assert value["registry_status"] == "mismatch" and not value["accepted_protocols"]


def test_revocation_keeps_original_decision_and_removes_acceptance(example):
    root, runtime, protocol, receipt, path = example
    original = path.read_bytes()
    event = seal({"schema_version": 1, "kind": "revocation", "revocation_id": "b" * 32,
        "receipt_id": receipt["receipt_id"], "receipt_sha256": receipt["receipt_sha256"],
        "review": {"decision": "revoked", "reviewer": "Private revoker", "reviewed_at": "2026-10-19T01:00:00Z", "decision_sha256": "4" * 64}}, "revocation_sha256")
    private_write(root / "revocations" / (event["revocation_id"] + ".json"), event)
    value = registry.acceptance_summary(str(root), runtime, protocol)
    assert value["registry_status"] == "revoked" and not value["accepted_protocols"]
    assert path.read_bytes() == original
    event["receipt_sha256"] = "5" * 64
    private_write(root / "revocations" / (event["revocation_id"] + ".json"), seal(event, "revocation_sha256"))
    assert registry.acceptance_summary(str(root), runtime, protocol)["registry_status"] == "invalid"


@pytest.mark.parametrize("mode", ["tampered", "file_permissions", "root_permissions", "symlink", "duplicate_json", "nonfinite", "oversize"])
def test_corrupt_or_unsafe_registry_fails_closed(example, mode):
    root, runtime, protocol, receipt, path = example
    if mode == "tampered":
        receipt["protocol_id"] = "changed-without-new-hash"
        private_write(path, receipt)
    elif mode == "file_permissions":
        path.chmod(0o644)
    elif mode == "root_permissions":
        root.chmod(0o755)
    elif mode == "symlink":
        target = path.with_suffix(".private")
        path.rename(target)
        path.symlink_to(target)
    else:
        path.chmod(0o600)
        if mode == "duplicate_json":
            path.write_text('{"kind":"approval",' + json.dumps(receipt)[1:])
        elif mode == "nonfinite":
            path.write_text('{"value": NaN}')
        else:
            path.write_bytes(b" " * (registry.MAX_FILE_BYTES + 1))
        path.chmod(0o400)
    value = registry.acceptance_summary(str(root), runtime, protocol)
    assert value["registry_status"] == "invalid" and value["accepted_protocol_count"] == 0


def test_empty_owner_private_registry_lock_is_supported(example):
    root, runtime, protocol, _, _ = example
    lock = root / ".registry.lock"
    lock.touch(mode=0o600)
    assert registry.acceptance_summary(str(root), runtime, protocol)["registry_status"] == "verified"
    lock.write_text("unexpected data")
    assert registry.acceptance_summary(str(root), runtime, protocol)["registry_status"] == "invalid"


def test_another_effective_owner_or_hardlinked_receipt_is_not_trusted(example, monkeypatch):
    root, runtime, protocol, _, path = example
    owner = registry.os.geteuid()
    monkeypatch.setattr(registry.os, "geteuid", lambda: owner + 1)
    assert registry.acceptance_summary(str(root), runtime, protocol)["registry_status"] == "invalid"
    monkeypatch.setattr(registry.os, "geteuid", lambda: owner)
    (root.parent / "external-hardlink.json").hardlink_to(path)
    assert registry.acceptance_summary(str(root), runtime, protocol)["registry_status"] == "invalid"


def test_historical_receipt_does_not_expand_current_runtime_scope(example):
    root, runtime, protocol, receipt, _ = example
    historical = deepcopy(receipt)
    historical["receipt_id"] = "c" * 32
    historical["protocol_id"] = "historical-only"
    historical["binding"]["runtime_fingerprint"] = "9" * 64
    private_write(root / "receipts" / (historical["receipt_id"] + ".json"), seal(historical))
    value = registry.acceptance_summary(str(root), runtime, protocol)
    assert value["accepted_protocols"] == [receipt["protocol_id"]]
    assert len(value["accepted_scopes"]) == 1
    runtime["runtime_fingerprint"] = None
    value = registry.acceptance_summary(str(root), runtime, protocol)
    assert value["registry_status"] == "mismatch" and not value["accepted_scopes"]


@pytest.mark.parametrize("change", ["missing_evidence_kind", "partial_request", "unknown_field", "random_seed"])
def test_review_requires_explicit_complete_scope_and_three_evidence_kinds(example, change):
    _, _, _, receipt, _ = example
    if change == "missing_evidence_kind":
        receipt["evidence"].pop()
    elif change == "partial_request":
        receipt["scopes"][0]["scientific_request"] = {"smiles": "*CC*", "seed": 1729}
    elif change == "unknown_field":
        receipt["arbitrary_scope"] = "all polymers"
    else:
        receipt["scopes"][0]["scientific_request"]["seed"] = None
    with pytest.raises(ValueError):
        registry.validate_receipt(seal(receipt))


def test_legacy_canonical_18_field_scope_preserves_exact_shape_and_qm_defaults(example):
    root, runtime, protocol, receipt, path = example
    legacy = ScientificRequest(smiles="*CC*", seed=1729).model_dump(mode="json")
    assert len(legacy) == 18 and "native_configuration" not in legacy and "model_configuration" not in legacy
    receipt["scopes"][0]["scientific_request"] = legacy
    private_write(path, seal(receipt))
    result = registry.acceptance_summary(str(root), runtime, protocol)
    assert result["registry_status"] == "verified"
    scope = result["accepted_scopes"][0]
    assert scope["request_sha256"] == registry.canonical_sha256(legacy)
    assert scope["qm"]["opt_method"] == "wb97m-d3bj" and scope["qm"]["charge_method"] == "HF"
    assert scope["model_mode"] == "homopolymer" and scope["properties"] == []


def test_capabilities_integrates_external_summary_and_preserves_runtime_seal(example):
    root, runtime, _, receipt, _ = example
    app = FastAPI()
    app.include_router(polymer_md.router)
    app.state.settings = SimpleNamespace(polymer_md_dev_enabled=True, polymer_md_submit_enabled=False,
                                         polymer_md_acceptance_root=str(root))
    app.state.polymer_md_repository = SimpleNamespace(schema_ready=lambda: True)
    async def health():
        return {"ready": True, "runtime": runtime}
    app.state.polymer_md_worker_client = SimpleNamespace(health=health)
    with TestClient(app) as client:
        data = client.get("/api/v1/polymer-md/capabilities").json()
    assert data["acceptance_summary"]["accepted_protocols"] == [receipt["protocol_id"]]
    assert data["runtime"]["accepted_protocols"] == [] and data["runtime"]["scientific_acceptance"] == "pending"
    assert data["acceptance_summary"]["binding"]["scientific_protocol_sha256"] == registry.canonical_sha256(data["scientific_protocol"])
