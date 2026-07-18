from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import stat
import tempfile
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.responses import FileResponse

from .monomer_dft_models import MAX_ARTIFACT_BYTES, validate_portable_artifact_filename
from .monomer_dft_worker_client import MonomerDftWorkerStream


MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_ENTRIES = 100
_SHA256_ETAG = re.compile(r'^"([0-9a-f]{64})"$')
_ALLOWED_ZIP_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_PROCESS_SPOOL = re.compile(r"^process-([1-9][0-9]*)-([1-9][0-9]*)$")


class MonomerDftDownloadProxyError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "artifact_integrity_mismatch",
        status_code: int = 502,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


@dataclass(slots=True)
class VerifiedMonomerDftDownload:
    path: Path
    size_bytes: int
    sha256: str
    _release_slot: Callable[[], Awaitable[None]]
    _released: bool = field(default=False, init=False)

    async def cleanup(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            await asyncio.to_thread(self.path.unlink, missing_ok=True)
        finally:
            await self._release_slot()


class VerifiedMonomerDftFileResponse(FileResponse):
    """File response that releases the verified spool lease on every ASGI exit."""

    def __init__(self, *, verified: VerifiedMonomerDftDownload, **kwargs: Any) -> None:
        self._verified = verified
        super().__init__(path=verified.path, **kwargs)

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await asyncio.shield(self._verified.cleanup())


class MonomerDftDownloadProxy:
    """Verify Worker downloads before an HTTP success response is committed."""

    def __init__(self, *, spool_root: str, max_concurrent: int = 2) -> None:
        if max_concurrent != 2:
            raise ValueError("monomer DFT download concurrency must be exactly 2")
        self._spool_base = Path(spool_root)
        self._process_identity = (os.getpid(), _process_start_ticks(os.getpid()))
        self._spool_root = self._spool_base / (
            f"process-{self._process_identity[0]}-{self._process_identity[1]}"
        )
        self._max_concurrent = max_concurrent
        self._slot_lock = asyncio.Lock()
        self._active_downloads = 0

    async def _acquire_slot(self) -> None:
        async with self._slot_lock:
            if self._active_downloads >= self._max_concurrent:
                raise MonomerDftDownloadProxyError(
                    "monomer DFT download capacity is full",
                    code="download_capacity_full",
                    status_code=503,
                    retryable=True,
                )
            self._active_downloads += 1

    async def _release_slot(self) -> None:
        remove_process_spool = False
        async with self._slot_lock:
            if self._active_downloads <= 0:  # pragma: no cover - defensive invariant
                raise RuntimeError("monomer DFT download slot underflow")
            self._active_downloads -= 1
            remove_process_spool = self._active_downloads == 0
        if remove_process_spool:
            await asyncio.to_thread(
                _remove_empty_process_spool,
                self._spool_base,
                self._spool_root,
            )

    async def verify_artifact(
        self,
        *,
        open_stream: Callable[[], Awaitable[MonomerDftWorkerStream]],
        artifact: dict[str, Any],
    ) -> VerifiedMonomerDftDownload:
        size_bytes = artifact.get("size_bytes")
        sha256 = artifact.get("sha256")
        if (
            isinstance(size_bytes, int)
            and not isinstance(size_bytes, bool)
            and size_bytes > MAX_ARTIFACT_BYTES
        ):
            raise MonomerDftDownloadProxyError(
                "DFT artifact exceeds the supported size limit",
                code="artifact_size_out_of_contract",
                retryable=False,
            )
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or not 0 <= size_bytes <= MAX_ARTIFACT_BYTES
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        ):
            raise MonomerDftDownloadProxyError(
                "DFT artifact manifest exceeds the supported download contract",
                code="artifact_manifest_invalid",
                retryable=False,
            )
        return await self._verify_stream(
            open_stream=open_stream,
            maximum_bytes=MAX_ARTIFACT_BYTES,
            expected_size=size_bytes,
            expected_sha256=sha256,
            suffix=_safe_spool_suffix(str(artifact.get("name") or "artifact.bin")),
        )

    async def verify_bundle(
        self,
        *,
        open_stream: Callable[[], Awaitable[MonomerDftWorkerStream]],
        artifacts: list[dict[str, Any]],
    ) -> VerifiedMonomerDftDownload:
        verified = await self._verify_stream(
            open_stream=open_stream,
            maximum_bytes=MAX_BUNDLE_BYTES,
            expected_size=None,
            expected_sha256=None,
            suffix=".zip",
        )
        verification = asyncio.create_task(
            asyncio.to_thread(
                _verify_and_canonicalize_zip,
                verified.path,
                artifacts,
            ),
            name="monomer-dft-verify-bundle",
        )
        try:
            # Shield the task so request cancellation cannot abandon a live
            # thread that is still reading/replacing the same spool path.
            canonical_size, canonical_sha256 = await asyncio.shield(verification)
            verified.size_bytes = canonical_size
            verified.sha256 = canonical_sha256
        except asyncio.CancelledError:
            # A Python thread cannot be force-cancelled.  Wait for its atomic
            # verify/repack operation to finish before unlinking the spool and
            # releasing the global download slot, then preserve cancellation.
            # Explicitly consume every additional cancellation while this
            # non-abandonable cleanup is in progress; otherwise a disconnect
            # followed by shutdown/timeout cancellation can leak both the
            # private file and its global slot.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                current.uncancel()
            while not verification.done():
                try:
                    await asyncio.shield(verification)
                except asyncio.CancelledError:
                    if current is not None and current.cancelling():
                        current.uncancel()
                except Exception:
                    break
            if verification.done() and not verification.cancelled():
                with contextlib.suppress(Exception):
                    verification.result()
            cleanup = asyncio.create_task(
                verified.cleanup(),
                name="monomer-dft-cleanup-cancelled-bundle",
            )
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    if current is not None and current.cancelling():
                        current.uncancel()
                except Exception:
                    break
            if cleanup.done() and not cleanup.cancelled():
                with contextlib.suppress(Exception):
                    cleanup.result()
            raise
        except MonomerDftDownloadProxyError as exc:
            await asyncio.shield(verified.cleanup())
            if exc.code in {
                "artifact_manifest_invalid",
                "artifact_not_found",
                "artifact_size_out_of_contract",
            }:
                raise
            raise MonomerDftDownloadProxyError(
                str(exc),
                code="artifact_bundle_invalid",
                retryable=exc.retryable,
            ) from exc
        except Exception as exc:
            await asyncio.shield(verified.cleanup())
            raise MonomerDftDownloadProxyError(
                "DFT artifact bundle does not match the persisted manifest",
                code="artifact_bundle_invalid",
            ) from exc
        return verified

    async def _verify_stream(
        self,
        *,
        open_stream: Callable[[], Awaitable[MonomerDftWorkerStream]],
        maximum_bytes: int,
        expected_size: int | None,
        expected_sha256: str | None,
        suffix: str,
    ) -> VerifiedMonomerDftDownload:
        await self._acquire_slot()
        stream: MonomerDftWorkerStream | None = None
        temporary_path: Path | None = None
        try:
            await asyncio.to_thread(
                _prepare_process_spool,
                self._spool_base,
                self._spool_root,
                self._process_identity,
            )
            stream = await open_stream()
            declared_size, declared_sha256 = _validated_upstream_headers(
                stream.response.headers,
                maximum_bytes=maximum_bytes,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
            file_descriptor, temporary_name = await asyncio.to_thread(
                tempfile.mkstemp,
                prefix=".download-",
                suffix=suffix,
                dir=self._spool_root,
            )
            temporary_path = Path(temporary_name)
            os.fchmod(file_descriptor, 0o600)
            digest = hashlib.sha256()
            actual_size = 0
            with os.fdopen(file_descriptor, "wb", closefd=True) as output:
                try:
                    async for chunk in stream.raw_body_iterator:
                        actual_size += len(chunk)
                        if actual_size > maximum_bytes or actual_size > declared_size:
                            raise MonomerDftDownloadProxyError(
                                "DFT worker download exceeds its declared size"
                            )
                        digest.update(chunk)
                        await asyncio.to_thread(output.write, chunk)
                    await asyncio.to_thread(_flush_file, output)
                except MonomerDftDownloadProxyError:
                    raise
                except Exception as exc:
                    raise MonomerDftDownloadProxyError(
                        "DFT worker download was interrupted",
                        code="worker_download_interrupted",
                    ) from exc

            actual_sha256 = digest.hexdigest()
            if actual_size != declared_size or actual_sha256 != declared_sha256:
                raise MonomerDftDownloadProxyError(
                    "DFT worker download does not match its declared checksum"
                )
            if expected_size is not None and actual_size != expected_size:
                raise MonomerDftDownloadProxyError(
                    "DFT worker artifact size does not match the persisted manifest"
                )
            if expected_sha256 is not None and actual_sha256 != expected_sha256:
                raise MonomerDftDownloadProxyError(
                    "DFT worker artifact checksum does not match the persisted manifest"
                )
            return VerifiedMonomerDftDownload(
                path=temporary_path,
                size_bytes=actual_size,
                sha256=actual_sha256,
                _release_slot=self._release_slot,
            )
        except OSError as exc:
            if temporary_path is not None:
                await asyncio.to_thread(temporary_path.unlink, missing_ok=True)
            await self._release_slot()
            raise MonomerDftDownloadProxyError(
                "monomer DFT download staging is unavailable",
                code="download_staging_unavailable",
                status_code=503,
            ) from exc
        except BaseException:
            if temporary_path is not None:
                await asyncio.to_thread(temporary_path.unlink, missing_ok=True)
            await self._release_slot()
            raise
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    await stream.close()


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OSError("download spool root must be a real directory")
    if metadata.st_uid != os.geteuid():
        raise OSError("download spool root must be owned by the backend uid")
    os.chmod(path, 0o700)


def _process_start_ticks(pid: int) -> int:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw.rsplit(")", 1)[1].split()
        start_ticks = int(fields[19])
    except (OSError, IndexError, ValueError) as exc:
        raise OSError(f"cannot establish process identity for PID {pid}") from exc
    if start_ticks <= 0:
        raise OSError(f"invalid process identity for PID {pid}")
    return start_ticks


def _process_identity_is_live(pid: int, start_ticks: int) -> bool:
    try:
        return _process_start_ticks(pid) == start_ticks
    except OSError:
        return False


def _remove_private_spool_tree(path: Path, *, expected_device: int) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_dev != expected_device
    ):
        raise OSError("stale download spool has an unsafe identity")
    for child in path.iterdir():
        child_metadata = child.lstat()
        if (
            stat.S_ISLNK(child_metadata.st_mode)
            or child_metadata.st_uid != os.geteuid()
            or child_metadata.st_dev != expected_device
        ):
            raise OSError("stale download spool contains an unsafe entry")
        if stat.S_ISDIR(child_metadata.st_mode):
            _remove_private_spool_tree(child, expected_device=expected_device)
        elif stat.S_ISREG(child_metadata.st_mode):
            child.unlink()
        else:
            raise OSError("stale download spool contains a special file")
    path.rmdir()


