from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("test_openscience_overlay_image.sh", "test_openscience_bridge_browser.sh",
           "test_frontend_image_permissions.sh")
PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
PROXY = "http://fixture-user:fixture-secret@proxy.invalid:3128"

# This CLI double models Docker config.json proxy injection and explicit --env
# precedence. It never opens a socket or invokes the real Docker executable.
DOCKER = r'''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
path = Path(os.environ["FAKE_DOCKER_STATE"])
state = json.loads(path.read_text())
config = json.loads((Path(os.environ["DOCKER_CONFIG"]) / "config.json").read_text())
defaults = config["proxies"]["default"]
names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
call = {"args": args, "parent_proxy": {key: os.environ.get(key) for key in names},
        "client_proxy": defaults}
state["calls"].append(call)
output = ""
error = ""
status = 0
if args[0] in ("create", "run"):
    name = args[args.index("--name") + 1]
    env = {}
    for option, upper in (("httpProxy", "HTTP_PROXY"), ("httpsProxy", "HTTPS_PROXY"), ("allProxy", "ALL_PROXY")):
        env[upper] = env[upper.lower()] = defaults[option]
    for i, arg in enumerate(args[:-1]):
        if arg in ("--env", "-e"):
            key, value = args[i + 1].split("=", 1)
            env[key] = value
    state["containers"][name] = {"env": env, "started": args[0] == "run"}
    call["runtime_env"] = env
    output = name + "\n"
    if args[0] == "run" and os.environ.get("FAKE_START_FAIL") == "1":
        error, status = "fixture container startup failed\n", 1
elif args[0] == "start":
    state["containers"][args[1]]["started"] = True
    if os.environ.get("FAKE_START_FAIL") == "1":
        error, status = "fixture container startup failed\n", 1
elif args[0] == "inspect":
    name = args[1]
    container = state["containers"][name]
    clean = all(container["env"].get(key) == "" for key in names)
    health = os.environ.get("FAKE_HEALTH", "healthy") if clean else "unhealthy"
    if "json .State.Health" in args[-1]:
        output = "status=running exit=0 health=" + json.dumps({"Status": health, "Log": [{"Output": "local readiness fixture"}]}) + "\n"
    elif args[-1].startswith("status="):
        output = "status=running health=" + health + "\n"
    else:
        output = health + "\n"
elif args[0] == "exec":
    env = state["containers"][args[1]]["env"]
    if any(env.get(key) for key in names) or os.environ.get("FAKE_HTTP", "ok") == "fail":
        error, status = "fixture loopback request failed\n", 1
    elif os.environ.get("FAKE_HTTP", "ok") != "empty":
        output = "<html>local fixture</html>\n"
elif args[0] == "logs":
    output = "nginx fixture diagnostics\n"
elif args[0] not in ("pull", "build", "rm"):
    error, status = "unexpected fixture command\n", 99
path.write_text(json.dumps(state))
sys.stdout.write(output)
sys.stderr.write(error)
raise SystemExit(status)
'''


