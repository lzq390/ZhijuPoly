#!/usr/bin/env python3
"""Run a production PMD reader only with an enforced read-only job mount."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import polymer_md_analysis_dev as analysis
from scripts import polymer_md_worker_dev as worker
from scripts.polymer_md_prod_contract import PRODUCTION_FIELDS, validate_production_binding, verify_source_binding

FIELDS = PRODUCTION_FIELDS | {"schema_version", "runtime_root", "release_root", "job_root", "spool_root", "socket_path"}
SPOOL_BUDGET_BYTES = 48 * 1024**3


def load_configuration(path):
    path = worker.absolute_path(str(path))
    values = worker.read_private_json(path)
    if (not isinstance(values, dict) or set(values) != FIELDS
            or type(values["schema_version"]) is not int or values["schema_version"] != 2):
        raise ValueError("analysis production configuration fields or schema version are invalid")
    paths = {key: worker.absolute_path(value) for key, value in values.items()
             if key not in {"schema_version", "environment", "operation_id", "source_sha"}}
    validate_production_binding(values)
    runtime = paths["runtime_root"]
    if (not path.is_relative_to(runtime) or paths["job_root"] != runtime / "jobs"
            or paths["spool_root"] != runtime / "analysis-spool"
            or paths["socket_path"] != paths["socket_root"] / "analysis.sock"):
        raise ValueError("analysis must use its operation's private jobs, spool and socket layout")
    if len(os.fsencode(paths["socket_path"])) > 107:
        raise ValueError("analysis Unix socket path is too long")
    for target in (runtime, paths["job_root"], paths["spool_root"], paths["socket_root"], path.parent):
        worker.owned(target, 0o700, directory=True)
    if paths["release_root"].is_relative_to(runtime) or runtime.is_relative_to(paths["release_root"]):
        raise ValueError("analysis immutable release must be separate from its mutable runtime")
    return values


def verify_configuration(values):
    release = Path(values["release_root"])
    manifest = analysis.verify(release)
    for path in release.rglob("*"):
        if path.is_file():
            info = path.lstat()
            if info.st_uid != os.getuid() or info.st_mode & 0o222 or path.suffix in {".pyc", ".pyo"}:
                raise ValueError("analysis frozen files must be owned immutable source files")
    verify_source_binding(values, [{"path": name, "sha256": digest} for name, digest in manifest["files"].items()])
    return manifest


def clean_environment(values, manifest):
    environment = {key: value for key, value in os.environ.items()
                   if key in {"HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ"}}
    environment.update(PATH="/usr/bin:/bin", PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
        POLYMER_MD_ENVIRONMENT="prod", NEXPOLY_OPERATION_ID=values["operation_id"], POLYMER_MD_SOURCE_SHA=values["source_sha"],
        POLYMER_MD_ANALYSIS_JOB_ROOT=values["job_root"], POLYMER_MD_ANALYSIS_SPOOL_ROOT=values["spool_root"],
        POLYMER_MD_ANALYSIS_SPOOL_BUDGET_BYTES=str(SPOOL_BUDGET_BYTES), POLYMER_MD_ANALYSIS_TIMEOUT_SECONDS="90",
        POLYMER_MD_ANALYSIS_PYTHON=manifest["python"], OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1",
        CUDA_VISIBLE_DEVICES="", NVIDIA_VISIBLE_DEVICES="none")
    return environment


def run_analysis(values, manifest):
    # A ReadOnlyPaths directive in a unit file is not proof that this host
    # supports user-service mount namespaces. Check the actual view before exec.
    if not os.statvfs(values["job_root"]).f_flag & os.ST_RDONLY:
        raise ValueError("production analysis jobs must be mounted read-only by the service manager")
    os.umask(0o077)
    os.chdir(Path(values["release_root"]) / "source")
    python = manifest["python"]
    os.execve(python, [python, "-B", "-s", "-m", "workers.polymer_md_analysis", "--uds", values["socket_path"]],
              clean_environment(values, manifest))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "run"))
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    values = load_configuration(args.config)
    manifest = verify_configuration(values)
    if args.action == "verify":
        print(json.dumps({"verified": True, "operation_id": values["operation_id"], "source_sha": values["source_sha"],
            "release_fingerprint": Path(values["release_root"]).name, "spool_budget_bytes": SPOOL_BUDGET_BYTES,
            "read_only_mount_verified": bool(os.statvfs(values["job_root"]).f_flag & os.ST_RDONLY)}, sort_keys=True))
        return 0
    run_analysis(values, manifest)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        raise SystemExit(1)