def _prepare_process_spool(
    base: Path,
    process_root: Path,
    process_identity: tuple[int, int],
) -> None:
    _ensure_private_directory(base)
    base_metadata = base.lstat()
    for candidate in base.iterdir():
        match = _PROCESS_SPOOL.fullmatch(candidate.name)
        if match is None:
            raise OSError("download spool contains an unrecognized entry")
        candidate_metadata = candidate.lstat()
        if (
            stat.S_ISLNK(candidate_metadata.st_mode)
            or not stat.S_ISDIR(candidate_metadata.st_mode)
            or candidate_metadata.st_uid != os.geteuid()
            or candidate_metadata.st_dev != base_metadata.st_dev
        ):
            raise OSError("download spool contains an unsafe process entry")
        pid, start_ticks = (int(value) for value in match.groups())
        if (pid, start_ticks) == process_identity:
            continue
        if _process_identity_is_live(pid, start_ticks):
            continue
        _remove_private_spool_tree(
            candidate,
            expected_device=base_metadata.st_dev,
        )
    _ensure_private_directory(process_root)
    if process_root.parent != base:
        raise OSError("process download spool escaped its configured root")


def _remove_empty_process_spool(base: Path, process_root: Path) -> None:
    if process_root.parent != base:
        raise OSError("process download spool escaped its configured root")
    try:
        process_root.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        # Another active download still owns a file.  The final lease release
        # will retry removal.
        return


