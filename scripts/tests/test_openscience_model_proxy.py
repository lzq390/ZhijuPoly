"""Restricted model relay boundaries and local two-hop tunnel integration."""
import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[2] / "ops/openscience-mcp-dev/model_proxy.py"
SPEC = importlib.util.spec_from_file_location("model_proxy", PATH)
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


class ModelProxyTests(unittest.TestCase):
    def test_only_development_subnet(self):
        self.assertTrue(proxy.allowed_peer("192.168.80.2"))
        for address in ("127.0.0.1", "172.30.0.2", "192.168.64.2", "203.0.113.1", "::1", "invalid"):
            with self.subTest(address=address):
                self.assertFalse(proxy.allowed_peer(address))

    def test_only_exact_provider_tls_tunnel(self):
        self.assertTrue(proxy.allowed_request(b"CONNECT api.relayrouter.ai:443 HTTP/1.1\r\nHost: api.relayrouter.ai:443\r\n\r\n"))
        for request in (
            b"CONNECT attacker.example:443 HTTP/1.1\r\n\r\n",
            b"CONNECT api.relayrouter.ai:80 HTTP/1.1\r\n\r\n",
            b"GET http://api.relayrouter.ai/ HTTP/1.1\r\n\r\n",
            b"CONNECT 127.0.0.1:14096 HTTP/1.1\r\n\r\n",
            b"CONNECT api.relayrouter.ai:443 HTTP/1.1\n\n",
            b"CONNECT api.relayrouter.ai:443 HTTP/1.1\r\nX: " + b"x" * 8192 + b"\r\n\r\n",
        ):
            with self.subTest(request=request[:60]):
                self.assertFalse(proxy.allowed_request(request))


class ModelProxyTunnelTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_hop_unix_tunnel_and_target_restriction(self):
        received = []

        async def upstream(reader, writer):
            try:
                received.append(await reader.readuntil(b"\r\n\r\n"))
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                data = await reader.readexactly(4)
                writer.write(data)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "model.sock"
            upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
            port = upstream_server.sockets[0].getsockname()[1]
            async with upstream_server:
                with patch.object(proxy, "UPSTREAM_PORT", port), patch.object(proxy, "SOCKET_PATH", socket_path), patch.object(proxy, "allowed_peer", return_value=True):
                    egress = await asyncio.start_unix_server(lambda r, w: proxy.relay(r, w, "egress"), path=str(socket_path))
                    bridge = await asyncio.start_server(lambda r, w: proxy.relay(r, w, "bridge"), "127.0.0.1", 0)
                    async with egress, bridge:
                        address = bridge.sockets[0].getsockname()
                        reader, writer = await asyncio.open_connection(*address)
                        writer.write(b"CONNECT api.relayrouter.ai:443 HTTP/1.1\r\nProxy-Authorization: Bearer must-not-forward\r\n\r\n")
                        await writer.drain()
                        self.assertIn(b"200", await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2))
                        writer.write(b"ping")
                        await writer.drain()
                        self.assertEqual(await asyncio.wait_for(reader.readexactly(4), 2), b"ping")
                        writer.close()
                        await writer.wait_closed()
                        self.assertEqual(received, [b"CONNECT api.relayrouter.ai:443 HTTP/1.1\r\nHost: api.relayrouter.ai:443\r\n\r\n"])

                        reader, writer = await asyncio.open_connection(*address)
                        writer.write(b"CONNECT 127.0.0.1:14096 HTTP/1.1\r\n\r\n")
                        await writer.drain()
                        self.assertIn(b"403", await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2))
                        writer.close()
                        await writer.wait_closed()
                        self.assertEqual(len(received), 1)


if __name__ == "__main__":
    unittest.main()
