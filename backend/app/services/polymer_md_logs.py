"""Bounded, owner-fenced log tails from the read-only Worker artifact mount.

The caller supplies the repository's private, owner-checked job. Completed
files are selected only from that attempt's manifest. Live scientific files
also require an unchanged journal execution identity on both sides of the read.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import re
import stat

from .polymer_md_repository import PolymerMdError


BYTE_LIMIT = 256 * 1024
JOURNAL_LIMIT = 8 * 1024 * 1024
_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}\Z")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SOURCES = ("engine", "stdout", "runner")


def _invalid(message="日志存储引用无效。"):
    return PolymerMdError("log_reference_invalid", message, 502)


def _changed():
    return PolymerMdError("stage_log_changed", "阶段执行身份已变化，请刷新后重试。", 409)


def _storage():
    return PolymerMdError("log_storage_unavailable", "阶段日志暂不可读取。", 503)


def _open_at(directory_fd, parts, *, missing_ok=False):
    """Open every component without following links, including the final file."""
    if not parts or any(not isinstance(p, str) or not _PART.fullmatch(p) for p in parts):
        raise _invalid()
    current, fd = os.dup(directory_fd), None
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = next_fd
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _invalid("日志必须是普通文件。")
        result, fd = fd, None
        return result
    except OSError as exc:
        if missing_ok and exc.errno == errno.ENOENT:
            return None
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise _invalid() from None
        raise _storage() from None
    finally:
        if fd is not None:
            os.close(fd)
        os.close(current)


def _open_root(root):
    path = Path(root)
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise _invalid()
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except OSError as exc:
        os.close(current)
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise _invalid() from None
        raise _storage() from None


def _read_journal(root_fd, prefix):
    fd = _open_at(root_fd, [*prefix, "journal.json"])
    try:
        before = os.fstat(fd)
        if before.st_size > JOURNAL_LIMIT:
            raise _invalid("阶段执行记录超过读取上限。")
        parts, remaining = [], before.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                raise _changed()
            parts.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise _changed()
        try:
            value = json.loads(b"".join(parts), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError):
            raise _invalid("阶段执行记录无效。") from None
        if not isinstance(value, dict):
            raise _invalid("阶段执行记录无效。")
        return value
    finally:
        os.close(fd)


def _started_ns(run):
    value = run.get("started_at")
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError()
        return int(value.timestamp() * 1_000_000_000)
    except (ValueError, OverflowError, OSError):
        raise _changed() from None


def _live_fence(root_fd, prefix, job, run):
    value = _read_journal(root_fd, prefix)
    request, snapshot, scope = value.get("request"), value.get("snapshot"), value.get("running_scope")
    if not all(isinstance(v, dict) for v in (request, snapshot, scope)):
        raise _changed()
    for key in ("job_id", "attempt_token", "owner_user_id", "request_sha256", "enqueue_sequence"):
        if key not in job or request.get(key) != job[key]:
            raise _changed()
    if (snapshot.get("job_id") != job["job_id"] or snapshot.get("attempt_token") != job["attempt_token"]
            or not job.get("worker_instance_id") or snapshot.get("worker_instance_id") != job["worker_instance_id"]
            or snapshot.get("stage") != run["stage"] or job.get("stage") != run["stage"]
            or snapshot.get("status") not in {"running", "cancel_requested"}
            or job.get("status") not in {"running", "cancel_requested"}
            or scope.get("stage_run_id") != run["stage_run_id"]
            or value.get("artifacts_deleting") or value.get("artifacts_deleted")):
        raise _changed()
    journal_runs = snapshot.get("stage_runs", [])
    if not isinstance(journal_runs, list):
        raise _changed()
    exact = [s for s in journal_runs if isinstance(s, dict) and s.get("stage_run_id") == run["stage_run_id"]]
    if (len(exact) != 1 or exact[0].get("stage") != run["stage"] or exact[0].get("status") != "running"
            or exact[0].get("attempt", 1) != run.get("attempt", 1)
            or _started_ns(exact[0]) != _started_ns(run)):
        raise _changed()
    # A scientific directory is reused by retries. Only the latest exact run
    # may read it, even when a reconciled DB snapshot temporarily lags the Worker.
    for stages in (job["stage_runs"], journal_runs):
        for stage in stages:
            if isinstance(stage, dict) and stage.get("stage") == run["stage"]:
                attempt = stage.get("attempt", 1)
                if (not isinstance(attempt, int) or isinstance(attempt, bool)
                        or attempt > run.get("attempt", 1)):
                    raise _changed()
    return (tuple(request[k] for k in ("job_id", "attempt_token", "owner_user_id", "request_sha256", "enqueue_sequence")),
            snapshot["worker_instance_id"], snapshot["status"], snapshot["stage"],
            json.dumps(scope, sort_keys=True, allow_nan=False))


def _protocol_names(job, stage):
    """Ordered basenames from the pinned scientific protocol, never a glob."""
    if stage in {"eq1", "eq2", "eq3", "additional_1", "additional_2"}:
        return {"engine": ["engine.log"], "stdout": ["engine.stdout"]}
    if stage == "sp":
        return {"engine": ["engine.log", "sample.log"], "stdout": ["rerun.stdout", "sample.stdout"]}
    if stage == "tg":
        return {"engine": ["engine.log", "high_temperature.log"],
                "stdout": ["step-wise.stdout", "high-temperature.stdout"]}
    if stage == "tc":
        axis = job.get("scientific_request", {}).get("tc_axis")
        if axis not in {"x", "y", "z"}:
            raise _invalid("RNEMD 日志轴配置无效。")
        return {"engine": [f"nemd_TC-MP_{axis}.log"], "stdout": ["engine.stdout"]}
    return {}


@dataclass(frozen=True)
class _Candidate:
    source: str
    parts: tuple[str, ...]
    label: str
    size: int | None = None
    retained_tail: bool = False


def _manifest_candidate(job, run, artifact, source, relative, *, retained_tail=False):
    ref = artifact.get("storage_ref")
    if (artifact.get("attempt_token") != job["attempt_token"]
            or artifact.get("stage_run_id") != run["stage_run_id"]
            or ref != "/".join([job["job_id"], job["attempt_token"], *relative])
            or artifact.get("filename") != relative[-1]
            or not isinstance(artifact.get("size_bytes"), int) or isinstance(artifact["size_bytes"], bool)
            or not 0 <= artifact["size_bytes"] <= 20 * 1024**3
            or not isinstance(artifact.get("sha256"), str) or not _SHA.fullmatch(artifact["sha256"])):
        raise _invalid()
    return _Candidate(source, tuple([job["job_id"], job["attempt_token"], *relative]),
                      artifact["filename"], artifact["size_bytes"], retained_tail)


def _candidates(job, run, live):
    names = _protocol_names(job, run["stage"])
    names["runner"] = ["runner.log"]
    result = {source: [] for source in _SOURCES}
    if live:
        for source, basenames in names.items():
            for basename in basenames:
                relative = (["stages", run["stage_run_id"], basename] if source == "runner"
                            else ["science", run["stage"], basename])
                result[source].append(_Candidate(source, tuple([job["job_id"], job["attempt_token"], *relative]), basename))
        return result
    completed = run["status"] == "completed"
    artifacts = [a for a in job.get("artifacts", []) if a.get("stage_run_id") == run["stage_run_id"]]
    for source, basenames in names.items():
        for basename in basenames:
            for artifact in artifacts:
                filename = artifact.get("filename")
                if source == "runner":
                    if filename != basename or artifact.get("kind") != "log":
                        continue
                    relative = ["stages", run["stage_run_id"], basename]
                elif completed:
                    if filename != basename or artifact.get("kind") != "log":
                        continue
                    relative = ["science", run["stage"], basename]
                else:
                    if artifact.get("kind") != "failure_log" or filename not in {basename, basename + ".tail.log"}:
                        continue
                    ref = artifact.get("storage_ref")
                    parts = ref.split("/") if isinstance(ref, str) else []
                    if (len(parts) != 7 or parts[2:5] != ["stages", run["stage_run_id"], "failure-diagnostics"]
                            or re.fullmatch(r"[0-9a-f]{12}", parts[5]) is None):
                        raise _invalid()
                    relative = parts[2:]
                candidate = _manifest_candidate(job, run, artifact, source, relative,
                                                retained_tail=filename.endswith(".tail.log"))
                if any(c.parts == candidate.parts for c in result[source]):
                    raise _invalid("日志清单含有重复引用。")
                result[source].append(candidate)
    return result


def _read_tail(fd, candidate, tail_lines):
    before = os.fstat(fd)
    if candidate.size is not None and before.st_size != candidate.size:
        raise _invalid("日志大小与产物清单不一致。")
    start = max(0, before.st_size - BYTE_LIMIT)
    os.lseek(fd, start, os.SEEK_SET)
    remaining, chunks = before.st_size - start, []
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            raise _storage()
        chunks.append(chunk)
        remaining -= len(chunk)
    after = os.fstat(fd)
    if candidate.size is not None:
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise _invalid("已封存日志在读取期间发生变化。")
    elif after.st_size < before.st_size or (after.st_size == before.st_size and after.st_mtime_ns != before.st_mtime_ns):
        raise _changed()
    lines = b"".join(chunks).decode("utf-8", errors="replace").splitlines()
    if start and lines:
        lines = lines[1:]
    return lines[-tail_lines:], bool(start or len(lines) > tail_lines or candidate.retained_tail)


def _redact(lines, root, job):
    cleaned = []
    private = sorted({str(root), job["attempt_token"]}, key=len, reverse=True)
    for line in lines:
        for value in private:
            line = line.replace(value, "[private]")
        line = re.sub(r"(?<![A-Za-z0-9])/(?:[^\s\"'<>]+)", "[private]", line)
        line = re.sub(r"\b[A-Za-z]:[\\/][^\s\"'<>]+", "[private]", line)
        line = re.sub(r"(?i)(\bauthorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1[private]", line)
        cleaned.append(re.sub(r"(?:\[private\]){2,}", "[private]", line))
    return cleaned


def read_stage_logs(root, job, stage_run_id, tail_lines=200, source="auto") -> dict:
    """Return one bounded tail; ownership must already be checked by the router."""
    if (not isinstance(tail_lines, int) or isinstance(tail_lines, bool) or not 1 <= tail_lines <= 1000
            or source not in {"auto", *_SOURCES}):
        raise PolymerMdError("invalid_log_request", "日志来源或行数无效。", 422)
    if (not isinstance(job.get("job_id"), str) or not _UUID.fullmatch(job["job_id"])
            or not isinstance(job.get("attempt_token"), str) or not _SHA.fullmatch(job["attempt_token"])
            or not isinstance(stage_run_id, str) or not _PART.fullmatch(stage_run_id)):
        raise _invalid()
    runs = [r for r in job.get("stage_runs", []) if r.get("stage_run_id") == stage_run_id]
    if job.get("artifact_state") != "available" or len(runs) != 1:
        raise PolymerMdError("stage_not_found", "该任务的阶段日志不存在。", 404)
    run = runs[0]
    if not isinstance(run.get("stage"), str) or not _PART.fullmatch(run["stage"]):
        raise _invalid()
    live = run.get("status") == "running"
    result = {"stage_run_id": stage_run_id, "stage": run["stage"], "status": run["status"],
              "available": False, "lines": [], "truncated": False, "tail_lines": tail_lines,
              "byte_limit": BYTE_LIMIT, "resolved_source": None, "source_label": None,
              "available_sources": [], "read_at": None}
    root_fd, opened = _open_root(root), []
    try:
        prefix = [job["job_id"], job["attempt_token"]]
        before = _live_fence(root_fd, prefix, job, run) if live else None
        started_ns = _started_ns(run) if live else None
        candidates = _candidates(job, run, live)
        available = {}
        for key in _SOURCES:
            for candidate in candidates[key]:
                fd = _open_at(root_fd, candidate.parts, missing_ok=True)
                if fd is None:
                    continue
                opened.append(fd)
                info = os.fstat(fd)
                # The old scientific directory can briefly outlive the next
                # retry's launch intent. An old log is never evidence for it.
                if live and info.st_mtime_ns < started_ns:
                    continue
                if candidate.size is not None and info.st_size != candidate.size:
                    raise _invalid("日志大小与产物清单不一致。")
                available[key] = (candidate, fd)
                result["available_sources"].append({"source": key, "label": candidate.label})
                break
        selected = next(iter(available), None) if source == "auto" else source
        if selected in available:
            candidate, fd = available[selected]
            lines, truncated = _read_tail(fd, candidate, tail_lines)
            result.update(available=True, lines=_redact(lines, root, job), truncated=truncated,
                          resolved_source=selected, source_label=candidate.label)
        if live and _live_fence(root_fd, prefix, job, run) != before:
            raise _changed()
        result["read_at"] = datetime.now(timezone.utc).isoformat()
        return result
    finally:
        for fd in opened:
            os.close(fd)
        os.close(root_fd)
