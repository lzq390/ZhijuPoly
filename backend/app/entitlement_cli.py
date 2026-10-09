"""Explicit entitlement configuration; preview by default, never migrations.

Use an existing service/admin DSN supplied through a named environment variable.
The CLI never prints its value and never changes runtime feature flags.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from uuid import UUID

from task_usage.entitlement_policy import load_policy, policy_sha256

MAX_POLICY_BYTES = 1024 * 1024
DEFAULT_DSN_ENV = "APP_SERVICE_POSTGRES_DSN"


class CliError(Exception):
    def __init__(self, code, exit_code=2):
        self.code, self.exit_code = code, exit_code
        super().__init__(code)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default diagnostics echo invalid arguments. These may
        # accidentally contain a DSN, so emit only a fixed error code instead.
        raise CliError("invalid_arguments")


def parser():
    result = Parser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True, parser_class=Parser)
    for command in ("validate-policy", "install-policy"):
        sub = commands.add_parser(command)
        sub.add_argument("--file", required=True, help="Explicit policy JSON file")
        sub.add_argument("--allow-example", action="store_true", help="Explicitly accept an example policy")
        if command == "install-policy":
            sub.add_argument("--apply", action="store_true", help="Write after all readiness and permission checks")
            sub.add_argument("--dsn-env", default=DEFAULT_DSN_ENV, help="Name of an existing service/admin DSN environment variable")
    sub = commands.add_parser("set-tier")
    sub.add_argument("--owner", required=True, help="Existing non-system account UUID")
    sub.add_argument("--tier", choices=("free", "member"), required=True)
    sub.add_argument("--apply", action="store_true", help="Write after all readiness and permission checks")
    sub.add_argument("--dsn-env", default=DEFAULT_DSN_ENV, help="Name of an existing service/admin DSN environment variable")
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate policy key")
        result[key] = value
    return result


def read_policy(filename, *, allow_example=False):
    try:
        path = Path(filename)
        if not path.is_file():
            raise CliError("policy_file_unavailable")
        with path.open("rb") as handle:
            data = handle.read(MAX_POLICY_BYTES + 1)
        if len(data) > MAX_POLICY_BYTES:
            raise CliError("policy_file_too_large")
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite policy value")))
        document = load_policy(document)
    except CliError:
        raise
    except OSError:
        raise CliError("policy_file_unavailable") from None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise CliError("invalid_policy") from None
    if document["example"] and not allow_example:
        raise CliError("example_policy_requires_allow_example")
    return document


def _policy_receipt(policy):
    return {"policy_version": policy["version"], "policy_sha256": policy_sha256(policy),
            "period": policy["period"], "example": policy["example"]}


def _connect(dsn):
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)


def _check_authority(connection):
    row = connection.execute("""SELECT current_user AS role,
        (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) AS superuser,
        pg_has_role(current_user,'nexpoly_service','USAGE') AS service,
        pg_has_role(current_user,(SELECT relowner FROM pg_class
            WHERE oid='usage_entitlements.owner_entitlements'::regclass),'MEMBER') AS schema_owner""").fetchone()
    if not row or not (row["superuser"] or row["schema_owner"] or row["service"]):
        raise CliError("configuration_permission_denied", 3)
    if not row["superuser"] and not row["schema_owner"]:
        from app.auth.schema import validate_runtime_role
        from app.auth.service_privilege_contract import validate_service_auth_privileges
        try:
            validate_runtime_role(connection, "nexpoly_service")
            # An explicit admin CLI may use a deliberate SET ROLE DSN option.
            # Validate the effective role's complete least-privilege contract.
            validate_service_auth_privileges(connection, role=row["role"])
        except ValueError:
            raise CliError("configuration_permission_denied", 3) from None


def _check_readiness(connection):
    from app.auth.isolation_ledger import validate_application_migration_ledger
    from app.auth.schema import validate_isolation_schema
    try:
        # The exact release ledger validates the complete usage/entitlement
        # schemas, including RLS, ACLs and exact invoker trigger definitions.
        validate_application_migration_ledger(connection, require_usage=True, require_entitlements=True)
        validate_isolation_schema(connection)
    except ValueError:
        raise CliError("database_readiness_failed", 3) from None


def _admin_store():
    # Explicit configuration functions intentionally work with collection OFF.
    # Runtime hooks in task_usage.entitlements retain their default-off behavior.
    from task_usage import _entitlement_store
    return _entitlement_store


def _database_operation(args, *, policy=None, owner=None):
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", args.dsn_env):
        raise CliError("invalid_dsn_environment_name")
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        raise CliError("dsn_environment_missing", 3)
    try:
        with _connect(dsn) as connection:
            if not args.apply:
                connection.execute("SET TRANSACTION READ ONLY")
            connection.execute("SET LOCAL statement_timeout = '30s'")
            connection.execute("SET LOCAL lock_timeout = '5s'")
            _check_authority(connection)
            _check_readiness(connection)
            base = {"command": args.command, "mode": "apply" if args.apply else "preview",
                    "readiness_verified": True}
            if args.command == "install-policy":
                row = connection.execute("SELECT policy_sha256,document FROM usage_entitlements.policy_versions WHERE version=%s",
                                         (policy["version"],)).fetchone()
                receipt = _policy_receipt(policy)
                if row is not None and (row["policy_sha256"] != receipt["policy_sha256"] or row["document"] != policy):
                    raise CliError("policy_version_conflict")
                if not args.apply:
                    return {**base, **receipt, "would_install": row is None, "written": False}
                result = _admin_store().install_policy(connection, policy)
                return {**base, **receipt, "installed": bool(result["installed"]), "written": bool(result["installed"])}
            user = connection.execute("SELECT user_id,is_system FROM auth.users WHERE user_id=%s", (owner,)).fetchone()
            if user is None:
                raise CliError("owner_not_found")
            if user["is_system"]:
                raise CliError("system_owner_forbidden")
            row = connection.execute("SELECT tier FROM usage_entitlements.owner_entitlements WHERE owner_user_id=%s", (owner,)).fetchone()
            previous = row["tier"] if row else "member"
            receipt = {"owner_user_id": owner, "observed_tier": previous, "requested_tier": args.tier}
            if not args.apply:
                return {**base, **receipt, "would_change": previous != args.tier, "would_write": row is None or previous != args.tier, "written": False}
            # Core rechecks existence/non-system under the auth.users lock that
            # linearizes this configuration with actual first-start admission.
            result = _admin_store().set_owner_tier(connection, owner_user_id=owner, tier=args.tier)
            return {**base, **receipt, "changed": bool(result["changed"]), "written": bool(result["changed"])}
    except CliError:
        raise
    except Exception as exc:
        from task_usage.entitlements import EntitlementConflict, EntitlementPolicyError
        if isinstance(exc, EntitlementConflict) and exc.code == "policy_version_conflict":
            raise CliError("policy_version_conflict") from None
        if isinstance(exc, EntitlementPolicyError):
            raise CliError("configuration_rejected", 3) from None
        # Never expose psycopg messages, exception repr, DSNs or environment data.
        raise CliError("database_operation_failed", 3) from None


def execute(args):
    if args.command in {"validate-policy", "install-policy"}:
        policy = read_policy(args.file, allow_example=args.allow_example)
        if args.command == "validate-policy":
            return {"command": args.command, "valid": True, **_policy_receipt(policy), "written": False}
        return _database_operation(args, policy=policy)
    try:
        owner = str(UUID(args.owner))
    except (ValueError, TypeError, AttributeError):
        raise CliError("invalid_owner_uuid") from None
    return _database_operation(args, owner=owner)


def main(argv=None):
    try:
        receipt = execute(parser().parse_args(argv))
    except CliError as exc:
        print(json.dumps({"ok": False, "code": exc.code}, sort_keys=True), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "code": "configuration_interrupted"}), file=sys.stderr)
        return 130
    except Exception:
        print(json.dumps({"ok": False, "code": "configuration_failed"}), file=sys.stderr)
        return 3
    print(json.dumps({"ok": True, **receipt}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
