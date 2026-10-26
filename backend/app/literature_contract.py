"""Finite local literature expansion policy, without applying migrations.

Legacy exact ledgers remain exact. Only the complete reviewed 0023 prefix may
extend to this planned 0024 migration; no intervening usage/entitlement history
can be omitted. Runtime/API admission is deliberately not enabled here.
"""
from __future__ import annotations

LITERATURE_MIGRATION = "0024_literature_projection"
LITERATURE_MIGRATION_SHA256 = '2a5681e4b5000ed9cbf6ec88e1d8d7e6c6c5d0faf82270e802a3a9abb2060cf3'
LITERATURE_TABLES = (
    ("knowledge", "literature_generations"),
    ("knowledge", "literature_catalog"),
    ("knowledge", "literature_snapshots"),
)
_CANONICAL_PREFIX = (('0001_app_data_governance', 'd5fc9f3d063f1cba476834f3530519b7970cd54f3c3711d05aba1f1cb2fd34f9'), ('0002_lab_identity_defaults', '580ed6dc7c34970aabd662bc47765e9d02446c28aea1c4fa8fb2a99f05b1ac2f'), ('0003_runtime_postgres_cutover', '0888ac9abd1b6b642f0addd42274b5408981a26c27f1140b7b656ff34ad73ce3'), ('0004_monomer_md_jobs', 'b3ad64728f399f42b2bf9edb47ad035ac70f09fce6ced48e7b422ea74d5a7e8e'), ('0005_byteff2_formal_monomer_md', 'c9ec808c50915b82a696ab482ed676c62bc75f00a9af21baf9e7f66b185bacb5'), ('0006_property_filter_records', '57b103dc656334cf5e52bdc9512576a303ae0044ec5fb64eb7cba802021eceaa'), ('0007_polytao_jobs', 'b15268a475e8daf8dd58be988a228a0440e59a31dbf11d5d6b52e0974c3daab5'), ('0008_polytao_backend_runtime', 'd0d8b2187aad8657269600873d3d2630e30c7d72da2f6662e18ab22031deff90'), ('0009_monomer_md_job_leases', 'ef1757a81976f351459e8257bd492aa6267cbf507c4ea85506fefa2d465d2db8'), ('0010_deployment_control', 'f7fad29bcf1da1c6903a688a7312a67216bc11002ac558209ff56e25f69cf7cd'), ('0011_monomer_md_demo_steps', '9a03f38329199aa707818c2099b9811d46366bafe0ddaeb39ae53bc20d0a68ed'), ('0012_drop_polytao_jobs', 'c59b6f1efe9f926ad135379bd1a7141a7920730fa93c0e802646b1b913511728'), ('0013_monomer_dft_jobs', 'ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc'), ('0014_monomer_md_task_queue_cancel', '7d91b451371eaf10542440c8b947c9ac50b51e3d553cb205a76aca196eaf8df6'), ('0015_property_filter_performance', 'e0159576c09d31de8a7da46f728d36553f67aa75adba344f93cdc302cf000732'), ('0016_monomer_polymerization_batch', 'c79b22540864ee3d7cbfb66d63870da1a65dff22250cf85acf47b688dbd9c976'), ('0017_user_isolation_prepare', '0eeef0058fbda71770627005fe617f5a56261cf25e236c4a159d0b059db76adb'), ('0018_user_isolation_cutover', '0755d9d802eba5fd69225a34f61bc01828b32a63f815802d2905636809c573f2'), ('0019_service_auth_least_privilege', '23f950f17126af7f73495938da41544585186e13dc44e060cfecf90bad207f23'), ('0020_structure_library', '4f8ff9a2d9c1f7cf41140d3517f70dcb52b045fadac84a09d292bdf8095aa0b7'), ('0021_usage_metering', '3e79f0fcefe0bdfff5e6c00f184e22cf17621d5a1cc46a5ddb6fb8f296680f62'), ('0022_usage_entitlements', 'fe536a6256493931f6d773197a3e1e742b17bacc3c675e4fcbc32864e0747bb5'), ('0023_polymer_md_jobs', 'e73e00e16b8fc3c72e3716dae523d02d264a3e2e6989671f6019aca3440a1379'))
_BOUNDARIES = {"0017_user_isolation_prepare", "0018_user_isolation_cutover",
               "0019_service_auth_least_privilege", "0020_structure_library",
               "0021_usage_metering", "0022_usage_entitlements", "0023_polymer_md_jobs"}


def registered_literature_ledger() -> list[dict[str, str]]:
    """Return fresh records so callers cannot mutate the pinned contract."""
    return [*({"version": version, "checksum": checksum} for version, checksum in _CANONICAL_PREFIX),
            {"version": LITERATURE_MIGRATION, "checksum": LITERATURE_MIGRATION_SHA256}]


def compatible_identity_ledger(observed: list[dict], expected: list[dict]) -> bool:
    canonical = registered_literature_ledger()
    allowed = [canonical[:index + 1] for index, row in enumerate(canonical)
               if row["version"] in _BOUNDARIES or row["version"] == LITERATURE_MIGRATION]
    if expected not in allowed:
        return False
    if observed == expected:
        return True
    return expected == canonical[:-1] and observed == canonical


def optional_literature_tables(connection) -> tuple[str, ...]:
    """Preserve historical backups, rejecting partial or ambiguous table sets."""
    names = tuple(schema + "." + table for schema, table in LITERATURE_TABLES)
    rows = connection.execute(
        "SELECT name,to_regclass(name) AS relation FROM unnest(%s::text[]) AS name",
        (list(names),),
    ).fetchall()
    if len(rows) != len(names) or {row["name"] for row in rows} != set(names):
        raise ValueError("Literature backup table inventory is incomplete")
    present = [row["relation"] is not None for row in rows]
    if any(present) and not all(present):
        raise ValueError("Literature backup requires all registered projection tables")
    return names if all(present) else ()
