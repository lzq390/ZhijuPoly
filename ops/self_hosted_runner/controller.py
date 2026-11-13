#!/usr/bin/env python3
"""One disposable VM, one ephemeral runner, durable recovery before replenishment."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

# Installed code is a root-owned snapshot. -I deliberately excludes cwd/PYTHONPATH.
PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE))
from common import (Config, DEFAULT_CONFIG, GIB, RunnerError, atomic_json,
                    read_json, regular_file, validate_uuid)
from github_api import APIError, GitHub
from template import TemplateError

LOCK = Path("/run/nexpoly-runner-controller/controller.lock")
PROXY_HEALTH_URL = "https://github.com/robots.txt"
PROXY_HEALTH_USER_AGENT = "nexpoly-runner-health/1.0"


def proxy_failure_reason(error: OSError | ValueError) -> str:
    """Classify failures without copying remote responses or credentials to logs."""
    if isinstance(error, urllib.error.HTTPError):
        code = error.code
        return f"HTTP {code}" if type(code) is int and 100 <= code <= 599 else "HTTP failure"
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, TimeoutError):
        return "HTTPS request timed out"
    if isinstance(reason, ConnectionRefusedError):
        return "proxy connection refused"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "TLS certificate verification failed"
    if isinstance(reason, ssl.SSLError):
        return "TLS negotiation failed"
    if isinstance(reason, socket.gaierror):
        return "DNS lookup failed"
    # urllib represents a rejected HTTPS CONNECT as an OSError, not HTTPError.
    # Extract only its numeric status; the rest of the message is untrusted.
    message = reason.args[0] if isinstance(reason, OSError) and reason.args else None
    if isinstance(message, str):
        tunnel = re.match(r"Tunnel connection failed: ([1-5][0-9]{2})\b", message)
        if tunnel:
            return f"proxy CONNECT returned HTTP {tunnel[1]}"
    if isinstance(error, ValueError):
        return "invalid HTTPS proxy configuration"
    return "HTTPS connection failed"


def command(args: list[str], timeout: int = 40) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        # Do not include arbitrary external output (or credentials) in logs.
        raise RunnerError(f"Host operation failed: {Path(args[0]).name}") from exc


@contextlib.contextmanager
def exclusive_lock():
    LOCK.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunnerError("Controller/template operation already holds the global lock; drain and stop controller first") from exc
        yield
    finally:
        os.close(fd)


class Host:
    def __init__(self, config: Config):
        self.config = config
        self.channels: dict[str, socket.socket] = {}
        self.buffer = b""

    def active_instances(self) -> set[str]:
        output = command(["systemctl", "list-units", "--no-legend", "--plain",
                          "--state=activating,active,reloading,deactivating", "nexpoly-runner-vm@*.service"])
        result = set()
        for line in output.splitlines():
            name = line.split()[0]
            if not name.startswith("nexpoly-runner-vm@") or not name.endswith(".service"):
                raise RunnerError("Unexpected VM service identity")
            result.add(validate_uuid(name.removeprefix("nexpoly-runner-vm@").removesuffix(".service")))
        return result

    def raw_config(self):
        import host
        return host.read_config(self.config.config_file)

    def preflight(self, *, require_template=True, require_idle=True) -> dict:
        import host
        import template
        config = self.raw_config()
        if require_idle and self.active_instances():
            raise RunnerError("Another VM service is still active; refusing to launch a second VM")
        if not host.storage_check(config):
            raise RunnerError("Dedicated ext4 storage is not mounted with its expected backing file")
        firewall = host.firewall_report(config)
        if not firewall["ok"]:
            raise RunnerError("Dedicated UID firewall check failed: " + firewall["reason"])
        try:
            profiles = Path("/sys/kernel/security/apparmor/profiles").read_text()
        except OSError as exc:
            raise RunnerError("Cannot verify AppArmor enforcement") from exc
        if "nexpoly-runner-vm (enforce)" not in profiles.splitlines():
            raise RunnerError("VM AppArmor profile is not enforced")
        for unit in ("nexpoly-runner-firewall.service", "nexpoly-runner-proxy.service"):
            if command(["systemctl", "is-active", unit]) != "active":
                raise RunnerError("Dedicated network service is not active")
        if shutil.disk_usage(self.config.root).free < self.config.host_min_free_gib * GIB:
            raise RunnerError("Host disk is below its 100 GiB reserve")
        if shutil.disk_usage(self.config.state).free < self.config.state_min_free_gib * GIB:
            raise RunnerError("Runner filesystem is below its 40 GiB reserve")
        memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        available = int(memory.get("MemAvailable", "0 kB").split()[0]) * 1024
        if require_idle and available < 64 * GIB:
            raise RunnerError("Host needs at least 64 GiB MemAvailable before reserving a 32 GiB VM")
        self.proxy_check()
        if not require_template:
            return {}
        manifest = read_json(self.config.manifest)
        if not isinstance(manifest, dict):
            raise RunnerError("Build and verify a base template first")
        template.verify_template(self.config.state / "templates" / manifest["image"], manifest)
        return manifest

    def proxy_check(self) -> None:
        # A listening SSH proxy is insufficient: verify a real HTTPS round trip.
        # Use the public web endpoint: an unauthenticated API probe consumes the
        # shared egress IP's API quota and can mistake rate limiting for outage.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": self.config.host_proxy}))
        request = urllib.request.Request(PROXY_HEALTH_URL, headers={"User-Agent": PROXY_HEALTH_USER_AGENT})
        def failure(reason: str) -> RunnerError:
            return RunnerError(
                f"Dedicated proxy cannot reach GitHub ({reason}); check "
                "127.0.0.1:27892 -> upstream 127.0.0.1:27891. "
                "A listening port alone does not verify HTTPS connectivity.")
        try:
            with opener.open(request, timeout=15) as response:
                if response.status != 200:
                    code = response.status
                    reason = f"HTTP {code}" if type(code) is int and 100 <= code <= 599 else "HTTP failure"
                    raise failure(reason)
                response.read(1024)
        except (OSError, ValueError) as exc:
            raise failure(proxy_failure_reason(exc)) from exc

    def create(self, instance_id: str, manifest: dict) -> None:
        import template
        path = self.config.instance(instance_id)
        if path.exists() or path.is_symlink():
            raise RunnerError("Refusing to reuse a VM instance directory")
        template.prepare_runner_instance(self.config, path, manifest)

    def start(self, instance_id: str) -> None:
        command(["systemctl", "start", self.config.unit(instance_id)])

    def unit_state(self, instance_id: str) -> dict:
        output = command(["systemctl", "show", self.config.unit(instance_id),
                          "--property=ActiveState,MainPID,ControlGroup"])
        fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if "ActiveState" not in fields or "MainPID" not in fields:
            raise RunnerError("Cannot establish VM service state")
        return fields

    def stopped(self, instance_id: str) -> bool:
        fields = self.unit_state(instance_id)
        if fields["ActiveState"] not in ("inactive", "failed") or fields["MainPID"] != "0":
            return False
        group = fields.get("ControlGroup", "")
        if group:
            if not group.startswith("/") or ".." in Path(group).parts:
                raise RunnerError("Invalid systemd cgroup path")
            events = Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.events"
            try:
                if "populated 0" not in events.read_text().splitlines():
                    return False
            except FileNotFoundError:
                pass  # A removed cgroup cannot contain a remaining process.
        return True

    def stop(self, instance_id: str) -> None:
        command(["systemctl", "stop", self.config.unit(instance_id)], timeout=150)
        if not self.stopped(instance_id):
            raise RunnerError("VM cgroup still contains processes; overlay retained")
        self.close_channels()

    def arm_deadline(self, instance_id: str, deadline: float) -> None:
        timer = f"nexpoly-runner-deadline-{validate_uuid(instance_id)}"
        state = command(["systemctl", "show", timer + ".timer", "--property=ActiveState", "--value"])
        if state == "active":
            return  # A restart must never extend an already armed deadline.
        remaining = max(1, int(deadline - time.time()))
        command(["systemd-run", "--quiet", "--collect", "--unit=" + timer,
                 f"--on-active={remaining}s", "--timer-property=AccuracySec=1s",
                 "--property=Type=oneshot", "--property=NoNewPrivileges=yes",
                 "/usr/bin/systemctl", "stop", self.config.unit(instance_id)])

    def poweroff(self, instance_id: str) -> None:
        # ACPI shutdown, then the controller's bounded grace period and cgroup kill.
        try:
            with socket.socket(socket.AF_UNIX) as channel:
                channel.settimeout(2)
                channel.connect(str(self.config.runtime(instance_id) / "qmp.sock"))
                channel.recv(4096)
                channel.sendall(b'{"execute":"qmp_capabilities"}\n')
                channel.recv(4096)
                channel.sendall(b'{"execute":"system_powerdown"}\n')
        except OSError:
            pass

    def connect(self, instance_id: str, name: str):
        if name not in self.channels:
            sock = socket.socket(socket.AF_UNIX)
            sock.settimeout(1)
            try:
                sock.connect(str(self.config.runtime(instance_id) / f"{name}.sock"))
            except OSError:
                sock.close()
                return None
            self.channels[name] = sock
        return self.channels[name]

    def bootstrap(self, instance_id: str, payload: dict) -> bool:
        channel = self.connect(instance_id, "bootstrap")
        if channel is None:
            return False
        try:
            channel.sendall((json.dumps(payload) + "\n").encode())
        except OSError as exc:
            raise RunnerError("Bootstrap channel interrupted; recycle this instance") from exc
        channel.close()
        self.channels.pop("bootstrap", None)
        return True

    def events(self, instance_id: str) -> list[dict]:
        channel = self.connect(instance_id, "events")
        if channel is None:
            return []
        channel.setblocking(False)
        try:
            chunk = channel.recv(8192)
        except (BlockingIOError, TimeoutError):
            return []
        except OSError:
            chunk = b""
        if not chunk:
            channel.close()
            self.channels.pop("events", None)
            return []
        self.buffer += chunk
        if len(self.buffer) > 16384:
            self.buffer = b""
            return []
        results = []
        while b"\n" in self.buffer and len(results) < 32:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if len(line) > 2048:
                continue
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    results.append(value)
            except (ValueError, UnicodeError):
                continue
        return results

    def close_channels(self):
        for channel in self.channels.values():
            channel.close()
        self.channels.clear()
        self.buffer = b""

    def remove(self, instance_id: str) -> None:
        if not self.stopped(instance_id):
            raise RunnerError("Cannot remove an instance until its entire VM cgroup has exited")
        deadline = f"nexpoly-runner-deadline-{validate_uuid(instance_id)}"
        # Stop only this UUID's transient watchdog, never unrelated host timers.
        for suffix in (".timer", ".service"):
            unit = deadline + suffix
            state = command(["systemctl", "show", unit, "--property=ActiveState", "--value"])
            if state not in ("inactive", "failed"):
                command(["systemctl", "stop", unit])
        path = self.config.instance(instance_id)
        if path.is_symlink():
            raise RunnerError("Instance directory must not be a symbolic link")
        if path.exists():
            if not path.is_dir() or path.stat().st_uid != 0:
                raise RunnerError("Instance directory is not controlled by root")
            # Never mount or inspect the guest filesystem; delete this UUID only.
            shutil.rmtree(path)
        self.close_channels()

    def metrics(self, instance_id: str) -> dict:
        fields = command(["systemctl", "show", self.config.unit(instance_id),
                          "--property=MemoryPeak,MemoryCurrent,CPUUsageNSec,IOReadBytes,IOWriteBytes"])
        result = {key: int(value) for line in fields.splitlines() if "=" in line
                  for key, value in [line.split("=", 1)] if value.isdecimal()}
        disk = self.config.instance(instance_id) / "disk.qcow2"
        if disk.is_file() and not disk.is_symlink():
            result["overlay_allocated_bytes"] = disk.stat().st_blocks * 512
        rules = json.loads(command(["nft", "--json", "list", "table", "inet", "nexpoly_runner"]))
        result["proxy_output_bytes"] = sum(expression["counter"].get("bytes", 0)
            for item in rules.get("nftables", []) if item.get("rule", {}).get("chain") == "proxy_egress"
            for expression in item["rule"].get("expr", []) if isinstance(expression.get("counter"), dict))
        return result

    def record_metrics(self, instance: dict) -> None:
        logs = self.config.state / "logs"
        now = time.time()
        path = logs / f"instance-{validate_uuid(instance['id'])}.json"
        atomic_json(path, {key: instance.get(key) for key in (
            "id", "name", "runner_id", "job_id", "created_at", "reason", "metrics")})
        # Only our regular metric records; never traverse guest files or delete
        # the dedicated journal namespace. Its separate cap is 1 GiB.
        files = []
        for item in logs.glob("instance-*.json"):
            if item.is_symlink() or not stat.S_ISREG(item.lstat().st_mode):
                continue
            try:
                validate_uuid(item.name.removeprefix("instance-").removesuffix(".json"))
            except RunnerError:
                continue
            files.append(item)
        size = sum(item.stat().st_size for item in files)
        for item in sorted(files, key=lambda value: value.stat().st_mtime):
            info = item.stat()
            if now - info.st_mtime > 7 * 86400 or size > 64 * 1024 * 1024:
                item.unlink()
                size -= info.st_size


class Controller:
    def __init__(self, config: Config, api, host, clock=time.time):
        self.config, self.api, self.host, self.clock = config, api, host, clock
        self.state = read_json(config.journal, {"schema_version": 1, "failures": 0, "instance": None})
        if not isinstance(self.state, dict) or self.state.get("schema_version") != 1:
            raise RunnerError("Unsupported controller journal; manual review required")
        self.next_api = 0.0
        self.next_discovery = 0.0
        self.next_launch = 0.0
        self.bootstrap_ready = False
        self.next_metrics = 0.0

    def save(self):
        atomic_json(self.config.journal, self.state)

    def paused(self) -> bool:
        return self.config.drain_file.exists()

    def pause(self, reason: str):
        atomic_json(self.config.drain_file, {"reason": reason, "at": self.clock()})

    def resume_request(self):
        path = self.config.controller_dir / "resume.json"
        if path.exists():
            # Reset failures only on an explicit administrator resume request.
            read_json(path)
            self.state["failures"] = 0
            self.save()
            self.config.drain_file.unlink(missing_ok=True)
            path.unlink()

    def recovery(self):
        build_path = self.config.controller_dir / "template-build.json"
        build = read_json(build_path)
        if build:
            instance_id = validate_uuid(build["instance_id"])
            self.host.stop(instance_id)
            self.host.remove(instance_id)
            build_path.unlink()
        instance = self.state.get("instance")
        if instance:
            instance_id = validate_uuid(instance["id"])
            if instance.get("name") != self.config.name_prefix + instance_id:
                raise RunnerError("Instance identity does not match its durable journal")
            # Keep an already-running job alive. Never retry bootstrap into a used
            # disk: the durable 'bootstrap_attempted' flag precedes transmission.
            self.state["last_recovery_at"] = self.clock()
            self.save()
            if instance.get("job_started_at") and instance.get("phase") not in ("stopping", "cleanup"):
                self.arm_deadline()
        registered = {instance["id"]} if instance else set()
        if hasattr(self.host, "active_instances") and self.host.active_instances() - registered:
            raise RunnerError("Unjournaled VM service exists; manual reconciliation required")
        instances = self.config.state / "instances"
        if instances.exists():
            unknown = [p.name for p in instances.iterdir() if p.name not in registered]
            if unknown:
                raise RunnerError("Unjournaled instance directories exist; inspect before resuming")

    def begin(self):
        manifest = self.host.preflight()
        # Authentication succeeds before creating any VM or accepting a job.
        self.api.token()
        instance_id = uuid.uuid4().hex
        self.state["instance"] = {
            "id": instance_id, "name": self.config.name_prefix + instance_id,
            "phase": "booting", "created_at": self.clock(),
            "template_sha256": manifest["sha256"], "bootstrap_attempted": False,
            "registered": False, "runner_id": None, "job_id": None,
            "job_started_at": None, "stop_at": None, "boot_failure": False,
        }
        self.save()  # Before any disk or service side effect.
        try:
            self.host.create(instance_id, manifest)
            self.host.start(instance_id)
        except RunnerError:
            self.request_stop("vm_start_failed", boot_failure=True)
            raise
        self.next_api = 0
        self.bootstrap_ready = False
        print(json.dumps({"event": "vm_created", "instance": instance_id, "at": self.clock()}), flush=True)

    def request_stop(self, reason: str, *, boot_failure: bool = False, grace: bool = False):
        instance = self.state["instance"]
        if instance["phase"] in ("stopping", "cleanup"):
            return
        instance.update(phase="stopping", reason=reason, boot_failure=boot_failure,
                        stop_at=self.clock() + (self.config.shutdown_timeout_seconds if grace else 0))
        self.save()
        print(json.dumps({"event": "vm_stopping", "instance": instance["id"], "reason": reason,
                          "at": self.clock()}), flush=True)
        if grace:
            self.host.poweroff(instance["id"])

    def observe_job(self, job: dict):
        instance = self.state["instance"]
        if job.get("runner_id") != instance["runner_id"] or job.get("runner_name") != instance["name"]:
            raise RunnerError("Job does not belong to the registered instance")
        instance["job_id"] = int(job["id"])
        if job.get("status") in ("in_progress", "completed"):
            instance["job_started_at"] = instance["job_started_at"] or self.clock()
            instance["phase"] = "running"
        self.save()
        if instance["job_started_at"]:
            self.arm_deadline()
        if job.get("status") == "completed":
            self.request_stop("github_job_completed", grace=True)

    def arm_deadline(self):
        instance = self.state["instance"]
        if hasattr(self.host, "arm_deadline"):
            self.host.arm_deadline(instance["id"], instance["job_started_at"] + self.config.job_timeout_seconds)

    def poll_api(self):
        instance = self.state["instance"]
        now = self.clock()
        if now < self.next_api:
            return
        self.next_api = now + self.config.poll_seconds
        try:
            with self.api.budget() if hasattr(self.api, "budget") else contextlib.nullcontext():
                self._poll_api(instance, now)
            self.state.pop("api_error", None)
        except APIError as exc:
            self.next_api = now + exc.retry_after
            self.state["api_error"] = {"status": exc.status, "retry_at": self.next_api}
            self.save()

    def _poll_api(self, instance, now):
        runner = (self.api.runner(instance["runner_id"]) if instance["runner_id"]
                  else self.api.find_runner(instance["name"]))
        if runner:
            if runner.get("name") != instance["name"]:
                raise RunnerError("GitHub runner identity mismatch")
            labels = {item.get("name", "").lower() for item in runner.get("labels", [])}
            if not {"self-hosted", "linux", "x64", self.config.label} <= labels:
                raise RunnerError("GitHub runner labels do not match the configured route")
            instance["runner_id"] = int(runner["id"])
            instance["registered"] = True
            if instance["phase"] == "booting":
                instance["phase"] = "idle"
            if runner.get("busy"):
                instance["job_started_at"] = instance["job_started_at"] or now
                instance["phase"] = "running"
            self.save()
            if instance["job_started_at"]:
                self.arm_deadline()
        # A 404 or busy=false is NOT completion, including during drain.
        # A registered idle runner can receive a job immediately after a poll;
        # drain therefore prevents replenishment and lets this instance finish.
        if instance["job_id"]:
            self.observe_job(self.api.job(instance["job_id"]))
        elif instance["runner_id"]:
            job = None
            if instance.get("run_hint"):
                job = self.api.find_job(instance["run_hint"], instance["runner_id"])
            elif instance["job_started_at"] and now >= self.next_discovery:
                self.next_discovery = now + 120
                job = self.api.discover_job(instance["runner_id"])
            if job:
                self.observe_job(job)

    def cleanup(self):
        instance = self.state["instance"]
        if not self.host.stopped(instance["id"]):
            raise RunnerError("Cleanup requires a stopped VM cgroup")
        now = self.clock()
        if now < self.next_api:
            return
        try:
            with self.api.budget() if hasattr(self.api, "budget") else contextlib.nullcontext():
                runner = self.api.find_runner(instance["name"])
                if runner:
                    if runner.get("name") != instance["name"]:
                        raise RunnerError("Refusing to remove an unrelated runner")
                    self.api.delete_runner(int(runner["id"]))
        except APIError as exc:
            self.next_api = now + exc.retry_after
            self.state["api_error"] = {"status": exc.status, "retry_at": self.next_api}
            self.save()
            return  # Keep journal and overlay; never launch another VM yet.
        if hasattr(self.host, "record_metrics"):
            self.host.record_metrics(instance)
        self.host.remove(instance["id"])
        print(json.dumps({"event": "vm_recycled", "instance": instance["id"],
                          "runner_id": instance["runner_id"], "job_id": instance["job_id"],
                          "reason": instance.get("reason"), "at": now}), flush=True)
        self.state["last_instance"] = {key: instance.get(key) for key in
                                       ("id", "name", "runner_id", "job_id", "created_at", "reason")}
        self.state["last_instance"]["finished_at"] = now
        self.state["failures"] = self.state.get("failures", 0) + 1 if instance["boot_failure"] else 0
        self.state["instance"] = None
        self.state.pop("api_error", None)
        self.next_launch = now + min(300, 10 * 2 ** min(self.state["failures"], 5))
        self.save()
        if self.state["failures"] >= self.config.failure_limit:
            self.pause("three_consecutive_boot_failures")

    def tick(self):
        self.resume_request()
        instance = self.state.get("instance")
        now = self.clock()
        if instance is None:
            if self.state.get("failures", 0) >= self.config.failure_limit:
                self.pause("three_consecutive_boot_failures")
            if not self.paused() and now >= self.next_launch:
                self.begin()
            return
        if now >= self.next_metrics and hasattr(self.host, "metrics"):
            self.next_metrics = now + 30
            try:
                metrics = self.host.metrics(instance["id"])
                previous = instance.setdefault("metrics", {})
                if "proxy_output_bytes" in metrics:
                    previous.setdefault("proxy_output_bytes_initial", metrics["proxy_output_bytes"])
                for key, value in metrics.items():
                    previous[key] = max(previous.get(key, 0), value)
                previous["elapsed_seconds"] = max(0, now - instance["created_at"])
                self.save()
            except (RunnerError, OSError, ValueError):
                pass  # Missing telemetry never weakens teardown or deadlines.
        if instance["phase"] == "cleanup":
            self.cleanup()
            return
        if self.host.stopped(instance["id"]):
            instance["boot_failure"] = not instance["registered"]
            instance.setdefault("reason", "vm_exited")
            instance["phase"] = "cleanup"
            self.next_api = 0
            self.save()
            return
        if instance["phase"] == "stopping":
            if now >= instance["stop_at"]:
                self.host.stop(instance["id"])
                instance["phase"] = "cleanup"
                self.next_api = 0
                self.save()
            return
        # Deadlines are evaluated before any network call; an API outage cannot
        # indefinitely postpone a registered job's host-side watchdog.
        if not instance["registered"] and now - instance["created_at"] >= self.config.boot_timeout_seconds:
            self.request_stop("startup_timeout", boot_failure=True)
            return
        if instance["job_started_at"] and now - instance["job_started_at"] >= self.config.job_timeout_seconds:
            self.request_stop("host_job_deadline")
            return
        if self.paused() and not instance["bootstrap_attempted"]:
            self.request_stop("drained_before_registration")
            return
        for event in self.host.events(instance["id"]):
            if event.get("event") == "ready":
                self.bootstrap_ready = True
            # Untrusted guest hints only narrow a subsequent authenticated query.
            if event.get("event") in ("started", "completed"):
                run_id = event.get("run_id")
                if type(run_id) is int and 0 < run_id < 2 ** 63:
                    instance["run_hint"] = run_id
                    # An untrusted start hint can shorten this VM's lifetime,
                    # but cannot establish completion or mutate another VM.
                    if event["event"] == "started":
                        instance["job_started_at"] = instance["job_started_at"] or now
                    self.save()
                    if instance["job_started_at"]:
                        self.arm_deadline()
            # ready/registered/exit are not sufficient evidence of job status.
        if not instance["bootstrap_attempted"]:
            # Open channel before marking attempt. Once marked, any interrupted
            # send forces recycling after startup timeout, never a second use.
            if self.bootstrap_ready and now >= self.next_api and self.host.connect(instance["id"], "bootstrap") is not None:
                try:
                    token = self.api.registration_token()
                except APIError as exc:
                    self.next_api = now + exc.retry_after
                    return
                instance["bootstrap_attempted"] = True
                self.save()
                try:
                    self.host.bootstrap(instance["id"], {
                        "repo_url": "https://github.com/" + self.config.repository,
                        "runner_name": instance["name"], "registration_token": token,
                        "labels": [self.config.label]})
                finally:
                    del token
        self.poll_api()


def doctor(config: Config) -> dict:
    import host
    checks = host.doctor(host.read_config(config.config_file))["checks"]
    checks.append({"name": "github_app_ids", "ok": bool(config.app_id and config.installation_id),
                   "detail": "dedicated repository App installation required"})
    try:
        key = regular_file(config.private_key_file, root_owned=True)
        key_ok = not bool(key.st_mode & 0o077)
    except RunnerError:
        key_ok = False
    checks.append({"name": "github_app_key", "ok": key_ok, "detail": "root-owned /etc/nexpoly-runner/app.pem mode 0600"})
    try:
        Host(config).preflight(require_idle=False)
        template_ok = True
        detail = "storage, firewall, enforced AppArmor, HTTPS proxy and template integrity verified"
    except (RunnerError, TemplateError, OSError, ValueError, KeyError) as exc:
        template_ok = False
        detail = str(exc) if isinstance(exc, RunnerError) else "preflight unavailable"
    checks.append({"name": "runtime_preflight", "ok": template_ok, "detail": detail})
    return {"ready": all(item["ok"] for item in checks), "non_mutating": True, "checks": checks}


def serve(config: Config):
    stopping = False
    def terminate(_signum, _frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    with exclusive_lock():
        controller = Controller(config, GitHub(config), Host(config))
        controller.recovery()
        while not stopping:
            try:
                controller.tick()
                controller.state.pop("health_error", None)
            except (RunnerError, TemplateError, OSError, ValueError, KeyError) as exc:
                reason = str(exc) if isinstance(exc, RunnerError) else type(exc).__name__
                controller.state["health_error"] = {"at": time.time(), "reason": reason}
                controller.save()
                print(json.dumps({"event": "controller_retry", "reason": reason}), flush=True)
                controller.next_launch = time.time() + 30
                # Transient health/API errors stop replenishment, not active jobs.
                for _ in range(30):
                    if stopping:
                        break
                    time.sleep(1)
                continue
            time.sleep(1)
        # A controller restart preserves the VM and durable state for recovery.
        # Operational shutdown is drain -> idle -> stop (documented procedure).
        controller.host.close_channels()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "doctor", "status", "drain", "resume"):
        sub.add_parser(name)
    update = sub.add_parser("update-template")
    update.add_argument("--lock", type=Path, default=PACKAGE / "guest/software-lock.json")
    args = parser.parse_args(argv)
    mutation = args.command not in ("doctor", "status")
    if mutation and os.geteuid() != 0:
        parser.error("administrator execution is required for this command")
    try:
        config = Config.load(args.config, privileged=mutation)
        if args.command == "doctor":
            report = doctor(config)
            print(json.dumps(report, indent=2))
            return 0 if report["ready"] else 1
        if args.command == "status":
            print(json.dumps({"paused": config.drain_file.exists(), "state": read_json(config.journal),
                              "template": read_json(config.manifest)}, indent=2))
        elif args.command == "drain":
            atomic_json(config.drain_file, {"reason": "administrator_drain", "at": time.time()})
            print("Drain requested; active VM/job is retained until completion. Check status before stopping.")
        elif args.command == "resume":
            atomic_json(config.controller_dir / "resume.json", {"at": time.time()})
            print("Resume requested; the controller will recheck health before replenishing.")
        elif args.command == "update-template":
            import template
            with exclusive_lock():
                journal = read_json(config.journal, {})
                if journal.get("instance"):
                    raise RunnerError("Drain the controller and wait for its instance to be recycled before template update")
                if not config.drain_file.exists():
                    raise RunnerError("Run drain before template update")
                Host(config).preflight(require_template=False)
                print(json.dumps(template.build_template(config, args.lock), indent=2))
        else:
            serve(config)
        return 0
    except (RunnerError, TemplateError, OSError, ValueError, KeyError) as exc:
        print(str(exc) if isinstance(exc, (RunnerError, TemplateError)) else f"Operation failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
