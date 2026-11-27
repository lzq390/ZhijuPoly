"""Reconcile one deploy-m6 alert pair against its two persisted receiver receipts."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

OPS = Path(__file__).resolve().parents[2] / "ops/openscience-per-user-dev"
sys.path.insert(0, str(OPS))
from oncall_receiver import SOCKET, lookup  # noqa: E402


def read(path):
    return json.loads(path.read_bytes())


def reconcile(registry, first_sequence):
    db = read(registry / "registry.json")
    root = registry / "host-watermark"
    relay = read(root / "receiver.json")
    deployment, filesystem = db["deployment"], db["disk_watermark"]["filesystem_uuid"]
    if (
        first_sequence < 1
        or first_sequence < relay["first_sequence"]
        or relay["socket"] != SOCKET
        or {o["environment"] for o in db["candidate_owners"].values()} != {"deploy-m6"}
        or deployment != relay["deployment"]
        or filesystem != relay["filesystem_uuid"]
    ):
        raise ValueError("Pair does not belong to the enrolled deploy-m6 subscription")

    matched = []
    for seq, kind, previous, state in (
        (first_sequence, "firing", "normal", "low"),
        (first_sequence + 1, "resolved", "low", "normal"),
    ):
        expected = f"{deployment}:{filesystem}:{seq}"
        emitted = read(root / "alerts" / f"{seq:012d}.json")
        fields = dict(
            id=expected,
            deployment=deployment,
            filesystem_uuid=filesystem,
            environment="deploy-m6",
            sequence=seq,
            notification=kind,
            previous=previous,
            state=state,
        )
        if any(emitted.get(k) != v for k, v in fields.items()):
            raise ValueError(f"Spool transition does not match {expected}")

        receipt = lookup(expected)
        acks = [r for r in relay["receipts"] if r["id"] == expected]
        if (
            receipt["event"] != emitted
            or not receipt["receipt_id"]
            or len(acks) != 1
            or acks[0]["sequence"] != seq
            or acks[0]["notification"] != kind
        ):
            raise ValueError(
                f"Receiver record, spooled payload and relay acknowledgement must match for {expected}"
            )
        event_time, received, acknowledged = (
            datetime.fromisoformat(value)
            for value in (emitted["at"], receipt["received_at"], acks[0]["at"])
        )
        if (
            any(t.utcoffset() is None for t in (event_time, received, acknowledged))
            or not event_time <= received <= acknowledged
        ):
            raise ValueError(f"Invalid timestamps for {expected}")
        matched.append(
            {
                "event_id": expected,
                "notification": kind,
                "receiver_record": receipt,
                "relay_acknowledgement": acks[0],
                "emit_to_receive_ms": round(
                    (received - event_time).total_seconds() * 1000, 3
                ),
            }
        )

    if (
        matched[0]["receiver_record"]["receipt_id"]
        == matched[1]["receiver_record"]["receipt_id"]
    ):
        raise ValueError("Receiver returned one receipt for two distinct transitions")
    return {
        "environment": "deploy-m6",
        "deployment": deployment,
        "filesystem_uuid": filesystem,
        "receiver_socket": relay["socket"],
        "queried_at": datetime.now(timezone.utc).isoformat(),
        "matched": matched,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        type=Path,
        required=True,
        help="deploy-m6 workspace registry directory",
    )
    parser.add_argument(
        "--first-sequence",
        type=int,
        required=True,
        help="Sequence of the firing event in this run",
    )
    args = parser.parse_args()
    print(json.dumps(reconcile(args.registry, args.first_sequence), indent=2))
