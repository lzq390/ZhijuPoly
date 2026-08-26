#!/usr/bin/env python3
"""Manage stable host firewall rules for the bridge-only AI tunnel proxy.

The rules deliberately match reviewed source subnet and destination gateway
pairs. They do not match Docker's generated ``br-<network-id>`` interface
names, so a Compose network recreation does not invalidate the firewall.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Sequence


IPTABLES = Path("/usr/sbin/iptables")
CHAIN = "NEXPOLY_TUNNEL_PROXY"
PORT = 17892
WAIT_SECONDS = 5


@dataclass(frozen=True)
class NetworkContract:
    name: str
    subnet: str
    gateway: str


NETWORKS = (
    NetworkContract("production", "172.27.0.0/16", "172.27.0.1"),
    NetworkContract("development", "172.28.0.0/16", "172.28.0.1"),
    NetworkContract("openscience", "172.30.0.0/16", "172.30.0.1"),
)


def _validate_contracts() -> None:
    seen_subnets: set[ipaddress.IPv4Network] = set()
    seen_gateways: set[ipaddress.IPv4Address] = set()
    for contract in NETWORKS:
        subnet = ipaddress.ip_network(contract.subnet, strict=True)
        gateway = ipaddress.ip_address(contract.gateway)
        if not isinstance(subnet, ipaddress.IPv4Network) or not isinstance(
            gateway, ipaddress.IPv4Address
        ):
            raise RuntimeError("tunnel proxy firewall supports IPv4 contracts only")
        if gateway not in subnet:
            raise RuntimeError(f"{contract.name} gateway is outside its subnet")
        if subnet in seen_subnets or gateway in seen_gateways:
            raise RuntimeError("tunnel proxy firewall contracts must be unique")
        seen_subnets.add(subnet)
        seen_gateways.add(gateway)


def _jump_rule() -> tuple[str, ...]:
    return ("-p", "tcp", "--dport", str(PORT), "-j", CHAIN)


def _accept_rule(contract: NetworkContract) -> tuple[str, ...]:
    return (
        "-p",
        "tcp",
        "-s",
        contract.subnet,
        "-d",
        contract.gateway,
        "--dport",
        str(PORT),
        "-m",
        "comment",
        "--comment",
        f"nexpoly tunnel proxy {contract.name}",
        "-j",
        "ACCEPT",
    )


def _reject_rule(contract: NetworkContract) -> tuple[str, ...]:
    return (
        "-p",
        "tcp",
        "-d",
        contract.gateway,
        "--dport",
        str(PORT),
        "-m",
        "comment",
        "--comment",
        f"nexpoly tunnel proxy reject {contract.name}",
        "-j",
        "REJECT",
        "--reject-with",
        "tcp-reset",
    )


class Iptables:
    def __init__(self, binary: Path = IPTABLES) -> None:
        self.binary = binary

    def run(
        self,
        arguments: Sequence[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            (str(self.binary), "-w", str(WAIT_SECONDS), *arguments),
            check=False,
            text=True,
            capture_output=True,
        )
        if check and completed.returncode != 0:
            detail = completed.stderr.strip() or "iptables command failed"
            raise RuntimeError(detail)
        return completed

    def chain_exists(self) -> bool:
        return self.run(("-S", CHAIN), check=False).returncode == 0

    def input_has_jump(self) -> bool:
        return self.run(("-C", "INPUT", *_jump_rule()), check=False).returncode == 0

    def remove_input_jumps(self) -> None:
        while self.input_has_jump():
            self.run(("-D", "INPUT", *_jump_rule()))

    def listed_rules(self, chain: str) -> tuple[tuple[str, ...], ...] | None:
        completed = self.run(("-S", chain), check=False)
        if completed.returncode != 0:
            return None
        declaration_seen = False
        rules: list[tuple[str, ...]] = []
        for raw_line in completed.stdout.splitlines():
            try:
                parts = tuple(shlex.split(raw_line))
            except ValueError:
                return None
            if parts in {("-N", chain), ("-P", chain, "ACCEPT"), ("-P", chain, "DROP")}:
                declaration_seen = True
            elif len(parts) >= 3 and parts[:2] == ("-A", chain):
                rules.append(parts[2:])
            else:
                return None
        return tuple(rules) if declaration_seen else None

    def input_jump_count(self) -> int:
        rules = self.listed_rules("INPUT")
        if rules is None:
            return -1
        return sum(rule == _jump_rule() for rule in rules)


def apply_rules(iptables: Iptables) -> None:
    _validate_contracts()
    if not iptables.chain_exists():
        iptables.run(("-N", CHAIN))

    # The systemd dependency and installer keep the listener stopped while this
    # chain is rebuilt. Detach every stale jump before replacing its contents.
    iptables.remove_input_jumps()
    iptables.run(("-F", CHAIN))
    for contract in NETWORKS:
        iptables.run(("-A", CHAIN, *_accept_rule(contract)))
    for contract in NETWORKS:
        iptables.run(("-A", CHAIN, *_reject_rule(contract)))
    iptables.run(("-A", CHAIN, "-j", "RETURN"))
    iptables.run(("-I", "INPUT", "1", *_jump_rule()))


def remove_rules(iptables: Iptables) -> None:
    iptables.remove_input_jumps()
    if iptables.chain_exists():
        iptables.run(("-F", CHAIN))
        iptables.run(("-X", CHAIN))


def rules_are_active(iptables: Iptables) -> bool:
    if not iptables.chain_exists() or iptables.input_jump_count() != 1:
        return False
    expected = (
        *[_accept_rule(contract) for contract in NETWORKS],
        *[_reject_rule(contract) for contract in NETWORKS],
        ("-j", "RETURN"),
    )
    return iptables.listed_rules(CHAIN) == expected


def _require_root() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("tunnel proxy firewall reconciliation requires root")
    if not IPTABLES.is_file() or not os.access(IPTABLES, os.X_OK):
        raise RuntimeError(f"iptables is unavailable at {IPTABLES}")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    if len(arguments) != 1 or arguments[0] not in {"start", "stop", "status"}:
        print(f"Usage: {Path(sys.argv[0]).name} {{start|stop|status}}", file=sys.stderr)
        return 2
    try:
        _require_root()
        iptables = Iptables()
        if arguments[0] == "start":
            apply_rules(iptables)
        elif arguments[0] == "stop":
            remove_rules(iptables)
        elif not rules_are_active(iptables):
            print("tunnel proxy firewall rules are incomplete", file=sys.stderr)
            return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
