from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator


class PostgresUnavailableError(RuntimeError):
    pass


@contextmanager
def postgres_connection(dsn: str) -> Iterator[object]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - depends on deployment environment
        raise PostgresUnavailableError("Postgres driver is not installed") from exc

    try:
        with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=3) as connection:
            yield connection
    except psycopg.OperationalError as exc:  # pragma: no cover - requires live Postgres failure
        raise PostgresUnavailableError("PI Postgres database is not reachable") from exc
