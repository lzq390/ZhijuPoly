"""Descriptor-bound inputs, copied once before native readers open their paths."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat

from .contracts import AnalysisError, DistanceAnalysisRequest, SourceRequest, TrajectoryAnalysisRequest

METADATA_LIMIT = 16 * 1024 ** 2
OUTPUT_RESERVE = 32 * 1024 ** 2


def open_root(root: Path):
    fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in root.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        result, fd = fd, -1
        return result
    finally:
        if fd != -1:
            os.close(fd)


def open_source(root_fd: int, relative: str):
    parts = relative.split("/")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", p) or p in {".", ".."} for p in parts):
        raise AnalysisError("artifact_reference_invalid", "分析输入的存储引用无效。", 409)
    directory = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        handle = os.fdopen(fd, "rb")
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            handle.close()
            raise AnalysisError("artifact_reference_invalid", "分析输入不是普通文件。", 409)
        return handle
    finally:
        os.close(directory)


def strict_json(raw: bytes):
    return json.loads(raw, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON")))


def check_space(spool: Path, required: int, budget: int, minimum: int, fraction: float):
    usage = shutil.disk_usage(spool)
    reserve = max(minimum, int(usage.total * fraction))
    if required > budget or usage.free - required < reserve:
        raise AnalysisError("analysis_storage_busy", "距离分析临时空间不足，请稍后重试；后台模拟继续运行。", 503)


def prepare_sources(settings, source: SourceRequest, private: Path):
    """Only this child calls the function; controller never hashes/copies inputs.

    The private spool has one active child. Admission's exclusive spool lock and
    startup cleanup mean this reservation is the entire service's live budget.
    Both source descriptors remain open throughout copying, even after unlink.
    """
    prefix = f"{source.job_id}/{source.attempt_token}/"
    with ExitStack() as stack:
        root = open_root(settings.job_root)
        stack.callback(os.close, root)
        metadata_file = stack.enter_context(open_source(root, f"{prefix}stages/{source.stage_run_id}/result.json"))
        before_metadata = os.fstat(metadata_file.fileno())
        if before_metadata.st_size > METADATA_LIMIT:
            raise AnalysisError("invalid_analysis_data", "阶段轨迹元数据超过允许大小。", 422)
        raw_metadata = metadata_file.read(METADATA_LIMIT + 1)
        if len(raw_metadata) > METADATA_LIMIT or len(raw_metadata) != before_metadata.st_size:
            raise AnalysisError("artifact_identity_mismatch", "阶段轨迹元数据在读取期间发生变化。", 409)
        result = strict_json(raw_metadata)
        after_metadata = os.fstat(metadata_file.fileno())
        stable = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if stable(before_metadata) != stable(after_metadata):
            raise AnalysisError("artifact_identity_mismatch", "阶段轨迹元数据在读取期间发生变化。", 409)
        if not isinstance(result, dict) or result.get("stage") != source.stage:
            raise AnalysisError("artifact_identity_mismatch", "轨迹元数据与当前阶段不匹配。", 409)
        payload = result.get("results", result)
        metadata = payload.get("trajectory") if isinstance(payload, dict) else None
        if not isinstance(metadata, dict):
            raise AnalysisError("trajectory_not_found", "当前已完成阶段没有可用轨迹。", 404)
        count = metadata.get("frame_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise AnalysisError("invalid_analysis_data", "轨迹缺少真实帧数。", 422)
        if isinstance(source, DistanceAnalysisRequest):
            query = source.distance
            end = query.stop_frame if query.stop_frame is not None else count
            if query.start_frame >= count or end > count or (end - query.start_frame + query.stride - 1) // query.stride > 10000:
                raise AnalysisError("invalid_analysis_request", "距离分析区间超出轨迹范围或超过一万个样本。", 422)
        elif isinstance(source, TrajectoryAnalysisRequest) and source.frame_index >= count:
            raise AnalysisError("invalid_analysis_request", "构象帧超出真实轨迹范围。", 422)
        inputs = []
        for key, name in (("xtc_file", "trajectory.xtc"), ("pdb_file", "cell.pdb")):
            relative = metadata.get(key)
            if not isinstance(relative, str):
                raise AnalysisError("trajectory_not_found", "轨迹输入文件记录不完整。", 404)
            if not relative.lower().endswith(".xtc" if key == "xtc_file" else ".pdb"):
                raise AnalysisError("artifact_reference_invalid", "轨迹输入类型无效。", 409)
            reference = prefix + "science/" + relative
            matches = [a for a in source.artifacts if a.storage_ref == reference]
            if len(matches) != 1:
                raise AnalysisError("artifact_identity_mismatch", "轨迹输入与该阶段的产物清单不匹配。", 409)
            artifact = matches[0]
            handle = stack.enter_context(open_source(root, reference))
            before = os.fstat(handle.fileno())
            if before.st_size != artifact.size_bytes:
                raise AnalysisError("artifact_integrity_mismatch", "轨迹输入大小与已保存记录不符。", 409)
            inputs.append((handle, artifact, before, name))
        required = sum(a.size_bytes for _, a, _, _ in inputs) + OUTPUT_RESERVE
        check_space(private, required, settings.spool_budget_bytes, settings.min_free_bytes, settings.free_fraction)
        for handle, artifact, before, name in inputs:
            digest, received = hashlib.sha256(), 0
            target_fd = os.open(private / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(target_fd, "wb") as target:
                while data := handle.read(1024 ** 2):
                    received += len(data)
                    if received > artifact.size_bytes:
                        raise AnalysisError("artifact_integrity_mismatch", "轨迹输入在读取期间发生变化。", 409)
                    digest.update(data)
                    target.write(data)
                    if received % (32 * 1024 ** 2) == 0:
                        check_space(private, required - sum((private / n).stat().st_size for _, _, _, n in inputs if (private / n).exists()), settings.spool_budget_bytes, settings.min_free_bytes, settings.free_fraction)
            after = os.fstat(handle.fileno())
            if (received != artifact.size_bytes or digest.hexdigest() != artifact.sha256
                    or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
                raise AnalysisError("artifact_integrity_mismatch", "轨迹输入摘要与已保存记录不符。", 409)
        metadata = {**metadata, "xtc_file": "trajectory.xtc", "pdb_file": "cell.pdb"}
        return {"stage": source.stage, "results": {"trajectory": metadata}}
