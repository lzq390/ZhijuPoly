"""Transaction-scoped publication core; callers own connection authentication.

No source database, object store, credentials, or CLI imports are needed here.
The caller supplies an authorized connection; this module never opens one.
"""
from __future__ import annotations

import uuid

from app.services.literature_normalizer import NORMALIZER_VERSION


class ImportStateError(ValueError):
    """Stable publication failure code, safe for a caller to report."""


def activate_generation(connection, generation_id: str, *, rollback: bool = False, allow_partial: bool = False) -> dict:
    generation_id = str(uuid.UUID(generation_id))
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(hashtext('nexpoly_literature_publish'))")
        # Catalog mutations lock both old/new parents in UUID order. Acquire
        # the target and current active parent in that same order before any
        # flag changes, including an idempotent publish or rollback.
        connection.execute("""SELECT generation_id FROM knowledge.literature_generations
            WHERE generation_id=%s OR is_active
            ORDER BY generation_id FOR UPDATE""", (generation_id,))
        generation = connection.execute("SELECT * FROM knowledge.literature_generations WHERE generation_id=%s", (generation_id,)).fetchone()
        if not generation:
            raise ImportStateError("GENERATION_NOT_FOUND")
        if rollback and generation["status"] != "published":
            raise ImportStateError("ROLLBACK_REQUIRES_PUBLISHED_GENERATION")
        if not rollback and generation["status"] not in {"ready", "published"}:
            raise ImportStateError("GENERATION_NOT_READY")
        if generation["normalizer_version"] != NORMALIZER_VERSION:
            raise ImportStateError("GENERATION_FORMAT_UNSUPPORTED")
        if generation["source_summary"].get("partial") and not allow_partial:
            raise ImportStateError("PARTIAL_GENERATION_REQUIRES_ALLOW_PARTIAL")
        count = connection.execute("SELECT count(*) n FROM knowledge.literature_catalog WHERE generation_id=%s", (generation_id,)).fetchone()["n"]
        if not count or count != generation["report"].get("ready_documents"):
            raise ImportStateError("GENERATION_CATALOG_COUNT_MISMATCH")
        connection.execute("UPDATE knowledge.literature_generations SET is_active=false WHERE is_active=true")
        connection.execute("UPDATE knowledge.literature_generations SET is_active=true,status='published',published_at=COALESCE(published_at,now()) WHERE generation_id=%s", (generation_id,))
    return {"generation_id": generation_id, "status": "published", "is_active": True, "documents": count, "action": "rollback" if rollback else "publish"}
