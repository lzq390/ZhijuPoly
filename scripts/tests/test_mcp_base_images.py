from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest
from scripts.ci.mcp_base_images import BASES, SOURCE, load_lock
from scripts.ci import validate_workflows as policy

ROOT = Path(__file__).resolve().parents[2]


class McpBaseImageTests(unittest.TestCase):
    def payload(self):
        return {"version": 1, "source": SOURCE, "images": {name: {
            "reference": f"ghcr.io/lzq390/{repository}@sha256:" + "a" * 64,
            "reviewed_image_id": "sha256:" + reviewed,
        } for name, (repository, reviewed, _) in BASES.items()}}

    def check(self, value):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            return load_lock(path)

    def test_public_build_arguments_require_all_three_exact_reviewed_base_identities(self):
        result = self.check(self.payload())
        self.assertEqual(len(result), 4)
        self.assertTrue(all("@sha256:" in value for value in result.values()))
        for mutate in (
            lambda value: value.update(source="https://example.invalid"),
            lambda value: value["images"].pop("ui"),
            lambda value: value["images"]["gateway"].update(reference="ghcr.io/lzq390/zhijupoly-mcp-gateway-base:latest"),
            lambda value: value["images"]["backend"].update(reviewed_image_id="sha256:" + "a" * 64),
            lambda value: value["images"]["ui"].update(reference="ghcr.io/lzq390/zhijupoly-mcp-openscience-ui-base@sha256:" + "0" * 64),
            lambda value: value["images"]["ui"].update(reference="ghcr.io/lzq390/zhijupoly-mcp-openscience-ui-base@sha256:" + "a" * 64 + "\nEVIL=true"),
        ):
            value = self.payload()
            mutate(value)
            with self.assertRaises(ValueError):
                self.check(value)

    def test_required_ci_images_cannot_be_skipped_or_run_against_host_stubs(self):
        text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        failures = []
        policy.validate_confirmed_mcp_images(text, failures)
        self.assertEqual(failures, [])
        for old, new in (
            ("      - mcp-derived-images\n", ""),
            ("capability: [polytao, retrosynthesis]", "capability: [polytao]"),
            ('nexpoly-ci-capability-gateway bun test "${test_paths[@]}"', 'nexpoly-ci-capability-gateway true'),
            ('--build-arg OPENSCIENCE_BASE_IMAGE="$OPENSCIENCE_BASE_IMAGE"', '--build-arg OPENSCIENCE_BASE_IMAGE=latest'),
            ('type=bind,source=$PWD/$test_file,target=$target,readonly', 'type=bind,source=$PWD/$test_file,target=$target'),
            ('RETROSYNTHESIS_MCP_REQUIRE_NATIVE_TEST: "1"', 'RETROSYNTHESIS_MCP_REQUIRE_NATIVE_TEST: "0"'),
            ('python -m venv "$RUNNER_TEMP/retrosynthesis-mcp-venv"', 'echo no isolated runtime'),
        ):
            failures = []
            policy.validate_confirmed_mcp_images(text.replace(old, new), failures)
            self.assertTrue(failures)


if __name__ == "__main__":
    unittest.main()
