from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "openscience_mcp_dev.py"
SPEC = importlib.util.spec_from_file_location("openscience_mcp_dev", SOURCE)
dev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dev)


@contextlib.contextmanager
def local_server(respond):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, headers, payload = respond(self.path)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class OpenScienceDevelopmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.root = Path(temporary.name)
        self.runtime = self.root / ".runtime/openscience-mcp-dev"
        self.addCleanup(temporary.cleanup)
        for name, value in (("ROOT", self.root), ("RUNTIME", self.runtime)):
            mocked = patch.object(dev, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def source(self):
        return {"provider": {dev.PROVIDER: {"name": "Test model", "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": "https://api.relayrouter.ai/v1", "apiKey": "private-model-key"},
                "models": {"large": {}, "small": {}}}, "other-provider": {}},
                "model": dev.PROVIDER + "/large", "small_model": dev.PROVIDER + "/small",
                "plugin": ["do-not-copy"], "permission": "allow", "billing": {"llm": "managed"},
                "mcp": {"unrelated": {"type": "local", "command": ["do-not-copy"]}}}

    def credential(self, *, url="http://127.0.0.1:18110/mcp"):
        directory = self.root / ".runtime/property-filter-mcp"
        directory.mkdir(parents=True, mode=0o700)
        path = directory / "client.env"
        path.write_text("NEXPOLY_MCP_URL=" + url + "\nNEXPOLY_MCP_TOKEN=" + "a" * 48 + "\n")
        path.chmod(0o600)
        return path

    def owned_container(self):
        return {"Config": {"Labels": {"com.docker.compose.project": dev.PROJECT,
                    "com.docker.compose.service": "openscience-backend",
                    "com.docker.compose.project.working_dir": str(self.root),
                    "com.docker.compose.project.config_files": str(self.root / dev.COMPOSE_FILE)}},
                "State": {"Running": True}, "Mounts": [
                    {"Destination": destination, "Type": "bind", "Source": str(self.runtime / name),
                     "RW": name in dev.DATA_DIRS}
                    for name, destination in {**dev.DATA_DIRS, **dev.PRIVATE_FILES}.items()]}

    def test_jsonc_preserves_urls_escaped_quotes_and_comment_characters(self):
        raw = r'''{
          // comment outside the string
          "url": "https://example.invalid/path/*literal*/", /* block comment */
          "text": "a\" // quoted", "list": [1, 2,],
        }'''
        value = dev.parse_jsonc(raw)
        self.assertEqual(value["url"], "https://example.invalid/path/*literal*/")
        self.assertEqual(value["text"], 'a" // quoted')
        self.assertEqual(value["list"], [1, 2])

    def test_jsonc_rejects_duplicates_and_malformed_secret_without_echo(self):
        for raw in ('{"key":1,"key":2}', '{"private-key":"private-value",/*', '{private-value}'):
            with self.subTest(raw=raw), self.assertRaises(dev.DevError) as raised:
                dev.parse_jsonc(raw)
            self.assertNotIn("private-value", str(raised.exception))

    def test_model_copy_whitelists_provider_and_restricts_both_permission_layers(self):
        result = dev.configuration(self.source())
        self.assertNotIn("plugin", result)
        self.assertEqual(set(result["provider"]), {dev.PROVIDER})
        self.assertEqual(set(result["mcp"]), {dev.MCP_NAME})
        self.assertEqual(result["billing"], {"llm": "byok", "compute": "byok"})
        permission = result["permission"]
        self.assertEqual(permission["*"], "deny")
        self.assertEqual(permission["mcp"], {"*": "deny", **dict.fromkeys(dev.TOOL_KEYS, "allow")})
        self.assertEqual({key for key, value in permission.items() if value == "allow"}, set(dev.TOOL_KEYS))
        self.assertEqual(result["agent"]["property-filter"]["permission"], permission)
        self.assertEqual(result["mcp"][dev.MCP_NAME]["headers"]["Authorization"],
                         "Bearer {file:/run/openscience/mcp-token}")

    def test_private_model_key_override_changes_only_independent_provider_copy(self):
        source = self.source()
        before = json.dumps(source, sort_keys=True)
        self.assertEqual(dev.configuration(source)["provider"][dev.PROVIDER]["options"]["apiKey"],
                         "private-model-key")
        dev.private_directory()
        dev.write_private("model-api-key", "development-only-key")
        result = dev.configuration(source)
        self.assertEqual(result["provider"][dev.PROVIDER]["options"]["apiKey"], "development-only-key")
        self.assertEqual(json.dumps(source, sort_keys=True), before)
        self.assertIsNot(result["provider"][dev.PROVIDER], source["provider"][dev.PROVIDER])
        for ending in ("\n", "\r\n"):
            dev.write_private("model-api-key", "development-only-key" + ending)
            self.assertEqual(dev.configuration(source)["provider"][dev.PROVIDER]["options"]["apiKey"],
                             "development-only-key")

    def test_model_key_override_rejects_invalid_contents_without_echo(self):
        dev.private_directory()
        for key in ("", "short", "private-secret\nvalue", "private secret value", "private-secret-value\t",
                    "private-secret-value\x00", "private-secret-value\u00a0", "x" * 4097,
                    "{file:/run/openscience/mcp-token}", "{env:PRIVATE_MODEL_KEY}"):
            dev.write_private("model-api-key", key)
            with self.subTest(length=len(key)), self.assertRaises(dev.DevError) as raised:
                dev.configuration(self.source())
            self.assertNotIn("private-secret", str(raised.exception))

    def test_model_key_override_rejects_unsafe_file_permissions_and_symlink(self):
        dev.private_directory()
        dev.write_private("model-api-key", "development-only-key")
        path = self.runtime / "model-api-key"
        path.chmod(0o640)
        with self.assertRaises(dev.DevError):
            dev.configuration(self.source())
        path.unlink()
        target = self.root / "outside-key"
        target.write_text("outside-private-key")
        target.chmod(0o600)
        path.symlink_to(target)
        with self.assertRaises(dev.DevError):
            dev.configuration(self.source())
        target.unlink()
        with self.assertRaises(dev.DevError):
            dev.configuration(self.source())

    def test_model_key_change_requires_new_setup_and_refuses_running_stack(self):
        dev.private_directory()
        dev.write_private("protected-baseline.json", "{}")
        with patch.object(dev, "read_source", return_value=json.dumps(self.source()).encode()), \
                patch.object(dev, "mcp_token", return_value="a" * 48), \
                patch.object(dev, "verify_shared", return_value=({}, {})):
            for name, value in dev.prepared_files({}).items():
                dev.write_private(name, value)
            dev.verify_prepared()
            dev.write_private("model-api-key", "development-only-key")
            with self.assertRaises(dev.DevError):
                dev.verify_prepared()
            with patch.object(dev, "verify_project_ownership", return_value=True), self.assertRaises(dev.DevError):
                dev.setup()
            with patch.object(dev, "verify_project_ownership", return_value=False):
                dev.setup()
            dev.verify_prepared()
            prepared = json.loads(dev.read_private("openscience.json"))
            self.assertEqual(prepared["provider"][dev.PROVIDER]["options"]["apiKey"], "development-only-key")

    def test_copy_rejects_cross_provider_defaults_external_credentials_and_commands(self):
        for mutation in (lambda source: source.update(model="other-provider/model"),
                         lambda source: source["provider"][dev.PROVIDER]["options"].update(apiKey="{env:KEY}"),
                         lambda source: source["provider"][dev.PROVIDER]["options"].update(tokenCommand="run"),
                         lambda source: source["provider"][dev.PROVIDER].update(npm="unapproved-package")):
            source = self.source()
            mutation(source)
            with self.assertRaises(dev.DevError):
                dev.configuration(source)

    def test_model_endpoint_requires_exact_approved_https_host(self):
        for url in ("http://api.relayrouter.ai/v1", "https://other.invalid/v1",
                    "https://api.relayrouter.ai:444/v1", "https://token@api.relayrouter.ai/v1",
                    "https://api.relayrouter.ai.invalid/v1", "https://api.relayrouter.ai/v1?key=secret",
                    "https://api.relayrouter.ai/v1#fragment", "https://api.relayrouter.ai:bad/v1",
                    "https://api.relayrouter.ai/\nv1"):
            source = self.source()
            source["provider"][dev.PROVIDER]["options"]["baseURL"] = url
            with self.subTest(url=url), self.assertRaises(dev.DevError):
                dev.configuration(source)

    def test_runtime_credentials_are_private_and_reject_symlink(self):
        dev.private_directory()
        for name in dev.RUNTIME_DIRS:
            self.assertEqual(stat.S_IMODE((self.runtime / name).stat().st_mode), 0o700)
            self.assertEqual((self.runtime / name).stat().st_uid, os.getuid())
        dev.write_private("mcp-token", "private-token\n")
        self.assertEqual(stat.S_IMODE((self.runtime / "mcp-token").stat().st_mode), 0o600)
        target = self.root / "outside"
        target.write_text("original")
        (self.runtime / "openscience.json").symlink_to(target)
        with self.assertRaises(dev.DevError):
            dev.write_private("openscience.json", "overwritten")
        self.assertEqual(target.read_text(), "original")

    def test_runtime_symlink_is_rejected(self):
        (self.root / ".runtime").mkdir()
        self.runtime.symlink_to(self.root)
        with self.assertRaises(dev.DevError):
            dev.private_directory()

    def test_proxy_socket_directory_symlink_is_rejected(self):
        dev.private_directory()
        (self.runtime / "proxy-socket").rmdir()
        (self.runtime / "proxy-socket").symlink_to(self.root)
        with self.assertRaises(dev.DevError):
            dev.private_directory()

    def test_source_mcp_url_and_permission_must_match_exactly(self):
        path = self.credential(url="http://example.invalid/mcp")
        with self.assertRaises(dev.DevError):
            dev.mcp_token()
        path.write_text("NEXPOLY_MCP_URL=http://127.0.0.1:18110/mcp\nNEXPOLY_MCP_TOKEN=" + "a" * 48 + "\n")
        self.assertEqual(dev.mcp_token(), "a" * 48)
        path.chmod(0o640)
        with self.assertRaises(dev.DevError):
            dev.mcp_token()

    def test_mcp_directory_symlink_is_rejected(self):
        self.credential()
        directory = self.root / ".runtime/property-filter-mcp"
        moved = self.root / "credentials"
        directory.rename(moved)
        directory.symlink_to(moved)
        with self.assertRaises(dev.DevError):
            dev.mcp_token()

    def test_foreign_worktree_project_and_foreign_secret_mount_are_rejected(self):
        for mode in ("working_dir", "secret_mount", "readonly"):
            container = self.owned_container()
            if mode == "working_dir":
                container["Config"]["Labels"]["com.docker.compose.project.working_dir"] = "/other"
            elif mode == "secret_mount":
                container["Mounts"][-1]["Source"] = "/other/mcp-token"
            else:
                container["Mounts"][-1]["RW"] = True
            with patch.object(dev, "run", return_value="container\n"), \
                    patch.object(dev, "inspect", return_value=container), self.assertRaises(dev.DevError):
                dev.verify_project_ownership()

    def test_owned_project_is_accepted(self):
        with patch.object(dev, "run", return_value="container\n"), \
                patch.object(dev, "inspect", return_value=self.owned_container()):
            self.assertTrue(dev.verify_project_ownership())

    def test_ui_accepts_only_expected_temporary_filesystem(self):
        container = self.owned_container()
        container["Config"]["Labels"]["com.docker.compose.service"] = "openscience-ui"
        container["Mounts"] = [{"Type": "tmpfs", "Destination": "/tmp"}]
        with patch.object(dev, "run", return_value="container\n"), \
                patch.object(dev, "inspect", return_value=container):
            self.assertTrue(dev.verify_project_ownership())
            container["Mounts"][0]["Type"] = "bind"
            with self.assertRaises(dev.DevError):
                dev.verify_project_ownership()

    def test_both_relays_require_own_script_socket_directory_and_exact_network(self):
        for service in ("model-proxy", "model-egress"):
            for mutation in (None, "source", "writable", "network", "extra", "socket-source",
                             "socket-readonly", "extra-network", "published-port"):
                container = self.owned_container()
                container["Config"]["Labels"]["com.docker.compose.service"] = service
                network = "host" if service == "model-egress" else dev.DEV_NETWORK
                container["HostConfig"] = {"NetworkMode": network, "PortBindings": {}}
                container["NetworkSettings"] = {"Networks": {network: {}}}
                container["Mounts"] = [
                    {"Type": "bind", "Destination": "/app/model_proxy.py", "RW": False,
                     "Source": str(self.root / "ops/openscience-mcp-dev/model_proxy.py")},
                    {"Type": "bind", "Destination": "/run/openscience-proxy", "RW": True,
                     "Source": str(self.runtime / "proxy-socket")},
                ]
                if mutation == "source":
                    container["Mounts"][0]["Source"] = "/other/model_proxy.py"
                elif mutation == "writable":
                    container["Mounts"][0]["RW"] = True
                elif mutation == "network":
                    container["HostConfig"]["NetworkMode"] = "shared-network"
                elif mutation == "extra":
                    container["Mounts"].append({"Type": "bind", "Destination": "/state"})
                elif mutation == "socket-source":
                    container["Mounts"][1]["Source"] = "/other/proxy-socket"
                elif mutation == "socket-readonly":
                    container["Mounts"][1]["RW"] = False
                elif mutation == "extra-network":
                    container["NetworkSettings"]["Networks"]["shared-network"] = {}
                elif mutation == "published-port":
                    container["HostConfig"]["PortBindings"] = {"17892/tcp": [{"HostPort": "17892"}]}
                with patch.object(dev, "run", return_value="container\n"), \
                        patch.object(dev, "inspect", return_value=container), \
                        self.subTest(service=service, mutation=mutation):
                    if mutation is None:
                        self.assertTrue(dev.verify_project_ownership())
                    else:
                        with self.assertRaises(dev.DevError):
                            dev.verify_project_ownership()

    def test_development_network_absent_or_matching_is_accepted(self):
        with patch.object(dev, "run", return_value="") as command:
            dev.verify_development_network()
        self.assertEqual(command.call_count, 1)
        network = {"Driver": "bridge", "Labels": {"com.docker.compose.project": dev.PROJECT,
                   "com.docker.compose.network": "default"},
                   "IPAM": {"Config": [{"Subnet": dev.DEV_SUBNET, "Gateway": dev.DEV_GATEWAY}]}}
        with patch.object(dev, "run", side_effect=[dev.DEV_NETWORK + "\n", json.dumps([network])]):
            dev.verify_development_network()

    def test_development_network_rejects_foreign_project_or_wrong_ipam(self):
        for mutation in ("project", "subnet", "gateway", "range"):
            network = {"Driver": "bridge", "Labels": {"com.docker.compose.project": dev.PROJECT,
                       "com.docker.compose.network": "default"},
                       "IPAM": {"Config": [{"Subnet": dev.DEV_SUBNET, "Gateway": dev.DEV_GATEWAY}]}}
            if mutation == "project":
                network["Labels"]["com.docker.compose.project"] = "shared-stack"
            elif mutation == "subnet":
                network["IPAM"]["Config"][0]["Subnet"] = "172.30.0.0/16"
            elif mutation == "gateway":
                network["IPAM"]["Config"][0]["Gateway"] = "192.168.80.2"
            else:
                network["IPAM"]["Config"].append({"Subnet": "192.168.96.0/20"})
            with patch.object(dev, "run", side_effect=[dev.DEV_NETWORK + "\n", json.dumps([network])]), \
                    self.subTest(mutation=mutation), self.assertRaises(dev.DevError):
                dev.verify_development_network()

    def test_shared_image_mismatch_rejected_before_reading_source_files(self):
        bad = {"Image": "wrong-image", "Config": {"Labels": {}}}
        with patch.object(dev, "inspect", return_value=bad), patch.object(dev, "read_source") as source:
            with self.assertRaises(dev.DevError):
                dev.verify_shared()
        source.assert_not_called()

    def test_private_baseline_detects_protected_changes(self):
        dev.private_directory()
        dev.write_private("protected-baseline.json", '{"files":{"source":"sha256-a"}}')
        dev.protected_unchanged({"files": {"source": "sha256-a"}})
        with self.assertRaises(dev.DevError):
            dev.protected_unchanged({"files": {"source": "sha256-b"}})

    def test_container_fingerprint_ignores_mount_order_but_detects_mount_changes(self):
        container = self.owned_container()
        original = dev.container_fingerprint(container)
        mounts = container["Mounts"]
        container["Mounts"] = mounts[2:] + mounts[:2]
        self.assertEqual(dev.container_fingerprint(container), original)
        container["Mounts"] = list(reversed(mounts))
        self.assertEqual(dev.container_fingerprint(container), original)
        container["Mounts"][0]["Source"] = "/different/runtime"
        self.assertNotEqual(dev.container_fingerprint(container), original)

    def test_container_fingerprint_preserves_command_argument_order(self):
        container = self.owned_container()
        container["Config"]["Cmd"] = ["run", "first", "second"]
        original = dev.container_fingerprint(container)
        container["Config"]["Cmd"] = ["run", "second", "first"]
        self.assertNotEqual(dev.container_fingerprint(container), original)

    def test_setup_does_not_overwrite_credentials_of_running_stack(self):
        dev.private_directory()
        dev.write_private("mcp-token", "existing-secret")
        with patch.object(dev, "verify_project_ownership", return_value=True), \
                patch.object(dev, "verify_shared", return_value=({}, {})), \
                patch.object(dev, "prepared_files", return_value={"mcp-token": "new-secret"}):
            with self.assertRaises(dev.DevError):
                dev.setup()
        self.assertEqual(dev.read_private("mcp-token"), "existing-secret")

    def test_verify_prepared_rejects_local_config_or_token_retargeting(self):
        dev.private_directory()
        dev.write_private("protected-baseline.json", "{}")
        dev.write_private("mcp-token", "wrong-secret")
        with patch.object(dev, "verify_shared", return_value=({}, {})), \
                patch.object(dev, "prepared_files", return_value={"mcp-token": "expected-secret"}):
            with self.assertRaises(dev.DevError):
                dev.verify_prepared()

    def test_proxy_environment_is_fixed_to_dev_network_without_shared_values(self):
        value = dev.backend_environment({"Config": {"Env": ["DATABASE_PASSWORD=secret", "OPENAI_API_KEY=secret",
                    "HTTPS_PROXY=http://proxy.invalid:123", "NO_PROXY=old-service"]}})
        self.assertNotIn("secret", value)
        self.assertNotIn("old-service", value)
        self.assertNotIn("proxy.invalid", value)
        self.assertNotIn(dev.DEV_GATEWAY, value)
        self.assertIn(dev.MCP_HOST, value)
        self.assertIn("HTTPS_PROXY=" + dev.MODEL_PROXY_URL, value)
        self.assertEqual(dev.MODEL_PROXY_URL, "http://model-proxy:17892")

    def test_compose_ignores_other_project_and_environment_files(self):
        with patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": "production", "OPENSCIENCE_CONFIG": "/shared",
                                     "NEXPOLY_OPENSCIENCE_DEV_RUNTIME": "/other"}), \
                patch.object(dev, "run", return_value="") as command:
            dev.compose("status")
        argv = command.call_args.args[0]
        self.assertEqual(argv[argv.index("-p") + 1], dev.PROJECT)
        self.assertEqual(argv[argv.index("--env-file") + 1], "/dev/null")
        environment = command.call_args.kwargs["env"]
        self.assertNotIn("COMPOSE_PROJECT_NAME", environment)
        self.assertNotIn("OPENSCIENCE_CONFIG", environment)
        self.assertEqual(environment["NEXPOLY_OPENSCIENCE_DEV_RUNTIME"], str(self.runtime))

    def test_command_errors_do_not_echo_credentials(self):
        result = subprocess.CompletedProcess(["docker"], 1, "private-output", "private-error")
        with patch.object(dev.subprocess, "run", return_value=result), self.assertRaises(dev.DevError) as raised:
            dev.run(["docker", "inspect", "test"])
        self.assertNotIn("private-output", str(raised.exception))
        self.assertNotIn("private-error", str(raised.exception))

    def test_build_diagnostics_remain_private(self):
        def fail(command, **kwargs):
            kwargs["stdout"].write("private-diagnostic")
            return subprocess.CompletedProcess(command, 1)
        output = io.StringIO()
        with patch.object(dev.subprocess, "run", side_effect=fail), contextlib.redirect_stdout(output):
            with self.assertRaises(dev.DevError):
                dev.run(["docker", "compose", "up"], diagnostics="docker-up.log")
        self.assertNotIn("private-diagnostic", output.getvalue())
        self.assertEqual(dev.read_private("docker-up.log"), "private-diagnostic")

    def test_smoke_ignores_proxy_and_refuses_redirect(self):
        visits = []
        with local_server(lambda path: (visits.append(path) or (200, {}, b"proxy"))) as proxy:
            with local_server(lambda path: (200, {}, b"ok")) as target:
                with patch.object(dev, "API_URL", target), patch.dict(os.environ, {"HTTP_PROXY": proxy,
                        "http_proxy": proxy, "NO_PROXY": "", "no_proxy": ""}):
                    self.assertEqual(dev.get_local(target + "/health"), b"ok")
            with local_server(lambda path: (302, {"Location": proxy + "/unexpected"}, b"")) as target:
                with patch.object(dev, "API_URL", target), self.assertRaises(urllib.error.HTTPError):
                    dev.get_local(target + "/health")
        self.assertEqual(visits, [])

    def test_smoke_refuses_nonlocal_endpoint_before_network(self):
        with patch.object(dev.urllib.request, "build_opener") as opener, self.assertRaises(dev.DevError):
            dev.get_local("http://example.invalid/health")
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
