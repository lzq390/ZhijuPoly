from __future__ import annotations

import importlib.util
from itertools import permutations
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts/public_ingress_firewall.py"
SPEC = importlib.util.spec_from_file_location("public_ingress_firewall", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
firewall = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = firewall
SPEC.loader.exec_module(firewall)


class PublicIngressFirewallTests(unittest.TestCase):
    def write_config(
        self,
        directory: Path,
        *,
        ipv4: str = "192.0.2.10/32,198.51.100.0/24",
        ipv6: str = "2001:db8::10/128",
        extra: str = "",
    ) -> Path:
        path = directory / "allowlist.conf"
        path.write_text(
            "\n".join(
                (
                    f"NEXPOLY_INGRESS_ALLOW_IPV4={ipv4}",
                    f"NEXPOLY_INGRESS_ALLOW_IPV6={ipv6}",
                    extra,
                )
            ),
            encoding="utf-8",
        )
        return path

    def test_valid_policy_is_canonical_and_covers_all_protected_ports(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            config = self.write_config(Path(raw_directory))
            policy = firewall.load_policy(config)

        self.assertEqual(
            tuple(str(item) for item in policy.allowed_ipv4),
            ("192.0.2.10/32", "198.51.100.0/24"),
        )
        self.assertEqual(
            tuple(str(item) for item in policy.allowed_ipv6),
            ("2001:db8::10/128",),
        )
        self.assertEqual(
            firewall.PROTECTED_PORTS,
            (81, 9000, 9001, 9011, 10000, 10001),
        )

    def test_adjacent_networks_cannot_combine_into_allow_all(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            config = self.write_config(
                Path(raw_directory), ipv4="0.0.0.0/1,128.0.0.0/1", ipv6=""
            )
            with self.assertRaisesRegex(firewall.FirewallError, "/0 is forbidden"):
                firewall.load_policy(config)

    def test_empty_allowlist_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            config = self.write_config(Path(raw_directory), ipv4="", ipv6="")
            with self.assertRaisesRegex(firewall.FirewallError, "allowlist is empty"):
                firewall.load_policy(config)

    def test_unknown_duplicate_and_non_cidr_values_are_rejected(self) -> None:
        fixtures = (
            ("UNSUPPORTED=value", "unsupported configuration key"),
            (
                "NEXPOLY_INGRESS_ALLOW_IPV4=203.0.113.5/32",
                "duplicate configuration key",
            ),
        )
        for extra, expected in fixtures:
            with (
                self.subTest(extra=extra),
                tempfile.TemporaryDirectory() as raw_directory,
            ):
                config = self.write_config(Path(raw_directory), extra=extra)
                with self.assertRaisesRegex(firewall.FirewallError, expected):
                    firewall.load_policy(config)

        with tempfile.TemporaryDirectory() as raw_directory:
            config = self.write_config(
                Path(raw_directory), ipv4="203.0.113.9", ipv6=""
            )
            with self.assertRaisesRegex(firewall.FirewallError, "invalid IPv4 CIDR"):
                firewall.load_policy(config)

    def test_rendered_rules_run_before_docker_dnat_and_default_to_drop(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            policy = firewall.load_policy(self.write_config(Path(raw_directory)))
        rendered = firewall.render_ruleset(policy)

        self.assertIn("hook prerouting priority -310", rendered)
        self.assertIn('meta iifname != "lo" fib daddr type local', rendered)
        self.assertIn(
            "tcp dport { 81, 9000, 9001, 9011, 10000, 10001 }",
            rendered,
        )
        self.assertIn("ip saddr @allowed_ipv4", rendered)
        self.assertIn("ip6 saddr @allowed_ipv6", rendered)
        self.assertIn("counter drop", rendered)
        self.assertNotIn("hook input", rendered)
        self.assertNotIn("DOCKER-USER", rendered)
        self.assertEqual(rendered.count("destroy table inet nexpoly_public_ingress"), 1)

    def test_apply_checks_then_commits_one_atomic_batch(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            policy = firewall.load_policy(self.write_config(Path(raw_directory)))
        calls: list[tuple[tuple[str, ...], str | None]] = []

        def run_nft(
            arguments: tuple[str, ...], *, payload: str | None = None
        ) -> subprocess.CompletedProcess[str]:
            calls.append((arguments, payload))
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with mock.patch.object(firewall, "_run_nft", side_effect=run_nft):
            firewall.apply_policy(policy)

        self.assertEqual(
            [item[0] for item in calls],
            [("--check", "--file", "-"), ("--file", "-")],
        )
        self.assertEqual(calls[0][1], calls[1][1])

    def test_status_requires_exact_managed_policy_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            policy = firewall.load_policy(self.write_config(Path(raw_directory)))
        digest = firewall.policy_digest(policy)
        expected_expressions = firewall._expected_rule_expressions(policy)

        def active_rule(comment: str) -> dict[str, object]:
            expression = [
                {"counter": {"packets": 0, "bytes": 0}}
                if "counter" in statement
                else statement
                for statement in expected_expressions[comment]
            ]
            return {
                "family": "inet",
                "table": "nexpoly_public_ingress",
                "chain": "prerouting",
                "comment": comment,
                "expr": expression,
            }

        nft_document = {
            "nftables": [
                {
                    "table": {
                        "family": "inet",
                        "name": "nexpoly_public_ingress",
                        "comment": f"NexPoly managed policy {digest}",
                    }
                },
                {
                    "set": {
                        "family": "inet",
                        "table": "nexpoly_public_ingress",
                        "name": "allowed_ipv4",
                        "type": "ipv4_addr",
                        "flags": ["interval"],
                        "elem": [
                            "192.0.2.10",
                            {"prefix": {"addr": "198.51.100.0", "len": 24}},
                        ],
                    }
                },
                {
                    "set": {
                        "family": "inet",
                        "table": "nexpoly_public_ingress",
                        "name": "allowed_ipv6",
                        "type": "ipv6_addr",
                        "flags": ["interval"],
                        "elem": ["2001:db8::10"],
                    }
                },
                {
                    "chain": {
                        "family": "inet",
                        "table": "nexpoly_public_ingress",
                        "name": "prerouting",
                        "type": "filter",
                        "hook": "prerouting",
                        "prio": -310,
                        "policy": "accept",
                    }
                },
                {"rule": active_rule(f"nexpoly allow IPv4 {digest}")},
                {"rule": active_rule(f"nexpoly allow IPv6 {digest}")},
                {"rule": active_rule(f"nexpoly deny untrusted ingress {digest}")},
            ]
        }
        completed = subprocess.CompletedProcess(
            (), 0, json.dumps(nft_document), ""
        )
        with mock.patch.object(firewall, "_run_nft", return_value=completed):
            self.assertTrue(firewall.policy_is_active(policy))
            entries = nft_document["nftables"]
            rules = entries[-3:]
            for order in permutations(range(len(rules))):
                if order == (0, 1, 2):
                    continue
                with self.subTest(rule_order=order):
                    completed.stdout = json.dumps(
                        {
                            "nftables": [
                                *entries[:-3],
                                *(rules[index] for index in order),
                            ]
                        }
                    )
                    self.assertFalse(firewall.policy_is_active(policy))
            nft_document["nftables"][-1]["rule"]["comment"] = "drifted"
            completed.stdout = json.dumps(nft_document)
            self.assertFalse(firewall.policy_is_active(policy))

    def test_status_rejects_allowlist_element_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            policy = firewall.load_policy(
                self.write_config(Path(raw_directory), ipv6="")
            )
        item = {
            "family": "inet",
            "table": "nexpoly_public_ingress",
            "name": "allowed_ipv4",
            "type": "ipv4_addr",
            "flags": ["interval"],
            "elem": ["203.0.113.99"],
        }
        self.assertNotEqual(
            firewall._nft_set_networks(item, version=4), policy.allowed_ipv4
        )


if __name__ == "__main__":
    unittest.main()
