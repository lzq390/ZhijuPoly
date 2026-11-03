"""Finite membership expansion and complete backup table inventory."""
from app.literature_contract import registered_literature_ledger, compatible_identity_ledger as _legacy_compatible
from task_usage.billing_contract import BILLING_VERSION, BILLING_CHECKSUM, BILLING_TABLES


def registered_billing_ledger():
    return [*registered_literature_ledger(), {"version": BILLING_VERSION, "checksum": BILLING_CHECKSUM}]


def compatible_identity_ledger(observed, expected):
    if _legacy_compatible(observed, expected):
        return True
    canonical = registered_billing_ledger()
    return observed == canonical and expected in (canonical, canonical[:-1], canonical[:-2])


def optional_billing_tables(connection):
    rows = connection.execute("SELECT name,to_regclass(name) AS relation FROM unnest(%s::text[]) AS name",
                              (list(BILLING_TABLES),)).fetchall()
    if len(rows) != len(BILLING_TABLES) or {row["name"] for row in rows} != set(BILLING_TABLES):
        raise ValueError("Membership table inventory is incomplete")
    present = [row["relation"] is not None for row in rows]
    if any(present) and not all(present):
        raise ValueError("Membership backup requires every billing table")
    return BILLING_TABLES if all(present) else ()
