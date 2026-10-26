"""Public evidence identity never exposes an execution fencing credential."""
import hashlib
import json

import pytest

from app.services.polymer_md_repository import public_job


JOB = "9c2bab31-8114-49a3-bb19-b1258a795941"
OTHER_JOB = "81ec7150-a537-43d0-b3d4-21cda662d744"
TOKEN = "7a" * 32


def test_public_job_publishes_versioned_attempt_identity_without_fencing_token():
    source = {"job_id": JOB, "attempt_token": TOKEN, "owner_user_id": "private-owner",
              "artifacts": [{"artifact_id": "log", "attempt_token": TOKEN,
                             "storage_ref": "private/file", "filename": "engine.log"}]}
    value = public_job(source)
    assert value["attempt_fingerprint"] == hashlib.sha256(
        ("polymer-md-attempt-v1:" + JOB + ":" + TOKEN).encode()).hexdigest()
    assert value == public_job(source)
    assert value["artifacts"] == [{"artifact_id": "log", "filename": "engine.log"}]
    assert "attempt_token" not in json.dumps(value) and TOKEN not in json.dumps(value)
    assert "owner_user_id" not in value
    assert source["attempt_token"] == TOKEN  # Internal startup fencing remains intact.


def test_public_attempt_identity_changes_with_both_attempt_and_job():
    same_job = public_job({"job_id": JOB, "attempt_token": TOKEN})["attempt_fingerprint"]
    new_attempt = public_job({"job_id": JOB, "attempt_token": "8b" * 32})["attempt_fingerprint"]
    new_job = public_job({"job_id": OTHER_JOB, "attempt_token": TOKEN})["attempt_fingerprint"]
    assert len({same_job, new_attempt, new_job}) == 3


@pytest.mark.parametrize("token", [None, "", "bad", "x" * 64, "A" * 64, True, b"a" * 64])
def test_missing_or_invalid_attempt_identity_is_unavailable(token):
    value = public_job({"job_id": JOB, "attempt_token": token,
                        "attempt_fingerprint": "forged"})
    assert "attempt_fingerprint" not in value
    assert "attempt_token" not in value


def test_missing_job_does_not_fabricate_attempt_identity():
    assert "attempt_fingerprint" not in public_job({"attempt_token": TOKEN})
