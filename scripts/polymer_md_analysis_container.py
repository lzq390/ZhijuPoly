#!/usr/bin/env python3
"""Render a standalone immutable production reader container; never starts it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.polymer_md_analysis_prod import load_configuration


def render_compose(values, config_path, image):
    if not isinstance(image, str) or re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9./:_-]*@)?sha256:[0-9a-f]{64}", image) is None:
        raise ValueError("analysis container requires a sealed image digest or local image ID")
    manifest = json.loads((Path(values["release_root"]) / "manifest.json").read_text())
    expected_lock = {"path": "/opt/nexpoly-analysis/requirements.lock", "sha256": hashlib.sha256(
        (ROOT / "workers/polymer_md_analysis/requirements-runtime.lock").read_bytes()).hexdigest()}
    if manifest.get("python") != "/usr/local/bin/python3.11" or manifest.get("dependency_locks") != [expected_lock]:
        raise ValueError("analysis release must bind the container interpreter and its visible sealed dependency lock")
    mounts = []
    for name, read_only in (("runtime_root", True), ("source_root", True), ("release_root", True),
                            ("job_root", True), ("spool_root", False), ("socket_root", False)):
        path = values[name]
        mounts.append({"type": "bind", "source": path, "target": path, "read_only": read_only,
                       "bind": {"create_host_path": False}})
    return {"name": "nexpoly-p9000-analysis", "services": {"polymer-md-analysis": {
        "image": image, "pull_policy": "never", "user": f"{os.getuid()}:{os.getgid()}",
        "read_only": True, "network_mode": "none", "init": True, "restart": "unless-stopped",
        "cpus": 1.0, "mem_limit": 2 * 1024**3, "memswap_limit": 2 * 1024**3, "pids_limit": 64,
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
        "tmpfs": [f"/tmp:rw,noexec,nosuid,nodev,size=268435456,mode=700,uid={os.getuid()},gid={os.getgid()}"],
        "entrypoint": ["/usr/local/bin/python3.11", "-I", "-B"],
        "command": [str(Path(values["release_root"]) / "source/scripts/polymer_md_analysis_prod.py"), "run", "--config", str(config_path)],
        "environment": {"HOME": "/tmp", "POLYMER_MD_ENVIRONMENT": "prod", "CUDA_VISIBLE_DEVICES": "", "NVIDIA_VISIBLE_DEVICES": "none"},
        "volumes": mounts,
        "labels": {"io.nexpoly.operation": values["operation_id"], "org.opencontainers.image.revision": values["source_sha"]},
        "stop_grace_period": "15s",
    }}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--image", required=True)
    args = parser.parse_args(argv)
    values = load_configuration(args.config)
    print(json.dumps(render_compose(values, args.config, args.image), indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        raise SystemExit(1)
