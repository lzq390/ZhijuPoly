"""Finite ledger and backup contract for durable retrosynthesis jobs."""
from app.billing_contract import registered_billing_ledger, compatible_identity_ledger as _legacy
from task_usage.retrosynthesis_contract import RETROSYNTHESIS_VERSION, RETROSYNTHESIS_CHECKSUM


def registered_retrosynthesis_ledger():
    return [*registered_billing_ledger(),
            {"version": RETROSYNTHESIS_VERSION, "checksum": RETROSYNTHESIS_CHECKSUM}]


def compatible_identity_ledger(observed, expected):
    if _legacy(observed, expected):
        return True
    canonical = registered_retrosynthesis_ledger()
    # Only the exact reviewed successor is accepted by the immediately preceding
    # application contracts. No unknown migration or checksum is tolerated.
    return observed == canonical and expected in (canonical, canonical[:-1], canonical[:-2], canonical[:-3])


def optional_retrosynthesis_tables(connection):
    row = connection.execute("SELECT to_regclass('retrosynthesis.jobs') IS NOT NULL AS present").fetchone()
    ledger = connection.execute("SELECT EXISTS(SELECT FROM governance.schema_migrations WHERE version=%s) AS present",
                                (RETROSYNTHESIS_VERSION,)).fetchone()
    if bool(row["present"]) != bool(ledger["present"]):
        raise ValueError("Retrosynthesis table and migration ledger disagree")
    return ("retrosynthesis.jobs",) if row["present"] else ()
