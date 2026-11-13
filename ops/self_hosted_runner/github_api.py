"""Repository-scoped GitHub App authentication. Credentials never enter state."""
from __future__ import annotations

import base64
import contextlib
import datetime
import json
import re
import subprocess
import time
import urllib.error
import urllib.request

from common import Config, RunnerError, regular_file


class APIError(RunnerError):
    def __init__(self, status: int = 0, retry_after: int = 30):
        self.status = status
        self.retry_after = min(900, max(30, retry_after))
        super().__init__(f"GitHub API unavailable (HTTP {status or 'transport error'})")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the App bearer credential to another endpoint.
        raise APIError(code)


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class GitHub:
    def __init__(self, config: Config):
        self.config = config
        self.prefix = f"/repos/{config.repository}"
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": config.host_proxy}), NoRedirect())
        self._token = ""
        self._expires = 0.0
        self._deadline = None

    @contextlib.contextmanager
    def budget(self, seconds=20):
        previous = self._deadline
        self._deadline = time.monotonic() + seconds
        try:
            yield
        finally:
            self._deadline = previous

    @staticmethod
    def validate_runner(record):
        if not isinstance(record, dict) or type(record.get("id")) is not int or not isinstance(record.get("name"), str) or type(record.get("busy")) is not bool:
            raise APIError()
        labels = record.get("labels")
        if not isinstance(labels, list) or not all(isinstance(label, dict) and isinstance(label.get("name"), str) for label in labels):
            raise APIError()
        return record

    def jwt(self) -> str:
        if not self.config.app_id or not self.config.installation_id:
            raise RunnerError("Configure the dedicated GitHub App and installation IDs first")
        info = regular_file(self.config.private_key_file, root_owned=True)
        if info.st_mode & 0o077:
            raise RunnerError("App private key must have mode 0600")
        now = int(time.time())
        header = b64(b'{"alg":"RS256","typ":"JWT"}')
        claims = b64(json.dumps({"iat": now - 60, "exp": now + 540,
                                 "iss": str(self.config.app_id)}, separators=(",", ":")).encode())
        body = f"{header}.{claims}"
        try:
            signed = subprocess.run(["/usr/bin/openssl", "dgst", "-sha256", "-sign",
                                     str(self.config.private_key_file)], input=body.encode(),
                                    capture_output=True, timeout=10, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RunnerError("Cannot sign GitHub App authentication request") from exc
        return f"{body}.{b64(signed.stdout)}"

    def _request(self, method: str, path: str, bearer: str, payload=None):
        if not path.startswith((self.prefix + "/", "/app/installations/")) or "\n" in path:
            raise RunnerError("Unexpected GitHub API endpoint")
        request = urllib.request.Request("https://api.github.com" + path,
            data=None if payload is None else json.dumps(payload).encode(), method=method,
            headers={"Authorization": "Bearer " + bearer, "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "nexpoly-runner-controller",
                     "Content-Type": "application/json"})
        try:
            remaining = 20 if self._deadline is None else self._deadline - time.monotonic()
            if remaining <= 0:
                raise APIError()
            with self.opener.open(request, timeout=min(20, remaining)) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise APIError(response.status)
                result = json.loads(raw) if raw else None
                if method != "DELETE" and not isinstance(result, dict):
                    raise APIError()
                return result
        except urllib.error.HTTPError as exc:
            retry = exc.headers.get("Retry-After", "30")
            raise APIError(exc.code, int(retry) if retry.isdecimal() else 30) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise APIError() from None

    def token(self) -> str:
        if time.time() < self._expires - 90:
            return self._token
        result = self._request("POST", f"/app/installations/{self.config.installation_id}/access_tokens",
                               self.jwt(), {"repositories": [self.config.repository.split("/")[1]],
                                            "permissions": {"administration": "write", "actions": "read"}})
        try:
            self._token = result["token"]
            self._expires = datetime.datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00")).timestamp()
            if not isinstance(self._token, str) or not self._token:
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError):
            self._token = ""
            raise APIError() from None
        return self._token

    def request(self, method: str, suffix: str, payload=None):
        try:
            return self._request(method, self.prefix + suffix, self.token(), payload)
        except APIError as exc:
            if exc.status == 401:
                self._expires = 0
            raise

    def registration_token(self) -> str:
        result = self.request("POST", "/actions/runners/registration-token", {})
        value = result.get("token", "")
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", value):
            raise APIError()
        return value

    def find_runner(self, name: str):
        for page in range(1, 101):
            result = self.request("GET", f"/actions/runners?per_page=100&page={page}")
            runners = result.get("runners")
            if not isinstance(runners, list) or not all(isinstance(r, dict) and type(r.get("id")) is int and isinstance(r.get("name"), str) for r in runners):
                raise APIError()
            found = [r for r in runners if r.get("name") == name]
            if len(found) > 1:
                raise RunnerError("Duplicate registered runner names")
            if found:
                return self.validate_runner(found[0])
            if len(runners) < 100:
                return None
        raise APIError()

    def runner(self, runner_id: int):
        try:
            result = self.request("GET", f"/actions/runners/{int(runner_id)}")
            return self.validate_runner(result)
        except APIError as exc:
            if exc.status == 404:
                return None
            raise

    def delete_runner(self, runner_id: int) -> None:
        try:
            self.request("DELETE", f"/actions/runners/{int(runner_id)}")
        except APIError as exc:
            if exc.status != 404:
                raise

    def job(self, job_id: int):
        result = self.request("GET", f"/actions/jobs/{int(job_id)}")
        if type(result.get("id")) is not int or result.get("status") not in ("queued", "waiting", "pending", "in_progress", "completed"):
            raise APIError()
        return result

    def find_job(self, run_id: int, runner_id: int):
        for page in range(1, 101):
            result = self.request("GET", f"/actions/runs/{int(run_id)}/jobs?filter=all&per_page=100&page={page}")
            jobs = result.get("jobs")
            if not isinstance(jobs, list) or not all(isinstance(job, dict) and type(job.get("id")) is int for job in jobs):
                raise APIError()
            found = [job for job in jobs if job.get("runner_id") == runner_id]
            if found:
                return max(found, key=lambda job: job["id"])
            if len(jobs) < 100:
                return None
        raise APIError()

    def discover_job(self, runner_id: int):
        # Recovery when the guest hook was lost. Limit expensive discovery to
        # active runs; runner busy state independently starts the hard deadline.
        result = self.request("GET", "/actions/runs?status=in_progress&per_page=100")
        runs = result.get("workflow_runs")
        if not isinstance(runs, list) or not all(isinstance(run, dict) and type(run.get("id")) is int for run in runs):
            raise APIError()
        for run in runs:
            job = self.find_job(int(run["id"]), runner_id)
            if job:
                return job
        return None
