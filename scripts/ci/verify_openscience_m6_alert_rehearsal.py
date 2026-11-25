"""Exercise watermark delivery on a disposable offline VM, using its real monitor and quota pool."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

import verify_openscience_m5_quota as vm


def drill(base, modules):
    report = vm.ROOT / "docs/verification/openscience-m6-ac22-20261124.json"
    bindings = json.loads(report.read_bytes())["runtime"]["dev-m1"]
    with TemporaryDirectory(prefix="openscience-m6-alert-") as tmp:
        work = Path(tmp)
        if shutil.disk_usage(work).free < 36 * vm.GIB:
            raise ValueError("Insufficient disk space including development reserve")
        inputs = work / "inputs"
        inputs.mkdir()
        users = {
            k: {
                f: bindings["test-user-" + k][f]
                for f in ("environment", "user_id", "workspace", "uid", "gid")
            }
            for k in "ab"
        }
        (inputs / "spec.json").write_text(
            json.dumps({"fixture": "openscience-m6-alert-drill-v1", "users": users})
        )
        ops = vm.ROOT / "ops/openscience-per-user-dev"
        for name in (
            "water_monitor.py",
            "water_pool.py",
            "water_alerts.py",
            "quota.py",
            "quota_nfs.py",
        ):
            shutil.copyfile(ops / name, inputs / name)
        shutil.copyfile(
            vm.ROOT / "scripts/openscience_per_user_dev.py",
            inputs / "openscience_per_user_dev.py",
        )
        shutil.copyfile(
            Path(__file__).with_name("openscience_m6_alert_guest.py"),
            inputs / "guest.py",
        )
        return vm.run_vm(
            base, work, inputs, modules, data_disks={"m5-waterpool": 768 * 1024**2}
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", type=Path, default=vm.default_image())
    parser.add_argument("--quota-modules", type=Path, default=vm.DEFAULT_MODULES)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output file")
    os.umask(0o077)
    started = datetime.now(timezone.utc).isoformat()
    result = drill(args.base_image.resolve(), args.quota_modules)
    result.update(
        started=started,
        finished=datetime.now(timezone.utc).isoformat(),
        temporary_disks_removed=True,
    )
    with args.output.open("x") as out:
        json.dump(result, out, indent=2)
        out.write("\n")
