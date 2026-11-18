#!/usr/bin/env python3
"""Quarantine and retire an unused standby using the installed controller.

No busy=false/404 observation is treated as job completion. This maintenance
entry point is intentionally separate from the normal controller lifecycle.
"""
from __future__ import annotations

import contextlib
import datetime
import importlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time

INSTALLED = Path("/usr/local/libexec/nexpoly-runner")
RETAIN_JOB = 75
RETAIN_UNCERTAIN = 76
RUN_STATUSES = {"queued", "waiting", "pending", "requested", "in_progress", "completed"}


class RetirementError(RuntimeError):
    pass


class WorkDetected(RetirementError):
    def __init__(self, message, job=None):
        super().__init__(message)
        self.job = job


def timestamp(value):
    if not isinstance(value, str):
        raise RetirementError("Workflow timestamp is unavailable")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone is required")
        return parsed.timestamp()
    except ValueError as error:
        raise RetirementError("Workflow timestamp is invalid") from error


class IdleRetirement:
    def __init__(self, controller, *, now=time.time, monotonic=time.monotonic, sleep=time.sleep):
        self.controller, self.api, self.host = controller, controller.api, controller.host
        self.config = controller.config
        self.now, self.monotonic, self.sleep = now, monotonic, sleep
        self.deadline = self.monotonic() + 600
        self.next_progress = 0

    def progress(self, message, *, force=False):
        if force or self.monotonic() >= self.next_progress:
            print(message, flush=True)
            self.next_progress = self.monotonic() + 30

    def budget(self):
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise RetirementError("Idle retirement exceeded its 10 minute deadline; VM retained")
        return self.api.budget(min(120, remaining)) if hasattr(self.api, "budget") else contextlib.nullcontext()

    def save(self):
        self.controller.save()

    def validate_instance(self):
        if not self.controller.paused() or (self.config.controller_dir / "resume.json").exists():
            raise RetirementError("Drain is required and no pending resume request is allowed")
        instance = self.controller.state.get("instance")
        active = self.host.active_instances()
        if instance is None:
            if active:
                raise RetirementError("An unjournaled VM exists; manual reconciliation required")
            return None
        created_at = instance.get("created_at")
        if type(created_at) not in {int, float} or not math.isfinite(created_at) or created_at < 0:
            raise RetirementError("Registered VM creation timestamp is invalid")
        identity = instance.get("id")
        if (not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity)
                or instance.get("name") != self.config.name_prefix + identity):
            raise RetirementError("Registered VM identity differs from its journal")
        if active - {identity}:
            raise RetirementError("Another VM exists; manual reconciliation required")
        folders = self.config.state / "instances"
        if not folders.is_dir() or any(path.name != identity for path in folders.iterdir()):
            raise RetirementError("Unjournaled instance storage exists")
        return instance

    def runner(self, instance):
        runner = self.api.runner(instance["runner_id"])
        if runner is not None and (runner.get("id") != instance["runner_id"]
                                   or runner.get("name") != instance["name"]):
            raise RetirementError("GitHub runner identity differs from the registered UUID")
        return runner

    def scan_jobs(self, instance):
        """Enumerate runs and actual jobs, including a job that just completed."""
        self.progress("Checking authenticated workflow runs and actual job assignments.", force=True)
        runs = {}
        for page in range(1, 11):
            self.progress("Still checking workflow assignments; registered VM is retained.")
            result = self.api.request("GET", f"/actions/runs?per_page=100&page={page}")
            records = result.get("workflow_runs") if isinstance(result, dict) else None
            if not isinstance(records, list) or len(records) > 100:
                raise RetirementError("Cannot enumerate workflow runs completely")
            for record in records:
                if (not isinstance(record, dict) or type(record.get("id")) is not int
                        or record.get("status") not in RUN_STATUSES or record["id"] in runs):
                    raise RetirementError("Workflow run enumeration changed or is invalid")
                runs[record["id"]] = record
            if len(records) < 100:
                break
        else:
            raise RetirementError("Workflow run enumeration exceeded the reviewed 1000 run limit")
        for run_id, run in runs.items():
            if run["status"] == "completed" and timestamp(run.get("updated_at")) < math.floor(instance["created_at"]):
                continue
            for page in range(1, 11):
                self.progress("Still checking actual job records; registered VM is retained.")
                result = self.api.request("GET", f"/actions/runs/{run_id}/jobs?filter=all&per_page=100&page={page}")
                jobs = result.get("jobs") if isinstance(result, dict) else None
                if not isinstance(jobs, list) or len(jobs) > 100:
                    raise RetirementError("Cannot enumerate actual workflow jobs completely")
                for job in jobs:
                    if (not isinstance(job, dict) or type(job.get("id")) is not int
                            or job.get("status") not in RUN_STATUSES):
                        raise RetirementError("Actual workflow job record is invalid")
                    same_id = job.get("runner_id") == instance["runner_id"]
                    same_name = job.get("runner_name") == instance["name"]
                    if same_id != same_name:
                        raise RetirementError("Actual job runner identity is inconsistent")
                    if same_id:
                        raise WorkDetected("An actual job was assigned to this runner; VM retained", job)
                if len(jobs) < 100:
                    break
            else:
                raise RetirementError("Actual job enumeration exceeded the reviewed 1000 job limit")

    def restore_route(self, instance):
        record = instance.get("maintenance_quarantine", {})
        if not record.get("attempted_at") or record.get("delete_attempted_at"):
            return False
        try:
            with self.budget():
                runner = self.runner(instance)
                if runner is None:
                    return False
                labels = {entry["name"].lower() for entry in runner["labels"]}
                if self.config.label not in labels:
                    self.api.request("POST", f"/actions/runners/{instance['runner_id']}/labels",
                                     {"labels": [self.config.label]})
                    runner = self.runner(instance)
                if runner is None or self.config.label not in {entry["name"].lower() for entry in runner["labels"]}:
                    return False
            record["restored_at"] = self.now()
            self.save()
            return True
        except Exception:
            return False  # No untrusted API response or credential is logged.

    def observe_isolated(self, instance, *, allow_missing=False):
        with self.budget():
            runner = self.runner(instance)
            if runner is None and not allow_missing:
                raise RetirementError("Runner registration disappeared; this does not prove no job was assigned")
            if runner is not None:
                if self.config.label in {entry["name"].lower() for entry in runner["labels"]}:
                    raise RetirementError("Runner still has its routing label; isolation is not confirmed")
                if runner.get("busy") is not False:
                    raise WorkDetected("Runner is busy; its VM must finish normally")
            self.scan_jobs(instance)

    def cleanup(self, instance, *, request_stop):
        if request_stop:
            self.controller.request_stop("maintenance_idle_retired", grace=True)
        end = min(self.deadline, self.monotonic() + self.config.shutdown_timeout_seconds + 90)
        while self.controller.state.get("instance") is not None:
            if not self.controller.paused() or (self.config.controller_dir / "resume.json").exists():
                raise RetirementError("Drain changed during retirement; cleanup stopped")
            if self.monotonic() >= end:
                raise RetirementError("Normal VM cleanup did not finish; state and disk retained")
            self.progress("Waiting for ordinary VM shutdown and cgroup cleanup; no replacement will be created.")
            self.controller.tick()
            if self.controller.state.get("instance") is not None:
                self.sleep(1)

    def run(self):
        instance = self.validate_instance()
        if instance is None:
            return 0
        if self.host.stopped(instance["id"]) or instance.get("phase") in {"stopping", "cleanup"}:
            self.cleanup(instance, request_stop=False)
            return 0
        if instance.get("job_id") or instance.get("job_started_at"):
            if instance.get("maintenance_quarantine") and not self.restore_route(instance):
                raise RetirementError("Assigned job route restore is unconfirmed; keep controller stopped")
            return RETAIN_JOB
        if type(instance.get("runner_id")) is not int or not instance.get("registered"):
            raise RetirementError("Cannot retire an unconfirmed registration; VM retained")
        try:
            with self.budget():
                runner = self.runner(instance)
                record = instance.get("maintenance_quarantine")
                if runner is not None and not {"self-hosted", "linux", "x64"} <= {
                        entry["name"].lower() for entry in runner["labels"]}:
                    raise RetirementError("Registered runner base labels differ from the approved identity")
                if runner is None and not (isinstance(record, dict) and record.get("delete_attempted_at")
                                           and len(record.get("clear_observations", [])) >= 2):
                    raise RetirementError("Runner 404 is not evidence of an unused standby")
                if record is None:
                    if runner is None or self.config.label not in {entry["name"].lower() for entry in runner["labels"]}:
                        raise RetirementError("Expected registered routing label is absent")
                    record = instance["maintenance_quarantine"] = {
                        "runner_id": instance["runner_id"], "attempted_at": self.now(), "clear_observations": []}
                    self.save()  # Durable before any API mutation.
                elif not isinstance(record, dict) or record.get("runner_id") != instance["runner_id"]:
                    raise RetirementError("Quarantine journal identity is invalid")
                if runner is not None and self.config.label in {entry["name"].lower() for entry in runner["labels"]}:
                    self.api.request("DELETE", f"/actions/runners/{instance['runner_id']}/labels/{self.config.label}")
                record["isolated_at"] = self.now()
                # Keep prior complete proof across an interrupted post-delete retry.
                # A new partial scan must not erase the proof that permits a 404.
                record["current_observations"] = []
                self.save()
            allow_missing = bool(record.get("delete_attempted_at"))
            for observation in range(2):
                if observation:
                    print("Standby route isolated; observing actual job assignments for at least 30 seconds.", flush=True)
                    before = self.monotonic()
                    while self.monotonic() - before < 30:
                        self.sleep(min(1, 30 - (self.monotonic() - before)))
                self.observe_isolated(instance, allow_missing=allow_missing)
                record["current_observations"].append(self.now())
                self.save()
            record["clear_observations"] = record.pop("current_observations")
            self.save()
            with self.budget():
                # Recheck the assignment surface immediately before removal.
                self.observe_isolated(instance, allow_missing=allow_missing)
                record["delete_attempted_at"] = self.now()
                self.save()
                self.api.delete_runner(instance["runner_id"])
                record["deleted_at"] = self.now()
                self.save()
            self.cleanup(instance, request_stop=True)
            return 0
        except WorkDetected as error:
            if not self.restore_route(instance):
                raise RetirementError("Job evidence found but route restore is unconfirmed; keep controller stopped") from error
            if error.job is not None:
                self.controller.observe_job(error.job)
            return RETAIN_JOB
        except Exception as error:
            if self.restore_route(instance):
                return RETAIN_UNCERTAIN
            raise RetirementError("Retirement is uncertain; VM/state retained and controller must remain stopped") from error


