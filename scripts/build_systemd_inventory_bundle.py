"""Export only fixed public source from one clean commit; never install."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

FILES = (
    "ops/__init__.py", "ops/gpu_broker/__init__.py", "ops/gpu_broker/broker.py",
    "ops/gpu_broker/server.py", "ops/gpu_broker/systemd_provider.py",
    "gpu_resource/__init__.py", "gpu_resource/client.py", "gpu_resource/transient_scope.py",
    "gpu_resource/systemd_inventory.py",
)
UNIT = "ops/systemd/nexpoly-gpu-systemd-inventory.service"


def build(root: Path) -> Path:
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args])
    if git("status", "--porcelain", "--untracked-files=no").strip():
        raise RuntimeError("bundle requires a clean tracked source commit")
    source = git("rev-parse", "HEAD").decode().strip()
    tree = git("rev-parse", "HEAD^{tree}").decode().strip()
    output = root / ".runtime/systemd-inventory-bundles" / source
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    hashes = {}
    for relative in FILES:
        body = git("show", source + ":" + relative)
        destination = output / "package" / relative
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        destination.write_bytes(body)
        destination.chmod(0o600)
        hashes[relative] = hashlib.sha256(body).hexdigest()
    manifest = {"schema_version": 1, "source_sha": source, "source_tree": tree, "files": hashes}
    (output / "package/manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    (output / "package/manifest.json").chmod(0o600)
    (output / "nexpoly-gpu-systemd-inventory.service").write_bytes(git("show", source + ":" + UNIT))
    (output / "nexpoly-gpu-systemd-inventory.service").chmod(0o600)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = build(Path(__file__).resolve().parents[1])
    print(json.dumps({"status": "public_bundle_only_not_installed", "path": str(result)}))
