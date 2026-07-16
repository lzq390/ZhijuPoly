from __future__ import annotations

import argparse
import asyncio
import json
import signal
from uuid import uuid4

from .config import load_settings
from .runner import MonomerMdRunner
from .runtime_health import probe_runtime_snapshot


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the bounded Monomer MD candidate runtime preflight."
    )
    parser.add_argument("--require-transport-ready", action="store_true")
    return parser.parse_args()


async def _run(*, require_transport_ready: bool) -> tuple[dict[str, object], bool]:
    loop = asyncio.get_running_loop()
    current_task = asyncio.current_task()
    if current_task is None:  # pragma: no cover - asyncio invariant
        raise RuntimeError("runtime preflight task is unavailable")
    handled_signals: list[signal.Signals] = []
    for signal_number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            loop.add_signal_handler(signal_number, current_task.cancel)
        except (NotImplementedError, RuntimeError):
            continue
        handled_signals.append(signal_number)
    try:
        settings = load_settings()
        runner = MonomerMdRunner(settings)
        snapshot = await probe_runtime_snapshot(
            settings,
            runner=runner,
            worker_instance_id=f"preflight-{uuid4().hex}",
        )
    finally:
        for signal_number in handled_signals:
            loop.remove_signal_handler(signal_number)
    transport = next(
        (item for item in snapshot.protocols if item.protocol == "Transport"),
        None,
    )
    transport_summary = {
        "supported": transport is not None and transport.supported is True,
        "runtime_ready": transport is not None and transport.runtime_ready is True,
        "runtime_error": None
        if transport is not None and transport.runtime_error is None
        else "unavailable",
    }
    payload: dict[str, object] = {
        "schema_version": 1,
        "runtime_ready": snapshot.runtime_ready is True,
        "transport": transport_summary,
    }
    strict_transport_ready = (
        transport_summary["supported"] is True
        and transport_summary["runtime_ready"] is True
        and transport_summary["runtime_error"] is None
    )
    ready = snapshot.runtime_ready is True and (
        not require_transport_ready or strict_transport_ready
    )
    return payload, ready


def main() -> int:
    args = _parse_args()
    try:
        payload, ready = asyncio.run(
            _run(require_transport_ready=args.require_transport_ready)
        )
    except BaseException:
        payload = {
            "schema_version": 1,
            "runtime_ready": False,
            "transport": {
                "supported": False,
                "runtime_ready": False,
                "runtime_error": "unavailable",
            },
        }
        ready = False
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
