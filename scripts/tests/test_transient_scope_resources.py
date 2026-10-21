import os
import unittest
from unittest.mock import patch

from gpu_resource.transient_scope import (
    ScopeResourceLimits,
    TransientScopeError,
    scope_resource_limits_from_environment,
    transient_scope_command,
)


class ScopeResourceContractTests(unittest.TestCase):
    lease = "a" * 32

    def test_unconfigured_profile_preserves_exact_default_command(self):
        self.assertEqual(
            transient_scope_command(self.lease, ("/usr/bin/python3", "-I", "-S", "gate.py")),
            transient_scope_command(self.lease, ("/usr/bin/python3", "-I", "-S", "gate.py"), resources=ScopeResourceLimits()),
        )

    def test_complete_properties_precede_the_fenced_target(self):
        limits = ScopeResourceLimits(200, 3 * 1024**3, 0, 256)
        command = transient_scope_command(self.lease, ("/usr/bin/python3", "-I", "-S", "gate.py"), resources=limits)
        end = command.index("--")
        self.assertIn("--unit=nexpoly-gpu-job-" + self.lease + ".scope", command)
        self.assertIn("--slice=nexpoly-gpu-jobs.slice", command)
        for item in limits.command_properties():
            self.assertLess(command.index(item), end)
        self.assertEqual(command[end + 1:], ("/usr/bin/python3", "-I", "-S", "gate.py"))

    def test_incomplete_or_unsafe_values_rejected(self):
        for values in (
            (200, None, 0, 256), (None, 1024, 0, 256),
            (True, 1024, 0, 256), (200.0, 1024, 0, 256),
            (float("nan"), 1024, 0, 256), (0, 1024, 0, 256),
            (200, -1, 0, 256), (200, 1024, -1, 256),
            (200, 1024, 0, 0), (200, 2**63, 0, 256),
            ("200%;Slice=other.slice", 1024, 0, 256),
        ):
            with self.subTest(values=values), self.assertRaises(TransientScopeError):
                ScopeResourceLimits(*values)
        with self.assertRaises(TransientScopeError):
            transient_scope_command(self.lease, ("/bin/true",), resources={"Slice": "other.slice"})

    def test_environment_only_accepts_complete_fixed_numeric_profile(self):
        prefix = "MONOMER_MD_GPU_SCOPE_"
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(scope_resource_limits_from_environment(prefix).configured)
        valid = {
            prefix + "CPU_QUOTA_PERCENT": "200",
            prefix + "MEMORY_MAX_BYTES": str(3 * 1024**3),
            prefix + "MEMORY_SWAP_MAX_BYTES": "0",
            prefix + "TASKS_MAX": "256",
        }
        with patch.dict(os.environ, valid, clear=True):
            self.assertEqual(scope_resource_limits_from_environment(prefix), ScopeResourceLimits(200, 3 * 1024**3, 0, 256))
        for bad in ("", "-1", "1.5", "NaN", " 200", "200\n", "200%;KillMode=none"):
            with patch.dict(os.environ, dict(valid, **{prefix + "CPU_QUOTA_PERCENT": bad}), clear=True), self.assertRaises(TransientScopeError):
                scope_resource_limits_from_environment(prefix)


if __name__ == "__main__":
    unittest.main()