def _safe_spool_suffix(name: str) -> str:
    """Keep mkstemp names below NAME_MAX for every valid artifact name."""

    suffix = Path(name).suffix
    if re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix):
        return suffix
    return ".bin"


def _flush_file(output) -> None:
    output.flush()
    os.fsync(output.fileno())


def _validated_upstream_headers(
    headers: Any,
    *,
    maximum_bytes: int,
    expected_size: int | None,
    expected_sha256: str | None,
) -> tuple[int, str]:
    raw_length = headers.get("content-length")
    raw_etag = headers.get("etag")
    raw_encoding = headers.get("content-encoding")
    if isinstance(raw_encoding, str) and raw_encoding.strip().lower() not in {"", "identity"}:
        raise MonomerDftDownloadProxyError(
            "DFT worker download must use identity content encoding"
        )
    if not isinstance(raw_length, str) or not raw_length.isascii() or not raw_length.isdigit():
        raise MonomerDftDownloadProxyError("DFT worker download is missing a valid Content-Length")
    declared_size = int(raw_length)
    match = _SHA256_ETAG.fullmatch(raw_etag or "")
    if declared_size > maximum_bytes or match is None:
        raise MonomerDftDownloadProxyError("DFT worker download headers exceed the supported contract")
    declared_sha256 = match.group(1)
    if expected_size is not None and declared_size != expected_size:
        raise MonomerDftDownloadProxyError(
            "DFT worker artifact size does not match the persisted manifest"
        )
    if expected_sha256 is not None and declared_sha256 != expected_sha256:
        raise MonomerDftDownloadProxyError(
            "DFT worker artifact checksum does not match the persisted manifest"
        )
    return declared_size, declared_sha256


