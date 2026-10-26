"""Read only, version checked access to assets frozen into local snapshots."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from functools import lru_cache
from contextlib import suppress
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit

import urllib3
from fastapi import HTTPException, Request
from minio import Minio
from minio.error import S3Error
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from app.services.private_execution import PrivateStreamingResponse

MAX_FULLTEXT_BYTES = 32 * 1024 * 1024


def _error(status: int, code: str, message: str):
    return HTTPException(status, detail={"code": code, "message": message, "retryable": status in {502, 503, 504}})


@lru_cache(maxsize=1)
def _client() -> Minio:
    endpoint = os.environ.get("LITERATURE_MINIO_ENDPOINT", "").strip()
    access = os.environ.get("LITERATURE_MINIO_ACCESS_KEY", "")
    secret = os.environ.get("LITERATURE_MINIO_SECRET_KEY", "")
    if not endpoint or not access or not secret:
        raise _error(503, "LITERATURE_ASSET_UNAVAILABLE", "文献文件服务尚未配置")
    secure = os.environ.get("LITERATURE_MINIO_SECURE", "true").lower() in {"1", "true", "yes"}
    if "://" in endpoint:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or parsed.path not in {"", "/"} or parsed.username:
            raise _error(503, "LITERATURE_ASSET_UNAVAILABLE", "文献文件服务配置无效")
        endpoint, secure = parsed.netloc, parsed.scheme == "https"
    return Minio(endpoint, access_key=access, secret_key=secret, secure=secure,
                 http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=5, read=20), retries=False, maxsize=16))


def _stat(asset: dict):
    try:
        return _client().stat_object(asset["bucket"], asset["object_key"],
                                     version_id=asset.get("object_version_id"))
    except HTTPException:
        raise
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchVersion", "NoSuchBucket"}:
            raise _error(404, "LITERATURE_ASSET_MISSING", "登记的文献文件不存在") from None
        raise _error(503, "LITERATURE_ASSET_UNAVAILABLE", "文献文件服务暂不可用") from None
    except (TimeoutError, urllib3.exceptions.TimeoutError):
        raise _error(504, "LITERATURE_ASSET_TIMEOUT", "文献文件读取超时") from None
    except Exception:
        raise _error(503, "LITERATURE_ASSET_UNAVAILABLE", "文献文件服务暂不可用") from None


def probe_asset(asset: dict) -> dict:
    """Ingest-only HEAD. An ETag is an opaque object version, never an MD5."""
    result = dict(asset)
    try:
        stat = _stat(asset)
        result.update(status="available", size_bytes=int(stat.size), etag=stat.etag,
                      object_version_id=getattr(stat, "version_id", None))
        result.pop("error_code", None)
    except HTTPException as exc:
        result.update(status="missing" if exc.status_code == 404 else "unverified", error_code=exc.detail["code"])
    return result


def get_literature_asset_summary(assets_manifest: list[dict]) -> dict:
    safe_keys = ("asset_uid", "role", "status", "filename", "media_type", "size_bytes", "exact_input_verified")
    assets = [{key: asset.get(key) for key in safe_keys} for asset in assets_manifest]
    roles = {a.get("role") for a in assets_manifest if a.get("status") == "available" and a.get("etag")}
    return {"assets": assets, "capabilities": {"has_fulltext": "full_text" in roles,
            "has_pdf": "pdf" in roles, "has_original": bool(roles & {"full_text", "pdf"}),
            "has_attachments": "attachment" in roles}}


def _select(assets: list[dict], *, uid: str | None = None, role: str | None = None) -> dict:
    found = [a for a in assets if (a.get("asset_uid") == uid if uid else a.get("role") == role)]
    if not found:
        raise _error(404, "LITERATURE_ASSET_MISSING", "本次收录未提供该文件")
    if role == "full_text" and len(found) > 1:
        exact = [a for a in found if a.get("status") == "available" and a.get("etag")
                 and a.get("exact_input_verified") is True]
        if len(exact) == 1 and all(a is exact[0] or (
                a.get("status") == "ambiguous" and a.get("error_code") == "NON_SELECTED_EXTRACTION_INPUT")
                for a in found):
            return exact[0]
    if len(found) != 1 or found[0].get("status") == "ambiguous":
        raise _error(409, "LITERATURE_ASSET_AMBIGUOUS", "存在多份候选原文，尚未确认版本")
    asset = found[0]
    if asset.get("status") != "available" or not asset.get("etag"):
        raise _error(503, "LITERATURE_ASSET_UNVERIFIED", "该文件尚未完成可用性核验")
    return asset


def _verify(asset: dict):
    stat = _stat(asset)
    if stat.etag != asset["etag"] or (asset.get("size_bytes") is not None and stat.size != asset["size_bytes"]):
        raise _error(409, "LITERATURE_ASSET_CHANGED", "原文对象已变化，请重新收录后核验")
    return stat


def _get(asset: dict, *, offset=0, length=0):
    try:
        return _client().get_object(asset["bucket"], asset["object_key"], offset=offset, length=length,
            version_id=asset.get("object_version_id"), request_headers={"If-Match": '"' + asset["etag"].strip('"') + '"'})
    except S3Error as exc:
        if exc.code in {"PreconditionFailed", "NoSuchVersion"}:
            raise _error(409, "LITERATURE_ASSET_CHANGED", "原文对象已变化，请重新收录后核验") from None
        if exc.code in {"NoSuchKey", "NoSuchObject"}:
            raise _error(404, "LITERATURE_ASSET_MISSING", "登记的文献文件不存在") from None
        raise _error(503, "LITERATURE_ASSET_UNAVAILABLE", "文献文件服务暂不可用") from None
    except (TimeoutError, urllib3.exceptions.TimeoutError):
        raise _error(504, "LITERATURE_ASSET_TIMEOUT", "文献文件读取超时") from None
    except HTTPException:
        raise
    except Exception:
        raise _error(503, "LITERATURE_ASSET_UNAVAILABLE", "文献文件服务暂不可用") from None


def _read(response, amount: int) -> bytes:
    try:
        return response.read(amount)
    except (TimeoutError, urllib3.exceptions.TimeoutError):
        raise _error(504, "LITERATURE_ASSET_TIMEOUT", "文献文件读取超时") from None
    except Exception:
        raise _error(503, "LITERATURE_ASSET_UNAVAILABLE", "文献文件读取中断") from None


def _release(response):
    # Cleanup must not hide an upstream error or cancellation.
    with suppress(Exception):
        response.close()
    with suppress(Exception):
        response.release_conn()


class _SafeTableParser(HTMLParser):
    """Extract cell text only; no source HTML is ever sent as executable markup."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.text, self.rows, self.row, self.cell = [], [], [], [], None
        self.in_table, self.ignore = False, 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignore += 1
        if self.ignore:
            return
        if tag == "table":
            if self.text:
                self.parts.append({"type": "text", "text": "".join(self.text)})
                self.text = []
            self.in_table, self.rows = True, []
        elif tag == "tr" and self.in_table:
            self.row = []
        elif tag in {"td", "th"} and self.in_table:
            self.cell = []
        elif tag in {"br", "p"}:
            (self.cell if self.cell is not None else self.text).append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.ignore = max(0, self.ignore - 1)
            return
        if self.ignore:
            return
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = []
        elif tag == "table" and self.in_table:
            self.parts.append({"type": "table", "rows": self.rows})
            self.in_table = False

    def handle_data(self, data):
        if not self.ignore:
            (self.cell if self.cell is not None else self.text).append(data)

    def result(self):
        if self.text:
            self.parts.append({"type": "text", "text": "".join(self.text)})
        return self.parts


