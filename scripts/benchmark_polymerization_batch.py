#!/usr/bin/env python3
"""CSV upload -> real SMiPoly -> PostgreSQL checkpoints -> CSV/XLSX benchmark.

Creates and drops an isolated database using the configured TEST Postgres role.
Never migrates the configured database. --rows 100 exercises 10,000 unique pairs.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def main():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from fastapi.testclient import TestClient
    from openpyxl import load_workbook
    from app.config import Settings
    from app.main import create_app
    from app.postgres_migrations import apply_postgres_migrations
    from app.services.polymerization_batch.chemistry import canonicalize, engine_fingerprint
    from app.services.polymerization_batch.models import BatchSettings
    from app.services.polymerization_batch.service import BASE_PATH, BatchService
    from app.services.polymerization_batch.worker import BatchWorker

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--restart", action="store_true", help="Kill and restart an actual worker process during generation")
    args = parser.parse_args()
    if not 1 <= args.rows <= 200:
        parser.error("rows must be 1..200")
    dsn = os.environ["APP_POSTGRES_DSN"]
    database = "nexpoly_batch_benchmark_" + uuid4().hex[:12]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
    isolated_dsn = make_conninfo(dsn, dbname=database)
    try:
        apply_postgres_migrations(isolated_dsn, allowed_kinds={"baseline", "expand"}, allow_contract_on_fresh_database=True)
        root = args.output.resolve()
        root.mkdir(parents=True, exist_ok=True)
        config = BatchSettings(enabled=True, storage_root=root / "files")
        a = set()
        for length in range(2, 40):
            for position in range(length):
                a.add(canonicalize("N" + "C" * position + "C(C)" + "C" * (length - position - 1) + "N"))
                if len(a) >= args.rows:
                    break
            if len(a) >= args.rows:
                break
        a = sorted(a)
        b = [canonicalize("O=C1OC(=O)c2c(" + "C" * length + ")c3c(cc21)C(=O)OC3=O") for length in range(1, args.rows + 1)]
        assert len(a) == len(set(a)) == len(b) == len(set(b)) == args.rows
        def csv_bytes(values):
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(["id", "name", "SMILES"])
            writer.writerows([[f"{index:04d}", f"monomer-{index}", value] for index, value in enumerate(values, 1)])
            return output.getvalue().encode("utf-8-sig")
        service = BatchService(isolated_dsn, config)
        app = create_app(Settings(app_postgres_dsn=isolated_dsn, model_enabled=False, retro_model_enabled=False))
        app.state.polymerization_batch = service
        worker = BatchWorker(isolated_dsn, config)
        worker.engine = engine_fingerprint()
        worker.heartbeat(force=True)
        client = TestClient(app)
        started = time.monotonic()
        upload = client.post(BASE_PATH + "/imports", files={"file_a": ("a.csv", csv_bytes(a)), "file_b": ("b.csv", csv_bytes(b))})
        upload.raise_for_status()
        imported = upload.json()
        preview = client.post(f"{BASE_PATH}/imports/{imported['import_id']}/preview", json={role: imported["tables"][role]["mapping"] for role in ("a", "b")})
        preview.raise_for_status()
        validated = preview.json()
        assert validated["statistics"]["unique_pairs"] == args.rows**2
        result = client.post(BASE_PATH + "/jobs", headers={"Idempotency-Key": uuid4().hex}, json={"import_id": imported["import_id"], "preview_revision": validated["preview_revision"], "target_class": "polyimide"})
        result.raise_for_status()
        job_id = result.json()["job_id"]
        preparation_seconds = time.monotonic() - started
        units, status_latencies = [], []
        restart_count = 0
        if args.restart:
            worker_env = {**os.environ, "APP_POSTGRES_DSN": isolated_dsn,
                          "MONOMER_POLYMERIZATION_BATCH_ENABLED": "true", "SMIPOLY_ENABLED": "true",
                          "MONOMER_POLYMERIZATION_BATCH_STORAGE_ROOT": str(config.storage_root),
                          "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "backend") + os.pathsep + str(Path(__file__).resolve().parents[1]),
                          "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
            def launch(log):
                return subprocess.Popen([sys.executable, "-m", "app.monomer_polymerization_batch_worker"],
                                        env=worker_env, stdout=log, stderr=log, start_new_session=True)
            with (root / "worker.log").open("wb") as log:
                process = launch(log)
                previous_count = -1
                try:
                    while time.monotonic() - started < config.job_seconds + 600:
                        before = time.monotonic()
                        response = client.get(f"{BASE_PATH}/jobs/{job_id}")
                        response.raise_for_status()
                        status_latencies.append(time.monotonic() - before)
                        job = worker.repository.get_job(job_id)
                        processed = job["summary"]["processed_pairs"]
                        if processed != previous_count:
                            print(json.dumps({"processed_pairs": processed, "seconds": round(time.monotonic() - started, 2), "restarts": restart_count}), flush=True)
                            previous_count = processed
                        if processed > 0 and job["execution_token"] and not restart_count and job["stage"] == "generate":
                            single_started = time.monotonic()
                            single = client.post("/api/v1/monomer-polymerization", json={
                                "monomer_a_smiles": a[0], "monomer_b_smiles": b[0], "target_class": "polyimide", "max_results": 10})
                            single.raise_for_status()
                            single_seconds = time.monotonic() - single_started
                            process.kill()
                            process.wait(timeout=10)
                            restart_count += 1
                            process = launch(log)
                        if job["status"] not in {"queued", "running", "cancelling"}:
                            break
                        if process.poll() is not None:
                            raise RuntimeError("worker unexpectedly exited; see worker.log")
                        time.sleep(0.25)
                    else:
                        raise RuntimeError("benchmark exceeded its deadline")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
            assert restart_count == 1, "the task must exercise a real interruption"
            units = [{"kind": item["kind"], "attempts": item["attempt"], "counts": item["counts"]}
                     for item in worker.repository.chunks(job_id)]
        else:
            while True:
                claimed = worker.repository.claim(worker.worker_id, worker.engine["fingerprint"])
                if not claimed:
                    raise RuntimeError("benchmark unexpectedly stopped claiming work")
                before = time.monotonic()
                worker.execute(*claimed)
                units.append({"kind": claimed[1]["kind"], "seconds": time.monotonic() - before})
                job = worker.repository.get_job(job_id)
                print(json.dumps({"stage": job["stage"], "processed_pairs": job["summary"]["processed_pairs"], "seconds": round(time.monotonic() - started, 2)}), flush=True)
                if job["status"] not in {"queued", "running", "cancelling"}:
                    break
        assert job["status"] == "completed", job.get("message")
        assert job["summary"]["processed_pairs"] == args.rows**2
        assert job["summary"]["pair_statuses"] == {"success": args.rows**2}
        archive = client.get(f"{BASE_PATH}/jobs/{job_id}/artifacts/results.zip")
        archive.raise_for_status()
        (root / "results.zip").write_bytes(archive.content)
        excel = client.get(f"{BASE_PATH}/jobs/{job_id}/artifacts/results.xlsx")
        excel.raise_for_status()
        (root / "results.xlsx").write_bytes(excel.content)
        workbook = load_workbook(root / "results.xlsx", read_only=True)
        excel_candidates = sum(1 for row in workbook["results"].rows) - 1
        workbook.close()
        assert excel_candidates == job["summary"]["candidate_count"]
        report = {"rows_a": args.rows, "rows_b": args.rows, "unique_pairs": args.rows**2,
                  "preparation_seconds": preparation_seconds, "units": units, "worker_restarts": restart_count,
                  "status_p95_seconds": sorted(status_latencies)[int(len(status_latencies) * .95)] if status_latencies else None,
                  "single_request_seconds_during_batch": single_seconds if restart_count else None,
                  "elapsed_seconds_including_download_verification": time.monotonic() - started,
                  "child_peak_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                  "parent_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                  "csv_zip_bytes": len(archive.content), "xlsx_bytes": len(excel.content), "summary": job["summary"]}
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({key: value for key, value in report.items() if key not in {"summary", "units"}}, indent=2), flush=True)
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


if __name__ == "__main__":
    main()
