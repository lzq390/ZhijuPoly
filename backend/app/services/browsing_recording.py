"""Single-process browsing records shared by application entrypoints."""

from asyncio import Lock
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from app.config import Settings
from app.models import KnowledgeSearchResponse, PropertyFilterSearchRequest, PropertyFilterSearchResponse
from app.recording_models import (
    ArticleObservation, FilterObservation, RecordingStart,
    RecordedKnowledgeSearchRequest, RecordedKnowledgeSearchResponse,
    RecordedFilterSearchRequest, RecordedFilterSearchResponse,
)
from app.services.knowledge_poc_summary import generate_knowledge_summary


# In-memory snapshots; process restart or eviction requires a new record/search.
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


class BrowsingRecordingStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.recent_searches: OrderedDict[str, tuple[KnowledgeSearchResponse, str | None]] = OrderedDict()
        self.recordings: OrderedDict[str, Recording] = OrderedDict()
        self.filter_searches: OrderedDict[str, tuple[PropertyFilterSearchRequest, PropertyFilterSearchResponse]] = OrderedDict()

    def get_recording(self, recording_id: str, active: bool = True) -> Recording:
        record = self.recordings.get(recording_id)
        if record is None:
            raise HTTPException(410, "记录已失效，请刷新页面后重新开始")
        if active and record.ended_at is not None:
            raise HTTPException(409, "记录已结束")
        if active and len(record.events) + record.pending >= MAX_RECORDING_EVENTS:
            raise HTTPException(409, "本次记录已达 200 个操作，请结束记录")
        return record

    async def start_recording(self, body: RecordingStart):
        if body.recording_id not in self.recordings:
            if len(self.recordings) >= MAX_RECORDINGS:
                # Do not silently discard an active record.
                expired = next((key for key, value in self.recordings.items() if value.ended_at and not value.summary_lock.locked()), None)
                if expired is None:
                    raise HTTPException(409, "记录容量已满，请结束已有记录或联系管理员")
                del self.recordings[expired]
            self.recordings[body.recording_id] = Recording()
        record = self.get_recording(body.recording_id, active=False)
        return {"recording_id": body.recording_id, "status": "stopped" if record.ended_at else "recording"}

    async def stop_recording(self, recording_id: str):
        record = self.get_recording(recording_id, active=False)
        if record.pending:
            raise HTTPException(409, "仍有搜索未完成，请稍后重试结束记录")
        if record.ended_at is None:
            record.ended_at = now()
        return {"recording_id": recording_id, "status": "stopped", "started_at": record.started_at,
                "ended_at": record.ended_at, "events": record.events}

    async def summarize_recording(self, recording_id: str):
        record = self.get_recording(recording_id, active=False)
        if record.ended_at is None:
            raise HTTPException(409, "请先结束记录再生成总结")
        async with record.summary_lock:
            if record.summary is None:
                record.summary = await generate_knowledge_summary(record.events, self.settings)
            return {"recording_id": recording_id, **record.summary}

    async def filter_search(self, body: RecordedFilterSearchRequest, execute: Callable[[], Awaitable[PropertyFilterSearchResponse]]):
        record = self.get_recording(body.recording_id) if body.recording_id else None
        filters = [condition.model_dump() for condition in body.filters]
        if record:
            record.pending += 1
        try:
            result = await execute()
        except Exception as exc:
            if record:
                record.append({"event": "property_filter.search_failed", "query": body.q, "filters": filters,
                               "status_code": getattr(exc, "status_code", 500)})
            raise
        finally:
            if record:
                record.pending -= 1
        search_id = uuid4().hex
        self.filter_searches[search_id] = (body, result)
        if len(self.filter_searches) > MAX_RECENT_SEARCHES:
            self.filter_searches.popitem(last=False)
        if record:
            record.filter_searches[search_id] = (body, result)
            record.append({"event": "property_filter.search_completed", "search_id": search_id,
                           "query": result.query, "filters": filters, "page": result.page, "page_size": result.page_size,
                           "matched_records": result.matched_records})
        return RecordedFilterSearchResponse(**result.model_dump(), search_id=search_id)

    async def observe_filter(self, body: FilterObservation):
        record = self.get_recording(body.recording_id) if body.recording_id else None
        snapshot = record.filter_searches.get(body.search_id) if record else None
        if snapshot is None:
            snapshot = self.filter_searches.get(body.search_id)
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

    async def search(self, body: RecordedKnowledgeSearchRequest, execute: Callable[[], Awaitable[KnowledgeSearchResponse]]):
        record = self.get_recording(body.recording_id) if body.recording_id else None
        if record:
            record.pending += 1
        try:
            result = await execute()
        except Exception as exc:
            if record:
                record.append({"event": "search.failed", "query": body.query,
                               "status_code": exc.status_code if isinstance(exc, HTTPException) else 500})
            raise
        finally:
            if record:
                record.pending -= 1
        search_id = uuid4().hex
        self.recent_searches[search_id] = (result, body.recording_id)
        if len(self.recent_searches) > MAX_RECENT_SEARCHES:
            self.recent_searches.popitem(last=False)
        if record:
            record.searches[search_id] = result
            record.append({"event": "search.completed", "search_id": search_id,
                           "query": result.query, "groups": [group.model_dump() for group in result.groups],
                           "page": result.page, "page_size": result.page_size, "total": result.total,
                           "results": [{"knowledge_id": row.knowledge_id, "title_en": row.title_en,
                                        "title_zh": row.title_zh} for row in result.results]})
        return RecordedKnowledgeSearchResponse(**result.model_dump(), search_id=search_id)

    async def observe_article(self, body: ArticleObservation):
        record = self.get_recording(body.recording_id) if body.recording_id else None
        result = record.searches.get(body.search_id) if record else None
        if result is None:
            snapshot = self.recent_searches.get(body.search_id)
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
