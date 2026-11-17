from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "polytao_mcp_dev.py"
SPEC = importlib.util.spec_from_file_location("polytao_mcp_dev", SOURCE)
dev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dev)
sys.modules.setdefault("polytao_mcp_dev", dev)
VERIFY_SPEC = importlib.util.spec_from_file_location("verify_polytao_mcp", SOURCE.with_name("verify_polytao_mcp.py"))
verifier = importlib.util.module_from_spec(VERIFY_SPEC)
VERIFY_SPEC.loader.exec_module(verifier)
verifier.dev = dev
ACCOUNT = "628cf3d3-c456-4fc5-a8a4-51d135e273ba"
SESSION = "c89168b0-d9a5-4f53-bf8f-f0ed5fa05730"


@contextlib.contextmanager
def local_http_server(respond):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.respond()

        def do_GET(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            status, headers, payload = respond(self.path, dict(self.headers), body)
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

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


class PolytaoDevelopmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / ".runtime" / "polytao-mcp"
        self.root_patch = patch.object(dev, "ROOT", self.root)
        self.runtime_patch = patch.object(dev, "RUNTIME", self.runtime)
        self.root_patch.start()
        self.runtime_patch.start()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.runtime_patch.stop)

    def identity(self):
        dev.private_directory()
        dev.write_private("server/identity.json", json.dumps({"account_uuid": ACCOUNT, "backend_url": dev.BACKEND_URL}))

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose is needed to parse the deployment manifest")
    def test_compose_preserves_tmpfs_options_as_one_absolute_mount(self):
        root = SOURCE.parents[1]
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith(("COMPOSE_", "NEXPOLY_"))}
        environment.update(NEXPOLY_POLYTAO_MCP_DEV_UID=str(os.getuid()),
                           NEXPOLY_POLYTAO_MCP_DEV_GID=str(os.getgid()),
                           NEXPOLY_POLYTAO_MCP_DEV_ACCOUNT_UUID=ACCOUNT,
                           NEXPOLY_POLYTAO_MCP_DEV_RUNTIME=str(self.runtime / "server"),
                           NEXPOLY_POLYTAO_MCP_DEV_JOURNAL=str(self.runtime / "journal"))
        result = subprocess.run(["docker", "compose", "--env-file", "/dev/null", "-f",
                                 str(root / "docker-compose.polytao-mcp-dev.yml"),
                                 "config", "--format", "json"], env=environment, capture_output=True,
                                text=True, check=True)
        service = json.loads(result.stdout)["services"]["polytao-mcp"]
        self.assertEqual(service["tmpfs"], ["/tmp:rw,noexec,nosuid,size=16m"])

    def test_setup_keeps_token_hash_only_in_server_mount(self):
        with patch.object(dev, "verify_project_ownership"), patch.object(dev, "verify_dev_target"), \
                patch.object(dev, "initialize_journal") as initialize:
            result = dev.setup(ACCOUNT)
            original = dev.read_private("client.env")
            dev.setup(None)
        self.assertEqual(initialize.call_args_list[0].kwargs, {"first_setup": True})
        self.assertEqual(initialize.call_args_list[1].kwargs, {"first_setup": False})
        token = dict(line.split("=", 1) for line in original.splitlines())["NEXPOLY_MCP_TOKEN"]
        registry = json.loads(dev.read_private("server/clients.json"))
        self.assertEqual(registry["clients"][0]["token_sha256"], hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(registry["clients"][0]["tools"], list(dev.TOOLS))
        ui_token = dict(line.split("=", 1) for line in dev.read_private("ui-client.env").splitlines())["NEXPOLY_MCP_TOKEN"]
        self.assertNotEqual(token, ui_token)
        self.assertEqual(registry["clients"][1]["client_id"], dev.UI_CLIENT_ID)
        self.assertEqual(registry["clients"][1]["owner_client_id"], dev.CLIENT_ID)
        self.assertEqual(registry["clients"][1]["tools"], list(dev.UI_TOOLS))
        self.assertEqual(registry["clients"][1]["token_sha256"], hashlib.sha256(ui_token.encode()).hexdigest())
        self.assertNotIn(ui_token, dev.read_private("server/clients.json"))
        self.assertNotIn("confirm_polytao_generation", dev.TOOLS)
        self.assertEqual(dev.read_private("client.env"), original)
        self.assertNotIn(token, json.dumps(result))
        self.assertNotIn(token, dev.read_private("server/clients.json"))
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o700)
        for name in ("server", "journal"):
            self.assertEqual(stat.S_IMODE((self.runtime / name).stat().st_mode), 0o700)
        for name in ("server/identity.json", "client.env", "ui-client.env", "server/clients.json"):
            self.assertEqual(stat.S_IMODE((self.runtime / name).stat().st_mode), 0o600)

    def test_existing_legacy_registry_migrates_scopes_without_rotating_owner_or_expiry(self):
        dev.private_directory()
        dev.create_client()
        original = dev.read_private("client.env")
        registry = json.loads(dev.read_private("server/clients.json"))
        model = registry["clients"][0]
        expiry = model["expires_at"]
        model["tools"] = ["get_polytao_capabilities", "calculate_polytao_descriptors", "submit_polytao_generation", "get_polytao_job"]
        dev.write_private("server/clients.json", json.dumps({"clients": [model]}))
        (self.runtime / "ui-client.env").unlink()
        dev.create_client()
        migrated = json.loads(dev.read_private("server/clients.json"))
        self.assertEqual(dev.read_private("client.env"), original)
        self.assertEqual(migrated["clients"][0]["tools"], list(dev.TOOLS))
        self.assertEqual(migrated["clients"][0]["expires_at"], expiry)
        self.assertEqual(migrated["clients"][1]["expires_at"], expiry)
        before = dev.read_private("ui-client.env")
        dev.create_client()
        self.assertEqual(dev.read_private("ui-client.env"), before)

    def test_registry_upgrade_rejects_rebound_ui_and_preserves_individual_revocations(self):
        dev.private_directory()
        dev.create_client()
        registry = json.loads(dev.read_private("server/clients.json"))
        registry["clients"][0]["tools"] = []
        registry["clients"][1]["tools"] = []
        dev.write_private("server/clients.json", json.dumps(registry))
        dev.create_client()
        value = json.loads(dev.read_private("server/clients.json"))
        self.assertEqual([client["tools"] for client in value["clients"]], [[], []])
        registry["clients"][1]["owner_client_id"] = "different-owner"
        dev.write_private("server/clients.json", json.dumps(registry))
        with self.assertRaisesRegex(dev.DevError, "fixed owner"):
            dev.create_client()

    def test_ui_rpc_uses_separate_private_client_token(self):
        dev.private_directory()
        dev.create_client()
        requests = []
        def respond(path, headers, body):
            requests.append(headers["Authorization"])
            return 200, {}, {"jsonrpc": "2.0", "id": 1, "result": {}}
        with local_http_server(respond) as url, patch.object(dev, "BASE_URL", url):
            for filename in ("client.env", "ui-client.env"):
                dev.write_private(filename, dev.read_private(filename).replace("http://127.0.0.1:18111", url))
            dev.rpc_client()('tools/list', {}, 1)
            dev.rpc_client(ui=True)('tools/list', {}, 1)
        self.assertEqual(len(requests), 2)
        self.assertNotEqual(requests[0], requests[1])

    def test_setup_does_not_reenable_revoked_client_or_change_identity(self):
        with patch.object(dev, "verify_project_ownership"), patch.object(dev, "verify_dev_target"), \
                patch.object(dev, "initialize_journal"):
            dev.setup(ACCOUNT)
            dev.write_private("server/clients.json", '{"clients":[]}\n')
            dev.setup(None)
            with self.assertRaisesRegex(dev.DevError, "identity is fixed"):
                dev.setup("20b60b39-704e-4dc1-a84d-6ad5ecebe9bb")
        self.assertEqual(json.loads(dev.read_private("server/clients.json")), {"clients": []})

    def test_initial_setup_requires_explicit_ordinary_uuid(self):
        with patch.object(dev, "verify_project_ownership"), patch.object(dev, "verify_dev_target"):
            for value in (None, "username", "00000000-0000-0000-0000-000000000001"):
                with self.assertRaises(dev.DevError):
                    dev.setup(value)

    def test_token_rotation_preserves_client_identity(self):
        dev.private_directory()
        dev.create_client()
        first = json.loads(dev.read_private("server/clients.json"))["clients"][0]
        dev.create_client(replace=True)
        second = json.loads(dev.read_private("server/clients.json"))["clients"][0]
        self.assertNotEqual(first["token_sha256"], second["token_sha256"])
        self.assertEqual(first["client_id"], second["client_id"])

    def test_runtime_and_secret_symlinks_are_rejected(self):
        (self.root / ".runtime").mkdir()
        self.runtime.symlink_to(self.root)
        with self.assertRaises(dev.DevError):
            dev.private_directory()
        self.runtime.unlink()
        dev.private_directory()
        target = self.root / "outside"
        target.write_text("outside")
        (self.runtime / "client.env").symlink_to(target)
        with self.assertRaises((dev.DevError, OSError)):
            dev.write_private("client.env", "secret")
        self.assertEqual(target.read_text(), "outside")

    def test_journal_initialization_is_stdlib_and_never_recreates_lost_files(self):
        dev.private_directory()
        # Use the actual source backend, while keeping every write in the temp runtime.
        with patch.object(dev, "ROOT", SOURCE.parents[1]):
            dev.initialize_journal(first_setup=True)
            dev.initialize_journal(first_setup=False)
            (self.runtime / "journal/submissions.sqlite3").unlink()
            with self.assertRaisesRegex(dev.DevError, "preserve"):
                dev.initialize_journal(first_setup=False)
        self.assertFalse((self.runtime / "journal/submissions.sqlite3").exists())

    def test_backend_production_identity_is_rejected_before_any_http(self):
        with patch.object(dev, "docker_field", return_value={"com.docker.compose.project": "nexpoly"}), \
                patch.object(dev, "opener") as connection:
            with self.assertRaises(dev.DevError):
                dev.verify_dev_target()
        connection.assert_not_called()

    def test_backend_requires_private_loopback_publication_and_development_auth(self):
        labels = {"com.docker.compose.project": "nexpoly_dev", "com.docker.compose.service": "backend",
                  "com.docker.compose.project.working_dir": str(dev.DEV_ROOT),
                  "com.docker.compose.project.config_files": ",".join(str(dev.DEV_ROOT / name) for name in
                    ("docker-compose.yml", "docker-compose.dev.yml", "docker-compose.user-isolation-dev.yml"))}
        values = [labels, True, {dev.DEV_NETWORK: {}}, ["GPU_BROKER_ENVIRONMENT=dev",
                  "AUTH_COOKIE_SECURE=false", "AUTH_COOKIE_NAME=nexpoly_dev_session"],
                  "nexpoly-dev-backend:latest", {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18000"}]}]
        with patch.object(dev, "docker_field", side_effect=values):
            with self.assertRaisesRegex(dev.DevError, "loopback"):
                dev.verify_dev_target()

    def test_compose_discards_environment_overrides_and_uses_only_new_project(self):
        self.identity()
        with patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": "nexpoly", "NEXPOLY_POLYTAO_MCP_BACKEND_URL": "secret"}), \
                patch.object(dev, "run", return_value="") as command:
            dev.compose("down")
        argv = command.call_args.args[0]
        environment = command.call_args.kwargs["env"]
        self.assertEqual(argv[argv.index("-p") + 1], dev.PROJECT)
        self.assertEqual(argv[argv.index("--env-file") + 1], "/dev/null")
        self.assertNotIn("COMPOSE_PROJECT_NAME", environment)
        self.assertNotIn("NEXPOLY_POLYTAO_MCP_BACKEND_URL", environment)
        self.assertEqual(environment["NEXPOLY_POLYTAO_MCP_DEV_ACCOUNT_UUID"], ACCOUNT)

    def test_container_control_is_serialized_with_session_and_token_changes(self):
        active = False
        @contextlib.contextmanager
        def locked():
            nonlocal active
            self.assertFalse(active)
            active = True
            try:
                yield
            finally:
                active = False
        def compose(*args):
            self.assertTrue(active)
            return ""
        for action in ("up", "down", "status"):
            with self.subTest(action=action), patch.object(dev, "operation_lock", locked), \
                    patch.object(dev, "compose", side_effect=compose) as command, \
                    patch.object(dev, "verify_project_ownership"), patch.object(dev, "verify_dev_target"), \
                    patch.object(dev, "verify_session_file"), patch.object(dev, "verify_port_available"), \
                    patch.object(dev, "read_private", return_value="{}"), \
                    patch.object(sys, "argv", [str(SOURCE), action]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(dev.main(), 0)
                command.assert_called_once()
            self.assertFalse(active)

    def test_foreign_checkout_and_unexpected_image_cannot_be_controlled(self):
        labels = {"com.docker.compose.project": dev.PROJECT, "com.docker.compose.service": dev.SERVICE,
                  "com.docker.compose.project.working_dir": "/another/checkout"}
        with patch.object(dev, "run", side_effect=["container\n", json.dumps(labels)]):
            with self.assertRaisesRegex(dev.DevError, "another checkout"):
                dev.verify_project_ownership()
        labels["com.docker.compose.project.working_dir"] = str(self.root)
        labels["com.docker.compose.project.config_files"] = str(self.root / "docker-compose.polytao-mcp-dev.yml")
        with patch.object(dev, "run", side_effect=["container\n", json.dumps(labels), "foreign:image\n"]):
            with self.assertRaisesRegex(dev.DevError, "image"):
                dev.verify_project_ownership()

    def test_private_mounts_are_verified_before_container_control(self):
        labels = {"com.docker.compose.project": dev.PROJECT, "com.docker.compose.service": dev.SERVICE,
                  "com.docker.compose.project.working_dir": str(self.root),
                  "com.docker.compose.project.config_files": str(self.root / "docker-compose.polytao-mcp-dev.yml")}
        mounts = [{"Type": "bind", "Destination": "/run/polytao-mcp", "Source": str(self.runtime / "server"), "RW": True}]
        with patch.object(dev, "run", side_effect=["container\n", json.dumps(labels), dev.IMAGE, json.dumps(mounts)]):
            with self.assertRaisesRegex(dev.DevError, "runtime|inventory"):
                dev.verify_project_ownership()

    def test_mount_inventory_is_exact_and_tmpfs_is_the_only_extra_allowed(self):
        labels = {"com.docker.compose.project": dev.PROJECT, "com.docker.compose.service": dev.SERVICE,
                  "com.docker.compose.project.working_dir": str(self.root),
                  "com.docker.compose.project.config_files": str(self.root / "docker-compose.polytao-mcp-dev.yml")}
        mounts = [{"Type": "bind", "Destination": "/run/polytao-mcp", "Source": str(self.runtime / "server"), "RW": False},
                  {"Type": "bind", "Destination": "/var/lib/polytao-mcp", "Source": str(self.runtime / "journal"), "RW": True},
                  {"Type": "tmpfs", "Destination": "/tmp", "RW": True}]
        with patch.object(dev, "run", side_effect=["container\n", json.dumps(labels), dev.IMAGE, json.dumps(mounts)]):
            dev.verify_project_ownership()
        mounts.append({"Type": "bind", "Destination": "/credentials", "Source": "/private", "RW": False})
        with patch.object(dev, "run", side_effect=["container\n", json.dumps(labels), dev.IMAGE, json.dumps(mounts)]):
            with self.assertRaisesRegex(dev.DevError, "inventory"):
                dev.verify_project_ownership()

    def test_foreign_compose_override_is_rejected(self):
        labels = {"com.docker.compose.project": dev.PROJECT, "com.docker.compose.service": dev.SERVICE,
                  "com.docker.compose.project.working_dir": str(self.root),
                  "com.docker.compose.project.config_files": str(self.root / "docker-compose.polytao-mcp-dev.yml") + ",/foreign.yml"}
        with patch.object(dev, "run", side_effect=["container\n", json.dumps(labels)]):
            with self.assertRaisesRegex(dev.DevError, "provenance"):
                dev.verify_project_ownership()

    def test_subprocess_failure_does_not_reveal_secret(self):
        failure = subprocess.CompletedProcess(["docker"], 1, "secret-output", "secret-error")
        with patch.object(dev.subprocess, "run", return_value=failure):
            with self.assertRaises(dev.DevError) as raised:
                dev.run(["docker"], payload="secret-password")
        self.assertNotIn("secret", str(raised.exception))

    def test_diagnostics_are_private_and_cannot_capture_credential_stdin(self):
        def failing(command, **kwargs):
            kwargs["stdout"].write("private-build-data\n")
            return subprocess.CompletedProcess(command, 1)
        output = io.StringIO()
        with patch.object(dev.subprocess, "run", side_effect=failing), contextlib.redirect_stdout(output):
            with self.assertRaises(dev.DevError):
                dev.run(["docker", "compose", "up"], diagnostics="docker-up.log")
        self.assertNotIn("private-build-data", output.getvalue())
        self.assertEqual(dev.read_private("docker-up.log"), "private-build-data\n")
        with self.assertRaises(dev.DevError):
            dev.run(["docker"], payload="password", diagnostics="docker-up.log")

    def test_login_prompts_password_verifies_session_and_never_stores_password(self):
        self.identity()
        requests = []
        account = {"authenticated": True, "user": {"id": ACCOUNT, "must_change_password": False},
                   "account_type": "member", "capabilities": {"polytao.use": True},
                   "session_id": SESSION, "csrf_token": "csrf-test"}
        def service(path, headers, body):
            requests.append((path, headers, body))
            extra = {"Set-Cookie": "nexpoly_dev_session=cookie-test; Max-Age=43200; HttpOnly; Path=/; SameSite=lax"} \
                if path.endswith("/login") else {}
            return 200, extra, account
        with local_http_server(service) as url, patch.object(dev, "BACKEND_LOOPBACK", url), \
                patch.object(dev, "verify_project_ownership"), patch.object(dev, "verify_dev_target"), \
                patch.object(dev.getpass, "getpass", return_value="secret-password"):
            result = dev.login("developer")
        saved = json.loads(dev.read_private("server/session.json"))
        self.assertEqual(saved["account_uuid"], ACCOUNT)
        self.assertEqual(saved["backend_url"], dev.BACKEND_URL)
        self.assertEqual(saved["cookie"], "nexpoly_dev_session=cookie-test")
        self.assertNotIn("secret-password", json.dumps(saved) + json.dumps(result))
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0][1]["Host"], "nexpoly_dev-backend-1:8000")
        self.assertEqual(requests[0][1]["Origin"], dev.BACKEND_URL)
        self.assertEqual(requests[1][1]["Cookie"], saved["cookie"])
        self.assertEqual(requests[1][1]["X-Session-Context"], SESSION)
        dev.verify_session_file()

    def test_login_wrong_user_member_or_password_requirement_is_rejected(self):
        valid = {"authenticated": True, "user": {"id": ACCOUNT, "must_change_password": False},
                 "account_type": "member", "capabilities": {"polytao.use": True}, "session_id": SESSION,
                 "csrf_token": "csrf"}
        variants = [dict(valid, account_type="free"), dict(valid, user={"id": ACCOUNT, "must_change_password": True}),
                    dict(valid, user={"id": "foreign", "must_change_password": False}),
                    dict(valid, capabilities={"polytao.use": False})]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(dev.DevError):
                dev.validate_account(value, ACCOUNT)

    def test_smoke_uses_only_readonly_tool_ignores_proxy_and_limits_response(self):
        requests = []
        proxy_requests = []
        ui_token = None
        def service(path, headers, body):
            request = json.loads(body)
            requests.append(request)
            if request["method"] == "initialize":
                result = {"protocolVersion": "2025-11-25"}
            elif request["method"] == "tools/list":
                tools = dev.UI_TOOLS if headers.get("Authorization") == "Bearer " + ui_token else dev.TOOLS
                result = {"tools": [{"name": name} for name in tools]}
            else:
                result = {"structuredContent": {"ok": True}}
            return 200, {}, {"jsonrpc": "2.0", "id": request["id"], "result": result}
        def proxy(path, headers, body):
            proxy_requests.append(path)
            return 502, {}, {}
        with local_http_server(service) as url, local_http_server(proxy) as proxy_url, \
                patch.object(dev, "BASE_URL", url), patch.dict(os.environ, {"HTTP_PROXY": proxy_url,
                    "HTTPS_PROXY": proxy_url, "ALL_PROXY": proxy_url, "NO_PROXY": "", "no_proxy": ""}):
            dev.private_directory()
            dev.create_client()
            ui_token = dict(line.split("=", 1) for line in dev.read_private("ui-client.env").splitlines())["NEXPOLY_MCP_TOKEN"]
            report = dev.smoke()
            self.assertFalse(report["generation_submitted"])
            self.assertEqual(report["model_tools"], list(dev.TOOLS))
            self.assertEqual(report["ui_tools"], list(dev.UI_TOOLS))
        self.assertEqual(proxy_requests, [])
        self.assertEqual([r["params"]["name"] for r in requests if r["method"] == "tools/call"],
                         ["get_polytao_capabilities"])
        with self.assertRaisesRegex(dev.DevError, "limit"):
            dev.read_json_response(io.BytesIO(b"x" * 100), max_bytes=5)

    def test_port_probe_matches_server_rebind_semantics_without_overriding_live_listener(self):
        with patch.object(dev, "run", return_value=""), patch.object(dev.socket, "socket") as socket_factory:
            listener = socket_factory.return_value.__enter__.return_value
            dev.verify_port_available()
            listener.setsockopt.assert_called_once_with(dev.socket.SOL_SOCKET, dev.socket.SO_REUSEADDR, 1)
            listener.bind.assert_called_once_with(("127.0.0.1", 18111))
            listener.bind.side_effect = OSError("busy")
            with self.assertRaisesRegex(dev.DevError, "already in use"):
                dev.verify_port_available()

    def test_redirect_does_not_forward_bearer(self):
        destination_requests = []
        def destination(path, headers, body):
            destination_requests.append(headers)
            return 200, {}, {}
        with local_http_server(destination) as destination_url:
            def redirect(path, headers, body):
                return 302, {"Location": destination_url + "/stolen"}, {}
            with local_http_server(redirect) as url, patch.object(dev, "BASE_URL", url):
                dev.private_directory()
                dev.create_client()
                with self.assertRaises(urllib.error.HTTPError):
                    dev.smoke()
        self.assertEqual(destination_requests, [])

    @contextlib.contextmanager
    def acceptance_rpc(self, *, submit_error=False, candidate="*CC*", status="completed"):
        calls = []
        def rpc(method, params, request_id, *, ui=False):
            if method == "initialize":
                return {"protocolVersion": "2025-11-25"}
            calls.append(params)
            name = params["name"]
            if name == "get_polytao_capabilities":
                value = {"enabled": True, "available": True}
            elif name == "calculate_polytao_descriptors":
                value = {"descriptors": {str(i): float(i) for i in range(15)}, "canonical_smiles": candidate}
            elif name == "prepare_polytao_generation":
                self.assertFalse(ui)
                value = {"draft_id": "ptd_" + "a" * 32, "revision": 1, "ready": True, "job_id": None,
                         "conversation_id": params["arguments"]["conversation_id"]}
            elif name == "confirm_polytao_generation":
                self.assertTrue(ui)
                if submit_error:
                    raise urllib.error.URLError("response lost")
                value = {"job_id": "test-job", "status": "submitted",
                         "submission": {"job_id": "test-job", "status": "pending"}}
            else:
                value = {"job_id": "test-job", "status": status, "requested_count": 3, "returned_count": 1,
                         "results": [{"rank": 1, "valid_smiles": True, "generated_smiles": candidate, "sa_score": None}],
                         "next_offset": None}
            return {"structuredContent": {"ok": True, **value}}
        with patch.object(dev, "verify_project_ownership"), patch.object(dev, "verify_dev_target"), \
                patch.object(dev, "verify_session_file"), patch.object(dev, "rpc_client", side_effect=lambda *, ui=False:
                    lambda method, params, request_id: rpc(method, params, request_id, ui=ui)), \
                patch.object(verifier, "runtime_metadata", return_value={"model_id": "fixture"}):
            yield calls

    def test_generation_verifier_submits_once_and_refuses_rerun_without_job_id(self):
        with self.acceptance_rpc() as calls:
            result = verifier.verify("stable-test-key", polls=1)
            self.assertEqual(result["status"], "passed")
            with self.assertRaisesRegex(verifier.VerificationError, "already has"):
                verifier.verify("stable-test-key", polls=1)
        self.assertEqual(sum(call["name"] == "prepare_polytao_generation" for call in calls), 1)
        self.assertEqual(sum(call["name"] == "confirm_polytao_generation" for call in calls), 1)
        report = json.loads(Path(result["report"]).read_text())
        self.assertEqual(report["job_id"], "test-job")
        self.assertTrue(report["submit_attempted"])

    def test_unknown_acceptance_submit_is_persisted_before_request_and_never_retried(self):
        with self.acceptance_rpc(submit_error=True) as calls:
            with self.assertRaises(urllib.error.URLError):
                verifier.verify("unknown-key", polls=1)
            with self.assertRaisesRegex(verifier.VerificationError, "already has"):
                verifier.verify("unknown-key", polls=1)
        self.assertEqual(sum(call["name"] == "confirm_polytao_generation" for call in calls), 1)
        reports = list(self.runtime.glob("acceptance-*.json"))
        self.assertEqual(len(reports), 1)
        self.assertTrue(json.loads(reports[0].read_text())["submit_attempted"])

    def test_acceptance_resume_queries_same_job_and_keeps_no_resubmit_marker(self):
        with self.acceptance_rpc(status="running"):
            pending = verifier.verify("resume-key", polls=1)
        self.assertEqual(pending["status"], "pending")
        with self.acceptance_rpc() as calls:
            result = verifier.verify("resume-key", job_id="test-job", polls=1)
            with self.assertRaises(verifier.VerificationError):
                verifier.verify("resume-key", polls=1)
        self.assertEqual(result["status"], "passed")
        self.assertFalse(any(call["name"] in {"prepare_polytao_generation", "confirm_polytao_generation"} for call in calls))
        with self.acceptance_rpc(), self.assertRaisesRegex(verifier.VerificationError, "differs"):
            verifier.verify("resume-key", job_id="foreign-job", polls=1)

    def test_acceptance_rejects_candidate_with_fewer_than_two_connection_atoms(self):
        with self.acceptance_rpc(candidate="CCC"), self.assertRaisesRegex(verifier.VerificationError, "fewer than two"):
            verifier.verify("invalid-candidate", polls=1)

    def test_acceptance_records_versions_without_session_or_private_status_fields(self):
        dev.private_directory()
        dev.write_private("server/session.json", json.dumps({"cookie": "nexpoly_dev_session=secret-cookie", "session_id": SESSION}))
        requests = []
        def service(path, headers, body):
            requests.append((path, headers))
            return 200, {}, {"model_id": "fixture-model", "model_revision": "revision", "worker_mode": "backend-in-memory",
                             "model_dir": "/private/model", "worker_status": "secret-worker-detail"}
        with local_http_server(service) as url, patch.object(dev, "BACKEND_LOOPBACK", url), \
                patch.object(dev, "ROOT", SOURCE.parents[1]), patch.object(dev, "run", return_value="fixture-id\n"), \
                patch.object(dev, "docker_field", return_value="sha256:fixture"):
            result = verifier.runtime_metadata()
        serialized = json.dumps(result)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("/private", serialized)
        self.assertEqual(result["model_id"], "fixture-model")
        self.assertEqual(len(result["adapter_source_sha256"]), 64)
        self.assertEqual(requests[0][1]["Cookie"], "nexpoly_dev_session=secret-cookie")


if __name__ == "__main__":
    unittest.main()
