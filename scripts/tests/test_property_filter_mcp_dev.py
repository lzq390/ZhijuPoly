from __future__ import annotations

import contextlib
import hashlib
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


SOURCE = Path(__file__).resolve().parents[1] / "property_filter_mcp_dev.py"
SPEC = importlib.util.spec_from_file_location("property_filter_mcp_dev", SOURCE)
dev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dev)


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
            for name, value in headers.items():
                self.send_header(name, value)
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


class DevelopmentLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / ".runtime" / "property-filter-mcp"
        self.root_patch = patch.object(dev, "ROOT", self.root)
        self.runtime_patch = patch.object(dev, "RUNTIME", self.runtime)
        self.root_patch.start()
        self.runtime_patch.start()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.runtime_patch.stop)

    def test_setup_keeps_plaintext_client_secret_out_of_server_mount(self):
        sql_commands = []

        def database(sql):
            if sql.startswith("SELECT EXISTS"):
                return "t" if sql_commands else "f"
            sql_commands.append(sql)
            return ""

        with patch.object(dev, "verify_dev_target"), patch.object(dev, "verify_project_ownership"), \
                patch.object(dev, "psql", side_effect=database):
            result = dev.setup()
            first_client = dev.read_private("client.env")
            dev.setup()
        token = dict(line.split("=", 1) for line in first_client.splitlines())["NEXPOLY_MCP_TOKEN"]
        registry = json.loads(dev.read_private("server/clients.json"))
        self.assertEqual(registry["clients"][0]["token_sha256"], hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(dev.read_private("client.env"), first_client)
        self.assertEqual({path.name for path in (self.runtime / "server").iterdir()}, {"dsn", "clients.json"})
        self.assertNotIn(token, json.dumps(result))
        self.assertNotIn(token, "".join(sql_commands))
        self.assertNotIn(dev.read_private("db-password").strip(), "".join(sql_commands))
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.runtime / "server").stat().st_mode), 0o700)
        for name in ("db-password", "client.env", "server/dsn", "server/clients.json"):
            self.assertEqual(stat.S_IMODE((self.runtime / name).stat().st_mode), 0o600)

    def test_setup_does_not_reenable_revoked_client(self):
        with patch.object(dev, "verify_dev_target"), patch.object(dev, "verify_project_ownership"), \
                patch.object(dev, "psql", return_value="f"):
            dev.setup()
            dev.write_private("server/clients.json", '{"clients":[]}\n')
            dev.setup()
        self.assertEqual(json.loads(dev.read_private("server/clients.json")), {"clients": []})

    def test_token_rotation_replaces_hash_and_client_secret(self):
        dev.private_directory()
        dev.create_client()
        first = json.loads(dev.read_private("server/clients.json"))["clients"][0]["token_sha256"]
        dev.create_client(replace=True)
        second = json.loads(dev.read_private("server/clients.json"))["clients"][0]["token_sha256"]
        self.assertNotEqual(first, second)

    def test_runtime_symlink_cannot_redirect_credentials(self):
        (self.root / ".runtime").mkdir()
        self.runtime.symlink_to(self.root)
        with self.assertRaises(dev.DevError):
            dev.private_directory()

    def test_secret_file_symlink_is_rejected(self):
        dev.private_directory()
        target = self.root / "outside"
        target.write_text("outside")
        (self.runtime / "client.env").symlink_to(target)
        with self.assertRaises(dev.DevError):
            dev.write_private("client.env", "new secret")
        self.assertEqual(target.read_text(), "outside")

    def test_production_container_label_is_rejected_before_database_access(self):
        with patch.object(dev, "docker_field", return_value={"com.docker.compose.project": "nexpoly"}), \
                patch.object(dev, "psql") as database:
            with self.assertRaises(dev.DevError):
                dev.verify_dev_target()
        database.assert_not_called()

    def test_compose_ignores_existing_environment_and_uses_only_mcp_project(self):
        with patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": "nexpoly", "NEXPOLY_MCP_DSN": "private-value"}), \
                patch.object(dev, "run", return_value="") as command:
            dev.compose("down")
        argv = command.call_args.args[0]
        self.assertEqual(argv[argv.index("-p") + 1], "nexpoly_mcp_dev")
        self.assertEqual(argv[argv.index("--env-file") + 1], "/dev/null")
        self.assertNotIn("COMPOSE_PROJECT_NAME", command.call_args.kwargs["env"])
        self.assertNotIn("NEXPOLY_MCP_DSN", command.call_args.kwargs["env"])

    def test_subprocess_error_never_reports_raw_sql_or_secret(self):
        failure = subprocess.CompletedProcess(["psql"], 1, "secret-output", "secret-sql-error")
        with patch.object(dev.subprocess, "run", return_value=failure):
            with self.assertRaises(dev.DevError) as raised:
                dev.run(["psql"], payload="secret-sql")
        self.assertNotIn("secret", str(raised.exception))

    def test_build_failure_preserves_private_diagnostics_without_printing_them(self):
        def failing_command(command, **kwargs):
            kwargs["stdout"].write("synthetic private build failure\n")
            return subprocess.CompletedProcess(command, 1)
        output = io.StringIO()
        with patch.object(dev.subprocess, "run", side_effect=failing_command), contextlib.redirect_stdout(output):
            with self.assertRaises(dev.DevError):
                dev.run(["docker", "compose", "up"], diagnostics="docker-up.log")
        self.assertEqual(dev.read_private("docker-up.log"), "synthetic private build failure\n")
        self.assertEqual(stat.S_IMODE((self.runtime / "docker-up.log").stat().st_mode), 0o600)
        self.assertNotIn("synthetic private", output.getvalue())

    def test_sql_input_cannot_be_logged(self):
        with self.assertRaises(dev.DevError):
            dev.run(["psql"], payload="sensitive SQL", diagnostics="docker-up.log")
        self.assertFalse((self.runtime / "docker-up.log").exists())

    def test_smoke_ignores_environment_proxy_and_keeps_token_on_loopback(self):
        proxy_requests = []
        service_requests = []

        def proxy(path, headers, body):
            proxy_requests.append(headers)
            return 502, {}, {}

        def service(path, headers, body):
            service_requests.append(headers)
            message = json.loads(body)
            if message["method"] == "initialize":
                result = {"protocolVersion": "2025-11-25"}
            elif message["method"] == "tools/list":
                result = {"tools": [{"name": name} for name in dev.TOOLS]}
            else:
                result = {"content": []}
            return 200, {}, {"jsonrpc": "2.0", "id": message["id"], "result": result}

        with local_http_server(proxy) as proxy_url, local_http_server(service) as service_url:
            with patch.object(dev, "BASE_URL", service_url), patch.dict(os.environ, {
                "HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url, "ALL_PROXY": proxy_url,
                "http_proxy": proxy_url, "https_proxy": proxy_url, "all_proxy": proxy_url,
                "NO_PROXY": "", "no_proxy": "",
            }):
                dev.private_directory()
                dev.create_client()
                self.assertEqual(dev.smoke()["catalog_call"], "ok")
        self.assertEqual(proxy_requests, [])
        self.assertEqual(len(service_requests), 3)
        self.assertTrue(all(headers["Authorization"].startswith("Bearer ") for headers in service_requests))

    def test_smoke_refuses_redirect_before_forwarding_bearer(self):
        destination_requests = []

        def destination(path, headers, body):
            destination_requests.append(headers)
            return 200, {}, {}

        with local_http_server(destination) as destination_url:
            def redirect(path, headers, body):
                return 302, {"Location": destination_url + "/stolen"}, {}

            with local_http_server(redirect) as service_url, patch.object(dev, "BASE_URL", service_url):
                dev.private_directory()
                dev.create_client()
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    dev.smoke()
                self.assertEqual(failure.exception.code, 302)
        self.assertEqual(destination_requests, [])

    def test_foreign_checkout_container_is_not_controlled(self):
        labels = {"com.docker.compose.project": dev.PROJECT,
                  "com.docker.compose.service": dev.SERVICE,
                  "com.docker.compose.project.working_dir": "/another/checkout"}
        with patch.object(dev, "run", side_effect=["container-id\n", json.dumps(labels)]):
            with self.assertRaisesRegex(dev.DevError, "another checkout"):
                dev.verify_project_ownership()

    def test_foreign_credential_mount_is_not_controlled_even_with_matching_checkout(self):
        labels = {"com.docker.compose.project": dev.PROJECT,
                  "com.docker.compose.service": dev.SERVICE,
                  "com.docker.compose.project.working_dir": str(self.root)}
        mounts = [{"Type": "bind", "Destination": "/run/property-filter-mcp",
                   "Source": "/another/checkout/.runtime/property-filter-mcp/server", "RW": False}]
        with patch.object(dev, "run", side_effect=["container-id\n", json.dumps(labels), json.dumps(mounts)]):
            with self.assertRaisesRegex(dev.DevError, "another runtime"):
                dev.verify_project_ownership()

    def test_matching_checkout_and_private_mount_are_accepted(self):
        labels = {"com.docker.compose.project": dev.PROJECT,
                  "com.docker.compose.service": dev.SERVICE,
                  "com.docker.compose.project.working_dir": str(self.root)}
        mounts = [{"Type": "bind", "Destination": "/run/property-filter-mcp",
                   "Source": str(self.runtime / "server"), "RW": False}]
        with patch.object(dev, "run", side_effect=["container-id\n", json.dumps(labels), json.dumps(mounts)]):
            dev.verify_project_ownership()

    def test_setup_cannot_take_over_existing_role_after_other_checkout_was_stopped(self):
        with patch.object(dev, "run", return_value=""), patch.object(dev, "verify_dev_target"), \
                patch.object(dev, "psql", return_value="t") as database:
            with self.assertRaisesRegex(dev.DevError, "no database credential"):
                dev.setup()
        self.assertEqual(database.call_count, 1)
        self.assertTrue(database.call_args.args[0].startswith("SELECT EXISTS"))
        self.assertFalse((self.runtime / "db-password").exists())
        self.assertFalse((self.runtime / "server/clients.json").exists())

    def test_every_mutating_command_rejects_foreign_project_before_local_or_docker_changes(self):
        for action in ("setup", "up", "down", "revoke", "rotate-token"):
            with self.subTest(action=action), patch.object(dev.sys, "argv", [str(SOURCE), action]), \
                    patch.object(dev, "verify_project_ownership", side_effect=dev.DevError("another checkout")), \
                    patch.object(dev, "compose") as compose, patch.object(dev, "psql") as database, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(dev.main(), 1)
                compose.assert_not_called()
                database.assert_not_called()
        self.assertFalse(self.runtime.exists())

    def test_up_rejects_wrong_database_target_and_password_before_compose(self):
        password = "a" * 48
        cases = {
            "host": f"postgresql://{dev.ROLE}:{password}@another-database:5432/{dev.DATABASE}",
            "port": f"postgresql://{dev.ROLE}:{password}@{dev.DEV_CONTAINER}:5433/{dev.DATABASE}",
            "database": f"postgresql://{dev.ROLE}:{password}@{dev.DEV_CONTAINER}:5432/another_database",
            "role": f"postgresql://another_role:{password}@{dev.DEV_CONTAINER}:5432/{dev.DATABASE}",
            "password": f"postgresql://{dev.ROLE}:{'b' * 48}@{dev.DEV_CONTAINER}:5432/{dev.DATABASE}",
        }
        dev.private_directory()
        dev.write_private("db-password", password + "\n")
        dev.write_private("server/clients.json", '{"clients":[]}\n')
        for name, dsn in cases.items():
            dev.write_private("server/dsn", dsn + "\n")
            error = io.StringIO()
            with self.subTest(field=name), patch.object(dev.sys, "argv", [str(SOURCE), "up"]), \
                    patch.object(dev, "verify_project_ownership"), patch.object(dev, "verify_dev_target"), \
                    patch.object(dev, "compose") as compose, contextlib.redirect_stderr(error):
                self.assertEqual(dev.main(), 1)
                compose.assert_not_called()
            self.assertNotIn(dsn, error.getvalue())
            self.assertNotIn(password, error.getvalue())

    def test_up_accepts_only_matching_local_password_and_fixed_development_dsn(self):
        password = "a" * 48
        dev.private_directory()
        dev.write_private("db-password", password + "\n")
        dev.write_private("server/dsn", f"postgresql://{dev.ROLE}:{password}@{dev.DEV_CONTAINER}:5432/{dev.DATABASE}\n")
        dev.write_private("server/clients.json", '{"clients":[]}\n')
        with patch.object(dev.sys, "argv", [str(SOURCE), "up"]), \
                patch.object(dev, "verify_project_ownership"), patch.object(dev, "verify_dev_target"), \
                patch.object(dev, "compose") as compose, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dev.main(), 0)
        compose.assert_called_once_with("up", "-d", "--build", "--wait", "--wait-timeout", "90", dev.SERVICE)


if __name__ == "__main__":
    unittest.main()
