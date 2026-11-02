"""Synthetic review fixtures only; no actual runtime or acceptance is modified."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from app.services.polymer_md_acceptance_registry import acceptance_summary, canonical_sha256
from polymer_md.contracts import ScientificRequest
from scripts import polymer_md_acceptance_registry as cli


def save(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def synthetic(tmp_path):
    manifest = {"source_sha": "8" * 40, "patch_version": "nexpoly-md-9", "synthetic_fixture_only": True}
    manifest["runtime_fingerprint"] = canonical_sha256(manifest)
    manifest_path = save(tmp_path / "synthetic-manifest.json", manifest)
    binding, _, protocol = cli.binding_from_manifest(manifest_path)
    evidence = []
    for label in ("raw_benchmark", "independent_recompute", "review_report"):
        path = tmp_path / (label + ".txt")
        path.write_text("SYNTHETIC_TEST_ONLY: " + label + "\n")
        path.chmod(0o600)
        evidence.append({"label": label, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size})
    review = {"decision": "approved", "reviewer": "PRIVATE_SYNTHETIC_REVIEWER",
        "reviewed_at": "2026-10-19T04:00:00+00:00", "protocol_id": "synthetic-test-only-v1",
        "binding": binding, "scopes": [{"public_material_label": "SYNTHETIC_TEST_ONLY",
            "scientific_request": ScientificRequest(smiles="*CC*", seed=1729).model_dump(mode="json")}],
        "evidence": evidence}
    review_path = save(tmp_path / "synthetic-review.json", review)
    return {"root": tmp_path / "registry", "manifest": manifest, "manifest_path": manifest_path,
        "protocol": protocol, "review": review, "review_path": review_path}


def test_dry_run_verifies_but_does_not_create_registry(synthetic, capsys):
    data = synthetic
    assert cli.main(["publish", "--registry", str(data["root"]), "--review", str(data["review_path"]),
                     "--manifest", str(data["manifest_path"]), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {"written": False, "dry_run": True, "kind": "approval"}
    assert "PRIVATE_SYNTHETIC_REVIEWER" not in output and "*CC*" not in output
    assert not data["root"].exists()


def test_actual_cli_publication_and_revocation_roundtrip_remain_scoped(synthetic, tmp_path):
    data = synthetic
    approval = cli.prepare_approval(data["review_path"], data["manifest_path"])
    result = cli.publish(data["root"], approval)
    assert result["written"]
    original = data["root"] / "receipts" / (approval["receipt_id"] + ".json")
    original_bytes = original.read_bytes()
    assert original.stat().st_mode & 0o777 == 0o400
    assert approval["review"]["decision_sha256"] == hashlib.sha256(data["review_path"].read_bytes()).hexdigest()
    summary = acceptance_summary(str(data["root"]), data["manifest"], data["protocol"])
    assert summary["registry_status"] == "verified"
    assert summary["scientific_acceptance"] == "scoped_external_review"
    assert summary["accepted_protocols"] == ["synthetic-test-only-v1"]
    public = json.dumps(summary)
    for private in ("PRIVATE_SYNTHETIC_REVIEWER", "*CC*", str(tmp_path), '"scientific_request"'):
        assert private not in public
    revocation_review = save(tmp_path / "synthetic-revocation-review.json", {
        "decision": "revoked", "reviewer": "PRIVATE_SYNTHETIC_REVIEWER", "reviewed_at": "2026-10-19T05:00:00Z",
        "receipt_id": approval["receipt_id"], "receipt_sha256": approval["receipt_sha256"]})
    revoked = cli.prepare_revocation(revocation_review)
    assert cli.publish(data["root"], revoked)["written"]
    assert original.read_bytes() == original_bytes
    summary = acceptance_summary(str(data["root"]), data["manifest"], data["protocol"])
    assert summary["registry_status"] == "revoked" and summary["accepted_protocols"] == []


def test_production_retention_binding_matches_real_status_and_cli_roundtrip(synthetic, tmp_path, capsys):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import polymer_md

    data = synthetic
    binding, _, protocol = cli.binding_from_manifest(data["manifest_path"], retention_enabled=False)
    assert binding != data["review"]["binding"]
    assert protocol["retention"]["enabled"] is False
    assert protocol["retention"]["artifact_days"] is None and protocol["retention"]["metadata_days"] is None
    review = deepcopy(data["review"])
    review["binding"] = binding
    save(data["review_path"], review)
    with pytest.raises(ValueError, match="does not bind"):
        cli.prepare_approval(data["review_path"], data["manifest_path"])
    approval = cli.prepare_approval(data["review_path"], data["manifest_path"], retention_enabled=False)
    with pytest.raises(ValueError, match="selected retention policy"):
        cli.publish(data["root"], approval)
    assert not data["root"].exists()
    assert cli.main(["publish", "--registry", str(data["root"]), "--review", str(data["review_path"]),
        "--manifest", str(data["manifest_path"]), "--retention-enabled", "false"]) == 0
    assert json.loads(capsys.readouterr().out)["written"]
    assert cli.main(["inspect", "--registry", str(data["root"]), "--manifest", str(data["manifest_path"]),
        "--retention-enabled", "false"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["registry_status"] == "verified"

    app = FastAPI()
    app.include_router(polymer_md.router)
    app.state.settings = SimpleNamespace(polymer_md_enabled=True, polymer_md_dev_enabled=False,
        polymer_md_retention_enabled=False, polymer_md_submit_enabled=False, polymer_md_acceptance_root=str(data["root"]))
    app.state.polymer_md_repository = SimpleNamespace(schema_ready=lambda: True)
    async def health():
        return {"ready": True, "runtime": data["manifest"]}
    app.state.polymer_md_worker_client = SimpleNamespace(health=health)
    app.state.polymer_md_analysis_client = None
    with TestClient(app) as client:
        status = client.get("/api/v1/polymer-md/status").json()
    assert status["scientific_protocol"] == protocol
    assert status["acceptance_summary"] == inspected
    assert inspected["binding"]["scientific_protocol_sha256"] == canonical_sha256(status["scientific_protocol"])
    assert inspected["accepted_protocols"] == ["synthetic-test-only-v1"]
    assert cli.main(["inspect", "--registry", str(data["root"]), "--manifest", str(data["manifest_path"])]) == 0
    assert json.loads(capsys.readouterr().out)["registry_status"] == "mismatch"

    revocation = save(tmp_path / "production-synthetic-revoke.json", {"decision": "revoked",
        "reviewer": "PRIVATE_SYNTHETIC_REVIEWER", "reviewed_at": "2026-11-02T10:00:00Z",
        "receipt_id": approval["receipt_id"], "receipt_sha256": approval["receipt_sha256"]})
    assert cli.main(["revoke", "--registry", str(data["root"]), "--review", str(revocation),
        "--retention-enabled", "false"]) == 0
    assert json.loads(capsys.readouterr().out)["written"]
    assert acceptance_summary(str(data["root"]), data["manifest"], protocol)["registry_status"] == "revoked"


def test_production_retention_binding_still_requires_real_review_and_evidence(synthetic):
    data = synthetic
    binding, _, _ = cli.binding_from_manifest(data["manifest_path"], retention_enabled=False)
    review = deepcopy(data["review"])
    review["binding"] = binding
    review["decision"] = "pending"
    save(data["review_path"], review)
    with pytest.raises(ValueError, match="explicit approved"):
        cli.prepare_approval(data["review_path"], data["manifest_path"], retention_enabled=False)
    review["decision"] = "approved"
    review["evidence"][0]["sha256"] = "f" * 64
    save(data["review_path"], review)
    with pytest.raises(ValueError, match="evidence bytes"):
        cli.prepare_approval(data["review_path"], data["manifest_path"], retention_enabled=False)
    assert not data["root"].exists()


@pytest.mark.parametrize("invalid", ["false", None, 0])
def test_retention_binding_requires_a_boolean_policy_in_programmatic_calls(synthetic, invalid):
    with pytest.raises(ValueError, match="explicit boolean"):
        cli.binding_from_manifest(synthetic["manifest_path"], retention_enabled=invalid)


@pytest.mark.parametrize("change", ["pending", "binding", "evidence_hash", "missing_evidence", "noncanonical_scope", "unfixed_seed"])
def test_incomplete_or_changed_review_never_publishes(synthetic, change):
    data = synthetic
    review = deepcopy(data["review"])
    if change == "pending": review["decision"] = "pending"
    elif change == "binding": review["binding"]["runtime_fingerprint"] = "f" * 64
    elif change == "evidence_hash": review["evidence"][0]["sha256"] = "f" * 64
    elif change == "missing_evidence": review["evidence"] = [review["evidence"][0]]
    elif change == "noncanonical_scope": review["scopes"][0]["scientific_request"].pop("temp")
    else: review["scopes"][0]["scientific_request"]["seed"] = None
    save(data["review_path"], review)
    with pytest.raises(ValueError): cli.prepare_approval(data["review_path"], data["manifest_path"])
    assert not data["root"].exists()


def test_changed_evidence_after_preparation_is_rechecked_before_publication(synthetic):
    data = synthetic
    approval = cli.prepare_approval(data["review_path"], data["manifest_path"])
    Path(approval["evidence"][0]["path"]).write_text("tampered evidence")
    with pytest.raises(ValueError): cli.publish(data["root"], approval)
    assert not data["root"].exists()


def test_publication_never_overwrites_an_existing_decision(synthetic):
    data = synthetic
    approval = cli.prepare_approval(data["review_path"], data["manifest_path"])
    assert cli.prepare_approval(data["review_path"], data["manifest_path"]) == approval
    cli.publish(data["root"], approval)
    path = data["root"] / "receipts" / (approval["receipt_id"] + ".json")
    before = path.read_bytes()
    with pytest.raises(FileExistsError): cli.publish(data["root"], approval)
    assert path.read_bytes() == before
    assert all(not item.name.startswith(".prepare-") for item in path.parent.iterdir())


def test_private_input_permissions_and_symlinks_are_rejected(synthetic, tmp_path):
    data = synthetic
    data["review_path"].chmod(0o644)
    with pytest.raises(ValueError): cli.prepare_approval(data["review_path"], data["manifest_path"])
    data["review_path"].chmod(0o600)
    link = tmp_path / "linked-review.json"
    link.symlink_to(data["review_path"])
    with pytest.raises(ValueError): cli.prepare_approval(link, data["manifest_path"])


def test_revocation_must_reference_exact_original_hash(synthetic, tmp_path):
    data = synthetic
    approval = cli.prepare_approval(data["review_path"], data["manifest_path"])
    cli.publish(data["root"], approval)
    review = save(tmp_path / "wrong-revocation.json", {"decision": "revoked", "reviewer": "SYNTHETIC_ONLY",
        "reviewed_at": "2026-10-19T05:00:00Z", "receipt_id": approval["receipt_id"], "receipt_sha256": "f" * 64})
    with pytest.raises(ValueError): cli.publish(data["root"], cli.prepare_revocation(review))
    assert list((data["root"] / "revocations").iterdir()) == []


def test_manifest_tampering_is_rejected_without_changing_any_runtime(synthetic):
    data = synthetic
    manifest = deepcopy(data["manifest"])
    manifest["patch_version"] = "changed"
    save(data["manifest_path"], manifest)
    with pytest.raises(ValueError): cli.binding_from_manifest(data["manifest_path"])
    assert not data["root"].exists()


@pytest.mark.parametrize("change", ["duplicate_key", "nonfinite"])
def test_ambiguous_decision_json_is_rejected(synthetic, change):
    data = synthetic
    if change == "duplicate_key":
        text = data["review_path"].read_text().replace('"decision": "approved"', '"decision": "pending", "decision": "approved"')
        data["review_path"].write_text(text)
    else:
        review = deepcopy(data["review"])
        review["evidence"][0]["size_bytes"] = float("nan")
        save(data["review_path"], review)
    with pytest.raises(ValueError): cli.prepare_approval(data["review_path"], data["manifest_path"])
    assert not data["root"].exists()


def test_dry_run_rejects_an_existing_public_registry_root(synthetic):
    data = synthetic
    approval = cli.prepare_approval(data["review_path"], data["manifest_path"])
    data["root"].mkdir(mode=0o755)
    data["root"].chmod(0o755)
    with pytest.raises(ValueError): cli.publish(data["root"], approval, dry_run=True)
    assert list(data["root"].iterdir()) == []


@pytest.mark.parametrize("budget", ["file", "events", "total"])
def test_publisher_cannot_create_events_the_reader_would_reject(synthetic, monkeypatch, budget):
    import app.services.polymer_md_acceptance_registry as reader
    data = synthetic
    first = cli.prepare_approval(data["review_path"], data["manifest_path"])
    if budget == "file":
        monkeypatch.setattr(reader, "MAX_FILE_BYTES", 200)
        with pytest.raises(ValueError): cli.publish(data["root"], first, dry_run=True)
        assert not data["root"].exists()
        return
    cli.publish(data["root"], first)
    original = data["root"] / "receipts" / (first["receipt_id"] + ".json")
    before = original.read_bytes()
    review = deepcopy(data["review"])
    review["protocol_id"] = "synthetic-other-protocol"
    save(data["review_path"], review)
    second = cli.prepare_approval(data["review_path"], data["manifest_path"])
    if budget == "events": monkeypatch.setattr(reader, "MAX_EVENTS", 1)
    else: monkeypatch.setattr(reader, "MAX_TOTAL_BYTES", len(before) + len(cli.document_bytes(second)) - 1)
    with pytest.raises(ValueError): cli.publish(data["root"], second)
    assert original.read_bytes() == before
    assert len(list(original.parent.iterdir())) == 1


@pytest.mark.parametrize("extra_budget", [0, 8])
def test_growing_evidence_stream_fails_at_the_first_oversized_block(synthetic, monkeypatch, extra_budget):
    data = synthetic
    entry = data["review"]["evidence"][0]
    target = Path(entry["path"])
    contents = target.read_bytes()
    original_open = Path.open
    reads = []
    closed = []

    class GrowingStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def read(self, maximum):
            reads.append(maximum)
            if len(reads) == 1:
                return contents
            if len(reads) == 2:
                return b"+"
            raise AssertionError("growing evidence must be rejected before another read")

        def fileno(self):
            raise AssertionError("oversized evidence must be rejected before the final stat")

    def growing_open(path, *args, **kwargs):
        return GrowingStream() if path == target else original_open(path, *args, **kwargs)

    monkeypatch.setattr(cli, "MAX_EVIDENCE_BYTES", len(contents) + extra_budget)
    monkeypatch.setattr(Path, "open", growing_open)
    with pytest.raises(ValueError, match="declared size or evidence budget"):
        cli.verify_evidence(data["review"]["evidence"])
    assert len(reads) == 2 and closed == [True]
    assert not data["root"].exists()


def test_evidence_observed_length_must_match_even_when_hash_and_file_identity_match(synthetic, monkeypatch):
    entries = deepcopy(synthetic["review"]["evidence"])
    target = Path(entries[0]["path"])
    original_open = Path.open
    truncated = target.read_bytes()[:-1]
    entries[0]["sha256"] = hashlib.sha256(truncated).hexdigest()

    class ShortStream:
        def __init__(self):
            self.stream = original_open(target, "rb")
            self.sent = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, maximum):
            if self.sent:
                return b""
            self.sent = True
            return truncated

        def fileno(self):
            return self.stream.fileno()

    def short_open(path, *args, **kwargs):
        return ShortStream() if path == target else original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", short_open)
    with pytest.raises(ValueError, match="evidence bytes changed"):
        cli.verify_evidence(entries)