def installed_modules():
    # Use the installed old configuration/transport contract before migration.
    for directory in (Path("/usr"), Path("/usr/local"), Path("/usr/local/libexec"), INSTALLED):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RetirementError("Installed maintenance import directories are not protected root-owned directories")
    for name in ("controller.py", "common.py", "github_api.py", "host.py", "template.py"):
        path = INSTALLED / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RetirementError("Installed maintenance dependencies are not protected root-owned files")
    sys.path.insert(0, str(INSTALLED))
    return importlib.import_module("controller")


def main():
    if os.geteuid() != 0:
        print("Administrator execution is required; no changes made.", file=sys.stderr)
        return 1
    legacy = None
    try:
        legacy = installed_modules()
        active = legacy.command(["systemctl", "show", "--property=ActiveState", "--value", "nexpoly-runner-controller.service"])
        if active not in {"inactive", "failed"}:
            raise RetirementError("Stop the drained controller first; its VM is preserved")
        config = legacy.Config.load(legacy.DEFAULT_CONFIG, privileged=True)
        with legacy.exclusive_lock():
            legacy.regular_file(config.journal, root_owned=True)
            legacy.regular_file(config.drain_file, root_owned=True)
            host = legacy.Host(config)
            try:
                controller = legacy.Controller(config, legacy.GitHub(config), host)
                result = IdleRetirement(controller).run()
            finally:
                host.close_channels()
        if result == RETAIN_JOB:
            print("Assigned/busy job retained with route restored; restart the drained installed controller to finish it.")
        elif result == RETAIN_UNCERTAIN:
            print("API proof incomplete; route restored and VM retained. Restart drained installed controller, then retry maintenance later.")
        else:
            print("Registered standby retirement and normal VM cleanup complete; controller remains drained/stopped.")
        return result
    except Exception as error:
        reason = str(error) if isinstance(error, RetirementError) else type(error).__name__
        print(f"Standby retirement failed: {reason}. Keep the controller drained/stopped; no replacement VM is launched.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
