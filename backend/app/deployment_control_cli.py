from __future__ import annotations

import argparse
import json

from app.config import Settings
from app.postgres_database import postgres_connection
from app.services.deployment_control import (
    aggregate_active_jobs,
    disable_drain,
    enable_drain,
    get_drain_state,
    validate_release_sha,
)


def _state_payload(state) -> dict[str, object]:
    return {
        "enabled": state.enabled,
        "reason": state.reason,
        "release_sha": state.release_sha,
        "activated_at": state.activated_at.isoformat() if state.activated_at else None,
        "activated_by": state.activated_by,
        "updated_at": state.updated_at.isoformat(),
    }


def _release_sha_argument(value: str) -> str:
    try:
        return validate_release_sha(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _build_parser(default_dsn: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control the persistent NexPoly deployment drain.")
    parser.add_argument("--dsn", default=default_dsn)
    subparsers = parser.add_subparsers(dest="command", required=True)

    drain_parser = subparsers.add_parser("drain", help="Block public API writes.")
    drain_parser.add_argument("--reason", required=True)
    drain_parser.add_argument("--actor", required=True)
    drain_parser.add_argument(
        "--release-sha",
        required=True,
        type=_release_sha_argument,
    )
    resume_parser = subparsers.add_parser("resume", help="Allow public API writes.")
    resume_parser.add_argument("--actor", required=True)
    resume_parser.add_argument(
        "--release-sha",
        required=True,
        type=_release_sha_argument,
    )
    subparsers.add_parser("status", help="Show drain state and active Postgres jobs.")
    return parser


def main() -> None:
    args = _build_parser(Settings().app_postgres_dsn).parse_args()

    with postgres_connection(args.dsn) as connection:
        if args.command == "drain":
            state = enable_drain(
                connection,
                reason=args.reason,
                activated_by=args.actor,
                release_sha=args.release_sha,
            )
        elif args.command == "resume":
            state = disable_drain(
                connection,
                expected_activated_by=args.actor,
                expected_release_sha=args.release_sha,
            )
        else:
            state = get_drain_state(connection)
        jobs = aggregate_active_jobs(connection)

    print(
        json.dumps(
            {
                "active_jobs_schema_version": jobs.active_jobs_schema_version,
                "drain": _state_payload(state),
                "active_jobs": jobs.counts,
                "active_total": jobs.total,
            }
        )
    )


if __name__ == "__main__":
    main()
