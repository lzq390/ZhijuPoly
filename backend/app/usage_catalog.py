"""Explicit accounting catalog for public work; observation is never a new task.

A route has one root owner: native job submission is metered by its manager,
not by a second HTTP request task. Methods and templates are deliberately
explicit so the route coverage test catches newly added unclassified APIs.
"""
from __future__ import annotations
from dataclasses import dataclass
import re

@dataclass(frozen=True, slots=True)
class UsageRoute:
    method: str
    path: str
    module: str | None
    mode: str  # sync, native, or observe


def _entries(module, mode, method, paths):
    return tuple(UsageRoute(method, path, module, mode) for path in paths)

ROUTES = (
    *_entries("assistant", "sync", "POST", (
        "/api/v1/assistant/chat/stream", "/api/v1/assistant/chat/image-stream")),
    *_entries("tg_assistant", "sync", "POST", (
        "/api/v1/assistant/tg/chat/stream", "/api/v1/assistant/tg/chat/image-stream")),
    *_entries("recording_summary", "sync", "POST", (
        "/api/v1/knowledge/recordings/{recording_id}/summary",)),
    *_entries("knowledge", "sync", "POST", ("/api/v1/knowledge/search",)),
    *_entries("predict", "sync", "POST", ("/api/v1/predict",)),
    *_entries("query", "sync", "POST", ("/api/v1/query/smiles",)),
    *_entries("query", "sync", "GET", ("/api/v1/polymer/{polymer_id}",)),
    *_entries("structure", "sync", "POST", (
        "/api/v1/structure/3d", "/api/v1/structure/2d", "/api/v1/structure/standardize-smiles")),
    *_entries("ocsr", "sync", "POST", ("/api/v1/structure/recognize-image",)),
    *_entries("retrosynthesis", "sync", "POST", ("/api/v1/monomer-retrosynthesis",)),
    *_entries("polymerization", "sync", "POST", ("/api/v1/monomer-polymerization",)),
    *_entries("batch_preflight", "sync", "POST", (
        "/api/v1/monomer-polymerization/batch/imports",
        "/api/v1/monomer-polymerization/batch/imports/{import_id}/preview")),
    *_entries("polytao", "sync", "POST", ("/api/v1/conditional-generation/polytao/descriptors",)),
    *_entries("reverse_design", "sync", "POST", ("/api/v1/reverse-design/tg",)),
    *_entries("online_knowledge", "sync", "POST", ("/api/v1/online-knowledge/search",)),
    *_entries("md_demo", "sync", "POST", ("/api/v1/md-demo/run", "/api/v1/md-demo/atom-distance")),
    *_entries("database_browser", "sync", "GET", (
        "/api/v1/database-browser/datasets/summary", "/api/v1/database-browser/datasets/analytics",
        "/api/v1/database-browser/structure-property", "/api/v1/database-browser/property-filter/options",
        "/api/v1/database-browser/property-filter/histogram", "/api/v1/database-browser/dft/molecules",
        "/api/v1/database-browser/dft/steps", "/api/v1/database-browser/formulation",
        "/api/v1/database-browser/experimental-process", "/api/v1/database-browser/experimental-property")),
    *_entries("database_browser", "sync", "POST", (
        "/api/v1/database-browser/smiles-lookup", "/api/v1/database-browser/property-filter/search")),
    *_entries("dft_history", "sync", "GET", (
        "/api/v1/dft/pca-sample", "/api/v1/dft/molecule/{mol_id}")),
    *_entries("conditional_generation", "native", "POST", ("/api/v1/conditional-generation/tg/jobs",)),
    *_entries("polytao", "native", "POST", ("/api/v1/conditional-generation/polytao/jobs",)),
    *_entries("reverse_design", "native", "POST", ("/api/v1/reverse-design/tg/jobs",)),
    *_entries("online_knowledge", "native", "POST", ("/api/v1/online-knowledge/jobs",)),
    *_entries("monomer_md", "native", "POST", ("/api/v1/monomer-md/jobs",)),
    *_entries("monomer_dft", "native", "POST", ("/api/v1/monomer-dft/jobs",)),
    *_entries("polymerization_batch", "native", "POST", ("/api/v1/monomer-polymerization/batch/jobs",)),
    *_entries(None, "observe", "GET", (
        "/health", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
        "/api/v1/assistant/tg/status", "/api/v1/assistant/tg/guide",
        "/api/v1/conditional-generation/tg/status", "/api/v1/conditional-generation/tg/jobs/{job_id}",
        "/api/v1/conditional-generation/polytao/status", "/api/v1/conditional-generation/polytao/jobs/{job_id}",
        "/api/v1/reverse-design/tg/jobs/{job_id}", "/api/v1/task-events",
        "/api/v1/online-knowledge/default-config", "/api/v1/online-knowledge/jobs/{job_id}",
        "/api/v1/online-knowledge/history", "/api/v1/md-demo/defaults",
        "/api/v1/monomer-polymerization/status",
        "/api/v1/monomer-polymerization/batch/jobs", "/api/v1/monomer-polymerization/batch/jobs/{job_id}",
        "/api/v1/monomer-polymerization/batch/jobs/{job_id}/results",
        "/api/v1/monomer-polymerization/batch/jobs/{job_id}/artifacts/{name}",
        "/api/v1/monomer-polymerization/batch/templates/{role}.{format}",
        "/api/v1/monomer-dft/status", "/api/v1/monomer-dft/capabilities",
        "/api/v1/monomer-dft/jobs", "/api/v1/monomer-dft/jobs/{job_id}",
        "/api/v1/monomer-dft/jobs/{job_id}/artifacts/{artifact_id}", "/api/v1/monomer-dft/jobs/{job_id}/bundle",
        "/api/v1/monomer-md/status", "/api/v1/monomer-md/protocols",
        "/api/v1/monomer-md/jobs", "/api/v1/monomer-md/jobs/{job_id}",
        "/api/v1/monomer-md/jobs/{job_id}/visualization/stages/{stage_id}/trajectory")),
    *_entries(None, "observe", "POST", (
        "/api/v1/knowledge/recordings", "/api/v1/knowledge/recordings/{recording_id}/stop",
        "/api/v1/knowledge/observations", "/api/v1/database-browser/property-filter/observations",
        "/api/v1/online-knowledge/history/clear", "/api/v1/online-knowledge/export-csv",
        "/api/v1/monomer-polymerization/batch/jobs/{job_id}/cancel",
        "/api/v1/monomer-dft/jobs/{job_id}/cancel", "/api/v1/monomer-md/jobs/{job_id}/cancel")),
    *_entries(None, "observe", "DELETE", (
        "/api/v1/online-knowledge/history/{history_id}",
        "/api/v1/monomer-dft/jobs/{job_id}/artifacts", "/api/v1/monomer-dft/jobs/{job_id}",
        "/api/v1/monomer-md/jobs/{job_id}/artifacts", "/api/v1/monomer-md/jobs/{job_id}")),
)
MODULES = tuple(sorted({route.module for route in ROUTES if route.module is not None}))


