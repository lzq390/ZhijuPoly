"""Seed only the two retained M1 workspace owners in the independent M2 database."""

import os
from pathlib import Path
from uuid import UUID

import psycopg

from app.auth.openscience_candidate import authority_environment
from app.auth.service import hash_password, verify_password


def main():
    environment = authority_environment()
    with psycopg.connect(os.environ["OPENSCIENCE_M2_ADMIN_DSN"], connect_timeout=3) as conn:
        if conn.execute(
            "SELECT current_database(), to_regclass('openscience_m2.workspace_bindings')"
        ).fetchone() != ("nexpoly_m2_dev", "openscience_m2.workspace_bindings"):
            raise ValueError("Use the initialized development authority database")
        conn.execute("LOCK TABLE openscience_m2.environments IN SHARE ROW EXCLUSIVE MODE")
        if conn.execute("SELECT 1 FROM openscience_m2.environments WHERE environment<>%s LIMIT 1", (environment,)).fetchone():
            raise ValueError("This authority database is already bound to another candidate")
        conn.execute("INSERT INTO openscience_m2.environments VALUES (%s, true) ON CONFLICT DO NOTHING", (environment,))
        for number, label in enumerate(("a", "b"), 1):
            owner = UUID(f"10000000-0000-4000-8000-{number:012d}")
            name = f"test-user-{label}"
            password = Path(os.environ[f"OPENSCIENCE_M2_PASSWORD_{label.upper()}_FILE"]).read_text().strip()
            # Re-running seed verifies the exact existing identity without replacing credentials.
            conn.execute(
                """INSERT INTO auth.users (user_id,username,password_hash,must_change_password)
                                VALUES (%s,%s,%s,false) ON CONFLICT (user_id) DO NOTHING""",
                (owner, name, hash_password(password)),
            )
            row = conn.execute(
                "SELECT username,password_hash FROM auth.users WHERE user_id=%s", (owner,)
            ).fetchone()
            if not row or row[0] != name or not verify_password(row[1], password):
                raise ValueError("Existing M2 account differs from supplied seed")
            created = conn.execute(
                """INSERT INTO openscience_m2.workspace_bindings
                                VALUES (%s,%s,%s,true) ON CONFLICT (environment,user_id) DO NOTHING
                                RETURNING workspace_user_id""",
                (environment, owner, name),
            ).fetchone()
            if created:
                conn.execute(
                    "UPDATE billing.accounts SET override_tier='member' WHERE owner_user_id=%s", (owner,)
                )
            bound = conn.execute(
                """SELECT workspace_user_id FROM openscience_m2.workspace_bindings
                                     WHERE environment=%s AND user_id=%s""",
                (environment, owner),
            ).fetchone()
            if not bound or bound[0] != name:
                raise ValueError("Existing workspace mapping differs from the retained M1 user")
    print(f"Provisioned the two {environment} workspace owners.")


if __name__ == "__main__":
    main()
