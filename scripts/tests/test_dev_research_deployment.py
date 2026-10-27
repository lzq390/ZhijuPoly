from __future__ import annotations

import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "scripts/dev_server_gpu.sh").read_text()


def function(name):
    start = re.search(r"(?m)^" + re.escape(name) + r"\(\) \{", SOURCE).start()
    return SOURCE[start:SOURCE.index("\n}\n", start)+3]


class ResearchDeploymentTests(unittest.TestCase):
    def private_env(self, root, text=None):
        path = root / "research.env"
        path.write_text(text if text is not None else (
            "POLYMER_MD_SOCKET_DIR=/tmp/synthetic-pmd-socket\n"
            "POLYMER_MD_JOB_ROOT=/tmp/synthetic-pmd-jobs\n"
            "POLYMER_MD_START_AUTHORIZATION_TOKEN=synthetic-token\n"
            "POLYMER_MD_SUBMIT_ENABLED=false\n"
            "LITERATURE_MINIO_ENDPOINT=host.docker.internal:15983\n"
            "LITERATURE_MINIO_ACCESS_KEY=synthetic-access\n"
            "LITERATURE_MINIO_SECRET_KEY=synthetic-secret\n"
            "LITERATURE_MINIO_SECURE=false\n"
        ))
        path.chmod(0o600)
        return path

    def load(self, path, *, pmd="true", literature="true", registry="false", extra="", suffix=""):
        harness = f'''
set -euo pipefail
NATIVE_COHORT_CONFIG_INPUT=""
NEXPOLY_DEV_USER_ISOLATION_ENABLED=true
NEXPOLY_DEV_POLYMER_MD_ENABLED="$2"
NEXPOLY_DEV_LITERATURE_ENABLED="$3"
NEXPOLY_DEV_POLYMER_MD_REGISTRY_ENABLED="$4"
NEXPOLY_DEV_RESEARCH_ENV_FILE="$1"
{extra}
{function("load_research_environment")}
load_research_environment
{suffix or 'printf "%s\\n" "$NEXPOLY_DEV_SCHEMA_TARGET" "${RESEARCH_BUILD_CONFIG_FILES[@]}"'}
'''
        return subprocess.run(["bash", "-c", harness, "research-test", str(path), pmd, literature, registry],
                              text=True, capture_output=True, timeout=10)

    def test_explicit_selection_and_highest_schema_boundary(self):
        with tempfile.TemporaryDirectory() as raw:
            path = self.private_env(Path(raw))
            for pmd, literature, target in [("false", "false", "user-isolation-current"),
                    ("true", "false", "polymer-md-0023"), ("false", "true", "literature-0024"),
                    ("true", "true", "literature-0024")]:
                result = self.load(path, pmd=pmd, literature=literature)
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = result.stdout.splitlines()
                self.assertEqual(lines[0], target)
                self.assertEqual("docker-compose.polymer-md-dev.yml" in lines, pmd == "true")
                self.assertEqual("docker-compose.literature-dev.yml" in lines, literature == "true")
            self.assertEqual(self.load(path, pmd="invalid").returncode, 2)
            self.assertEqual(self.load(path, pmd="false", registry="true").returncode, 2)
            self.assertEqual(self.load(path, extra="NATIVE_COHORT_CONFIG_INPUT=/synthetic/cohort").returncode, 2)
            self.assertEqual(self.load(path, extra="USAGE_ENTITLEMENTS_ENABLED=true").returncode, 2)
            base = path.read_text()
            for uid, gid, code in [("1001","1001",0),("0","0",2),("1002","1001",2),("1001","0",2)]:
                path = self.private_env(Path(raw), base + "POLYMER_MD_ACCEPTANCE_HOST_ROOT=/synthetic/registry\n"
                    +f"POLYMER_MD_ACCEPTANCE_UID={uid}\nPOLYMER_MD_ACCEPTANCE_GID={gid}\n")
                result = self.load(path, registry="true")
                self.assertEqual(result.returncode, code, result.stderr)

    def test_private_literal_configuration_rejects_credentials_from_other_stacks(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for text in ["APP_POSTGRES_DSN=must-not-be-imported\n",
                         "LITERATURE_MINIO_ENDPOINT=a\nLITERATURE_MINIO_ENDPOINT=b\n"]:
                result = self.load(self.private_env(root, text))
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("must-not-be-imported", result.stderr)
            path = self.private_env(root)
            path.chmod(0o644)
            self.assertEqual(self.load(path).returncode, 2)
            path.chmod(0o600)
            link = root / "linked.env"
            link.symlink_to(path)
            self.assertEqual(self.load(link).returncode, 2)
            path = self.private_env(root, "LITERATURE_MINIO_ENDPOINT=synthetic\n")
            self.assertEqual(self.load(path, extra="export POLYMER_MD_SOCKET_DIR=/old; export POLYMER_MD_JOB_ROOT=/old; export POLYMER_MD_START_AUTHORIZATION_TOKEN=old").returncode, 2)
            marker = root / "not-executed"
            text = self.private_env(root).read_text().replace("synthetic-secret", f"$(touch {marker})")
            result = self.load(self.private_env(root, text))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())
            self.assertEqual(self.load(path, extra='NEXPOLY_DEV_EXPECTED_RESEARCH_CONFIG_SHA256=sha256:stale').returncode, 2)

    def test_explicit_launch_inputs_survive_sourced_dotenv_defaults(self):
        with tempfile.TemporaryDirectory() as raw:
            path = self.private_env(Path(raw))
            capture = SOURCE[SOURCE.index("declare -A RESEARCH_LAUNCH_INPUTS=()"):SOURCE.index("readonly NATIVE_COHORT_CONFIG_INPUT=")]
            extra = capture + '''
# Simulate the later sourced configuration disagreeing with explicit inputs.
NEXPOLY_DEV_POLYMER_MD_ENABLED=false
NEXPOLY_DEV_LITERATURE_ENABLED=false
NEXPOLY_DEV_RESEARCH_ENV_FILE=/missing/dotenv-default.env
'''
            result = self.load(path, extra=extra)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines()[0], "literature-0024")
            result = self.load(path, pmd="false", literature="false",
                extra="NEXPOLY_DEV_EXPECTED_RESEARCH_CONFIG_SHA256=sha256:stale")
            self.assertEqual(result.returncode, 2)

    def test_build_command_only_builds_and_verifies_image_identity(self):
        branch = re.search(r"(?ms)^  build-backend\)\n(.*?)^    ;;", SOURCE).group(1)
        result = subprocess.run(["bash", "-c", '''
set -euo pipefail
build_backend_image() { printf 'build-clean-candidate\\n'; }
verify_backend_image_build_identity() { printf 'verify-image-identity\\n'; }
''' + branch], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["build-clean-candidate", "verify-image-identity"])

    def test_operator_reuse_requires_the_same_research_selectors_and_digest(self):
        with tempfile.TemporaryDirectory() as raw:
            path=Path(raw)/"identity.json"
            research={"NEXPOLY_DEV_POLYMER_MD_ENABLED":"true", "NEXPOLY_DEV_LITERATURE_ENABLED":"true",
                "NEXPOLY_DEV_POLYMER_MD_REGISTRY_ENABLED":"false", "NEXPOLY_DEV_RESEARCH_ENV_FILE":"/private/research.env",
                "NEXPOLY_DEV_RESEARCH_CONFIG_SHA256":"sha256:"+"a"*64}
            environment=dict(os.environ,**research)
            harness=f'''
set -euo pipefail
GPU_OPERATOR_IDENTITY="$1"
CURRENT_SOURCE_REVISION=revision; CURRENT_SOURCE_TREE=tree
NEXPOLY_DEV_USER_ISOLATION_ENABLED=true
gpu_operator_request() {{ printf '%s' '{{"schema_version":1,"operator_available":true,"source_sha":"revision","source_tree":"tree"}}'; }}
{function("gpu_operator_status_is_current")}
gpu_operator_status_is_current
'''
            for recorded,expected in [(None,False),({**research,"NEXPOLY_DEV_RESEARCH_CONFIG_SHA256":"sha256:"+"b"*64},False),(research,True)]:
                identity={"user_isolation_enabled":True}
                if recorded is not None:identity["research_configuration"]=recorded
                path.write_text(json.dumps(identity))
                result=subprocess.run(["bash","-c",harness,"operator-reuse",str(path)],env=environment,text=True,capture_output=True,timeout=10)
                self.assertEqual(result.returncode==0,expected,result.stderr)
            start=function("gpu_operator_up")
            for key in research:self.assertIn('--setenv="'+key+'=',start)
            self.assertNotIn('--setenv="POLYMER_MD_START_AUTHORIZATION_TOKEN=',start)
            self.assertNotIn('--setenv="LITERATURE_MINIO_SECRET_KEY=',start)

    def test_selected_files_affect_build_fingerprint_and_both_compose_arrays(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            env = self.private_env(root)
            for name in ["Dockerfile", *[p.name for p in ROOT.glob("docker-compose*.yml")]]:
                (root / name).write_text(name + "\n")
            start = SOURCE.index("BACKEND_BUILD_CONFIG_FILES=(")
            end = SOURCE.index("\nfi\nNEXPOLY_BUILD_REVISION", start)
            arrays_start = SOURCE.index("DEV_COMPOSE_FILES=(-f")
            arrays_end = SOURCE.index("GPU_SESSION_CONTROLLER=", arrays_start)
            suffix = f'''
cd "$(dirname "$1")"
NEXPOLY_DEV_GPU_LAUNCHER_ENABLED=true
{SOURCE[start:end]}
printf '%s\\n' "$BACKEND_BUILD_CONFIG_SHA256"
{SOURCE[arrays_start:arrays_end]}
printf 'CPU:%s\\n' "${{COMPOSE[*]}}"
printf 'GPU:%s\\n' "${{GPU_COMPOSE[*]}}"
'''
            before = self.load(env, suffix=suffix)
            self.assertEqual(before.returncode, 0, before.stderr)
            for line in before.stdout.splitlines()[1:]:
                self.assertIn("docker-compose.polymer-md-dev.yml", line)
                self.assertIn("docker-compose.literature-dev.yml", line)
                self.assertIn("docker-compose.research-dev.yml", line)
            gpu = before.stdout.splitlines()[2]
            self.assertLess(gpu.index("docker-compose.dev-gpu-session.yml"), gpu.index("docker-compose.research-dev.yml"))
            (root / "docker-compose.literature-dev.yml").write_text("changed-config\n")
            after = self.load(env, suffix=suffix)
            self.assertEqual(after.returncode, 0, after.stderr)
            self.assertNotEqual(before.stdout.splitlines()[0], after.stdout.splitlines()[0])

    @unittest.skipUnless(shutil.which("docker"), "Compose parser unavailable")
    def test_cpu_and_gpu_render_both_features_with_readonly_external_mounts(self):
        values = dict(os.environ, NEXPOLY_ASSET_ROOT="/tmp/synthetic-assets",
            APP_POSTGRES_DSN="postgresql://api:synthetic@lab-postgres:5432/synthetic",
            APP_SERVICE_POSTGRES_DSN="postgresql://service:synthetic@lab-postgres:5432/synthetic",
            AUTH_POSTGRES_DSN="postgresql://auth:synthetic@lab-postgres:5432/synthetic",
            AUTH_ADMIN_POSTGRES_DSN="postgresql://admin:synthetic@lab-postgres:5432/synthetic",
            MONOMER_DFT_START_AUTHORIZATION_TOKEN="synthetic-dft",
            POLYMER_MD_SOCKET_DIR="/tmp/synthetic-socket", POLYMER_MD_JOB_ROOT="/tmp/synthetic-jobs",
            POLYMER_MD_START_AUTHORIZATION_TOKEN="synthetic-pmd", POLYMER_MD_SUBMIT_ENABLED="false",
            POLYMER_MD_ACCEPTANCE_HOST_ROOT="/tmp/synthetic-registry",
            POLYMER_MD_ACCEPTANCE_UID="1001", POLYMER_MD_ACCEPTANCE_GID="1001",
            LITERATURE_MINIO_ENDPOINT="host.docker.internal:15983", LITERATURE_MINIO_ACCESS_KEY="synthetic",
            LITERATURE_MINIO_SECRET_KEY="synthetic", LITERATURE_MINIO_SECURE="false",
            NEXPOLY_DEV_SCHEMA_TARGET="literature-0024", NEXPOLY_DEV_RESEARCH_CONFIG_SHA256="sha256:"+"a"*64,
            NEXPOLY_DEV_GPU_SESSION_ID="d"*32, NEXPOLY_GPU_STATE_ROOT="/tmp/synthetic-gpu-state",
            NEXPOLY_DEV_POLYMER_MD_ENABLED="true", NEXPOLY_DEV_LITERATURE_ENABLED="true")
        for gpu in (False, True):
            files = ["docker-compose.yml", "docker-compose.dev.yml", "docker-compose.user-isolation-dev.yml"]
            if gpu: files.append("docker-compose.dev-gpu-session.yml")
            files += ["docker-compose.polymer-md-dev.yml", "docker-compose.polymer-md-registry-dev.yml",
                      "docker-compose.literature-dev.yml", "docker-compose.research-dev.yml"]
            cmd = ["docker", "compose", "--env-file", "/dev/null", "-p", "synthetic_research_config"]
            for name in files: cmd += ["-f", str(ROOT / name)]
            result = subprocess.run(cmd+["config", "--format", "json", "--no-env-resolution"], env=values,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            backend = json.loads(result.stdout)["services"]["backend"]
            self.assertEqual(backend["user"], "1001:1001")
            env = backend["environment"]
            self.assertEqual(env["NEXPOLY_POSTGRES_PREFLIGHT_ARGS"], "--strict --schema-target literature-0024 --service-context")
            self.assertEqual(env["POLYMER_MD_DEV_ENABLED"], "true")
            self.assertEqual(env["KNOWLEDGE_LITERATURE_ENABLED"], "true")
            self.assertEqual(env["POLYMER_MD_SUBMIT_ENABLED"], "false")
            self.assertEqual(env["DEPLOYMENT_DRAIN_ENABLED"], "true")
            self.assertEqual(env["USAGE_ENTITLEMENTS_ENABLED"], "false")
            mounts = {m["target"]:m for m in backend["volumes"]}
            for target in ["/app/polymer-md-socket", "/app/polymer-md-artifacts", "/app/polymer-md-acceptance"]:
                self.assertTrue(mounts[target]["read_only"])
                self.assertFalse(mounts[target].get("bind", {}).get("create_host_path", False))

    def test_maintenance_start_keeps_traffic_stopped_and_resume_keeps_database_drained(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); log = root / "events"
            controller = root / "controller.py"
            controller.write_text('print(\'{"status":"stopped"}\')\n')
            funcs = "\n".join(function(n) for n in ["maintenance_start_prebuilt", "maintenance_resume", "up_prebuilt", "assert_maintenance_services_stopped"])
            harness = f'''
set -euo pipefail
EVENT_LOG="$1"; GPU_SESSION_PYTHON="$2"; GPU_SESSION_CONTROLLER="$3"
DRAIN_READY="$4"
NEXPOLY_DEV_POLYMER_MD_ENABLED=true
NEXPOLY_DEV_USER_ISOLATION_ENABLED=true
NEXPOLY_DEV_GPU_DIRECT_START=1
NEXPOLY_DEV_GPU_LAUNCHER_ENABLED=true
NEXPOLY_DEV_FRONTEND_PORT=9001
NEXPOLY_DEV_SCHEMA_TARGET=literature-0024
COMPOSE=(compose)
event() {{ printf '%s\\n' "$*" >> "$EVENT_LOG"; }}
compose() {{ if [[ "$1" == ps ]]; then return 0; fi; event "$*"; }}
require_deployment_drained() {{ event drain-verified; [[ "$DRAIN_READY" == true ]]; }}
verify_backend_image_build_identity() {{ event image-verified; }}
validate_asset_release() {{ :; }}
prepare_canary_state_directory() {{ :; }}
prepare_worker_runtime_directories() {{ :; }}
prepare_dft_runtime_directories() {{ :; }}
gpu_operator_up() {{ event operator-up; }}
wait_backend_configured() {{ :; }}
verify_backend_drift() {{ event cpu-verified; }}
gpu_session_status() {{ event native-session-verified; }}
{funcs}
case "$5" in start) maintenance_start_prebuilt ;; resume) maintenance_resume ;; esac
'''
            for action in ["start", "resume"]:
                log.unlink(missing_ok=True)
                result = subprocess.run(["bash", "-c", harness, "maintenance", str(log), sys.executable, str(controller), "true", action], text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                events = log.read_text().splitlines()
                if action == "start":
                    self.assertTrue(any("--schema-target literature-0024" in x for x in events))
                    self.assertTrue(any(x == "up -d --no-deps --force-recreate backend" for x in events))
                    self.assertFalse(any("frontend-dev" in x or "polymerization-batch-worker" in x for x in events))
                else:
                    self.assertEqual(events, ["drain-verified", "native-session-verified",
                        "up -d --no-deps --wait --wait-timeout 120 polymerization-batch-worker",
                        "up -d --no-deps frontend-dev", "drain-verified"])
            log.unlink()
            rejected = subprocess.run(["bash", "-c", harness, "maintenance", str(log), sys.executable,
                str(controller), "false", "start"], text=True, capture_output=True, timeout=10)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse(any("up -d" in line or "operator-up" in line for line in log.read_text().splitlines()))

    def test_runtime_drift_rejects_private_configuration_and_mount_changes_without_values(self):
        import copy
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            environment = {"POLYMER_MD_START_AUTHORIZATION_TOKEN":"synthetic-private-token",
                "LITERATURE_MINIO_SECRET_KEY":"synthetic-private-secret", "KNOWLEDGE_LITERATURE_ENABLED":"true",
                "DEPLOYMENT_DRAIN_ENABLED":"true", "USAGE_ENTITLEMENTS_ENABLED":"false",
                "NEXPOLY_POSTGRES_PREFLIGHT_ARGS":"--strict --schema-target literature-0024 --service-context"}
            label = "com.nexpoly.dev.research-config"
            volume = {"target":"/app/polymer-md-acceptance","source":"/synthetic/registry","read_only":True}
            expected = {"services":{"backend":{"environment":environment,"labels":{label:"sha256:synthetic"},
                        "user":"1001:1001","volumes":[volume]}}}
            actual = {"Config":{"Env":[k+"="+v for k,v in environment.items()],
                                 "Labels":{label:"sha256:synthetic"},"User":"1001:1001"},
                      "Mounts":[{"Destination":volume["target"],"Source":volume["source"],"RW":False}]}
            config = root / "config.json"; inspect = root / "inspect.json"
            config.write_text(json.dumps(expected))
            harness = f'''
set -euo pipefail
EXPECTED="$1"; ACTUAL="$2"
NEXPOLY_DEV_POLYMER_MD_ENABLED=true
NEXPOLY_DEV_LITERATURE_ENABLED=true
COMPOSE=(compose); GPU_COMPOSE=(compose)
compose() {{ cat "$EXPECTED"; }}
docker() {{ [[ "$1" == inspect ]]; cat "$ACTUAL"; }}
{function("verify_research_runtime")}
verify_research_runtime synthetic "$3"
'''
            cases = [("unchanged", None), ("secret", lambda x:x["Config"]["Env"].append("LITERATURE_MINIO_SECRET_KEY=changed-private-secret")),
                ("unexpected",lambda x:x["Config"]["Env"].append("POLYMER_MD_UNEXPECTED=changed-private-secret")),
                ("schema",lambda x:x["Config"]["Env"].append("NEXPOLY_POSTGRES_PREFLIGHT_ARGS=--strict")),
                ("identity",lambda x:x["Config"]["Labels"].update({label:"changed"})),
                ("mount",lambda x:x["Mounts"][0].update(RW=True)),
                ("uid",lambda x:x["Config"].update(User="0:0"))]
            for mode in ["cpu","gpu"]:
                for name, mutate in cases:
                    with self.subTest(mode=mode,change=name):
                        value=copy.deepcopy(actual)
                        if mutate:mutate(value)
                        inspect.write_text(json.dumps([value]))
                        result=subprocess.run(["bash","-c",harness,"drift-test",str(config),str(inspect),mode],
                                              text=True,capture_output=True,timeout=10)
                        self.assertEqual(result.returncode==0, mutate is None, result.stderr)
                        self.assertNotIn("private-token",result.stderr)
                        self.assertNotIn("private-secret",result.stderr)


if __name__ == "__main__":
    unittest.main()
