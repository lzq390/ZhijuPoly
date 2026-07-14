from __future__ import annotations

import sys

import pytest

from app import import_postgres


def test_data_import_cli_requires_an_explicit_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["python -m app.import_postgres"])

    with pytest.raises(SystemExit) as exc_info:
        import_postgres.main()

    assert exc_info.value.code == 2
