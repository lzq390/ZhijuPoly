from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
                "plugin": ["do-not-copy"], "permission": {"bash": "ask"}, "billing": {"llm": "byok"},
                "agent": {"research": {"permission": {"mcp": {"capability_gateway_dft_job_submit": "ask"}}}},
                "mcp": {"knowledge_service": {"type": "remote", "url": dev.SHARED_MCP_ENDPOINTS["knowledge_service"],
                            "headers": {"Authorization": "Bearer private-knowledge-token"}},
                        "capability_gateway": {"type": "remote", "url": dev.SHARED_MCP_ENDPOINTS["capability_gateway"],
                            "headers": {"Authorization": "Bearer production-only-gateway-token"}},
                        "unrelated": {"type": "local", "command": ["do-not-copy"]}}}

    def production_gateway(self):
        return {"Id": "production-gateway", "Image": dev.GATEWAY_BASE_IMAGE, "Config": {
            "Labels": {"com.docker.compose.project": "capability-gateway",
                       "com.docker.compose.service": "capability-gateway",
                       "com.docker.compose.project.working_dir": str(dev.SHARED_GATEWAY_ROOT)},
            "Env": ["ZHIJU_BASE_URL=http://nexpoly_dev-backend-1:8000", "ZHIJU_ORIGIN=http://localhost:9001",
                    "ZHIJU_USERNAME=private-test-user", "ZHIJU_PASSWORD=private$test#password=literal",
                    "ZHIJU_CAPABILITY_TOKEN=production-only-gateway-token", "LISTEN_HOST=10.250.11.20",
                    "HTTP_PROXY=http://private-proxy:3128", "UNRELATED_SECRET=must-not-copy"]}, "Mounts": []}

    def owned_gateway(self):
        container = self.owned_container()
        container["Config"]["Labels"]["com.docker.compose.service"] = dev.GATEWAY_SERVICE
        container["Image"] = "development-gateway-image"
        container["HostConfig"] = {"ReadonlyRootfs": True, "PortBindings": {}, "PublishAllPorts": False}
        container["Mounts"] = [{"Type": "bind", "Source": str(self.runtime / "mcp-token"),
            "Destination": "/run/capability-gateway/property-filter-token", "RW": False},
            {"Type": "tmpfs", "Destination": "/tmp"}]
        container["NetworkSettings"] = {"Networks": {name: {"Aliases": [dev.GATEWAY_SERVICE]}
            for name in (dev.DEV_NETWORK, "nexpoly_mcp_clients_dev", "nexpoly_dev_default")}}
        return container

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

    def test_copy_preserves_shared_permissions_and_adds_restricted_filter_agent(self):
        result = dev.configuration(self.source())
        self.assertNotIn("plugin", result)
        self.assertEqual(set(result["provider"]), {dev.PROVIDER})
        self.assertEqual(set(result["mcp"]), {"knowledge_service", dev.MCP_NAME})
        self.assertEqual(result["billing"], {"llm": "byok"})
        self.assertEqual(result["default_agent"], "research")
        self.assertEqual(result["permission"]["bash"], "ask")
        self.assertNotIn("*", result["permission"])
        permission = result["agent"]["property-filter"]["permission"]
        self.assertEqual(permission["*"], "deny")
        self.assertEqual(permission["mcp"], {"*": "deny", **dict.fromkeys(dev.TOOL_KEYS, "allow")})
        self.assertEqual({key for key, value in permission.items() if value == "allow"}, set(dev.TOOL_KEYS))
        self.assertEqual(result["agent"]["research"]["permission"]["mcp"],
                         {"capability_gateway_dft_job_submit": "ask", **dict.fromkeys(dev.TOOL_KEYS, "allow")})
        self.assertEqual(result["mcp"][dev.MCP_NAME]["headers"]["Authorization"],
                         "Bearer {file:/run/openscience/gateway-token}")
        self.assertEqual(result["mcp"][dev.MCP_NAME]["url"], dev.GATEWAY_URL)
        self.assertEqual(set(result["permission"]) - {"bash"}, set(dev.TOOL_KEYS))
        self.assertTrue(all(key.startswith("capability_gateway_") for key in dev.TOOL_KEYS))
        encoded = json.dumps(result)
        for private in ("production-only-gateway-token", "nexpoly_property_filter_", dev.MCP_URL,
                        "/run/openscience/mcp-token"):
            self.assertNotIn(private, encoded)

    def test_shared_services_models_and_job_approval_are_copied_without_mutating_source(self):
        source = self.source()
        source["provider"]["deepseek-api"] = {"name": "Other model", "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": "http://183.162.245.47:11888/v1", "apiKey": "private-test-key"},
            "models": {"deepseek-v4-flash-agent": {}}}
        source["enabled_providers"] = [dev.PROVIDER, "deepseek-api"]
        source["mcp"].update({name: {"type": "remote", "url": endpoint,
            "headers": {"Authorization": "Bearer private-test-service-token"}, "oauth": False, "timeout": 30000}
            for name, endpoint in dev.SHARED_MCP_ENDPOINTS.items()})
        source["agent"] = {"research": {"permission": {"mcp": {"capability_gateway_dft_job_submit": "ask"}}}}
        before = json.loads(json.dumps(source))
        result = dev.configuration(source)
        self.assertEqual(source, before)
        self.assertEqual(result["provider"]["deepseek-api"], source["provider"]["deepseek-api"])
        self.assertEqual(set(result["mcp"]), {*dev.SHARED_MCP_ENDPOINTS, dev.MCP_NAME})
        self.assertEqual(result["agent"]["research"]["permission"]["mcp"]["capability_gateway_dft_job_submit"], "ask")
        self.assertNotIn("unrelated", result["mcp"])
        result["mcp"]["knowledge_service"]["headers"]["Authorization"] = "modified copy"
        self.assertEqual(source, before)

    def test_shared_service_copy_rejects_endpoint_or_authentication_retargeting(self):
        for url, auth in (("http://other-service:8082/mcp", "Bearer private-token"),
                          (dev.SHARED_MCP_ENDPOINTS["knowledge_service"], "Bearer {env:PRIVATE_KEY}")):
            source = self.source()
            source["mcp"]["knowledge_service"] = {"type": "remote", "url": url, "headers": {"Authorization": auth}}
            with self.subTest(url=url), self.assertRaises(dev.DevError):
                dev.configuration(source)

    def test_gateway_environment_uses_independent_auth_and_only_reviewed_connection_values(self):
        source = self.production_gateway()
        before = json.loads(json.dumps(source))
        token = "development-gateway-token-" + "a" * 32
        value = dev.gateway_environment(source, token)
        environment = dict(line.split("=", 1) for line in value.splitlines())
        expected = {"ZHIJU_BASE_URL", "ZHIJU_ORIGIN", "ZHIJU_USERNAME", "ZHIJU_PASSWORD",
                    "ZHIJU_CAPABILITY_TOKEN", "LISTEN_HOST", "PROPERTY_FILTER_MCP_URL",
                    "PROPERTY_FILTER_MCP_TOKEN_FILE", "PROPERTY_FILTER_MCP_TIMEOUT_MS"}
        self.assertEqual(set(environment), expected)
        self.assertEqual(environment["ZHIJU_CAPABILITY_TOKEN"], token)
        self.assertEqual(environment["LISTEN_HOST"], "0.0.0.0")
        self.assertEqual(environment["PROPERTY_FILTER_MCP_URL"], dev.MCP_URL)
        self.assertEqual(environment["PROPERTY_FILTER_MCP_TOKEN_FILE"], "/run/capability-gateway/property-filter-token")
        self.assertLess(int(environment["PROPERTY_FILTER_MCP_TIMEOUT_MS"]),
                        dev.configuration(self.source())["mcp"][dev.MCP_NAME]["timeout"])
        self.assertEqual(environment["ZHIJU_PASSWORD"], "private$test#password=literal")
        for private in ("production-only-gateway-token", "10.250.11.20", "must-not-copy", "private-proxy"):
            self.assertNotIn(private, value)
        self.assertEqual(source, before)

    def test_gateway_connection_rejects_retargeting_and_raw_environment_injection_without_echo(self):
        for name, value in ([("ZHIJU_BASE_URL", "http://other-backend:8000")]
                + [(name, value) for name in ("ZHIJU_BASE_URL", "ZHIJU_ORIGIN", "ZHIJU_USERNAME", "ZHIJU_PASSWORD")
                   for value in ("", "private-secret\nINJECTED=value", "private-secret\rvalue", "private-secret\0value")]):
            source = self.production_gateway()
            source["Config"]["Env"] = [item for item in source["Config"]["Env"] if not item.startswith(name + "=")]
            source["Config"]["Env"].append(name + "=" + value)
            with self.subTest(name=name, value=repr(value)), self.assertRaises(dev.DevError) as raised:
                dev.gateway_environment(source, "b" * 48)
            self.assertNotIn("private-secret", str(raised.exception))

    def test_gateway_source_requires_immutable_image_and_exact_shared_deployment_identity(self):
        for mutation in (None, "image", "project", "service", "working_dir"):
            source = self.production_gateway()
            if mutation == "image":
                source["Image"] = "other-image"
            elif mutation:
                label = {"project": "com.docker.compose.project", "service": "com.docker.compose.service",
                         "working_dir": "com.docker.compose.project.working_dir"}[mutation]
                source["Config"]["Labels"][label] = "other-deployment"
            with self.subTest(mutation=mutation), patch.object(dev, "inspect", return_value=source) as inspect:
                if mutation is None:
                    self.assertEqual(dev.gateway_source(), source)
                else:
                    with self.assertRaises(dev.DevError):
                        dev.gateway_source()
            inspect.assert_called_once_with(dev.SHARED_GATEWAY)

    def test_gateway_token_requires_private_file_and_url_safe_contents(self):
        dev.private_directory()
        valid = "a_b-" * 12
        dev.write_private("gateway-token", valid + "\n")
        self.assertEqual(dev.gateway_token(), valid)
        path = self.runtime / "gateway-token"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for token in ("", "short", "private-secret value", "private-secret\nvalue", "x" * 257):
            dev.write_private("gateway-token", token)
            with self.subTest(length=len(token)), self.assertRaises(dev.DevError) as raised:
                dev.gateway_token()
            self.assertNotIn("private-secret", str(raised.exception))
        dev.write_private("gateway-token", valid)
        path.chmod(0o640)
        with self.assertRaises(dev.DevError):
            dev.gateway_token()
        path.unlink()
        target = self.root / "outside-gateway-token"
        target.write_text(valid)
        target.chmod(0o600)
        path.symlink_to(target)
        with self.assertRaises(dev.DevError):
            dev.gateway_token()

    def test_prepared_files_separate_gateway_and_upstream_credentials(self):
        dev.private_directory()
        self.credential()
        dev.write_private("gateway-token", "b" * 48 + "\n")
        with patch.object(dev, "read_source", return_value=json.dumps(self.source()).encode()), \
                patch.object(dev, "gateway_source", return_value=self.production_gateway()):
            prepared = dev.prepared_files({})
        self.assertEqual(prepared["mcp-token"], "a" * 48 + "\n")
        self.assertEqual(prepared["gateway-token"], "b" * 48 + "\n")
        config = json.loads(prepared["openscience.json"])
        self.assertEqual(set(config["mcp"]), {"knowledge_service", "capability_gateway"})
        self.assertNotIn("a" * 48, prepared["gateway.env"])
        self.assertNotIn("a" * 48, prepared["openscience.json"])
        self.assertNotIn("production-only-gateway-token", json.dumps(prepared))
        self.assertEqual(dict(line.split("=", 1) for line in prepared["gateway.env"].splitlines())[
            "ZHIJU_CAPABILITY_TOKEN"], "b" * 48)
        self.assertNotIn("mcp-token", dev.PRIVATE_FILES)
        self.assertIn("gateway-token", dev.PRIVATE_FILES)

    def test_private_gateway_rejects_exposed_ports_shared_aliases_or_foreign_token_mounts(self):
        for mutation in (None, "token-source", "token-destination", "token-writable", "extra-mount",
                         "tmpfs", "missing-network", "extra-network", "production-alias", "port", "publish-all", "writable-root"):
            container = self.owned_gateway()
            if mutation == "token-source":
                container["Mounts"][0]["Source"] = "/shared/production-token"
            elif mutation == "token-destination":
                container["Mounts"][0]["Destination"] = "/other-token"
            elif mutation == "token-writable":
                container["Mounts"][0]["RW"] = True
            elif mutation == "extra-mount":
                container["Mounts"].append({"Type": "bind", "Source": "/shared", "Destination": "/shared", "RW": False})
            elif mutation == "tmpfs":
                container["Mounts"][1]["Destination"] = "/run"
            elif mutation == "missing-network":
                del container["NetworkSettings"]["Networks"]["nexpoly_mcp_clients_dev"]
            elif mutation == "extra-network":
                container["NetworkSettings"]["Networks"]["ks-agent"] = {}
            elif mutation == "production-alias":
                container["NetworkSettings"]["Networks"]["nexpoly_dev_default"]["Aliases"].append("capability-gateway")
            elif mutation == "port":
                container["HostConfig"]["PortBindings"] = {"3100/tcp": [{"HostPort": "3100"}]}
            elif mutation == "publish-all":
                container["HostConfig"]["PublishAllPorts"] = True
            elif mutation == "writable-root":
                container["HostConfig"]["ReadonlyRootfs"] = False
            with self.subTest(mutation=mutation), patch.object(dev, "run", return_value="gateway\n"), \
                    patch.object(dev, "inspect", return_value=container):
                if mutation is None:
                    self.assertTrue(dev.verify_project_ownership())
                else:
                    with self.assertRaises(dev.DevError):
                        dev.verify_project_ownership()

    def test_gateway_extension_requires_approved_base_layers_labels_and_nonroot_user(self):
        base = {"Id": dev.GATEWAY_BASE_IMAGE, "RootFS": {"Layers": ["gateway-base-layer"]}}
        derived = {"Id": "gateway-extension", "RootFS": {"Layers": ["gateway-base-layer", "extension-layer"]},
                   "Config": {"User": "bun", "Labels": {
                       "org.nexpoly.purpose": "capability-gateway-development",
                       "org.nexpoly.expected-base-image": dev.GATEWAY_BASE_IMAGE}}}
        for mutation in (None, "base-id", "image-id", "purpose", "base-label", "layers", "no-extension",
                         "root", "0:0", "0:1000", "root:1000"):
            candidate_base, candidate_derived = json.loads(json.dumps([base, derived]))
            if mutation == "base-id":
                candidate_base["Id"] = "other-base"
            elif mutation == "image-id":
                candidate_derived["Id"] = "other-extension"
            elif mutation == "purpose":
                candidate_derived["Config"]["Labels"]["org.nexpoly.purpose"] = "other-purpose"
            elif mutation == "base-label":
                candidate_derived["Config"]["Labels"]["org.nexpoly.expected-base-image"] = "other-base"
            elif mutation == "layers":
                candidate_derived["RootFS"]["Layers"][0] = "other-base-layer"
            elif mutation == "no-extension":
                candidate_derived["RootFS"]["Layers"].pop()
            elif mutation:
                candidate_derived["Config"]["User"] = mutation
            with self.subTest(mutation=mutation), \
                    patch.object(dev, "run", return_value=json.dumps([candidate_base, candidate_derived])):
                if mutation is None:
                    dev.verify_gateway_overlay("gateway-extension")
                else:
                    with self.assertRaises(dev.DevError):
                        dev.verify_gateway_overlay("gateway-extension")

    def test_gateway_extension_identity_is_checked_after_build_and_on_smoke(self):
        container = self.owned_gateway()
        with patch.object(dev, "run", return_value="gateway\n"), \
                patch.object(dev, "inspect", return_value=container), \
                patch.object(dev, "verify_gateway_overlay") as verify:
            self.assertTrue(dev.verify_project_ownership())
            verify.assert_not_called()
            self.assertTrue(dev.verify_project_ownership(require_ui_overlay=True))
            verify.assert_called_once_with(container["Image"])

    def test_renamed_development_services_are_accepted_for_shared_network_dns_isolation(self):
        for service in ("openscience-dev-backend", "openscience-dev-ui"):
            container = self.owned_container()
            container["Config"]["Labels"]["com.docker.compose.service"] = service
            shared_network = "ks-page" if service.endswith("ui") else "ks-agent"
            networks = {dev.DEV_NETWORK: {}, shared_network: {"Aliases": [service]}}
            container["NetworkSettings"] = {"Networks": networks}
            if service.endswith("ui"):
                container["Mounts"] = [{"Type": "tmpfs", "Destination": "/tmp"}]
            with patch.object(dev, "run", return_value="container\n"), \
                    patch.object(dev, "inspect", return_value=container):
                self.assertTrue(dev.verify_project_ownership())
                networks[shared_network]["Aliases"].append("openscience-backend")
                with self.assertRaises(dev.DevError):
                    dev.verify_project_ownership()

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
        dev.write_private("gateway-token", "b" * 48 + "\n")
        with patch.object(dev, "read_source", return_value=json.dumps(self.source()).encode()), \
                patch.object(dev, "mcp_token", return_value="a" * 48), \
                patch.object(dev, "gateway_source", return_value=self.production_gateway()), \
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

    def test_ui_overlay_validation_is_explicit_after_upgrade_and_on_smoke(self):
        container = self.owned_container()
        container["Image"] = "derived-image"
        container["Config"]["Labels"]["com.docker.compose.service"] = "openscience-ui"
        container["Mounts"] = [{"Type": "tmpfs", "Destination": "/tmp"}]
        with patch.object(dev, "run", return_value="container\n"), \
                patch.object(dev, "inspect", return_value=container), \
                patch.object(dev, "verify_ui_overlay") as verify:
            # An existing development UI can be upgraded by `up --build`.
            self.assertTrue(dev.verify_project_ownership())
            verify.assert_not_called()
            self.assertTrue(dev.verify_project_ownership(require_ui_overlay=True))
            verify.assert_called_once_with("derived-image")
        with patch.object(dev, "verify_project_ownership", return_value=False) as ownership:
            with self.assertRaises(dev.DevError):
                dev.smoke()
        ownership.assert_called_once_with(require_ui_overlay=True)

    def test_ui_overlay_requires_approved_base_layers_labels_and_nonroot_user(self):
        base = {"Id": dev.UI_IMAGE, "RootFS": {"Layers": ["base-layer-one", "base-layer-two"]}}
        derived = {"Id": "derived-image", "RootFS": {"Layers": ["base-layer-one", "base-layer-two", "overlay-layer"]},
                   "Config": {"User": "1001:1001", "Labels": {
                       "org.nexpoly.purpose": "openscience-mcp-ui-development",
                       "org.nexpoly.expected-base-image": dev.UI_IMAGE,
                       "org.nexpoly.expected-base-index-sha256": dev.UI_BASE_INDEX_SHA256,
                       "org.nexpoly.ui-overlay-format": "1"}}}
        for mutation in (None, "base-id", "derived-id", "purpose", "base-label", "index-label",
                         "format", "user", "layers", "missing-base-layers", "no-overlay-layer"):
            candidate_base, candidate_derived = json.loads(json.dumps([base, derived]))
            if mutation == "base-id":
                candidate_base["Id"] = "other-base"
            elif mutation == "derived-id":
                candidate_derived["Id"] = "other-image"
            elif mutation in {"purpose", "base-label", "index-label", "format"}:
                label = {"purpose": "org.nexpoly.purpose", "base-label": "org.nexpoly.expected-base-image",
                         "index-label": "org.nexpoly.expected-base-index-sha256", "format": "org.nexpoly.ui-overlay-format"}[mutation]
                candidate_derived["Config"]["Labels"][label] = "wrong"
            elif mutation == "user":
                candidate_derived["Config"]["User"] = "0:0"
            elif mutation == "layers":
                candidate_derived["RootFS"]["Layers"][0] = "other-base-layer"
            elif mutation == "missing-base-layers":
                candidate_base["RootFS"]["Layers"] = []
            elif mutation == "no-overlay-layer":
                candidate_derived["RootFS"]["Layers"].pop()
            with self.subTest(mutation=mutation), \
                    patch.object(dev, "run", return_value=json.dumps([candidate_base, candidate_derived])):
                if mutation is None:
                    dev.verify_ui_overlay("derived-image")
                else:
                    with self.assertRaises(dev.DevError):
                        dev.verify_ui_overlay("derived-image")

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

    def test_all_local_build_tags_are_pinned_before_source_configuration_is_read(self):
        backend = {"Image": dev.BACKEND_IMAGE, "Config": {"Labels": {
            "com.docker.compose.project": "openscience-backend-poc",
            "com.docker.compose.service": "openscience-backend",
            "com.docker.compose.project.working_dir": str(dev.SHARED_ROOT)}}}
        ui = {"Image": dev.UI_IMAGE, "Config": {"Labels": {
            "com.docker.compose.project": "openscience-ui-poc",
            "com.docker.compose.service": "openscience-ui",
            "com.docker.compose.project.working_dir": str(dev.SHARED_UI_ROOT)}}}
        approved = [dev.BACKEND_IMAGE, dev.UI_IMAGE, dev.UI_PATCH_IMAGE, dev.GATEWAY_BASE_IMAGE]
        tags = [dev.BACKEND_BASE_TAG, dev.UI_BASE_TAG, dev.UI_PATCH_TAG, dev.GATEWAY_BASE_TAG]
        for index, tag in enumerate(tags):
            responses = [json.dumps([{"Id": value}]) for value in approved[:index]]
            responses.append(json.dumps([{"Id": "unexpected-image"}]))
            with self.subTest(tag=tag), patch.object(dev, "inspect", side_effect=[backend, ui]), \
                    patch.object(dev, "run", side_effect=responses) as command, \
                    patch.object(dev, "read_source") as source:
                with self.assertRaises(dev.DevError):
                    dev.verify_shared()
            source.assert_not_called()
            self.assertEqual(command.call_args.args[0], ["docker", "image", "inspect", tag])

    def test_protected_snapshot_includes_production_gateway_environment_and_deployment_files(self):
        backend = {"Id": "shared-backend", "Image": dev.BACKEND_IMAGE, "Config": {"Labels": {
            "com.docker.compose.project": "openscience-backend-poc",
            "com.docker.compose.service": "openscience-backend",
            "com.docker.compose.project.working_dir": str(dev.SHARED_ROOT)}}, "Mounts": []}
        for destination, source, writable in [
            ("/run/openscience/openscience.jsonc", dev.SHARED_ROOT / "config/openscience.jsonc", False),
            ("/openscience/files", dev.SHARED_ROOT / "data/files", True),
            ("/openscience/records", dev.SHARED_ROOT / "data/records", True),
            ("/opt/zhiju-literature-metadata-search", dev.SHARED_ROOT / "literature/runtime", False),
        ]:
            backend["Mounts"].append({"Destination": destination, "Type": "bind", "Source": str(source), "RW": writable})
        for name in ("config", "state", "cache"):
            backend["Mounts"].append({"Destination": "/var/lib/openscience/" + name, "Type": "volume",
                "Name": "openscience-backend-poc_openscience_" + name, "RW": True})
        ui = {"Id": "shared-ui", "Image": dev.UI_IMAGE, "Config": {"Labels": {
            "com.docker.compose.project": "openscience-ui-poc", "com.docker.compose.service": "openscience-ui",
            "com.docker.compose.project.working_dir": str(dev.SHARED_UI_ROOT)}}, "Mounts": []}
        gateway = self.production_gateway()
        containers = {dev.SHARED_BACKEND: backend, dev.SHARED_UI: ui, dev.SHARED_GATEWAY: gateway}
        tags = {dev.BACKEND_BASE_TAG: dev.BACKEND_IMAGE, dev.UI_BASE_TAG: dev.UI_IMAGE,
                dev.UI_PATCH_TAG: dev.UI_PATCH_IMAGE, dev.GATEWAY_BASE_TAG: dev.GATEWAY_BASE_IMAGE}
        with patch.object(dev, "inspect", side_effect=lambda name: containers[name]), \
                patch.object(dev, "run", side_effect=lambda command: json.dumps([{"Id": tags[command[-1]]}])), \
                patch.object(dev, "read_source", side_effect=lambda path: str(path).encode()):
            original, _ = dev.verify_shared()
            self.assertEqual(set(original["containers"]), set(containers))
            self.assertIn(str(dev.SHARED_GATEWAY_ROOT / ".env"), original["files"])
            self.assertIn(str(dev.SHARED_GATEWAY_ROOT / "docker-compose.yml"), original["files"])
            self.assertNotIn("production-only-gateway-token", json.dumps(original))
            gateway["Config"]["Env"].append("NEW_SETTING=changed")
            changed, _ = dev.verify_shared()
        self.assertNotEqual(original["containers"][dev.SHARED_GATEWAY], changed["containers"][dev.SHARED_GATEWAY])
        dev.private_directory()
        dev.write_private("protected-baseline.json", json.dumps(original))
        with self.assertRaises(dev.DevError):
            dev.protected_unchanged(changed)

    def test_up_builds_then_requires_the_derived_ui_contract(self):
        with patch.object(dev.sys, "argv", ["openscience_mcp_dev.py", "up"]), \
                patch.object(dev, "operation_lock", return_value=contextlib.nullcontext()), \
                patch.object(dev, "verify_project_ownership", return_value=True) as ownership, \
                patch.object(dev, "verify_development_network"), patch.object(dev, "verify_prepared"), \
                patch.object(dev, "compose", return_value="") as compose, \
                patch.object(dev, "verify_shared", return_value=({}, {})), \
                patch.object(dev, "protected_unchanged"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dev.main(), 0)
        compose.assert_called_once_with("up", "-d", "--build", "--wait", "--wait-timeout", "120")
        self.assertEqual(ownership.call_count, 2)
        ownership.assert_called_with(require_ui_overlay=True)

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
        dev.write_private("gateway-token", "b" * 48 + "\n")
        with patch.object(dev, "verify_project_ownership", return_value=True), \
                patch.object(dev, "verify_shared", return_value=({}, {})), \
                patch.object(dev, "prepared_files", return_value={"mcp-token": "new-secret"}):
            with self.assertRaises(dev.DevError):
                dev.setup()
        self.assertEqual(dev.read_private("mcp-token"), "existing-secret")

    def setup_waiting_for_start(self, *, refresh_baseline=False):
        """Hold the actual file lock while a concurrent start changes state."""
        dev.private_directory()
        dev.write_private("mcp-token", "existing-secret")
        dev.write_private("gateway-token", "b" * 48 + "\n")
        dev.write_private("protected-baseline.json", '{"release":"old"}\n')
        waiting = threading.Event()
        acquired = threading.Event()
        state = {"running": False, "snapshot": {"release": "old"}}
        observations = []
        main_thread = threading.get_ident()
        real_flock = dev.fcntl.flock

        def flock(descriptor, operation):
            worker = threading.get_ident() != main_thread
            if worker:
                waiting.set()
            real_flock(descriptor, operation)
            if worker:
                acquired.set()

        def ownership():
            observations.append(("ownership", acquired.is_set()))
            return state["running"]

        def shared():
            observations.append(("shared", acquired.is_set()))
            return state["snapshot"], {}

        with patch.object(dev.fcntl, "flock", side_effect=flock), \
                patch.object(dev, "verify_project_ownership", side_effect=ownership), \
                patch.object(dev, "verify_shared", side_effect=shared), \
                patch.object(dev, "prepared_files", return_value={"mcp-token": "replacement-secret"}), \
                ThreadPoolExecutor(max_workers=1) as executor:
            with dev.operation_lock():
                pending = executor.submit(dev.setup, refresh_baseline=refresh_baseline)
                try:
                    self.assertTrue(waiting.wait(5), "Concurrent setup did not wait for the operation lock")
                    self.assertFalse(acquired.is_set())
                finally:
                    # Simulate a start finishing before releasing its lock.
                    state["running"] = True
                    if refresh_baseline:
                        state["snapshot"] = {"release": "new"}
            with self.assertRaisesRegex(dev.DevError, "Stop"):
                pending.result(timeout=5)
        self.assertTrue(observations)
        self.assertTrue(all(inside_lock for _, inside_lock in observations), observations)
        self.assertEqual(dev.read_private("mcp-token"), "existing-secret")
        self.assertEqual(json.loads(dev.read_private("protected-baseline.json")), {"release": "old"})

    def test_setup_rechecks_running_state_after_waiting_and_preserves_mounted_credentials(self):
        self.setup_waiting_for_start()

    def test_baseline_refresh_rechecks_running_state_after_waiting_and_keeps_old_baseline(self):
        self.setup_waiting_for_start(refresh_baseline=True)
        self.assertEqual(list(self.runtime.glob("protected-baseline.previous-*.json")), [])

    def test_setup_checks_shared_snapshot_while_holding_the_operation_lock(self):
        dev.private_directory()
        dev.write_private("gateway-token", "b" * 48 + "\n")
        locked = False
        observations = []

        @contextlib.contextmanager
        def operation_lock():
            nonlocal locked
            locked = True
            try:
                yield
            finally:
                locked = False

        def shared():
            observations.append(locked)
            return {"release": "current"}, {}

        with patch.object(dev, "operation_lock", operation_lock), \
                patch.object(dev, "verify_project_ownership", return_value=False), \
                patch.object(dev, "verify_shared", side_effect=shared), \
                patch.object(dev, "prepared_files", return_value={"mcp-token": "prepared-secret"}):
            dev.setup()
        self.assertEqual(observations, [True, True])

    def test_setup_creates_independent_gateway_token_once_and_requires_stop_when_missing(self):
        dev.private_directory()
        prepared = lambda backend: {"gateway-token": dev.gateway_token() + "\n"}
        with patch.object(dev, "verify_project_ownership", return_value=False), \
                patch.object(dev, "verify_shared", return_value=({}, {})), \
                patch.object(dev, "prepared_files", side_effect=prepared):
            dev.setup()
            token = dev.gateway_token()
            self.assertEqual(stat.S_IMODE((self.runtime / "gateway-token").stat().st_mode), 0o600)
            with patch.object(dev.secrets, "token_urlsafe") as generate:
                dev.setup()
            generate.assert_not_called()
            self.assertEqual(dev.gateway_token(), token)
        (self.runtime / "gateway-token").unlink()
        with patch.object(dev, "verify_project_ownership", return_value=True), \
                patch.object(dev, "verify_shared", return_value=({}, {})), \
                patch.object(dev, "prepared_files") as files, patch.object(dev.secrets, "token_urlsafe") as generate:
            with self.assertRaises(dev.DevError):
                dev.setup()
        files.assert_not_called()
        generate.assert_not_called()
        self.assertFalse((self.runtime / "gateway-token").exists())

    def test_baseline_refresh_requires_stopped_stack_and_preserves_existing_files(self):
        dev.private_directory()
        dev.write_private("protected-baseline.json", '{"release":"old"}\n')
        dev.write_private("mcp-token", "existing-secret")
        with patch.object(dev, "verify_project_ownership", return_value=True), \
                patch.object(dev, "verify_shared", return_value=({"release": "new"}, {})), \
                patch.object(dev, "prepared_files") as prepared:
            with self.assertRaises(dev.DevError):
                dev.setup(refresh_baseline=True)
        prepared.assert_not_called()
        self.assertEqual(json.loads(dev.read_private("protected-baseline.json")), {"release": "old"})
        self.assertEqual(dev.read_private("mcp-token"), "existing-secret")

    def test_setup_refresh_archives_old_baseline_only_after_pinned_release_verification(self):
        dev.private_directory()
        previous = '{"release":"old"}\n'
        dev.write_private("protected-baseline.json", previous)
        dev.write_private("files/session-history", "existing history")
        current = {"release": "new"}
        with patch.object(dev, "verify_project_ownership", return_value=False), \
                patch.object(dev, "verify_shared", return_value=(current, {})), \
                patch.object(dev, "prepared_files", return_value={"mcp-token": "new-secret"}):
            dev.setup(refresh_baseline=True)
        self.assertEqual(json.loads(dev.read_private("protected-baseline.json")), current)
        archives = list(self.runtime.glob("protected-baseline.previous-*.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_text(), previous)
        self.assertEqual(stat.S_IMODE(archives[0].stat().st_mode), 0o600)
        self.assertEqual(dev.read_private("files/session-history"), "existing history")

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
