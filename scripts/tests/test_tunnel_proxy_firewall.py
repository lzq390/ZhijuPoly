from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "tunnel_proxy_firewall.py"
SPEC = importlib.util.spec_from_file_location("tunnel_proxy_firewall", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
firewall = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = firewall
SPEC.loader.exec_module(firewall)


class Result:
    def __init__(
        self,
        returncode: int = 0,
        stderr: str = "",
        stdout: str = "",
    ) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


class RecordingIptables:
    def __init__(self, *, chain_exists: bool = False, jumps: int = 0) -> None:
        self.has_chain = chain_exists
        self.jumps = jumps
        self.rules: list[tuple[str, ...]] = []
        self.commands: list[tuple[str, ...]] = []

    def run(self, arguments, *, check=True):
        command = tuple(arguments)
        self.commands.append(command)
        if command == ("-S", firewall.CHAIN):
            return Result(0 if self.has_chain else 1)
        if command == ("-C", "INPUT", *firewall._jump_rule()):
            return Result(0 if self.jumps else 1)
        if command == ("-N", firewall.CHAIN):
            self.has_chain = True
        elif command == ("-F", firewall.CHAIN):
            self.rules.clear()
        elif command[:2] == ("-A", firewall.CHAIN):
            self.rules.append(command[2:])
        elif command == ("-I", "INPUT", "1", *firewall._jump_rule()):
            self.jumps += 1
        elif command == ("-D", "INPUT", *firewall._jump_rule()):
            self.jumps -= 1
        elif command == ("-X", firewall.CHAIN):
            self.has_chain = False
        return Result()

    def chain_exists(self):
        return self.run(("-S", firewall.CHAIN), check=False).returncode == 0

    def input_has_jump(self):
        return self.run(("-C", "INPUT", *firewall._jump_rule()), check=False).returncode == 0

    def remove_input_jumps(self):
        while self.input_has_jump():
            self.run(("-D", "INPUT", *firewall._jump_rule()))

    def listed_rules(self, chain):
        if chain == firewall.CHAIN:
            return tuple(self.rules) if self.has_chain else None
        if chain == "INPUT":
            return tuple(firewall._jump_rule() for _ in range(self.jumps))
        return None

    def input_jump_count(self):
        return self.jumps


class TunnelProxyFirewallTests(unittest.TestCase):
    def test_contract_rules_use_stable_address_pairs_not_bridge_ids(self) -> None:
        firewall._validate_contracts()
        rules = [firewall._accept_rule(contract) for contract in firewall.NETWORKS]
        rendered = "\n".join(" ".join(rule) for rule in rules)
        self.assertIn("172.27.0.0/16 -d 172.27.0.1", rendered)
        self.assertIn("172.28.0.0/16 -d 172.28.0.1", rendered)
        self.assertIn("172.30.0.0/16 -d 172.30.0.1", rendered)
        self.assertNotIn(" -i ", f" {rendered} ")
        self.assertNotIn("br-", rendered)

    def test_apply_removes_stale_jumps_and_rebuilds_one_complete_chain(self) -> None:
        runner = RecordingIptables(chain_exists=True, jumps=2)
        firewall.apply_rules(runner)

        self.assertEqual(runner.jumps, 1)
        self.assertIn(("-F", firewall.CHAIN), runner.commands)
        appends = [
            command
            for command in runner.commands
            if command[:2] == ("-A", firewall.CHAIN)
        ]
        self.assertEqual(len(appends), len(firewall.NETWORKS) * 2 + 1)
        self.assertEqual(appends[-1], ("-A", firewall.CHAIN, "-j", "RETURN"))
        self.assertEqual(
            runner.commands[-1],
            ("-I", "INPUT", "1", *firewall._jump_rule()),
        )

    def test_remove_deletes_all_jumps_and_owned_chain(self) -> None:
        runner = RecordingIptables(chain_exists=True, jumps=3)
        firewall.remove_rules(runner)
        self.assertEqual(runner.jumps, 0)
        self.assertFalse(runner.has_chain)
        self.assertEqual(
            runner.commands[-2:],
            [("-F", firewall.CHAIN), ("-X", firewall.CHAIN)],
        )

    def test_status_requires_one_jump_and_the_exact_ordered_chain(self) -> None:
        runner = RecordingIptables()
        firewall.apply_rules(runner)
        self.assertTrue(firewall.rules_are_active(runner))

        runner.rules.insert(0, ("-j", "ACCEPT"))
        self.assertFalse(firewall.rules_are_active(runner))
        runner.rules.pop(0)
        runner.jumps = 2
        self.assertFalse(firewall.rules_are_active(runner))

    def test_real_rule_parser_normalizes_canonical_iptables_output(self) -> None:
        runner = firewall.Iptables(Path("/fixture/iptables"))
        output = "\n".join(
            (
                f"-N {firewall.CHAIN}",
                f'-A {firewall.CHAIN} -s 172.27.0.0/16 '
                '-d 172.27.0.1/32 -p tcp -m tcp --dport 17892 -m comment '
                '--comment "nexpoly tunnel proxy production" -j ACCEPT',
            )
        )
        with mock.patch.object(runner, "run", return_value=Result(stdout=output)):
            listed = runner.listed_rules(firewall.CHAIN)
            self.assertIsNotNone(listed)
            self.assertEqual(
                firewall._normalize_rule(listed[0]),
                firewall._normalize_rule(firewall._accept_rule(firewall.NETWORKS[0])),
            )

    def test_status_accepts_canonical_iptables_rule_rendering(self) -> None:
        canonical_rules = []
        for contract in firewall.NETWORKS:
            canonical_rules.append(
                (
                    "-s",
                    contract.subnet,
                    "-d",
                    f"{contract.gateway}/32",
                    "-p",
                    "tcp",
                    "-m",
                    "tcp",
                    "--dport",
                    str(firewall.PORT),
                    "-m",
                    "comment",
                    "--comment",
                    f"nexpoly tunnel proxy {contract.name}",
                    "-j",
                    "ACCEPT",
                )
            )
        for contract in firewall.NETWORKS:
            canonical_rules.append(
                (
                    "-d",
                    f"{contract.gateway}/32",
                    "-p",
                    "tcp",
                    "-m",
                    "tcp",
                    "--dport",
                    str(firewall.PORT),
                    "-m",
                    "comment",
                    "--comment",
                    f"nexpoly tunnel proxy reject {contract.name}",
                    "-j",
                    "REJECT",
                    "--reject-with",
                    "tcp-reset",
                )
            )
        canonical_rules.append(("-j", "RETURN"))

        runner = mock.Mock()
        runner.chain_exists.return_value = True
        runner.input_jump_count.return_value = 1
        runner.listed_rules.return_value = tuple(canonical_rules)
        self.assertTrue(firewall.rules_are_active(runner))

    def test_input_jump_count_accepts_implicit_tcp_module(self) -> None:
        runner = firewall.Iptables(Path("/fixture/iptables"))
        output = "\n".join(
            (
                "-P INPUT ACCEPT",
                f"-A INPUT -p tcp -m tcp --dport {firewall.PORT} "
                f"-j {firewall.CHAIN}",
            )
        )
        with mock.patch.object(runner, "run", return_value=Result(stdout=output)):
            self.assertEqual(runner.input_jump_count(), 1)

    def test_rule_normalizer_rejects_unknown_matches(self) -> None:
        self.assertIsNone(
            firewall._normalize_rule(("-p", "tcp", "--sport", "1234", "-j", "ACCEPT"))
        )

    def test_cli_refuses_non_root_execution(self) -> None:
        with mock.patch.object(firewall.os, "geteuid", return_value=1001):
            self.assertEqual(firewall.main(["start"]), 1)

    def test_systemd_drop_in_replaces_legacy_start_and_stop(self) -> None:
        drop_in = (
            REPOSITORY_ROOT
            / "ops/systemd/nexpoly-tunnel-proxy-firewall.service.d/10-stable-address-rules.conf"
        ).read_text(encoding="utf-8")
        self.assertIn("ExecStart=\n", drop_in)
        self.assertIn("tunnel-proxy-firewall-stable start", drop_in)
        self.assertIn("ExecStop=\n", drop_in)
        self.assertIn("tunnel-proxy-firewall-stable stop", drop_in)
        self.assertNotIn("ExecReload=", drop_in)

        installer = (
            REPOSITORY_ROOT / "scripts/install_tunnel_proxy_firewall.sh"
        ).read_text(encoding="utf-8")
        install_section = installer.split(
            "# Stop the listener first, then stop the firewall", 1
        )[1]
        self.assertLess(
            install_section.index('systemctl stop "$SOCKET"'),
            install_section.index('systemctl stop "$SERVICE"'),
        )

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose is not available")
    def test_compose_pins_production_and_development_bridge_addresses(self) -> None:
        base_command = [
            "docker",
            "compose",
            "-f",
            str(REPOSITORY_ROOT / "docker-compose.yml"),
        ]
        environment = os.environ.copy()
        environment.update(
            {
                "NEXPOLY_DOCKER_NETWORK_SUBNET": "10.88.0.0/16",
                "NEXPOLY_DOCKER_NETWORK_GATEWAY": "10.88.0.1",
            }
        )
        production = subprocess.run(
            [*base_command, "config", "--format", "json"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
        )
        production_network = json.loads(production.stdout)["networks"]["default"]
        self.assertEqual(
            production_network["ipam"]["config"],
            [{"subnet": "172.27.0.0/16", "gateway": "172.27.0.1"}],
        )

        environment.update(
            {
                "NEXPOLY_ASSET_ROOT": "/tmp/nexpoly-test-assets",
            }
        )
        development = subprocess.run(
            [
                *base_command,
                "-f",
                str(REPOSITORY_ROOT / "docker-compose.dev.yml"),
                "config",
                "--format",
                "json",
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
        )
        development_network = json.loads(development.stdout)["networks"]["default"]
        self.assertEqual(
            development_network["ipam"]["config"],
            [{"subnet": "172.28.0.0/16", "gateway": "172.28.0.1"}],
        )


if __name__ == "__main__":
    unittest.main()
