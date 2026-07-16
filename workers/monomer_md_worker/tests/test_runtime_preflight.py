from __future__ import annotations

import asyncio
import json
import signal
from types import SimpleNamespace

import pytest

from workers.monomer_md_worker.app import runtime_preflight
from workers.monomer_md_worker.app.runtime_health import (
    ProtocolRuntimeSnapshot,
    RuntimeSnapshot,
)


def test_preflight_emits_only_the_bounded_safe_contract(monkeypatch) -> None:
    settings = SimpleNamespace()
    runner = object()
    snapshot = RuntimeSnapshot(
        True,
        False,
        "secret runtime path /private/openmm",
        (
            ProtocolRuntimeSnapshot(
                "Transport",
                True,
                False,
                "secret plugin path /private/plugin",
            ),
        ),
    )

    async def fake_probe(*_args, **_kwargs):
        return snapshot

    monkeypatch.setattr(runtime_preflight, "load_settings", lambda: settings)
    monkeypatch.setattr(runtime_preflight, "MonomerMdRunner", lambda _settings: runner)
    monkeypatch.setattr(runtime_preflight, "probe_runtime_snapshot", fake_probe)

    payload, ready = asyncio.run(
        runtime_preflight._run(require_transport_ready=True)
    )

    assert ready is False
    assert payload == {
        "schema_version": 1,
        "runtime_ready": False,
        "transport": {
            "supported": True,
            "runtime_ready": False,
            "runtime_error": "unavailable",
        },
    }
    encoded = json.dumps(payload)
    assert "secret" not in encoded
    assert "/private" not in encoded


def test_transport_is_required_only_when_the_release_gate_requests_it(
    monkeypatch,
) -> None:
    snapshot = RuntimeSnapshot(
        True,
        True,
        None,
        (ProtocolRuntimeSnapshot("Transport", True, False, "unavailable"),),
    )

    async def fake_probe(*_args, **_kwargs):
        return snapshot

    monkeypatch.setattr(runtime_preflight, "load_settings", lambda: object())
    monkeypatch.setattr(runtime_preflight, "MonomerMdRunner", lambda _settings: object())
    monkeypatch.setattr(runtime_preflight, "probe_runtime_snapshot", fake_probe)

    _payload, compatible_ready = asyncio.run(
        runtime_preflight._run(require_transport_ready=False)
    )
    _payload, strict_ready = asyncio.run(
        runtime_preflight._run(require_transport_ready=True)
    )

    assert compatible_ready is True
    assert strict_ready is False


def test_termination_signal_cancels_probe_through_cooperative_cleanup(
    monkeypatch,
) -> None:
    callbacks: dict[signal.Signals, object] = {}
    removed: list[signal.Signals] = []
    probe_started = asyncio.Event()
    probe_cancelled = asyncio.Event()

    class FakeLoop:
        def add_signal_handler(self, signal_number, callback) -> None:
            callbacks[signal_number] = callback

        def remove_signal_handler(self, signal_number) -> bool:
            removed.append(signal_number)
            return True

    async def fake_probe(*_args, **_kwargs):
        probe_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            probe_cancelled.set()
            raise

    monkeypatch.setattr(runtime_preflight, "load_settings", lambda: object())
    monkeypatch.setattr(runtime_preflight, "MonomerMdRunner", lambda _settings: object())
    monkeypatch.setattr(runtime_preflight, "probe_runtime_snapshot", fake_probe)
    monkeypatch.setattr(runtime_preflight.asyncio, "get_running_loop", FakeLoop)

    async def scenario() -> None:
        task = asyncio.create_task(
            runtime_preflight._run(require_transport_ready=True)
        )
        await probe_started.wait()
        callback = callbacks[signal.SIGTERM]
        assert callable(callback)
        callback()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert probe_cancelled.is_set()
    assert set(removed) == {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}
