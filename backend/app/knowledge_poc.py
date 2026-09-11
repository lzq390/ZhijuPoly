"""Lightweight development entrypoint for the knowledge-summary POC."""

from asyncio import Lock
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.knowledge_poc_trace import KnowledgeTraceMiddleware
from app.middleware import BrowserCrossSiteProtectionMiddleware
from app.models import (
    KnowledgeSearchRequest, KnowledgeSearchResponse,
    PropertyFilterOptionsResponse, PropertyFilterHistogramResponse, PropertyFilterSearchResponse, PropertyFilterSearchRequest,
)
from app.postgres_database import postgres_connection
from app.routers.database_browser import (
    get_property_filter_options, get_property_filter_histogram, search_property_filter,
)
from app.routers.knowledge import search_knowledge
from app.services.knowledge_poc_summary import generate_knowledge_summary

# Temporary single-process POC snapshots; reload or eviction requires a new search.
MAX_RECENT_SEARCHES = 32
MAX_RECORDINGS = 32
MAX_RECORDING_EVENTS = 200


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Recording:
    started_at: str = field(default_factory=now)
    ended_at: str | None = None
    events: list[dict] = field(default_factory=list)
    searches: dict[str, KnowledgeSearchResponse] = field(default_factory=dict)
    filter_searches: dict[str, tuple[PropertyFilterSearchRequest, PropertyFilterSearchResponse]] = field(default_factory=dict)
    pending: int = 0
    summary: dict | None = None
    summary_lock: Lock = field(default_factory=Lock)

    def append(self, event: dict):
        self.events.append({"sequence": len(self.events) + 1, "time": now(), **event})


class RecordingStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recording_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class PocKnowledgeSearchRequest(KnowledgeSearchRequest):
    recording_id: str | None = Field(default=None, min_length=1, max_length=64)


class PocKnowledgeSearchResponse(KnowledgeSearchResponse):
    search_id: str


class ArticleObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search_id: str = Field(min_length=1, max_length=64)
    knowledge_id: int = Field(gt=0)
    source: Literal["result_card", "drawer_reopen", "reaction_tab"]
    recording_id: str | None = Field(default=None, min_length=1, max_length=64)


class PocFilterSearchResponse(PropertyFilterSearchResponse):
    search_id: str


class PocFilterSearchRequest(PropertyFilterSearchRequest):
    recording_id: str | None = Field(default=None, min_length=1, max_length=64)


class FilterObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recording_id: str | None = Field(default=None, min_length=1, max_length=64)
    search_id: str = Field(min_length=1, max_length=64)
    result_index: int = Field(ge=0)
    source: Literal["measurement_details", "smiles"]
    filter_index: int | None = Field(default=None, ge=0, le=7)
    smiles_field: Literal["smiles", "canonical_smiles"] | None = None

    @model_validator(mode="after")
    def validate_target(self):
        if self.source == "measurement_details":
            if self.filter_index is None or self.smiles_field is not None:
                raise ValueError("Measurement observations require only filter_index")
        elif self.smiles_field is None or self.filter_index is not None:
            raise ValueError("SMILES observations require only smiles_field")
        return self


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()
    app = FastAPI(title="Knowledge Summary POC", version="0.1.0")
    app.state.settings = app_settings
    app.state.postgres_connection_factory = postgres_connection
    # Reuse the SQL-only filter endpoints without enabling other database modules.
    for path, endpoint, method, response_model in (
        ("options", get_property_filter_options, "GET", PropertyFilterOptionsResponse),
        ("histogram", get_property_filter_histogram, "GET", PropertyFilterHistogramResponse),
    ):
        app.add_api_route(f"/api/v1/database-browser/property-filter/{path}", endpoint,
                          methods=[method], response_model=response_model)
    recent_searches: OrderedDict[str, tuple[KnowledgeSearchResponse, str | None]] = OrderedDict()
    recordings: OrderedDict[str, Recording] = OrderedDict()
    filter_searches: OrderedDict[str, tuple[PropertyFilterSearchRequest, PropertyFilterSearchResponse]] = OrderedDict()

    @app.post("/api/v1/database-browser/property-filter/search", response_model=PocFilterSearchResponse)
    async def filter_search(body: PocFilterSearchRequest, request: Request, response: Response):
        record = get_recording(body.recording_id) if body.recording_id else None
        filters = [condition.model_dump() for condition in body.filters]
        if record:
            record.pending += 1
        try:
            result = await run_in_threadpool(search_property_filter, body, request, response)
        except Exception as exc:
            if record:
                record.append({"event": "property_filter.search_failed", "query": body.q, "filters": filters,
                               "status_code": getattr(exc, "status_code", 500)})
            raise
        finally:
            if record:
                record.pending -= 1
        search_id = uuid4().hex
        filter_searches[search_id] = (body, result)
        if len(filter_searches) > MAX_RECENT_SEARCHES:
            filter_searches.popitem(last=False)
        if record:
            record.filter_searches[search_id] = (body, result)
            record.append({"event": "property_filter.search_completed", "search_id": search_id,
                           "query": result.query, "filters": filters, "page": result.page, "page_size": result.page_size,
                           "matched_records": result.matched_records})
        return PocFilterSearchResponse(**result.model_dump(), search_id=search_id)

    @app.post("/api/v1/database-browser/property-filter/observations")
    async def observe_filter(body: FilterObservation):
        record = get_recording(body.recording_id) if body.recording_id else None
        snapshot = record.filter_searches.get(body.search_id) if record else None
        if snapshot is None:
            snapshot = filter_searches.get(body.search_id)
            if snapshot and body.recording_id and getattr(snapshot[0], "recording_id", None) not in (None, body.recording_id):
                raise HTTPException(409, "筛选属于另一条记录，请重新筛选")
        if snapshot is None:
            raise HTTPException(410, "筛选快照已失效，请重新筛选")
        submitted, result = snapshot
        if body.result_index >= len(result.results):
            raise HTTPException(404, "材料不属于这次筛选结果")
        material = result.results[body.result_index]
        observation = {"search_id": body.search_id, "result_index": body.result_index, "source": body.source,
                       "query": result.query, "page": result.page, "page_size": result.page_size,
                       "filters": [condition.model_dump() for condition in submitted.filters],
                       "polymer_name": material.polymer_name}
        if body.source == "measurement_details":
            records = [row.model_dump() for row in material.records if row.filter_index == body.filter_index]
            if not records:
                raise HTTPException(404, "该材料没有对应条件的测量记录")
            observation.update(event="property_filter.measurements_viewed", filter_index=body.filter_index, records=records)
        else:
            value = getattr(material, body.smiles_field)
            if not value:
                raise HTTPException(404, "该材料未提供对应的 SMILES")
            observation.update(event="property_filter.smiles_viewed", smiles_field=body.smiles_field, smiles_value=value)
        if record:
            observation["recording_id"] = body.recording_id
            record.append(observation)
        return observation

    def get_recording(recording_id: str, active: bool = True) -> Recording:
        record = recordings.get(recording_id)
        if record is None:
            raise HTTPException(410, "记录已失效，请刷新页面后重新开始")
        if active and record.ended_at is not None:
            raise HTTPException(409, "记录已结束")
        if active and len(record.events) + record.pending >= MAX_RECORDING_EVENTS:
            raise HTTPException(409, "本次记录已达 200 个操作，请结束记录")
        return record

    @app.post("/api/v1/knowledge/recordings")
    async def start_recording(body: RecordingStart):
        if body.recording_id not in recordings:
            if len(recordings) >= MAX_RECORDINGS:
                # Do not silently discard an active record.
                expired = next((key for key, value in recordings.items() if value.ended_at and not value.summary_lock.locked()), None)
                if expired is None:
                    raise HTTPException(409, "记录容量已满，请结束已有记录或重启 POC 服务")
                del recordings[expired]
            recordings[body.recording_id] = Recording()
        record = get_recording(body.recording_id, active=False)
        return {"recording_id": body.recording_id, "status": "stopped" if record.ended_at else "recording"}

    @app.post("/api/v1/knowledge/recordings/{recording_id}/stop")
    async def stop_recording(recording_id: str):
        record = get_recording(recording_id, active=False)
        if record.pending:
            raise HTTPException(409, "仍有搜索未完成，请稍后重试结束记录")
        if record.ended_at is None:
            record.ended_at = now()
        return {"recording_id": recording_id, "status": "stopped", "started_at": record.started_at,
                "ended_at": record.ended_at, "events": record.events}

    @app.post("/api/v1/knowledge/recordings/{recording_id}/summary")
    async def summarize_recording(recording_id: str):
        record = get_recording(recording_id, active=False)
        if record.ended_at is None:
            raise HTTPException(409, "请先结束记录再生成总结")
        async with record.summary_lock:
            if record.summary is None:
                record.summary = await generate_knowledge_summary(record.events, app_settings)
            return {"recording_id": recording_id, **record.summary}

    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.allowed_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["ETag", "Server-Timing"],
    )
    app.add_middleware(BrowserCrossSiteProtectionMiddleware)
    app.add_middleware(KnowledgeTraceMiddleware)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/knowledge/search", response_model=PocKnowledgeSearchResponse)
    async def search(body: PocKnowledgeSearchRequest, request: Request):
        record = get_recording(body.recording_id) if body.recording_id else None
        if record:
            record.pending += 1
        try:
            result = await search_knowledge(body, request)
        except Exception as exc:
            if record:
                record.append({"event": "search.failed", "query": body.query,
                               "status_code": exc.status_code if isinstance(exc, HTTPException) else 500})
            raise
        finally:
            if record:
                record.pending -= 1
        search_id = uuid4().hex
        recent_searches[search_id] = (result, body.recording_id)
        if len(recent_searches) > MAX_RECENT_SEARCHES:
            recent_searches.popitem(last=False)
        if record:
            record.searches[search_id] = result
            record.append({"event": "search.completed", "search_id": search_id,
                           "query": result.query, "groups": [group.model_dump() for group in result.groups],
                           "page": result.page, "page_size": result.page_size, "total": result.total,
                           "results": [{"knowledge_id": row.knowledge_id, "title_en": row.title_en,
                                        "title_zh": row.title_zh} for row in result.results]})
        return PocKnowledgeSearchResponse(**result.model_dump(), search_id=search_id)

    @app.post("/api/v1/knowledge/observations")
    async def observe_article(body: ArticleObservation):
        record = get_recording(body.recording_id) if body.recording_id else None
        result = record.searches.get(body.search_id) if record else None
        if result is None:
            snapshot = recent_searches.get(body.search_id)
            if snapshot and body.recording_id and snapshot[1] not in (None, body.recording_id):
                raise HTTPException(409, "检索属于另一条记录，请重新搜索")
            result = snapshot[0] if snapshot else None
        if result is None:
            raise HTTPException(410, "Search snapshot expired; search again before observing articles")
        article = next((row for row in result.results if row.knowledge_id == body.knowledge_id), None)
        if article is None:
            raise HTTPException(404, "Article is not in this search result")
        observation = {"event": "article.opened", "search_id": body.search_id,
                "knowledge_id": article.knowledge_id, "source": body.source,
                "query": result.query, "title_en": article.title_en, "title_zh": article.title_zh,
                "abstract_characters": len(article.abstract)}
        if body.source == "reaction_tab":
            observation["event"] = "article.reaction_viewed"
            observation["reaction_info"] = article.model_dump(include={
                "polymer_iupac", "formulation", "catalyst", "solvent", "temperature",
                "reaction_time", "analysis", "judgement_reason",
            })
        if record:
            observation["recording_id"] = body.recording_id
            record.append({**observation, "article": article.model_dump()})
        return observation

    return app