def _load_fulltext(asset: dict) -> list:
    stat = _verify(asset)
    if stat.size > MAX_FULLTEXT_BYTES:
        raise _error(413, "LITERATURE_FULLTEXT_TOO_LARGE", "正文超过读取上限")
    response = _get(asset)
    try:
        raw = _read(response, MAX_FULLTEXT_BYTES + 1)
        if len(raw) > MAX_FULLTEXT_BYTES:
            raise _error(413, "LITERATURE_FULLTEXT_TOO_LARGE", "正文超过读取上限")
        if len(raw) != stat.size:
            raise _error(502, "LITERATURE_ASSET_TRUNCATED", "文献文件内容不完整")
        value = json.loads(raw)
        if isinstance(value, dict):
            value = value.get("blocks", value.get("data"))
        if not isinstance(value, list):
            raise ValueError("unsupported blocks")
        return value
    except HTTPException:
        raise
    except (ValueError, UnicodeError):
        raise _error(502, "LITERATURE_FULLTEXT_INVALID", "原文块结构无法解析") from None
    finally:
        _release(response)


async def get_literature_fulltext(*, assets_manifest: list[dict], document_uid: str,
                                  snapshot_uid: str, cursor: str | None = None, limit: int = 100) -> dict:
    asset = _select(assets_manifest, role="full_text")
    binding = hashlib.sha256(f'{document_uid}:{snapshot_uid}:{asset["asset_uid"]}:{asset["etag"]}'.encode()).hexdigest()[:24]
    offset = 0
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if decoded["binding"] != binding or type(decoded["offset"]) is not int or decoded["offset"] < 0:
                raise ValueError()
            offset = decoded["offset"]
        except (ValueError, TypeError, KeyError):
            raise _error(422, "LITERATURE_CURSOR_INVALID", "正文游标不属于本次文献快照") from None
    blocks = await run_in_threadpool(_load_fulltext, asset)
    output = []
    for index, block in enumerate(blocks[offset:offset + limit], offset):
        if not isinstance(block, dict):
            block = {"text": str(block)}
        text = str(block.get("text") or block.get("content") or "")
        parser = _SafeTableParser()
        parser.feed(text)
        output.append({"block_id": str(block.get("idx", block.get("id", index))),
                       "type": str(block.get("type") or "text"), "text": text,
                       "heading": str(block["heading"]) if block.get("heading") else None,
                       "parts": parser.result()})
    next_offset = offset + limit
    next_cursor = base64.urlsafe_b64encode(json.dumps({"binding": binding, "offset": next_offset}).encode()).decode().rstrip("=") if next_offset < len(blocks) else None
    return {"document_uid": document_uid, "snapshot_uid": snapshot_uid, "blocks": output,
            "next_cursor": next_cursor, "exact_input_verified": bool(asset.get("exact_input_verified"))}


