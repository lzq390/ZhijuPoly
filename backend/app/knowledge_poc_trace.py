"""Temporary HTTP observation for the isolated POC; no reading events are inferred."""

import json
import logging
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("uvicorn.error")
SEARCH_PATH = "/api/v1/knowledge/search"
FILTER_SEARCH_PATH = "/api/v1/database-browser/property-filter/search"
FILTER_OBSERVATION_PATH = "/api/v1/database-browser/property-filter/observations"
OBSERVATION_PATH = "/api/v1/knowledge/observations"
RECORDING_PATH = "/api/v1/knowledge/recordings"
ABSTRACT_PREVIEW_CHARS = 160


def _json_object(body: bytearray) -> dict:
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class KnowledgeTraceMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int = 64 * 1024):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        started = perf_counter()
        event = {"time": datetime.now(timezone.utc).isoformat(),
                 "request_id": uuid4().hex[:12], "method": scope["method"],
                 "path": scope["path"], "status": None, "completed": False}
        bodies = {"request": bytearray(), "response": bytearray()}
        truncated = {"request": False, "response": False}

        def capture(kind: str, body: bytes):
            if scope["path"] not in (SEARCH_PATH, FILTER_SEARCH_PATH, FILTER_OBSERVATION_PATH, OBSERVATION_PATH) and not scope["path"].startswith(RECORDING_PATH):
                return
            remaining = self.max_body_bytes - len(bodies[kind])
            bodies[kind].extend(body[:remaining])
            truncated[kind] = truncated[kind] or len(body) > remaining

        async def observed_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                capture("request", message.get("body", b""))
            return message

        async def observed_send(message: Message):
            if message["type"] == "http.response.start":
                event["status"] = message["status"]
            elif message["type"] == "http.response.body":
                capture("response", message.get("body", b""))
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                event["completed"] = True

        try:
            await self.app(scope, observed_receive, observed_send)
        finally:
            event["duration_ms"] = round((perf_counter() - started) * 1000, 2)
            if scope["path"] == SEARCH_PATH:
                request = _json_object(bodies["request"])
                response = _json_object(bodies["response"])
                event["request"] = {key: request[key] for key in
                                    ("query", "groups", "terms", "page", "page_size", "top_k", "recording_id")
                                    if key in request}
                event["response"] = {key: response[key] for key in
                                     ("search_id", "query", "groups", "terms", "total", "page", "page_size")
                                     if key in response}
                results = response.get("results", [])
                event["response"]["results"] = []
                for row in results if isinstance(results, list) else []:
                    if not isinstance(row, dict):
                        continue
                    item = {key: row[key] for key in
                            ("knowledge_id", "title_zh", "title_en", "source_file") if key in row}
                    abstract = row.get("abstract")
                    if isinstance(abstract, str):
                        item.update(abstract_characters=len(abstract),
                                    abstract_preview=abstract[:ABSTRACT_PREVIEW_CHARS])
                    event["response"]["results"].append(item)
                event["truncated"] = truncated
            elif scope["path"] == FILTER_SEARCH_PATH:
                request = _json_object(bodies["request"])
                response = _json_object(bodies["response"])
                event["request"] = {key: request[key] for key in ("q", "page", "page_size", "recording_id") if key in request}
                filters = request.get("filters", [])
                event["request"]["filters"] = [
                    {key: condition[key] for key in
                     ("filter_type", "property_key", "canonical_unit", "property_name",
                      "property_unit_clean", "min_value", "max_value") if key in condition}
                    for condition in (filters if isinstance(filters, list) else []) if isinstance(condition, dict)
                ]
                event["response"] = {key: response[key] for key in
                                     ("search_id", "query", "page", "page_size", "total_records", "matched_records", "source_status")
                                     if key in response}
                event["response"]["results"] = []
                results = response.get("results", [])
                for row in results if isinstance(results, list) else []:
                    if not isinstance(row, dict):
                        continue
                    item = {key: row[key] for key in
                            ("polymer_name", "smiles", "canonical_smiles", "matched_filters") if key in row}
                    records = row.get("records", [])
                    item["records"] = [
                        {key: record[key] for key in
                         ("filter_record_id", "filter_index", "source_row_number", "property_name", "property_key",
                          "property_label", "property_value", "property_value_num", "property_unit_raw",
                          "property_unit_clean", "canonical_value", "canonical_unit", "value_origin",
                          "reliable_score", "unit_conversion_status", "label_source", "soft_quality_flags", "duplicate_flag")
                         if key in record}
                        for record in (records if isinstance(records, list) else []) if isinstance(record, dict)
                    ]
                    event["response"]["results"].append(item)
                event["truncated"] = truncated
            elif scope["path"] == FILTER_OBSERVATION_PATH:
                request = _json_object(bodies["request"])
                response = _json_object(bodies["response"])
                event["request"] = {key: request[key] for key in
                                    ("search_id", "result_index", "source", "filter_index", "smiles_field", "recording_id") if key in request}
                event["response"] = {key: response[key] for key in
                                     ("event", "search_id", "result_index", "source", "query", "page", "page_size",
                                      "filters", "polymer_name", "filter_index", "records", "smiles_field", "smiles_value", "recording_id")
                                     if key in response}
                event["truncated"] = truncated
            elif scope["path"] == OBSERVATION_PATH:
                request = _json_object(bodies["request"])
                response = _json_object(bodies["response"])
                event["request"] = {key: request[key] for key in
                                    ("search_id", "knowledge_id", "source", "recording_id") if key in request}
                event["response"] = {key: response[key] for key in
                                     ("event", "search_id", "knowledge_id", "source", "query", "recording_id",
                                      "title_en", "title_zh", "abstract_characters", "reaction_info", "detail")
                                     if key in response}
                event["truncated"] = truncated
            elif scope["path"].startswith(RECORDING_PATH):
                request = _json_object(bodies["request"])
                response = _json_object(bodies["response"])
                event["request"] = {key: request[key] for key in ("recording_id",) if key in request}
                event["response"] = {key: response[key] for key in
                                     ("recording_id", "status", "started_at", "ended_at", "detail") if key in response}
                if isinstance(response.get("events"), list):
                    event["response"]["event_count"] = len(response["events"])
                event["truncated"] = truncated
            logger.info("KNOWLEDGE_TRACE %s", json.dumps(event, ensure_ascii=False))
