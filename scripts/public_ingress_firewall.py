#!/usr/bin/env python3
"""Apply a source-IP allowlist before Docker publishes NexPoly web ports.

The managed nftables chain runs in prerouting, before Docker's destination NAT.
That is intentional: filtering only the host INPUT chain (including ordinary
UFW rules) does not reliably cover Docker-published ports.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any, Sequence, cast


NFT = Path("/usr/sbin/nft")
DEFAULT_CONFIG = Path("/etc/nexpoly/public-ingress-allowlist.conf")
TABLE_FAMILY = "inet"
TABLE_NAME = "nexpoly_public_ingress"
CHAIN_NAME = "prerouting"
PROTECTED_PORTS = (81, 9000, 9001, 9011, 10000, 10001)
MAX_NETWORKS_PER_FAMILY = 256
CONFIG_KEYS = {
    "NEXPOLY_INGRESS_ALLOW_IPV4",
    "NEXPOLY_INGRESS_ALLOW_IPV6",
}


class FirewallError(RuntimeError):
    pass


@dataclass(frozen=True)
class Policy:
    allowed_ipv4: tuple[ipaddress.IPv4Network, ...]
    allowed_ipv6: tuple[ipaddress.IPv6Network, ...]


def _comma_separated(value: str) -> tuple[str, ...]:
    if not value.strip():
        return ()
    parts = tuple(part.strip() for part in value.split(","))
    if any(not part for part in parts):
        raise FirewallError("allowlist values must not contain empty items")
    return parts


def _read_assignments(path: Path) -> dict[str, str]:
    if path.is_symlink():
        raise FirewallError(f"configuration must not be a symlink: {path}")
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise FirewallError(f"configuration is missing: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise FirewallError(f"configuration is not a regular file: {path}")

    assignments: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise FirewallError(f"invalid configuration line {line_number}")
        key, value = (part.strip() for part in line.split("=", 1))
        if key not in CONFIG_KEYS:
            raise FirewallError(
                f"unsupported configuration key on line {line_number}: {key}"
            )
        if key in assignments:
            raise FirewallError(f"duplicate configuration key: {key}")
        assignments[key] = value

    missing = CONFIG_KEYS - assignments.keys()
    if missing:
        raise FirewallError(
            "configuration is missing required keys: " + ", ".join(sorted(missing))
        )
    return assignments


def _parse_networks(
    values: Sequence[str], *, version: int
) -> tuple[ipaddress.IPv4Network, ...] | tuple[ipaddress.IPv6Network, ...]:
    parsed: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for value in values:
        if "/" not in value:
            raise FirewallError(f"invalid IPv{version} CIDR: {value}")
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise FirewallError(f"invalid IPv{version} CIDR: {value}") from exc
        if network.version != version:
            raise FirewallError(f"CIDR is not IPv{version}: {value}")
        parsed.append(network)

    collapsed = tuple(ipaddress.collapse_addresses(parsed))
    if len(collapsed) > MAX_NETWORKS_PER_FAMILY:
        raise FirewallError(
            f"IPv{version} allowlist exceeds {MAX_NETWORKS_PER_FAMILY} networks"
        )
    if any(network.prefixlen == 0 for network in collapsed):
        raise FirewallError(
            f"IPv{version} /0 is forbidden because it disables source restriction"
        )
    return collapsed


def load_policy(path: Path) -> Policy:
    assignments = _read_assignments(path)
    allowed_ipv4 = _parse_networks(
        _comma_separated(assignments["NEXPOLY_INGRESS_ALLOW_IPV4"]), version=4
    )
    allowed_ipv6 = _parse_networks(
        _comma_separated(assignments["NEXPOLY_INGRESS_ALLOW_IPV6"]), version=6
    )
    if not allowed_ipv4 and not allowed_ipv6:
        raise FirewallError(
            "the allowlist is empty; specify at least one trusted CIDR explicitly"
        )
    return Policy(
        allowed_ipv4=cast(tuple[ipaddress.IPv4Network, ...], allowed_ipv4),
        allowed_ipv6=cast(tuple[ipaddress.IPv6Network, ...], allowed_ipv6),
    )


def _policy_document(policy: Policy) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "protected_ports": list(PROTECTED_PORTS),
        "allowed_ipv4": [str(network) for network in policy.allowed_ipv4],
        "allowed_ipv6": [str(network) for network in policy.allowed_ipv6],
    }


def policy_digest(policy: Policy) -> str:
    payload = json.dumps(
        _policy_document(policy),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _set_literal(values: Sequence[str | int]) -> str:
    # Callers pass only already-parsed numeric ports or canonical IP networks.
    return "{ " + ", ".join(str(value) for value in values) + " }"


def render_ruleset(policy: Policy) -> str:
    digest = policy_digest(policy)
    ports = _set_literal(PROTECTED_PORTS)
    match = (
        'meta iifname != "lo" fib daddr type local '
        f"tcp dport {ports}"
    )
    lines = [
        f"destroy table {TABLE_FAMILY} {TABLE_NAME}",
        (
            f"add table {TABLE_FAMILY} {TABLE_NAME} "
            f'{{ comment "NexPoly managed policy {digest}"; }}'
        ),
    ]

    if policy.allowed_ipv4:
        lines.extend(
            (
                f"add set {TABLE_FAMILY} {TABLE_NAME} allowed_ipv4 "
                "{ type ipv4_addr; flags interval; }",
                f"add element {TABLE_FAMILY} {TABLE_NAME} allowed_ipv4 "
                + _set_literal(tuple(str(network) for network in policy.allowed_ipv4)),
            )
        )
    if policy.allowed_ipv6:
        lines.extend(
            (
                f"add set {TABLE_FAMILY} {TABLE_NAME} allowed_ipv6 "
                "{ type ipv6_addr; flags interval; }",
                f"add element {TABLE_FAMILY} {TABLE_NAME} allowed_ipv6 "
                + _set_literal(tuple(str(network) for network in policy.allowed_ipv6)),
            )
        )

    lines.append(
        f"add chain {TABLE_FAMILY} {TABLE_NAME} {CHAIN_NAME} "
        "{ type filter hook prerouting priority -310; policy accept; }"
    )
    if policy.allowed_ipv4:
        lines.append(
            f"add rule {TABLE_FAMILY} {TABLE_NAME} {CHAIN_NAME} {match} "
            "meta nfproto ipv4 ip saddr @allowed_ipv4 counter accept "
            f'comment "nexpoly allow IPv4 {digest}"'
        )
    if policy.allowed_ipv6:
        lines.append(
            f"add rule {TABLE_FAMILY} {TABLE_NAME} {CHAIN_NAME} {match} "
            "meta nfproto ipv6 ip6 saddr @allowed_ipv6 counter accept "
            f'comment "nexpoly allow IPv6 {digest}"'
        )
    lines.append(
        f"add rule {TABLE_FAMILY} {TABLE_NAME} {CHAIN_NAME} {match} "
        f'counter drop comment "nexpoly deny untrusted ingress {digest}"'
    )
    return "\n".join(lines) + "\n"


def _require_root_owned_private_config(path: Path) -> None:
    metadata = path.stat()
    if metadata.st_uid != 0:
        raise FirewallError("installed firewall configuration must be owned by root")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise FirewallError(
            "installed firewall configuration must have mode 0600 or stricter"
        )


def _require_runtime(path: Path) -> None:
    if os.geteuid() != 0:
        raise FirewallError("firewall changes and status checks require root")
    if not NFT.is_file() or not os.access(NFT, os.X_OK):
        raise FirewallError(f"nft is unavailable at {NFT}")
    _require_root_owned_private_config(path)


def _run_nft(
    arguments: Sequence[str], *, payload: str | None = None
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        (str(NFT), *arguments),
        input=payload,
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or "nft command failed"
        )
        raise FirewallError(detail)
    return completed


def apply_policy(policy: Policy) -> None:
    ruleset = render_ruleset(policy)
    _run_nft(("--check", "--file", "-"), payload=ruleset)
    # A single nft batch is committed atomically: the previous working policy
    # remains active if validation or any netlink operation fails.
    _run_nft(("--file", "-"), payload=ruleset)


def _nft_set_networks(
    item: dict[str, Any], *, version: int
) -> tuple[ipaddress.IPv4Network, ...] | tuple[ipaddress.IPv6Network, ...] | None:
    expected_type = "ipv4_addr" if version == 4 else "ipv6_addr"
    if (
        item.get("family") != TABLE_FAMILY
        or item.get("table") != TABLE_NAME
        or item.get("type") != expected_type
        or item.get("flags") != ["interval"]
        or not isinstance(item.get("elem"), list)
    ):
        return None

    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for element in item["elem"]:
        if isinstance(element, str):
            try:
                address = ipaddress.ip_address(element)
            except ValueError:
                return None
            prefix = address.max_prefixlen
            raw_network = f"{address}/{prefix}"
        elif isinstance(element, dict) and isinstance(element.get("prefix"), dict):
            address = element["prefix"].get("addr")
            prefix = element["prefix"].get("len")
            if not isinstance(address, str) or not isinstance(prefix, int):
                return None
            raw_network = f"{address}/{prefix}"
        else:
            return None
        try:
            network = ipaddress.ip_network(raw_network, strict=True)
        except ValueError:
            return None
        if network.version != version:
            return None
        networks.append(network)
    return tuple(ipaddress.collapse_addresses(networks))


def _common_rule_expression() -> list[dict[str, Any]]:
    return [
        {
            "match": {
                "op": "!=",
                "left": {"meta": {"key": "iifname"}},
                "right": "lo",
            }
        },
        {
            "match": {
                "op": "==",
                "left": {"fib": {"result": "type", "flags": ["daddr"]}},
                "right": "local",
            }
        },
        {
            "match": {
                "op": "==",
                "left": {"payload": {"protocol": "tcp", "field": "dport"}},
                "right": {"set": list(PROTECTED_PORTS)},
            }
        },
    ]


def _normalized_rule_expression(rule: dict[str, Any]) -> list[dict[str, Any]] | None:
    expression = rule.get("expr")
    if not isinstance(expression, list):
        return None
    normalized: list[dict[str, Any]] = []
    for statement in expression:
        if not isinstance(statement, dict):
            return None
        normalized.append({"counter": {}} if "counter" in statement else statement)
    return normalized


def _expected_rule_expressions(policy: Policy) -> dict[str, list[dict[str, Any]]]:
    digest = policy_digest(policy)
    common = _common_rule_expression()
    expected: dict[str, list[dict[str, Any]]] = {}
    if policy.allowed_ipv4:
        expected[f"nexpoly allow IPv4 {digest}"] = [
            *common,
            {
                "match": {
                    "op": "==",
                    "left": {"payload": {"protocol": "ip", "field": "saddr"}},
                    "right": "@allowed_ipv4",
                }
            },
            {"counter": {}},
            {"accept": None},
        ]
    if policy.allowed_ipv6:
        expected[f"nexpoly allow IPv6 {digest}"] = [
            *common,
            {
                "match": {
                    "op": "==",
                    "left": {"payload": {"protocol": "ip6", "field": "saddr"}},
                    "right": "@allowed_ipv6",
                }
            },
            {"counter": {}},
            {"accept": None},
        ]
    expected[f"nexpoly deny untrusted ingress {digest}"] = [
        *common,
        {"counter": {}},
        {"drop": None},
    ]
    return expected


def policy_is_active(policy: Policy) -> bool:
    completed = _run_nft(
        ("--json", "list", "table", TABLE_FAMILY, TABLE_NAME)
    )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise FirewallError("nft returned invalid JSON") from exc
    entries = document.get("nftables")
    if not isinstance(entries, list):
        return False

    digest = policy_digest(policy)
    expected_table_comment = f"NexPoly managed policy {digest}"
    expected_rules = _expected_rule_expressions(policy)

    tables = [
        entry["table"]
        for entry in entries
        if isinstance(entry, dict) and "table" in entry
    ]
    chains = [
        entry["chain"]
        for entry in entries
        if isinstance(entry, dict) and "chain" in entry
    ]
    sets = [
        entry["set"]
        for entry in entries
        if isinstance(entry, dict) and "set" in entry
    ]
    rules = [
        entry["rule"]
        for entry in entries
        if isinstance(entry, dict) and "rule" in entry
    ]
    if (
        len(tables) != 1
        or tables[0].get("family") != TABLE_FAMILY
        or tables[0].get("name") != TABLE_NAME
        or tables[0].get("comment") != expected_table_comment
    ):
        return False
    if len(chains) != 1:
        return False
    chain = chains[0]
    if (
        chain.get("family") != TABLE_FAMILY
        or chain.get("table") != TABLE_NAME
        or chain.get("name") != CHAIN_NAME
        or chain.get("hook") != "prerouting"
        or chain.get("type") != "filter"
        or chain.get("policy") != "accept"
        or int(chain.get("prio", 0)) != -310
    ):
        return False
    actual_sets = {item.get("name"): item for item in sets}
    expected_set_names = {
        name
        for name, enabled in (
            ("allowed_ipv4", bool(policy.allowed_ipv4)),
            ("allowed_ipv6", bool(policy.allowed_ipv6)),
        )
        if enabled
    }
    if set(actual_sets) != expected_set_names:
        return False
    if policy.allowed_ipv4 and _nft_set_networks(
        actual_sets["allowed_ipv4"], version=4
    ) != policy.allowed_ipv4:
        return False
    if policy.allowed_ipv6 and _nft_set_networks(
        actual_sets["allowed_ipv6"], version=6
    ) != policy.allowed_ipv6:
        return False

    actual_rules: list[tuple[str, list[dict[str, Any]] | None]] = []
    for rule in rules:
        if (
            rule.get("family") != TABLE_FAMILY
            or rule.get("table") != TABLE_NAME
            or rule.get("chain") != CHAIN_NAME
            or not isinstance(rule.get("comment"), str)
        ):
            return False
        actual_rules.append((rule["comment"], _normalized_rule_expression(rule)))
    # Chain order matters: an early deny rule also blocks trusted sources.
    return actual_rules == list(expected_rules.items())


def remove_policy() -> None:
    _run_nft(("destroy", "table", TABLE_FAMILY, TABLE_NAME))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("validate", "render", "apply", "status", "remove")
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--confirm-remove",
        action="store_true",
        help="required with remove because removal reopens the protected ports",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        policy = load_policy(arguments.config)
        if arguments.action == "validate":
            print(json.dumps(_policy_document(policy), sort_keys=True))
            return 0
        if arguments.action == "render":
            print(render_ruleset(policy), end="")
            return 0

        _require_runtime(arguments.config)
        if arguments.action == "apply":
            apply_policy(policy)
        elif arguments.action == "status":
            if not policy_is_active(policy):
                raise FirewallError("installed public-ingress firewall policy has drifted")
        elif arguments.action == "remove":
            if not arguments.confirm_remove:
                raise FirewallError("remove requires --confirm-remove")
            remove_policy()
    except (FirewallError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
