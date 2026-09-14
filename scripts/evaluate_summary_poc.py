"""Replay a frozen summary fixture against the configured model, bypassing session caches."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from time import monotonic
from datetime import datetime, timezone
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import Settings
from app.services import knowledge_poc_summary


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=ROOT / "scripts/fixtures/summary-session-20260909.json")
    parser.add_argument("--prompt", type=Path, help="Optional candidate prompt; otherwise use the current application prompt")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60.0, help="Model wait limit for this evaluation only")
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("Output already exists; use a new filename to preserve the comparison")
    fixture_bytes = args.fixture.read_bytes()
    fixture = json.loads(fixture_bytes)
    events = fixture["recording"]["events"]
    if args.prompt:
        knowledge_poc_summary.SUMMARY_PROMPT = args.prompt.read_text().strip()
    settings = Settings()
    evidence = json.dumps(knowledge_poc_summary.build_summary_evidence(events), ensure_ascii=False)
    started = monotonic()
    try:
        result = await knowledge_poc_summary.generate_knowledge_summary(events, settings, timeout_seconds=args.timeout)
        result["status"] = "success"
    except HTTPException as exc:
        result = {"status": "error", "error_status": exc.status_code, "error": exc.detail}
    artifact = {
        "tested_at": datetime.now(timezone.utc).isoformat(), "model": settings.assistant_model,
        "timeout_seconds": args.timeout,
        "fixture_sha256": digest(fixture_bytes), "evidence_sha256": digest(evidence.encode()),
        "prompt_sha256": digest(knowledge_poc_summary.SUMMARY_PROMPT.encode()),
        "prompt": knowledge_poc_summary.SUMMARY_PROMPT, "event_count": len(events),
        "elapsed_seconds": round(monotonic() - started, 2), **result,
        "paragraph_count": len([part for part in result.get("summary", "").split("\n\n") if part.strip()]),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(artifact, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({key: value for key, value in artifact.items() if key != "prompt"}, ensure_ascii=False))
    if result["status"] == "error":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
