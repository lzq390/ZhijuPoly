from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest

import httpx

from workers.polymer_md_analysis.tests.helpers import fixture, query


class CliTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_socket_lifespan_probe_and_real_synthetic_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            body, _, _ = fixture(root / "jobs")
            uds = root / "analysis.sock"
            environment = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG"}}
            environment.update(PYTHONPATH=str(Path(__file__).resolve().parents[3]), PYTHONDONTWRITEBYTECODE="1",
                POLYMER_MD_ANALYSIS_JOB_ROOT=str(root / "jobs"), POLYMER_MD_ANALYSIS_SPOOL_ROOT=str(root / "spool"),
                POLYMER_MD_ANALYSIS_MIN_FREE_BYTES="0", POLYMER_MD_ANALYSIS_FREE_FRACTION="0")
            process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m", "workers.polymer_md_analysis", "--uds", str(uds),
                cwd=str(Path(__file__).resolve().parents[3]), env=environment, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(uds)), base_url="http://reader", timeout=5) as client:
                    for _ in range(150):
                        if process.returncode is not None:
                            self.fail("isolated analysis CLI exited before becoming healthy")
                        try:
                            health = await client.get("/health")
                            if health.status_code == 200 and health.json().get("ready"):
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(.05)
                    else:
                        self.fail("isolated analysis CLI did not become healthy")
                    self.assertEqual(health.json()["service"], "polymer-md-analysis")
                    response = await client.post("/distance", json=query(body).model_dump(mode="json"))
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(len(response.json()["points"]), 29)
            finally:
                if process.returncode is None:
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    process.kill(); await process.wait()
            self.assertFalse(uds.exists())
            self.assertFalse(list((root / "spool").glob("request-*")))


if __name__ == "__main__":
    unittest.main()
