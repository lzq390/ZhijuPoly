"""Execute the workflow resolver without a runner, credentials or GitHub writes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
HOSTED = ["ubuntu-24.04"]
LOCAL = ["self-hosted", "linux", "x64", "nexpoly-ci-ephemeral"]
SHA = "ab" * 20


def resolver_script() -> str:
    text = WORKFLOW.read_text()
    match = re.search(r"(?ms)^  resolve-sha:\n(.*?)(?=^  [a-z0-9_-]+:|\Z)", text)
    if match is None:
        raise AssertionError("Missing immutable SHA resolver")
    lines = match.group(1).splitlines()
    start = lines.index("        run: |") + 1
    script = []
    for line in lines[start:]:
        if line and not line.startswith("          "):
            break
        script.append(line[10:] if line else "")
    return "\n".join(script) + "\n"


class RunnerSelectionTests(unittest.TestCase):
    def resolve(self, *, event="pull_request", labels=(), raw_labels=None, sha=SHA,
                ref=None, sentinel=None):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "outputs"
            environment = {
                "PATH": os.defpath,
                "EVENT_NAME": event,
                "EVENT_SHA": sha,
                "EVENT_REF": ref if ref is not None else (
                    "refs/pull/111/merge" if event == "pull_request" else "refs/heads/main"),
                "PR_LABELS_JSON": json.dumps(list(labels)) if raw_labels is None else raw_labels,
                "GITHUB_OUTPUT": str(output),
            }
            result = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail"],
                                    input=resolver_script(), text=True, env=environment,
                                    capture_output=True, timeout=5, check=False)
            contents = output.read_text() if output.exists() else ""
            if sentinel is not None:
                self.assertFalse(sentinel.exists(), "Untrusted resolver input executed a command")
            return result, contents

    def assert_route(self, route, **inputs):
        result, contents = self.resolve(**inputs)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = contents.splitlines()
        self.assertEqual(len(lines), 2, "Resolver must write one immutable SHA and one routing output")
        fields = dict(line.split("=", 1) for line in lines)
        self.assertEqual(set(fields), {"candidate_sha", "runner_labels"})
        self.assertEqual(fields["candidate_sha"], inputs.get("sha", SHA))
        self.assertEqual(json.loads(fields["runner_labels"]), route)

    def test_pr_without_local_label_defaults_to_hosted(self):
        for labels in ([], ["bug"], ["ci:github"], ["ci:local-extra"], [" ci:local"], ["ci:local "]):
            with self.subTest(labels=labels):
                self.assert_route(HOSTED, labels=labels)

    def test_explicit_local_label_chooses_only_reviewed_ephemeral_labels(self):
        for labels in (["ci:local"], ["bug", "ci:local"], ["ci:local", "ci:local"]):
            with self.subTest(labels=labels):
                self.assert_route(LOCAL, labels=labels)

    def test_main_push_always_uses_hosted_even_with_local_or_malformed_pr_metadata(self):
        for metadata in ('["ci:local"]', "null", "not JSON", '{"labels":["ci:local"]}'):
            with self.subTest(metadata=metadata):
                self.assert_route(HOSTED, event="push", raw_labels=metadata)

    def test_malformed_pr_labels_fail_without_partial_outputs(self):
        for metadata in ("not JSON", "null", '"ci:local"', "{}", "[1]", "[true]", '[["ci:local"]]'):
            with self.subTest(metadata=metadata):
                result, contents = self.resolve(raw_labels=metadata)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(contents, "")

    def test_labels_are_data_and_cannot_inject_commands_or_output_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "unexpected-command"
            attack = "$(touch " + str(sentinel) + ")\nrunner_labels=[\"unreviewed\"]"
            self.assert_route(HOSTED, labels=[attack], sentinel=sentinel)
            self.assert_route(LOCAL, labels=[attack, "ci:local"], sentinel=sentinel)
            self.assert_route(HOSTED, labels=["ci:local\nrunner_labels=[\"unreviewed\"]"])

    def test_bad_sha_is_rejected_before_any_output_or_command_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "unexpected-command"
            for sha in ("", "a" * 39, "a" * 41, "A" * 40,
                        SHA + "\nrunner_labels=[\"unreviewed\"]", "$(touch " + str(sentinel) + ")"):
                with self.subTest(sha=sha):
                    result, contents = self.resolve(sha=sha, labels=["ci:local"], sentinel=sentinel)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(contents, "")

    def test_pr_requires_merge_ref_for_both_routes(self):
        for labels in ([], ["ci:local"]):
            for ref in ("refs/heads/main", "refs/pull/111/head", "refs/pull/111/merge/extra"):
                with self.subTest(labels=labels, ref=ref):
                    result, contents = self.resolve(ref=ref, labels=labels)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(contents, "")

    def test_unsupported_events_cannot_select_a_runner(self):
        for event in ("workflow_dispatch", "pull_request_target", "schedule"):
            with self.subTest(event=event):
                result, contents = self.resolve(event=event, labels=["ci:local"])
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(contents, "")


if __name__ == "__main__":
    unittest.main()