class LocalRuntimeProxyTests(unittest.TestCase):
    def exercise(self, script: str, *, omit_overrides: bool = False,
                 health: str = "healthy", http: str = "ok", start_fail: bool = False,
                 existing_target: bool = False, body_override: str | None = None):
        source = (ROOT / "scripts/ci" / script).read_text()
        array = re.search(r"(?ms)^readonly -a LOCAL_RUNTIME_PROXY_ARGS=\(\n.*?^\)", source)
        self.assertIsNotNone(array)
        # Execute the production functions with a CLI double. Image content
        # checks remain in the scripts and are exercised by the real CI job.
        functions = re.findall(r"(?ms)^(\w+\(\) \{\n.*?^\})", source)
        functions = [body for body in functions if not body.startswith("cleanup()")]
        trap = next(line for line in source.splitlines() if line.startswith("trap ") and line.endswith(" ERR"))
        function_source = "\n".join(functions)
        if omit_overrides:
            function_source = function_source.replace('"${LOCAL_RUNTIME_PROXY_ARGS[@]}"', "")
        with tempfile.TemporaryDirectory(prefix="nexpoly-local-runtime-proxy-") as directory:
            root = Path(directory)
            binary = root / "bin"
            binary.mkdir()
            (binary / "docker").write_text(DOCKER)
            (binary / "docker").chmod(0o755)
            config = root / "docker-config"
            config.mkdir()
            original_config = json.dumps({"proxies": {"default": {
                "httpProxy": PROXY, "httpsProxy": PROXY, "allProxy": PROXY,
                "noProxy": "localhost,127.0.0.1,::1",
            }}})
            (config / "config.json").write_text(original_config)
            state = root / "state.json"
            containers = {"fixture-local": {"env": dict.fromkeys(PROXY_NAMES, PROXY), "started": True}} if existing_target else {}
            state.write_text(json.dumps({"calls": [], "containers": containers}))
            environment = {**os.environ, **dict.fromkeys(PROXY_NAMES, PROXY),
                           "DOCKER_CONFIG": str(config), "FAKE_DOCKER_STATE": str(state),
                           "FAKE_HEALTH": health, "FAKE_HTTP": http,
                           "FAKE_START_FAIL": "1" if start_fail else "0",
                           "PATH": str(binary) + os.pathsep + os.defpath}
            variables = {
                "CANDIDATE_IMAGE": "fixture-candidate", "IMAGE_TAG": "fixture-candidate",
                "CANDIDATE_CONTAINER": "fixture-local", "RUNTIME_CONTAINER": "fixture-local",
                "BROWSER_CONTAINER": "fixture-browser", "PLAYWRIGHT_IMAGE": "fixture-playwright",
                "REPOSITORY_ROOT": str(root), "STAGING": str(root),
                "INDEX_FILE": str(root / "index.html"), "RUNTIME_ATTEMPTED": "0",
                "REMOVE_CANDIDATE": "0" if existing_target else "1", "VERIFY_STAGE": "fixture",
            }
            initialization = "\n".join(f"{key}={shlex.quote(value)}" for key, value in variables.items())
            body = "docker pull fixture-base >/dev/null\ndocker build fixture-context >/dev/null\n"
            if existing_target:
                body += "RUNTIME_ATTEMPTED=1\nwait_for_local_candidate\n"
            else:
                if "create_local_candidate()" in function_source:
                    body += "create_local_candidate\n"
                body += "start_local_candidate\nwait_for_local_candidate\n"
            if "run_local_browser_probe()" in function_source:
                body += "run_local_browser_probe\n"
            if body_override is not None:
                body = body_override
            harness = ("set -Eeuo pipefail\n" + array.group(0) + "\n" + function_source + "\n"
                       + initialization + "\nsleep() { :; }\n" + trap + "\n" + body)
            result = subprocess.run(["bash", "-c", harness], env=environment,
                                    capture_output=True, text=True, timeout=12)
            self.assertEqual((config / "config.json").read_text(), original_config)
            record = json.loads(state.read_text())
            return result, record

    def test_only_local_runtime_receives_empty_proxy_overrides(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                result, record = self.exercise(script)
                self.assertEqual(result.returncode, 0, result.stderr)
                runtime_calls = [call for call in record["calls"] if call["args"][0] in ("create", "run")]
                self.assertTrue(runtime_calls)
                for call in runtime_calls:
                    self.assertEqual(call["runtime_env"], dict.fromkeys(PROXY_NAMES, ""))
                for call in record["calls"]:
                    self.assertEqual(call["parent_proxy"], dict.fromkeys(PROXY_NAMES, PROXY))
                    self.assertEqual(call["client_proxy"]["httpProxy"], PROXY)
                self.assertEqual([call["args"][0] for call in record["calls"][:2]], ["pull", "build"])
                if "bridge_browser" in script:
                    browser = next(call for call in runtime_calls if "fixture-browser" in call["args"])
                    self.assertIn("container:fixture-local", browser["args"])
                    self.assertIn("/work:ro", " ".join(browser["args"]))

    def test_missing_runtime_overrides_reproduces_failure_and_diagnostics(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                result, record = self.exercise(script, omit_overrides=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("verification failed at container-", result.stderr)
                self.assertIn("health=", result.stderr)
                self.assertIn("nginx fixture diagnostics", result.stderr)
                self.assertNotIn("fixture-secret", result.stderr)
                self.assertTrue(any(call["args"][0] == "logs" for call in record["calls"]))

    def test_health_and_http_readiness_remain_bounded_with_last_state_diagnostics(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                result, record = self.exercise(script, health="starting", http="fail")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("readiness deadline", result.stderr)
                self.assertIn("verification failed at container-", result.stderr)
                self.assertIn("local readiness fixture", result.stderr)
                self.assertIn("nginx fixture diagnostics", result.stderr)
                polls = [call for call in record["calls"] if call["args"][0] in ("inspect", "exec")]
                self.assertLessEqual(len(polls), 31)
                self.assertNotIn("fixture-secret", result.stderr)

    def test_start_failure_reports_container_state_and_recent_logs(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                result, record = self.exercise(script, start_fail=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("verification failed at container-start", result.stderr)
                self.assertIn("health=", result.stderr)
                logs = [call for call in record["calls"] if call["args"][0] == "logs"]
                self.assertEqual(logs[0]["args"][1:3], ["--tail", "50"])
                self.assertNotIn("fixture-secret", result.stderr)

    def test_existing_browser_target_is_not_recreated_or_its_logs_dumped(self):
        source = (ROOT / "scripts/ci/test_openscience_bridge_browser.sh").read_text()
        self.assertRegex(source, r"(?s)if \(\( REMOVE_CANDIDATE == 1 \)\); then\n  create_local_candidate\n  start_local_candidate\nfi")
        result, record = self.exercise("test_openscience_bridge_browser.sh", health="unhealthy", existing_target=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("status=running health=unhealthy", result.stderr)
        self.assertFalse(any(call["args"][0] == "logs" for call in record["calls"]))
        self.assertFalse(any(call["args"][0] in ("create", "start", "run") for call in record["calls"]))
        self.assertEqual(record["containers"]["fixture-local"]["env"], dict.fromkeys(PROXY_NAMES, PROXY))

    def test_frontend_empty_response_does_not_satisfy_readiness(self):
        result, _ = self.exercise("test_frontend_image_permissions.sh", http="empty")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nonempty index", result.stderr)

    def test_expected_zero_match_pipeline_is_quiet_but_parent_assertion_still_fails(self):
        zero_matches = '"$(printf unchanged | grep missing | wc -l)"'
        for script in SCRIPTS:
            with self.subTest(script=script):
                result, _ = self.exercise(script, body_override=f'test {zero_matches} -eq 0\nprintf "assertion passed\\n"\n')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "assertion passed\n")
                self.assertNotIn("verification failed", result.stderr)
                result, _ = self.exercise(script, body_override=f'VERIFY_STAGE=static-policy\ntest {zero_matches} -eq 1\n')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr.count("verification failed at static-policy"), 1)

    def test_failed_command_substitution_reports_actual_parent_failure_once(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                result, _ = self.exercise(script, body_override='VERIFY_STAGE=runtime-inspection\nvalue="$(false)"\n')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr.count("verification failed at runtime-inspection"), 1)


if __name__ == "__main__":
    unittest.main()