def _pattern(path):
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(path)) + "$")

_COMPILED = tuple((route, _pattern(route.path)) for route in ROUTES)
_NON_TASK_PREFIXES = ("/api/v1/auth/", "/api/v1/usage/", "/internal/",
                      "/api/v1/dev-gpu-session/", "/api/v1/lab-data/")


def match_usage_route(method: str, path: str) -> UsageRoute | None:
    method = method.upper()
    if method in {"OPTIONS", "HEAD"} or path.startswith(_NON_TASK_PREFIXES):
        return UsageRoute(method, path, None, "observe")
    for route, pattern in _COMPILED:
        if method == route.method and pattern.fullmatch(path):
            return route
    return None


def usage_catalog() -> list[dict[str, str]]:
    return [{"method": route.method, "path": route.path, "module": route.module, "mode": route.mode}
            for route in ROUTES]


def catalog_modules() -> list[dict[str, str]]:
    labels = {
        "assistant": ("通用助手", "ai"), "tg_assistant": ("Tg 助手", "ai"),
        "recording_summary": ("浏览记录摘要", "ai"), "knowledge": ("知识检索", "knowledge"),
        "online_knowledge": ("在线知识抽取", "knowledge"),
        "conditional_generation": ("Tg 条件生成", "models"), "polytao": ("PolyTAO 生成", "models"),
        "predict": ("性能预测", "models"), "ocsr": ("分子图像识别", "models"),
        "retrosynthesis": ("单体逆合成", "models"), "reverse_design": ("逆向设计", "search"),
        "query": ("结构相似性与查询", "search"), "structure": ("分子结构处理", "chemistry"),
        "database_browser": ("材料数据库浏览", "search"), "dft_history": ("DFT 历史查询", "search"),
        "monomer_md": ("分子动力学", "science"), "monomer_dft": ("DFT 计算", "science"),
        "polymerization": ("单体聚合", "chemistry"),
        "polymerization_batch": ("批量聚合", "chemistry"),
        "batch_preflight": ("批量导入校验", "chemistry"), "md_demo": ("分子动力学演示", "demo"),
    }
    notes = {
        "ai": "记录任务生命周期与实际供应商调用；未返回的 token 保持未知，网络等待不冒充计算时间。",
        "models": "记录任务生命周期与本地模型调用时长；未测量硬件利用率或硬件计费单位。",
        "science": "记录原生任务生命周期；无法证明的执行时长与产物字节保持未知。",
        "knowledge": "记录检索或抽取生命周期；外部服务未返回的用量保持未知。",
        "search": "记录查询生命周期与请求墙钟时长；不据此估算数据库或模型资源用量。",
        "chemistry": "记录本仓库处理任务生命周期；缺少独立资源观测的指标保持未知。",
        "demo": "记录演示请求生命周期；演示结果不代表真实科学计算消耗。",
    }
    result = [{"id": module, "label": labels[module][0], "category": labels[module][1],
               "coverage": "partial", "note": notes[labels[module][1]]} for module in MODULES]
    result.append({"id": "openscience", "label": "OpenScience 独立服务", "category": "external",
                   "coverage": "external", "note": "9011 iframe 独立服务尚未接入可验证的 owner 与 usage，不能归属或计量其内部执行。"})
    return result
