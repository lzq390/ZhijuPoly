"""Startup must validate the whole installed prefix and still require MD 0023."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app import main
from app.postgres_preflight import (SCHEMA_TARGET_CURRENT_ISOLATION,
                                   SCHEMA_TARGET_LITERATURE, SCHEMA_TARGET_POLYMER_MD,
                                   SCHEMA_TARGET_STRUCTURE_LIBRARY)


@pytest.mark.parametrize("boundary", [SCHEMA_TARGET_POLYMER_MD, SCHEMA_TARGET_LITERATURE])
def test_md_startup_accepts_only_validated_current_private_boundaries(monkeypatch, boundary):
    check = Mock(return_value={"schema_target": boundary})
    monkeypatch.setattr(main, "run_preflight", check)
    monkeypatch.setattr(main, "preflight_blockers", lambda report: [])
    ready = Mock()
    app = SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(polymer_md_dev_enabled=True),
        auth=SimpleNamespace(assert_application_ready=ready)))
    main._run_database_startup_preflight(app, required=False)
    assert check.call_args.kwargs == {"mode": "runtime", "strict": True,
                                    "schema_target": SCHEMA_TARGET_CURRENT_ISOLATION}
    assert app.state.database_preflight_errors == ()
    ready.assert_called_once_with()


@pytest.mark.parametrize("boundary", [SCHEMA_TARGET_STRUCTURE_LIBRARY, "unknown", None])
def test_md_startup_rejects_missing_md_even_when_legacy_startup_is_optional(monkeypatch, boundary):
    monkeypatch.setattr(main, "run_preflight", lambda *args, **kwargs: {"schema_target": boundary})
    monkeypatch.setattr(main, "preflight_blockers", lambda report: [])
    app = SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(polymer_md_dev_enabled=True),
        auth=SimpleNamespace(assert_application_ready=Mock())))
    with pytest.raises(RuntimeError, match="Polymer MD requires"):
        main._run_database_startup_preflight(app, required=False)


def test_md_startup_never_discards_a_current_boundary_blocker(monkeypatch):
    monkeypatch.setattr(main, "run_preflight", lambda *args, **kwargs: {"schema_target": SCHEMA_TARGET_LITERATURE})
    monkeypatch.setattr(main, "preflight_blockers", lambda report: ["schema drift"])
    app = SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(polymer_md_dev_enabled=True),
        auth=SimpleNamespace(assert_application_ready=Mock())))
    with pytest.raises(RuntimeError, match="schema drift"):
        main._run_database_startup_preflight(app, required=False)
