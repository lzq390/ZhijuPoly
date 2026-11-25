"""Ordered delivery of immutable watermark events to the configured local receiver."""

from datetime import datetime, timezone
import fcntl
import http.client
import json
import os
from pathlib import Path
import socket
import sys
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from openscience_per_user_dev import write_atomic  # noqa: E402


def read(path):
    return json.loads(path.read_bytes())


def save(root, record):
    write_atomic(
        root / "receiver.json",
        json.dumps(record, sort_keys=True).encode() + b"\n",
        0o600,
    )


def event_id(filesystem_uuid, sequence, deployment=None):
    prefix = f"{deployment}:" if deployment is not None else ""
    return f"{prefix}{filesystem_uuid}:{sequence}"


def register(root, path, filesystem_uuid, *, deployment=None):
    """The monitor lock protects first registration. Re-running configure preserves the last receipt."""
    if (
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or ".." in Path(path).parts
        or "\0" in path
        or len(os.fsencode(path)) >= 108
    ):
        raise ValueError("Expected an absolute receiver Unix socket path")
    config = root / "receiver.json"
    if config.exists():
        record = read(config)
        if (record["socket"], record["filesystem_uuid"], record.get("deployment")) != (
            path,
            filesystem_uuid,
            deployment,
        ):
            raise ValueError(
                "Existing receiver belongs to another socket, pool or deployment"
            )
        next_sequence(record)
        return
    state_file = root / "state.json"
    current = read(state_file) if state_file.exists() else {}
    if current and (current["filesystem_uuid"], current.get("deployment")) != (
        filesystem_uuid,
        deployment,
    ):
        raise ValueError("Receiver pool or deployment differs from the monitored pool")
    # Include an outstanding incident if the operator subscribes after it was raised.
    first = current.get("sequence", 0) + (
        0 if current.get("state") in ("low", "unavailable") else 1
    )
    record = {
        "socket": path,
        "filesystem_uuid": filesystem_uuid,
        "first_sequence": first,
        "last_receipt": None,
        "receipts": [],
    }
    if deployment is not None:
        record["deployment"] = deployment
    save(root, record)


def next_sequence(record, latest=None):
    seq = record["first_sequence"]
    if type(seq) is not int or seq < 1:
        raise ValueError("Invalid receiver subscription sequence")
    receipt = record["last_receipt"]
    if receipt is not None:
        ack = receipt["sequence"]
        if (
            type(ack) is not int
            or ack < seq
            or receipt["id"]
            != event_id(record["filesystem_uuid"], ack, record.get("deployment"))
        ):
            raise ValueError("Invalid watermark receiver receipt")
        seq = ack + 1
    if latest is not None and seq > latest + 1:
        raise ValueError("Watermark feed predates the receiver checkpoint")
    return seq


def retain(root, sequence):
    config = root / "receiver.json"
    try:
        return sequence >= next_sequence(read(config))
    except FileNotFoundError:
        return False
    except (OSError, ValueError, KeyError, TypeError):
        # Preserve the backlog for operator recovery without blocking disk safety.
        return True


def post(path, event):
    # No DNS, proxy, redirects or TCP. The operator supplies a local receiver.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(2)
        peer.connect(path)
        client = http.client.HTTPConnection("localhost")
        client.sock = peer
        try:
            client.request(
                "POST",
                "/watermark",
                json.dumps(event).encode(),
                {
                    "Content-Type": "application/json",
                    "Idempotency-Key": event["id"],
                },
            )
            with client.getresponse() as response:
                if not 200 <= response.status < 300:
                    raise ValueError(f"Receiver returned HTTP {response.status}")
                body = response.read(4097)
            if len(body) > 4096 or json.loads(body) != {"id": event["id"]}:
                raise ValueError("Receiver did not acknowledge the expected event")
        finally:
            client.close()


def send_next(root, history):
    # Networking holds only the relay lock. Watermark observations/admission use monitor.lock.
    with (root / "relay.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = read(root / "receiver.json")
        state = read(root / "state.json")
        fs = record["filesystem_uuid"]
        deployment = record.get("deployment")
        if (state["filesystem_uuid"], state.get("deployment")) != (fs, deployment):
            raise ValueError(
                "Receiver pool or deployment differs from the recorded observation"
            )
        seq = next_sequence(record, state["sequence"])
        if record["last_receipt"] and seq - 1 <= state["sequence"] - history:
            # Also finish a prune interrupted immediately after committing its receipt.
            (root / "alerts" / f"{seq - 1:012d}.json").unlink(missing_ok=True)
        if seq > state["sequence"]:
            return False
        event = read(root / "alerts" / f"{seq:012d}.json")
        if (
            event["id"] != event_id(fs, seq, deployment)
            or event["filesystem_uuid"] != fs
            or event["sequence"] != seq
            or event.get("deployment") != deployment
        ):
            raise ValueError("Watermark feed contains a different event")
        post(record["socket"], event)
        # Older configurations only recorded their last acknowledgement. Preserve
        # it, without fabricating receipts for earlier events. Commit history and
        # cursor together so a retry after interruption uses the same event ID.
        previous = record["last_receipt"]
        receipts = record.get("receipts", [previous] if previous else [])
        receipt = {
            "id": event["id"],
            "sequence": seq,
            "notification": event["notification"],
            "at": datetime.now(timezone.utc).isoformat(),
        }
        record["last_receipt"] = receipt
        record["receipts"] = (receipts + [receipt])[-history:]
        save(root, record)
        return True


def serve(root, history, cadence, *, stop=None):
    if stop is None:
        stop = Event()
    last_error = None
    while not stop.is_set():
        try:
            sent = (root / "receiver.json").exists() and send_next(root, history)
            if last_error:
                print("Watermark receiver resumed", file=sys.stderr, flush=True)
            last_error = None
            if sent:
                continue
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            http.client.HTTPException,
        ) as exc:
            message = f"{type(exc).__name__}: {exc}"
            if message != last_error:
                print(
                    "Watermark delivery pending:", message, file=sys.stderr, flush=True
                )
            last_error = message
        stop.wait(cadence)