def _verify_zip_members(path: Path, artifacts: list[dict[str, Any]]) -> None:
    expected: dict[str, dict[str, Any]] = {}
    folded_names: set[str] = set()
    expected_uncompressed_bytes = 0
    for artifact in artifacts:
        if artifact.get("available") is not True:
            continue
        try:
            name = validate_portable_artifact_filename(artifact.get("name"))
        except ValueError as exc:
            raise MonomerDftDownloadProxyError(
                "persisted DFT artifact name is not portable",
                code="artifact_manifest_invalid",
                retryable=False,
            ) from exc
        folded = name.casefold()
        if name in expected or folded in folded_names:
            raise MonomerDftDownloadProxyError(
                "persisted DFT artifact names are ambiguous",
                code="artifact_manifest_invalid",
                retryable=False,
            )
        size_bytes = artifact.get("size_bytes")
        sha256 = artifact.get("sha256")
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or not 0 <= size_bytes <= MAX_ARTIFACT_BYTES
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        ):
            raise MonomerDftDownloadProxyError(
                "persisted DFT artifact metadata is invalid",
                code="artifact_manifest_invalid",
                retryable=False,
            )
        expected_uncompressed_bytes += size_bytes
        if expected_uncompressed_bytes > MAX_BUNDLE_BYTES:
            raise MonomerDftDownloadProxyError(
                "DFT artifact bundle exceeds the supported expanded size limit",
                code="artifact_size_out_of_contract",
                retryable=False,
            )
        expected[name] = artifact
        folded_names.add(folded)
        if len(expected) > MAX_BUNDLE_ENTRIES:
            raise MonomerDftDownloadProxyError(
                "DFT artifact bundle contains too many persisted members",
                code="artifact_manifest_invalid",
                retryable=False,
            )
    if not expected:
        raise MonomerDftDownloadProxyError(
            "DFT job has no available artifacts",
            code="artifact_not_found",
            status_code=404,
            retryable=False,
        )

    try:
        with zipfile.ZipFile(path, mode="r") as archive:
            members = archive.infolist()
            if len(members) > MAX_BUNDLE_ENTRIES:
                raise MonomerDftDownloadProxyError(
                    "DFT artifact bundle contains too many members"
                )
            actual_names: set[str] = set()
            actual_folded_names: set[str] = set()
            for member in members:
                try:
                    member_name = validate_portable_artifact_filename(member.filename)
                except ValueError as exc:
                    raise MonomerDftDownloadProxyError(
                        "DFT artifact bundle contains an unsafe member name"
                    ) from exc
                folded = member_name.casefold()
                unix_mode = (member.external_attr >> 16) & 0o170000
                if (
                    member.is_dir()
                    or member.flag_bits & 0x1
                    or member.compress_type not in _ALLOWED_ZIP_COMPRESSION
                    or (unix_mode not in {0, stat.S_IFREG})
                    or member_name in actual_names
                    or folded in actual_folded_names
                ):
                    raise MonomerDftDownloadProxyError(
                        "DFT artifact bundle contains an unsupported member"
                    )
                actual_names.add(member_name)
                actual_folded_names.add(folded)
            if actual_names != set(expected):
                raise MonomerDftDownloadProxyError(
                    "DFT artifact bundle members do not match the persisted manifest"
                )

            for member in members:
                descriptor = expected[member.filename]
                expected_size = int(descriptor["size_bytes"])
                if member.file_size != expected_size:
                    raise MonomerDftDownloadProxyError(
                        "DFT artifact bundle member size does not match the persisted manifest"
                    )
                digest = hashlib.sha256()
                actual_size = 0
                with archive.open(member, mode="r") as content:
                    for chunk in iter(lambda: content.read(1024 * 1024), b""):
                        actual_size += len(chunk)
                        if actual_size > expected_size:
                            raise MonomerDftDownloadProxyError(
                                "DFT artifact bundle member exceeds its persisted size"
                            )
                        digest.update(chunk)
                if actual_size != expected_size or digest.hexdigest() != descriptor["sha256"]:
                    raise MonomerDftDownloadProxyError(
                        "DFT artifact bundle member checksum does not match the persisted manifest"
                    )
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        if isinstance(exc, MonomerDftDownloadProxyError):
            raise
        raise MonomerDftDownloadProxyError("DFT worker returned an invalid ZIP bundle") from exc


