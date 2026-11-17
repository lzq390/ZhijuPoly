from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "retrosynthesis_mcp_dev.py"
SPEC = importlib.util.spec_from_file_location("retrosynthesis_mcp_dev_tested", SOURCE)
dev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dev)
sys.modules.setdefault("retrosynthesis_mcp_dev", dev)
VERIFY_SPEC = importlib.util.spec_from_file_location("retrosynthesis_mcp_verifier_tested", SOURCE.with_name("verify_retrosynthesis_mcp.py"))
verifier = importlib.util.module_from_spec(VERIFY_SPEC)
VERIFY_SPEC.loader.exec_module(verifier)
verifier.dev = dev
ACCOUNT = "628cf3d3-c456-4fc5-a8a4-51d135e273ba"
SESSION = "c89168b0-d9a5-4f53-bf8f-f0ed5fa05730"


@contextlib.contextmanager
def http_server(respond):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, payload = respond(self.path, dict(self.headers))
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


class RetrosynthesisDevelopmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / ".runtime/retrosynthesis-mcp"
        for name, value in (("ROOT", self.root), ("RUNTIME", self.runtime)):
            target = patch.object(dev, name, value)
            target.start()
            self.addCleanup(target.stop)
        # Journal setup uses a stdlib-only module from the real source tree.
        self.path_patch = patch.object(sys, "path", [str(SOURCE.parents[1] / "backend"), *sys.path])
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def identity(self):
        dev.private_directory()
        dev.write_private("server/identity.json", json.dumps({"account_uuid": ACCOUNT, "backend_url": dev.BACKEND_URL}))

    def session(self):
        self.identity()
        value = {"account_uuid": ACCOUNT, "backend_url": dev.BACKEND_URL, "cookie": "nexpoly_dev_session=private-cookie",
            "session_id": SESSION, "csrf_token": "private-csrf", "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        dev.write_private("server/session.json", json.dumps(value))
        return value

    def test_setup_keeps_fixed_account_journal_and_revocations(self):
        with patch.object(dev, "verify_project_ownership"), patch.object(dev, "verify_dev_target"):
            dev.setup(ACCOUNT)
            before = dev.read_private("journal/submissions.sqlite3.identity")
            dev.write_private("server/clients.json", '{"clients":[]}')
            dev.setup(None)
            self.assertEqual(dev.read_private("journal/submissions.sqlite3.identity"), before)
            self.assertEqual(json.loads(dev.read_private("server/clients.json"))["clients"], [])
            with self.assertRaisesRegex(dev.DevError, "fixed"):
                dev.setup("cb8edea7-608f-42d0-ac49-c9530ed90a58")

    def test_missing_journal_is_not_recreated_on_repeat_setup(self):
        with patch.object(dev, "verify_project_ownership"), patch.object(dev, "verify_dev_target"):
            dev.setup(ACCOUNT)
            path = self.runtime / "journal/submissions.sqlite3"
            path.unlink()
            with self.assertRaisesRegex(dev.DevError, "preserve"):
                dev.setup(None)
            self.assertFalse(path.exists())

    def test_journal_rejects_conflicting_native_target(self):
        self.identity()
        dev.initialize_journal(first_setup=True)
        with sqlite3.connect(self.runtime / "journal/submissions.sqlite3") as db:
            db.execute("UPDATE metadata SET backend_url='http://other:8000',account_uuid=?", (ACCOUNT,))
        with self.assertRaisesRegex(dev.DevError, "validation"):
            dev.initialize_journal(first_setup=False)

    def test_credentials_are_private_separate_and_model_cannot_confirm(self):
        self.identity()
        dev.create_client()
        registry = json.loads(dev.read_private("server/clients.json"))["clients"]
        by_id = {item["client_id"]: item for item in registry}
        model, ui = by_id[dev.CLIENT_ID], by_id[dev.UI_CLIENT_ID]
        self.assertEqual(set(model["tools"]), set(dev.TOOLS))
        self.assertEqual(set(ui["tools"]), set(dev.UI_TOOLS))
        self.assertNotIn("confirm_monomer_retrosynthesis", model["tools"])
        self.assertEqual(ui["owner_client_id"], model["client_id"])
        tokens = [dict(line.split("=", 1) for line in dev.read_private(name).splitlines())["NEXPOLY_MCP_TOKEN"] for name in ("client.env", "ui-client.env")]
        self.assertNotEqual(*tokens)
        self.assertEqual(hashlib.sha256(tokens[0].encode()).hexdigest(), model["token_sha256"])
        self.assertEqual((self.runtime / "server/clients.json").stat().st_mode & 0o777, 0o600)

    def project_inventory(self):
        labels = {"com.docker.compose.project": dev.PROJECT, "com.docker.compose.service": dev.SERVICE,
            "com.docker.compose.project.working_dir": str(self.root),
            "com.docker.compose.project.config_files": str(self.root / "docker-compose.retrosynthesis-mcp-dev.yml")}
        mounts = [{"Type": "bind", "Destination": "/run/retrosynthesis-mcp", "Source": str(self.runtime / "server"), "RW": False},
            {"Type": "bind", "Destination": "/var/lib/retrosynthesis-mcp", "Source": str(self.runtime / "journal"), "RW": True},
            {"Type": "tmpfs", "Destination": "/tmp", "RW": True}]
        return ["container\n", json.dumps(labels), dev.IMAGE, json.dumps(mounts), json.dumps({dev.DEV_NETWORK: {}, dev.CLIENT_NETWORK: {}}), "{}"]

    def test_existing_project_requires_exact_checkout_mounts_networks_and_no_ports(self):
        with patch.object(dev, "run", side_effect=self.project_inventory()):
            dev.verify_project_ownership()
        for position, value in ((1, json.dumps({})), (3, "[]"), (4, json.dumps({"production": {}})), (5, json.dumps({"18211/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18211"}]}))):
            inventory = self.project_inventory()
            inventory[position] = value
            with self.subTest(position=position), patch.object(dev, "run", side_effect=inventory), self.assertRaises(dev.DevError):
                dev.verify_project_ownership()

    def test_private_files_reject_symlinks_and_hardlinks(self):
        self.identity()
        dev.write_private("secret", "private")
        (self.runtime / "linked").symlink_to(self.runtime / "secret")
        with self.assertRaises(OSError):
            dev.read_private("linked")
        os.link(self.runtime / "secret", self.runtime / "hardlinked")
        with self.assertRaises(dev.DevError):
            dev.read_private("hardlinked")

    def test_compose_environment_cannot_override_fixed_target(self):
        self.identity()
        with patch.dict(os.environ, {"COMPOSE_FILE": "/foreign.yml", "COMPOSE_PROJECT_NAME": "production",
                "NEXPOLY_RETROSYNTHESIS_MCP_BACKEND_URL": "http://foreign", "NEXPOLY_RETROSYNTHESIS_MCP_DEV_ACCOUNT_UUID": "foreign"}), \
                patch.object(dev, "run", return_value="") as run:
            dev.compose("ps")
        args, kwargs = run.call_args
        self.assertNotIn("COMPOSE_FILE", kwargs["env"])
        self.assertNotIn("COMPOSE_PROJECT_NAME", kwargs["env"])
        self.assertNotIn("NEXPOLY_RETROSYNTHESIS_MCP_BACKEND_URL", kwargs["env"])
        self.assertEqual(kwargs["env"]["NEXPOLY_RETROSYNTHESIS_MCP_DEV_ACCOUNT_UUID"], ACCOUNT)
        self.assertIn("/dev/null", args[0])

    def test_rpc_uses_internal_exec_and_passes_secret_only_over_stdin(self):
        self.identity()
        dev.create_client()
        with patch.object(dev, "run", return_value='{"jsonrpc":"2.0","id":1,"result":{"ok":true}}') as run:
            result = dev.rpc_client()("tools/list", {}, 1)
        self.assertTrue(result["ok"])
        command = run.call_args.args[0]
        payload = json.loads(run.call_args.kwargs["payload"])
        self.assertIn("exec", command)
        self.assertIn("-T", command)
        self.assertNotIn(payload["token"], " ".join(command))
        script = command[-1]
        self.assertIn("127.0.0.1:18211", script)
        self.assertIn("NoRedirect()", script)
        self.assertIn("ProxyHandler({})", script)
        self.assertIn("262144", script)

    def test_smoke_has_exact_scopes_and_never_confirms(self):
        calls = []
        def client(*, ui=False):
            def rpc(method, params, request_id):
                calls.append((ui, method, params))
                if method == "initialize":
                    return {"protocolVersion": "2025-11-25"}
                if method == "tools/list":
                    return {"tools": [{"name": name} for name in (dev.UI_TOOLS if ui else dev.TOOLS)]}
                return {"structuredContent": {"ok": True}}
            return rpc
        with patch.object(dev, "rpc_client", side_effect=client):
            self.assertFalse(dev.smoke()["generation_submitted"])
        self.assertEqual([params["name"] for _, method, params in calls if method == "tools/call"], ["get_retrosynthesis_capabilities"])

    def test_control_checks_ownership_inside_operation_lock(self):
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
        def guard(*args, **kwargs):
            self.assertTrue(active)
            return ""
        for action in ("up", "down", "status", "smoke"):
            with self.subTest(action=action), patch.object(dev, "operation_lock", locked), \
                    patch.object(dev, "verify_project_ownership", side_effect=guard), patch.object(dev, "verify_dev_target", side_effect=guard), \
                    patch.object(dev, "verify_session_file", side_effect=guard), patch.object(dev, "initialize_journal", side_effect=guard), \
                    patch.object(dev, "read_private", return_value="{}"), patch.object(dev, "compose", side_effect=guard), \
                    patch.object(dev, "smoke", side_effect=lambda: {"ok": bool(active)}), \
                    patch.object(sys, "argv", [str(SOURCE), action]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(dev.main(), 0)
            self.assertFalse(active)

    def test_import_session_revalidates_account_and_keeps_secrets_out_of_output(self):
        session = self.session()
        source = self.root / "authorized-session.json"
        source.write_text(json.dumps(session))
        source.chmod(0o600)
        requests = []
        def respond(path, headers):
            requests.append((path, headers))
            return 200, {"authenticated": True, "user": {"id": ACCOUNT, "must_change_password": False}, "account_type": "member",
                "capabilities": {"retrosynthesis.use": True}, "session_id": SESSION, "csrf_token": session["csrf_token"]}
        output = io.StringIO()
        with http_server(respond) as url, patch.object(dev, "BACKEND_LOOPBACK", url), \
                patch.object(dev, "verify_project_ownership"), patch.object(dev, "verify_dev_target"), \
                patch.object(sys, "argv", [str(SOURCE), "import-session", "--session-source", str(source)]), contextlib.redirect_stdout(output):
            self.assertEqual(dev.main(), 0)
        self.assertEqual(requests[0][0], "/api/v1/auth/session")
        self.assertEqual(requests[0][1]["Cookie"], session["cookie"])
        self.assertEqual(requests[0][1]["X-Session-Context"], SESSION)
        self.assertNotIn("private-cookie", output.getvalue())
        self.assertEqual(json.loads(dev.read_private("server/session.json")), session)

    def test_session_rejects_header_injection_expiry_and_account_changes(self):
        value = self.session()
        for changes in ({"cookie": "secret\r\nInjected: yes"}, {"csrf_token": ""}, {"expires_at": "2020-01-01T00:00:00Z"}, {"account_uuid": "foreign"}):
            with self.subTest(changes=changes), self.assertRaises(dev.DevError):
                dev.validate_session({**value, **changes}, dev.identity())

    @contextlib.contextmanager
    def verification(self, *, lose_confirm=False, status="completed"):
        calls = []
        canonical = "Nc1ccc(N)cc1"
        draft_id = "rtd_" + "a" * 32
        result = {"input_smiles": canonical, "canonical_smiles": canonical, "target_role": "auto", "inferred_target_role": "diamine",
            "model_id": "ReactionT5", "query_time_ms": 123.0, "total": 0, "candidates": []}
        def client(*, ui=False):
            def rpc(method, params, request_id):
                if method == "initialize":
                    return {"protocolVersion": "2025-11-25"}
                tool, args = params["name"], params["arguments"]
                calls.append((tool, ui))
                common = {"draft_id": draft_id, "conversation_id": args.get("conversation_id"), "revision": 2,
                    "resolved_target": {"canonical_smiles": canonical}, "job_id": None}
                if tool == "get_retrosynthesis_capabilities":
                    value = {"enabled": True, "available": True, "submission_ready": True}
                elif tool == "prepare_monomer_retrosynthesis":
                    value = {**common, "revision": 1, "ready": False}
                elif tool == "resolve_retrosynthesis_target":
                    value = {"status": "resolved", "target": {"canonical_smiles": canonical}}
                elif tool == "update_retrosynthesis_draft":
                    self.assertTrue(ui)
                    value = {**common, "ready": True}
                elif tool == "get_retrosynthesis_draft":
                    value = {**common, "ready": True, "job_id": "10000000-0000-0000-0000-000000000003" if len([tool for tool, _ in calls if tool == "prepare_monomer_retrosynthesis"]) == 0 else None}
                elif tool == "confirm_monomer_retrosynthesis":
                    self.assertTrue(ui)
                    reports = list(self.runtime.glob("acceptance-*.json"))
                    self.assertTrue(json.loads(reports[0].read_text())["submit_attempted"])
                    if lose_confirm:
                        raise OSError("private transport data")
                    value = {**common, "job_id": "10000000-0000-0000-0000-000000000003", "submission": {"replayed": True}}
                elif tool == "get_retrosynthesis_job":
                    value = {"job_id": args["job_id"], "status": status, "created_at": "2026-11-17T01:00:00Z",
                        "started_at": "2026-11-17T01:00:01Z", "finished_at": "2026-11-17T01:00:02Z", "result": result}
                else:
                    self.fail("Unexpected tool " + tool)
                return {"structuredContent": {"ok": True, **value}}
            return rpc
        with patch.object(dev, "verify_project_ownership"), patch.object(dev, "verify_dev_target"), patch.object(dev, "verify_session_file"), \
                patch.object(dev, "rpc_client", side_effect=client), patch.object(verifier, "runtime_metadata", return_value={"model_loaded_before": False}), \
                patch.object(verifier, "native_get", return_value={"job_id": "10000000-0000-0000-0000-000000000003", "status": "completed", "result": result}):
            yield calls

    def test_verifier_confirms_twice_one_job_and_records_truthful_timings(self):
        with self.verification() as calls:
            result = verifier.verify("stable-report-key", polls=1, scenario="cold")
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["returned_count"], 0)
            self.assertEqual(result["timings"]["queue_and_loading_ms"], 1000)
            self.assertEqual(result["timings"]["model_prediction_ms"], 123.0)
            with self.assertRaisesRegex(verifier.VerificationError, "submission marker"):
                verifier.verify("stable-report-key", polls=1)
        self.assertEqual(calls.count(("prepare_monomer_retrosynthesis", False)), 1)
        self.assertEqual(calls.count(("confirm_monomer_retrosynthesis", True)), 2)

    def test_verifier_persists_unknown_before_send_and_resume_never_confirms(self):
        with self.verification(lose_confirm=True) as calls:
            with self.assertRaises(OSError):
                verifier.verify("lost-response", polls=1)
            self.assertEqual(calls.count(("confirm_monomer_retrosynthesis", True)), 1)
        report_path = next(self.runtime.glob("acceptance-*.json"))
        report = json.loads(report_path.read_text())
        self.assertTrue(report["submit_attempted"])
        self.assertNotIn("private transport data", report["error"])
        with self.verification() as resumed:
            result = verifier.verify("lost-response", resume=True, polls=1)
            self.assertEqual(result["status"], "passed")
            self.assertFalse(any(tool in {"prepare_monomer_retrosynthesis", "confirm_monomer_retrosynthesis"} for tool, _ in resumed))

    def test_verifier_pending_resume_rejects_a_different_job(self):
        with self.verification(status="running"):
            pending = verifier.verify("running-task", polls=1)
            self.assertEqual(pending["status"], "pending")
        with self.verification() as resumed:
            result = verifier.verify("running-task", resume=True, polls=1)
            self.assertEqual(result["job_id"], pending["job_id"])
            self.assertFalse(any(tool == "confirm_monomer_retrosynthesis" for tool, _ in resumed))
            with self.assertRaisesRegex(verifier.VerificationError, "differs"):
                verifier.verify("running-task", job_id="different-job", polls=1)

    def test_verifier_never_calls_non_retrosynthesis_native_paths(self):
        with self.assertRaises(verifier.VerificationError):
            verifier.native_get("https://foreign.invalid/session")
        with self.assertRaises(verifier.VerificationError):
            verifier.native_get("/api/v1/monomer-retrosynthesis/../../admin")

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose parses deployment manifest")
    def test_compose_has_no_public_port_and_uses_only_development_networks(self):
        self.identity()
        environment = dev.compose_environment()
        result = subprocess.run(["docker", "compose", "--env-file", "/dev/null", "-f", str(SOURCE.parents[1] / "docker-compose.retrosynthesis-mcp-dev.yml"),
            "config", "--format", "json"], env=environment, capture_output=True, text=True, check=True)
        rendered = json.loads(result.stdout)
        service = rendered["services"][dev.SERVICE]
        self.assertFalse(service.get("ports"))
        self.assertTrue(service["read_only"])
        self.assertEqual(service["tmpfs"], ["/tmp:rw,noexec,nosuid,size=16m"])
        self.assertEqual(set(service["networks"]), {"development-backend", "development-clients"})
        self.assertTrue(rendered["networks"]["development-clients"]["internal"])
        self.assertNotIn("prod", json.dumps(rendered))


if __name__ == "__main__":
    unittest.main()
