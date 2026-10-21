from __future__ import annotations

import sys

import pytest

from app import import_postgres


@pytest.mark.parametrize(
    ("flags", "refresh"),
    [([], True), (["--refresh-analytics-snapshot"], True), (["--no-refresh-analytics-snapshot"], False)],
)
def test_data_import_refreshes_analytics_by_default(
    monkeypatch: pytest.MonkeyPatch, flags: list[str], refresh: bool,
) -> None:
    captured = {}

    def import_data(_settings, **options):
        captured.update(options)
        return import_postgres.PostgresImportStats()

    monkeypatch.setattr(import_postgres, "import_all_to_postgres", import_data)
    monkeypatch.setattr(sys, "argv", ["app.import_postgres", "--dataset", "core", *flags])
    import_postgres.main()

    assert captured["refresh_analytics_snapshot"] is refresh


@pytest.mark.parametrize("cause_type", [RuntimeError, ValueError])
def test_data_import_cli_reports_committed_import_when_snapshot_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cause_type: type[Exception],
) -> None:
    calls = 0
    committed = import_postgres.PostgresImportStats()
    committed.add("core", 2)

    def import_data(_settings, **_options):
        nonlocal calls
        calls += 1
        try:
            raise cause_type("synthetic-connection-detail-must-not-be-printed")
        except Exception as exc:
            raise import_postgres.AnalyticsSnapshotRefreshError(committed) from exc

    monkeypatch.setattr(import_postgres, "import_all_to_postgres", import_data)
    monkeypatch.setattr(sys, "argv", ["app.import_postgres", "--dataset", "core"])

    with pytest.raises(SystemExit) as exc_info:
        import_postgres.main()

    output = capsys.readouterr()
    assert exc_info.value.code == 1
    assert calls == 1
    assert "core\trows=2" in output.out
    assert "Data import already committed" in output.err
    assert "not rolled back" in output.err
    assert "Retry only" in output.err
    assert "python -m app.generate_database_analytics_snapshot" in output.err
    assert "do not repeat the data import" in output.err
    assert "synthetic-connection-detail" not in output.out + output.err
    assert "Traceback" not in output.err


def test_data_import_cli_requires_an_explicit_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["python -m app.import_postgres"])

    with pytest.raises(SystemExit) as exc_info:
        import_postgres.main()

    assert exc_info.value.code == 2


@pytest.mark.parametrize("dataset", ["online", "lab"])
def test_data_import_cli_rejects_retired_mutable_dataset_names(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dataset: str,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "python -m app.import_postgres",
            "--dataset",
            dataset,
            "--skip-migrations",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        import_postgres.main()

    assert exc_info.value.code == 2
    assert (
        f"business-mutable datasets cannot be imported or rebuilt: {dataset}"
        in capsys.readouterr().err
    )
