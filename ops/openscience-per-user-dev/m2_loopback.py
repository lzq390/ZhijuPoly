"""Run the existing synthetic model and a private Unix transport into its loopback namespace."""
import asyncio
import os
import runpy
import sys
import threading

SOCKET = "/m2-transport/api"


async def relay(reader, writer):
    upstream = None
    tasks = []
    try:
        peer, upstream = await asyncio.open_connection("127.0.0.1", 4096)

        async def copy(source, target):
            while data := await source.read(65536):
                target.write(data)
                await target.drain()

        tasks = [asyncio.create_task(copy(reader, upstream)), asyncio.create_task(copy(peer, writer))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except OSError:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if upstream:
            upstream.close()
        writer.close()


async def main():
    server = await asyncio.start_unix_server(relay, SOCKET)
    # Peer UIDs differ; the enclosing host runtime directory is owner-only.
    os.chmod(SOCKET, 0o666)
    async with server:
        await server.serve_forever()


def run_model():
    sys.argv[:] = ["/fixture.py", "serve"]
    runpy.run_path("/fixture.py", run_name="__main__")


if __name__ == "__main__":
    threading.Thread(target=run_model, daemon=True).start()
    asyncio.run(main())
