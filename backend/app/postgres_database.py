from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator


class PostgresUnavailableError(RuntimeError):
    pass


@contextmanager
def postgres_connection(dsn: str) -> Iterator[object]:
    from app.auth.context import database_settings, is_service_context, current_owner_id
    import os
    settings = database_settings()
    # Trusted background services explicitly opt in; an API request can never
    # elevate by supplying a header or owner argument.
    if is_service_context():
        dsn = getattr(settings, "service_dsn", None) or os.getenv("APP_SERVICE_POSTGRES_DSN", dsn)
    elif settings is not None and dsn == settings.application_dsn:
        dsn = settings.application_dsn
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - depends on deployment environment
        raise PostgresUnavailableError("Postgres driver is not installed") from exc

    try:
        with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=3) as connection:
            owner = current_owner_id(required=False)
            if owner is not None and not is_service_context():
                connection.execute("SELECT set_config('app.user_id', %s, true)", (owner,))
            yield connection
    except psycopg.OperationalError as exc:  # pragma: no cover - requires live Postgres failure
        raise PostgresUnavailableError("PI Postgres database is not reachable") from exc