def _verify_and_canonicalize_zip(
    path: Path,
    artifacts: list[dict[str, Any]],
) -> tuple[int, str]:
    """Run all ZIP filesystem work as one non-abandonable thread operation."""

    _verify_zip_members(path, artifacts)
    return _canonicalize_verified_zip(path, artifacts)


def _canonicalize_verified_zip(
    path: Path,
    artifacts: list[dict[str, Any]],
) -> tuple[int, str]:
    """Replace a verified Worker ZIP with a deterministic manifest-only archive.

    Member hashes are already bound to PostgreSQL by ``_verify_zip_members``.
    Repacking removes unbound container data such as ZIP comments, per-member
    comments/extra fields, prepended stubs and trailing bytes before the public
    response is committed.
    """

    expected_names = sorted(
        validate_portable_artifact_filename(artifact.get("name"))
        for artifact in artifacts
        if artifact.get("available") is True
    )
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".canonical-bundle-",
        suffix=".zip",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with (
            zipfile.ZipFile(path, mode="r") as source,
            os.fdopen(file_descriptor, "w+b", closefd=True) as output,
        ):
            file_descriptor = -1
            with zipfile.ZipFile(
                output,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as target:
                target.comment = b""
                for name in expected_names:
                    canonical = zipfile.ZipInfo(
                        filename=name,
                        date_time=(1980, 1, 1, 0, 0, 0),
                    )
                    canonical.compress_type = zipfile.ZIP_DEFLATED
                    canonical.create_system = 3
                    canonical.external_attr = (stat.S_IFREG | 0o600) << 16
                    canonical.internal_attr = 0
                    canonical.extra = b""
                    canonical.comment = b""
                    with (
                        source.open(name, mode="r") as member,
                        target.open(canonical, mode="w") as destination,
                    ):
                        for chunk in iter(lambda: member.read(1024 * 1024), b""):
                            destination.write(chunk)
            _flush_file(output)
            size_bytes = os.fstat(output.fileno()).st_size
        if size_bytes > MAX_BUNDLE_BYTES:
            raise MonomerDftDownloadProxyError(
                "canonical DFT artifact bundle exceeds the supported size limit",
                code="artifact_size_out_of_contract",
                retryable=False,
            )
        os.replace(temporary_path, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        # Validate the bytes that will actually be served, then bind their public
        # ETag to the canonical archive rather than the untrusted Worker wrapper.
        _verify_zip_members(path, artifacts)
        digest = hashlib.sha256()
        with path.open("rb") as canonical_stream:
            for chunk in iter(lambda: canonical_stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return size_bytes, digest.hexdigest()
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        temporary_path.unlink(missing_ok=True)
