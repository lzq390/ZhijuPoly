"""Restricted development CONNECT relay to the host's existing outbound proxy.

The bridge side accepts only the new OpenScience subnet. The host side listens
only on a private Unix socket, avoiding changes to the host firewall or proxy.
Both sides allow only the configured model-provider TLS authority.
No credentials, request bodies, URLs, or tunnel data are logged or persisted.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
from pathlib import Path
import stat
import sys

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 17892
SOCKET_PATH = Path("/run/openscience-proxy/model.sock")
CLIENT_NETWORK = ipaddress.ip_network("192.168.80.0/20")
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 17891
PROVIDER_AUTHORITY = "api.relayrouter.ai:443"
MAX_HEADER_BYTES = 8192


def allowed_peer(address: str) -> bool:
    try:
        return ipaddress.ip_address(address) in CLIENT_NETWORK
    except ValueError:
        return False


def allowed_request(header: bytes) -> bool:
    if len(header) > MAX_HEADER_BYTES or not header.endswith(b"\r\n\r\n"):
        return False
    try:
        method, authority, version = header.split(b"\r\n", 1)[0].decode("ascii").split(" ")
    except (UnicodeDecodeError, ValueError):
        return False
    return method == "CONNECT" and authority == PROVIDER_AUTHORITY and version in {"HTTP/1.0", "HTTP/1.1"}


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await asyncio.wait_for(reader.read(65536), timeout=120):
        writer.write(data)
        await writer.drain()


async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, mode: str) -> None:
    upstream = None
    transfers: set[asyncio.Task] = set()
    try:
        peer = writer.get_extra_info("peername")
        if mode == "bridge" and (not peer or not allowed_peer(peer[0])):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        if not allowed_request(header):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        connect = (asyncio.open_unix_connection(str(SOCKET_PATH)) if mode == "bridge"
                   else asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT))
        upstream_reader, upstream = await asyncio.wait_for(connect, timeout=5)
        # Forward a fresh CONNECT envelope, never arbitrary caller-supplied
        # headers (including proxy authorization) to the next hop.
        upstream.write(f"CONNECT {PROVIDER_AUTHORITY} HTTP/1.1\r\nHost: {PROVIDER_AUTHORITY}\r\n\r\n".encode("ascii"))
        await upstream.drain()
        transfers = {asyncio.create_task(pipe(reader, upstream)), asyncio.create_task(pipe(upstream_reader, writer))}
        await asyncio.wait(transfers, return_when=asyncio.FIRST_COMPLETED)
    except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        pass
    finally:
        for task in transfers:
            task.cancel()
        if transfers:
            await asyncio.gather(*transfers, return_exceptions=True)
        for connection in (upstream, writer):
            if connection is not None:
                connection.close()
                try:
                    await connection.wait_closed()
                except OSError:
                    pass


async def main(mode: str) -> None:
    if mode not in {"bridge", "egress"}:
        raise ValueError("Expected bridge or egress mode")
    active = 0

    async def bounded(reader, writer):
        nonlocal active
        if active >= 32:
            writer.close()
            await writer.wait_closed()
            return
        active += 1
        try:
            await relay(reader, writer, mode)
        finally:
            active -= 1

    if mode == "egress":
        parent = SOCKET_PATH.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise PermissionError("Proxy socket directory must be private and owned by the service user")
        try:
            existing = SOCKET_PATH.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
                raise PermissionError("Refusing to replace an unexpected proxy socket path")
            SOCKET_PATH.unlink()
        os.umask(0o077)
        server = await asyncio.start_unix_server(bounded, path=str(SOCKET_PATH), limit=MAX_HEADER_BYTES)
        SOCKET_PATH.chmod(0o600)
    else:
        server = await asyncio.start_server(bounded, LISTEN_HOST, LISTEN_PORT, limit=MAX_HEADER_BYTES)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"bridge", "egress"}:
        raise SystemExit("Usage: model_proxy.py bridge|egress")
    asyncio.run(main(sys.argv[1]))