def _range(value: str | None, size: int) -> tuple[int, int, bool]:
    if not value:
        return 0, size, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or not any(match.groups()) or size == 0:
        raise HTTPException(416, detail={"code": "LITERATURE_RANGE_INVALID"}, headers={"Content-Range": f"bytes */{size}"})
    left, right = match.groups()
    try:
        start = int(left) if left else max(0, size - int(right))
        end = min(size - 1, int(right)) if left and right else size - 1
        zero_suffix = not left and int(right) == 0
    except ValueError:
        # Python bounds decimal conversion length; oversized HTTP ranges are
        # invalid requests rather than unhandled server errors.
        raise HTTPException(416, detail={"code": "LITERATURE_RANGE_INVALID"}, headers={"Content-Range": f"bytes */{size}"}) from None
    if start >= size or end < start or zero_suffix:
        raise HTTPException(416, detail={"code": "LITERATURE_RANGE_INVALID"}, headers={"Content-Range": f"bytes */{size}"})
    return start, end - start + 1, True


async def get_literature_file_response(*, assets_manifest: list[dict], file_uid: str, request: Request) -> Response:
    asset = _select(assets_manifest, uid=file_uid)
    if asset.get("role") not in {"pdf", "attachment"}:
        raise _error(404, "LITERATURE_ASSET_MISSING", "该资产不支持文件下载")
    stat = await run_in_threadpool(_verify, asset)
    offset, length, partial = _range(request.headers.get("range"), stat.size)
    name = str(asset.get("filename") or "document").replace("\r", "").replace("\n", "")
    disposition = "inline" if asset["role"] == "pdf" and request.query_params.get("download") != "true" else "attachment"
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length), "Cache-Control": "private, no-store",
               "ETag": '"' + asset["etag"].strip('"') + '"', "X-Content-Type-Options": "nosniff",
               "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name, safe='')}"}
    if partial:
        headers["Content-Range"] = f"bytes {offset}-{offset + length - 1}/{stat.size}"
    status = 206 if partial else 200
    media = "application/pdf" if asset["role"] == "pdf" else "application/octet-stream"
    if request.method == "HEAD" or length == 0:
        return Response(status_code=status, headers=headers, media_type=media)
    upstream = await run_in_threadpool(_get, asset, offset=offset, length=length)
    try:
        # Report initial transport/read failures before sending HTTP 200/206.
        first = await run_in_threadpool(_read, upstream, min(64 * 1024, length))
        if not first or len(first) > min(64 * 1024, length):
            raise _error(502, "LITERATURE_ASSET_TRUNCATED", "文献文件内容不完整")
        producer = _FileChunks(upstream, first, length)
    except BaseException:
        _release(upstream)
        raise
    return PrivateStreamingResponse(producer, status_code=status, headers=headers, media_type=media)


class _FileChunks:
    """Explicit close works even when disconnect precedes the first iteration."""
    def __init__(self, upstream, first: bytes, length: int):
        self.upstream, self.first, self.remaining = upstream, first, length
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        try:
            if not self.remaining:
                self.close()
                raise StopIteration
            if self.first is not None:
                data, self.first = self.first, None
            else:
                data = _read(self.upstream, min(64 * 1024, self.remaining))
            if not data or len(data) > self.remaining:
                raise _error(502, "LITERATURE_ASSET_TRUNCATED", "文献文件内容不完整")
            self.remaining -= len(data)
            return data
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self.closed:
            self.closed = True
            _release(self.upstream)
