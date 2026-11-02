"""Worker-safe access-tier resolution, independent of quota collection flags."""
from __future__ import annotations
import json
from pathlib import Path
from uuid import UUID
from psycopg.rows import dict_row
from task_usage.entitlements import MIGRATION_VERSION, EntitlementPolicyError
from task_usage.billing_contract import BILLING_VERSION, BILLING_CHECKSUM

def release_contract():
    path=Path(__file__).with_name("entitlement_release.json")
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
        if value["schema_version"]!=1 or value["entitlement_version"]!=MIGRATION_VERSION:
            raise ValueError("release contract version")
        if value["ledger"][-1]!={"version":MIGRATION_VERSION,"checksum":value["entitlement_sql_sha256"]}:
            raise ValueError("release contract checksum")
        return value
    except (OSError,ValueError,TypeError,KeyError,IndexError) as exc:
        raise EntitlementPolicyError("The entitlement release contract is unavailable or invalid") from exc

# This suffix is an approved optional expansion, never a wildcard for later SQL.
OPTIONAL_MIGRATION = {"version": "0023_polymer_md_jobs", "checksum": "e73e00e16b8fc3c72e3716dae523d02d264a3e2e6989671f6019aca3440a1379"}

# A second exact suffix requires the complete reviewed polymer prefix.
LITERATURE_MIGRATION = {"version": "0024_literature_projection", "checksum": "2a5681e4b5000ed9cbf6ec88e1d8d7e6c6c5d0faf82270e802a3a9abb2060cf3"}


def accepted_release_ledgers():
    base = release_contract()["ledger"]
    polymer = [*base, dict(OPTIONAL_MIGRATION)]
    literature = [*polymer, dict(LITERATURE_MIGRATION)]
    return [base, polymer, literature, [*literature, {"version": BILLING_VERSION, "checksum": BILLING_CHECKSUM}]]


def resolve_access_tier(connection,*,owner_user_id):
    """Read only, no commits/auth locks/fallback on errors; caller owns start lock."""
    try: owner=str(UUID(str(owner_user_id)))
    except (ValueError,TypeError,AttributeError): raise EntitlementPolicyError("Invalid access owner") from None
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT to_regclass('usage_entitlements.owner_entitlements') AS tiers,to_regclass('governance.schema_migrations') AS ledger")
        objects=cursor.fetchone()
        ledger=[]
        if objects["ledger"] is not None:
            cursor.execute("SELECT version,checksum FROM governance.schema_migrations ORDER BY version")
            ledger=[dict(row) for row in cursor]
        if objects["tiers"] is None:
            if any(row["version"].endswith("_usage_entitlements") for row in ledger):
                raise EntitlementPolicyError("Entitlement migration claims a missing access-tier table")
            return "member"  # Preserve pre-cutover authentication initialization.
        if ledger not in accepted_release_ledgers():
            raise EntitlementPolicyError("Access tiers require the exact release-known migration ledger")
        if ledger[-1]["version"] == BILLING_VERSION:
            # Authority is evaluated on every HTTP admission and first start;
            # expiration does not depend on a scheduler or the collection flag.
            cursor.execute("""SELECT count(*) AS n,bool_and(c.relkind='r' AND c.relrowsecurity AND c.relforcerowsecurity) AS protected
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='billing' AND c.relname IN ('accounts','subscriptions')""")
            schema = cursor.fetchone()
            if schema["n"] != 2 or not schema["protected"]:
                raise EntitlementPolicyError("Membership access-tier isolation is invalid")
            cursor.execute("""SELECT CASE WHEN a.override_tier IS NOT NULL THEN a.override_tier
                  WHEN a.legacy_member OR s.paid_through>clock_timestamp() THEN 'member' ELSE 'free' END AS tier
                FROM billing.accounts a LEFT JOIN billing.subscriptions s USING(owner_user_id)
                WHERE a.owner_user_id=%s""", (owner,))
            row = cursor.fetchone()
            if row is None:
                return "free"
            if row["tier"] not in {"member", "free"}:
                raise EntitlementPolicyError("Unrecognized membership access tier")
            return row["tier"]
        cursor.execute("SELECT relkind,relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=%s::regclass",("usage_entitlements.owner_entitlements",))
        table=cursor.fetchone()
        if table is None or table["relkind"]!="r" or not table["relrowsecurity"] or not table["relforcerowsecurity"]:
            raise EntitlementPolicyError("Access-tier isolation is invalid")
        cursor.execute("SELECT tier FROM usage_entitlements.owner_entitlements WHERE owner_user_id=%s",(owner,))
        row=cursor.fetchone()
        if row is None: return "member"
        if row["tier"] not in {"member","free"}:
            raise EntitlementPolicyError("Unrecognized explicit access tier")
        return row["tier"]
