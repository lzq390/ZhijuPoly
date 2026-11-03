"""Pure acceptance-runner tests; no Docker, credentials or database access."""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


SOURCE = Path(__file__).resolve().parents[1] / "verify_property_filter_mcp.py"
SPEC = importlib.util.spec_from_file_location("verify_property_filter_mcp", SOURCE)
verify_mcp = importlib.util.module_from_spec(SPEC)
with patch.object(sys, "path", [str(SOURCE.parent), *sys.path]):
    SPEC.loader.exec_module(verify_mcp)


class AcceptanceEndpointTests(unittest.TestCase):
    def test_only_fixed_endpoints_are_accepted_before_credentials(self):
        self.assertEqual(verify_mcp.endpoint_url("direct"), "http://127.0.0.1:18110/mcp")
        self.assertEqual(verify_mcp.endpoint_url("gateway"), "http://127.0.0.1:9001/mcp")
        for endpoint in ("http://example.invalid/mcp", "http://127.0.0.1:9001/mcp", "production", ""):
            with self.subTest(endpoint=endpoint), patch.object(verify_mcp, "target_credentials") as credentials:
                with self.assertRaisesRegex(verify_mcp.VerificationError, "direct or gateway"):
                    asyncio.run(verify_mcp.verify(endpoint))
                credentials.assert_not_called()

    def test_cli_defaults_direct_and_stores_separate_gateway_report(self):
        for arguments, endpoint, filename in (
            ([], "direct", "acceptance.json"),
            (["--endpoint", "direct"], "direct", "acceptance.json"),
            (["--endpoint", "gateway"], "gateway", "acceptance-gateway.json"),
        ):
            with self.subTest(endpoint=endpoint):
                report = {"status": "passed", "endpoint": endpoint, "read_only": True}
                output = io.StringIO()
                with patch.object(verify_mcp, "verify", AsyncMock(return_value=report)) as run, \
                        patch.object(verify_mcp, "write_private") as write, contextlib.redirect_stdout(output):
                    self.assertEqual(verify_mcp.main(arguments), 0)
                run.assert_awaited_once_with(endpoint)
                self.assertEqual(write.call_args.args[0], filename)
                self.assertEqual(json.loads(write.call_args.args[1]), report)
                self.assertEqual(json.loads(output.getvalue()), report)

    def test_cli_rejects_arbitrary_url_without_running_verification(self):
        with patch.object(verify_mcp, "verify", AsyncMock()) as run, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                verify_mcp.main(["--endpoint", "http://example.invalid/mcp"])
        self.assertEqual(raised.exception.code, 2)
        run.assert_not_awaited()

    def test_failed_verification_preserves_previous_report_and_hides_transport_details(self):
        output = io.StringIO()
        failure = RuntimeError("Authorization: Bearer synthetic-private-token")
        with patch.object(verify_mcp, "verify", AsyncMock(side_effect=failure)), \
                patch.object(verify_mcp, "write_private") as write, contextlib.redirect_stdout(output):
            self.assertEqual(verify_mcp.main(["--endpoint", "gateway"]), 1)
        write.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["endpoint"], "gateway")
        self.assertEqual(payload["error_type"], "RuntimeError")
        self.assertNotIn("synthetic-private-token", output.getvalue())

    def test_transport_uses_selected_endpoint_and_direct_service_health_without_redirects(self):
        for endpoint in ("direct", "gateway"):
            with self.subTest(endpoint=endpoint):
                observed = self.exercise_transport(endpoint)
                self.assertEqual(observed["health_urls"], ["http://127.0.0.1:18110/healthz"])
                self.assertEqual(observed["transport_urls"], [verify_mcp.ENDPOINT_URLS[endpoint]])
                self.assertIs(observed["http_options"]["follow_redirects"], False)
                self.assertIs(observed["http_options"]["trust_env"], False)

    def test_redirect_is_rejected_before_transport_or_database_access(self):
        observed = self.exercise_transport("gateway", response_status=307)
        self.assertEqual(observed["health_urls"], ["http://127.0.0.1:18110/healthz"])
        self.assertEqual(observed["transport_urls"], [])

    def exercise_transport(self, endpoint, response_status=200):
        observed = {"health_urls": [], "transport_urls": []}

        class Response:
            status_code = response_status

            async def aread(self):
                return b'{"status":"ok"}'

            def json(self):
                return {"status": "ok"}

        class HTTP:
            def __init__(self, **kwargs):
                observed["http_options"] = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url):
                observed["health_urls"].append(url)
                response = Response()
                for hook in observed["http_options"]["event_hooks"]["response"]:
                    await hook(response)
                return response

        @contextlib.asynccontextmanager
        async def transport(url, **kwargs):
            observed["transport_urls"].append(url)
            yield None, None

        class Client:
            def __init__(self, transport, **kwargs):
                self.transport = transport

            async def __aenter__(self):
                await self.transport.__aenter__()
                return self

            async def __aexit__(self, *args):
                return await self.transport.__aexit__(*args)

            async def list_tools(self):
                # Stop after protocol routing; real tool and SQL validation are
                # retained in the runner and exercised by development acceptance.
                return SimpleNamespace(tools=[])

        with patch.object(verify_mcp, "target_credentials", return_value=("synthetic-dsn", "synthetic-token", {})), \
                patch.object(verify_mcp.httpx2, "AsyncClient", HTTP), \
                patch.object(verify_mcp, "streamable_http_client", transport), \
                patch.object(verify_mcp, "Client", Client), \
                patch.object(verify_mcp.psycopg, "connect") as database:
            message = "must not redirect" if response_status == 307 else "exactly the three"
            with self.assertRaisesRegex(verify_mcp.VerificationError, message):
                asyncio.run(verify_mcp.verify(endpoint))
            database.assert_not_called()
        return observed


if __name__ == "__main__":
    unittest.main()
