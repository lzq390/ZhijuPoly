"""Read local, immutable scientific review receipts without altering runtime seals.

The trust boundary is the owner-private, read-only filesystem mount. SHA256
detects changed receipts; it is not a signature or an automatic scientific review.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from polymer_md.contracts import NativeConfiguration, ScientificRequest

MAX_FILE_BYTES = 262144
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 256
QM_FIELDS = ("opt_method", "opt_basis", "charge", "charge_method", "charge_basis",
             "sp_method", "sp_basis", "polar_method", "polar_basis")
REQUIRED_EVIDENCE_LABELS = frozenset({"raw_benchmark", "independent_recompute", "review_report"})


def canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _keys(value, names):
    if type(value) is not dict or set(value) != set(names):
        raise ValueError("invalid acceptance registry fields")


def _text(value, maximum=200, pattern=None):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or any(ord(c) < 32 for c in value):
        raise ValueError("invalid acceptance registry text")
    if pattern is not None and re.fullmatch(pattern, value) is None:
        raise ValueError("invalid acceptance registry identifier")


def _hex(value, size=64):
    _text(value, size, r"[0-9a-f]{" + str(size) + "}")


def _binding(value):
    _keys(value, ("runtime_fingerprint", "source_sha", "patch_version", "scientific_protocol_sha256"))
    _hex(value["runtime_fingerprint"])
    _hex(value["source_sha"], 40)
    _hex(value["scientific_protocol_sha256"])
    _text(value["patch_version"], 128, r"[A-Za-z0-9._-]+")


def _review(value, decision):
    _keys(value, ("decision", "reviewer", "reviewed_at", "decision_sha256"))
    if value["decision"] != decision:
        raise ValueError("explicit review decision required")
    _text(value["reviewer"])
    _text(value["reviewed_at"], 40)
    try:
        parsed = datetime.fromisoformat(value["reviewed_at"].replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
    except ValueError:
        raise ValueError("review time must include timezone") from None
    _hex(value["decision_sha256"])


def _sealed(value, field):
    _hex(value[field])
    if value[field] != canonical_sha256({key: item for key, item in value.items() if key != field}):
        raise ValueError("acceptance registry document SHA256 mismatch")


def validate_receipt(document):
    """Shared strict private format for the local admin publisher and reader."""
    _keys(document, ("schema_version", "kind", "receipt_id", "protocol_id", "binding",
                     "review", "scopes", "evidence", "receipt_sha256"))
    if type(document["schema_version"]) is not int or document["schema_version"] != 1 or document["kind"] != "approval":
        raise ValueError("unsupported acceptance registry version")
    _hex(document["receipt_id"], 32)
    _text(document["protocol_id"], 128, r"[A-Za-z0-9][A-Za-z0-9._:-]*")
    _binding(document["binding"])
    _review(document["review"], "approved")
    scopes = document["scopes"]
    if type(scopes) is not list or not 1 <= len(scopes) <= 32:
        raise ValueError("explicit acceptance scopes required")
    hashes = set()
    for scope in scopes:
        _keys(scope, ("public_material_label", "scientific_request"))
        _text(scope["public_material_label"], 80, r"\w[\w .()+-]*")
        try:
            request = ScientificRequest.model_validate(scope["scientific_request"]).model_dump(mode="json")
            digest = canonical_sha256(request)
            if digest != canonical_sha256(scope["scientific_request"]):
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise ValueError("scope must contain a complete canonical ScientificRequest") from None
        if request["seed"] is None:
            raise ValueError("scope requires the persisted explicit scientific seed")
        if digest in hashes:
            raise ValueError("duplicate exact scientific request scope")
        hashes.add(digest)
    evidence = document["evidence"]
    if type(evidence) is not list or not 1 <= len(evidence) <= 256:
        raise ValueError("review evidence required")
    labels = set()
    for item in evidence:
        _keys(item, ("label", "path", "sha256", "size_bytes"))
        _text(item["label"], 80, r"\w[\w .()+-]*")
        _text(item["path"], 4096)
        if not Path(item["path"]).is_absolute() or ".." in Path(item["path"]).parts:
            raise ValueError("evidence path must be absolute")
        _hex(item["sha256"])
        if type(item["size_bytes"]) is not int or not 0 <= item["size_bytes"] <= 2**63 - 1:
            raise ValueError("invalid evidence file size")
        if item["label"] in labels:
            raise ValueError("duplicate evidence label")
        labels.add(item["label"])
    if not REQUIRED_EVIDENCE_LABELS <= labels:
        raise ValueError("raw benchmark, independent recomputation and review report evidence required")
    _sealed(document, "receipt_sha256")
    return document


def validate_revocation(document):
    _keys(document, ("schema_version", "kind", "revocation_id", "receipt_id", "receipt_sha256", "review", "revocation_sha256"))
    if type(document["schema_version"]) is not int or document["schema_version"] != 1 or document["kind"] != "revocation":
        raise ValueError("unsupported acceptance revocation version")
    _hex(document["revocation_id"], 32)
    _hex(document["receipt_id"], 32)
    _hex(document["receipt_sha256"])
    _review(document["review"], "revoked")
    _sealed(document, "revocation_sha256")
    return document


def _private(path, *, directory=False):
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("registry symbolic links forbidden")
    info = path.lstat()
    if (info.st_uid != os.geteuid() or info.st_mode & 0o077
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or not directory and info.st_nlink != 1):
        raise ValueError("registry must be an owner-private ordinary path")


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate registry JSON key")
        value[key] = item
    return value


def _read(path):
    _private(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        data = handle.read(MAX_FILE_BYTES + 1)
        after = os.fstat(handle.fileno())
    if len(data) > MAX_FILE_BYTES or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("registry file size or identity invalid")
    return json.loads(data, object_pairs_hook=_unique_pairs,
                      parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite registry JSON")))


def _public_scope(receipt, scope):
    request, review = scope["scientific_request"], receipt["review"]
    native = request.get("native_configuration")
    qm = native or NativeConfiguration(properties=["density"]).model_dump(mode="json")
    fields = ("eq_ns", "additional_ns", "max_additional", "sample_interval_ps", "tc_axis", "tc_ns", "tc_rep", "seed")
    return {"receipt_id": receipt["receipt_id"], "receipt_sha256": receipt["receipt_sha256"],
        "protocol_id": receipt["protocol_id"], "request_sha256": canonical_sha256(request),
        "material_label": scope["public_material_label"],
        "model_mode": (request.get("model_configuration") or {}).get("mode", "homopolymer"),
        "forcefield": request["forcefield"], "qm": {key: qm[key] for key in QM_FIELDS},
        "conditions": {"temperature_k": request["temp"], "pressure_atm": request["pressure"],
                       **{key: request[key] for key in fields}},
        "properties": native["properties"] if native else [], "reviewed_at": review["reviewed_at"],
        "reviewer_ref": hashlib.sha256(("polymer-md-reviewer-v1:" + review["reviewer"]).encode()).hexdigest(),
        "decision_sha256": review["decision_sha256"],
        "evidence": [{key: item[key] for key in ("label", "sha256", "size_bytes")} for item in receipt["evidence"]],
        "scope_policy": "exact_scientific_request_only"}


def acceptance_summary(root, runtime, protocol):
    """Fail closed, preserve the sealed runtime object, and expose safe scopes."""
    binding = {key: runtime.get(key) for key in ("runtime_fingerprint", "source_sha", "patch_version")}
    binding["scientific_protocol_sha256"] = canonical_sha256(protocol)
    result = {"scientific_acceptance": "pending", "adapter_scientific_validation": runtime.get("adapter_scientific_validation", "pending"),
        "accepted_protocols": [], "accepted_protocol_count": 0, "source": "external_registry",
        "registry_status": "unconfigured" if not root else "missing", "binding": binding, "accepted_scopes": []}
    if not root:
        return result
    try:
        path = Path(root)
        if not path.is_absolute():
            raise ValueError("registry root must be absolute")
        if not path.exists() and not path.is_symlink():
            return result
        _private(path, directory=True)
        if set(item.name for item in path.iterdir()) - {"receipts", "revocations", ".registry.lock"}:
            raise ValueError("unknown registry root entry")
        lock = path / ".registry.lock"
        if lock.exists() or lock.is_symlink():
            _private(lock)
            if lock.stat().st_size != 0:
                raise ValueError("registry lock must be an empty ordinary file")
        receipts, revoked, events, total = {}, [], 0, 0
        for kind in ("receipts", "revocations"):
            folder = path / kind
            if not folder.exists() and not folder.is_symlink():
                continue
            _private(folder, directory=True)
            for file in sorted(folder.iterdir()):
                if re.fullmatch(r"[0-9a-f]{32}\.json", file.name) is None:
                    raise ValueError("unknown registry event filename")
                events += 1
                if events > MAX_EVENTS:
                    raise ValueError("registry event budget exceeded")
                document = _read(file)
                total += file.stat().st_size
                if total > MAX_TOTAL_BYTES:
                    raise ValueError("registry byte budget exceeded")
                if kind == "receipts":
                    value = validate_receipt(document)
                    if file.stem != value["receipt_id"]:
                        raise ValueError("receipt filename mismatch")
                    receipts[value["receipt_id"]] = value
                else:
                    value = validate_revocation(document)
                    if file.stem != value["revocation_id"]:
                        raise ValueError("revocation filename mismatch")
                    revoked.append(value)
        revoked_ids = set()
        for event in revoked:
            original = receipts.get(event["receipt_id"])
            if original is None or original["receipt_sha256"] != event["receipt_sha256"]:
                raise ValueError("revocation has no exact original receipt")
            revoked_ids.add(event["receipt_id"])
        if not receipts:
            return result
        try:
            _binding(binding)
        except ValueError:
            result["registry_status"] = "mismatch"
            return result
        matches = [value for value in receipts.values() if value["binding"] == binding]
        accepted = [value for value in matches if value["receipt_id"] not in revoked_ids]
        if not accepted:
            result["registry_status"] = "revoked" if matches else "mismatch" if receipts else "missing"
            return result
        result.update(registry_status="verified", scientific_acceptance="scoped_external_review",
            accepted_protocols=sorted({value["protocol_id"] for value in accepted}),
            accepted_scopes=[_public_scope(value, scope) for value in accepted for scope in value["scopes"]])
        result["accepted_protocol_count"] = len(result["accepted_protocols"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
        result.update(registry_status="invalid", scientific_acceptance="pending", accepted_protocols=[],
                      accepted_protocol_count=0, accepted_scopes=[])
    return result
