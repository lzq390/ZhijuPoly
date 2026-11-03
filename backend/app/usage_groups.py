"""Display groups for usage reads; concrete task modules remain unchanged."""
from __future__ import annotations


class UsageFilterError(ValueError):
    """A module/group filter is invalid before any database read."""


MODULE_GROUPS = (
    ("structureWorkbench", "结构工作台", ("structure", "ocsr")),
    ("knowledge", "知识检索", ("knowledge", "online_knowledge")),
    ("polytaoGeneration", "聚合物生成", ("polytao",)),
    ("monomerRetrosynthesis", "单体逆合成反推", ("retrosynthesis",)),
    ("explorer", "聚合物相似性探索", ("query",)),
    ("databaseQuery", "数据库查询", ("database_query",)),
    ("databaseFilter", "数据库筛选", ("database_filter",)),
    ("database", "数据库分析", ("database_analysis", "dft_history")),
    ("homopolymerPrediction", "均聚物性质预测", ("predict",)),
    ("monomerPolymerization", "单体正向聚合", ("polymerization", "polymerization_batch", "batch_preflight")),
    ("mdSimulationDemo", "MD 模拟", ("polymer_md", "md_demo")),
    ("monomerMdSimulation", "单体 MD 模拟", ("monomer_md",)),
    ("monomerDft", "单体 DFT", ("monomer_dft",)),
    ("conditionalGeneration", "条件聚合物生成", ("conditional_generation",)),
    ("reverseDesign", "Tg 逆向设计", ("reverse_design", "tg_assistant")),
    ("highThroughputWorkflowDemo", "高通量优化演示", ()),
)


def resolve_group_modules(module: str | None, module_group: str | None) -> tuple[str, ...] | None:
    """Return group members, leaving legacy exact-module filtering to callers."""
    if module is not None and module_group is not None:
        raise UsageFilterError("Choose either module or module_group")
    if module_group is None:
        return None
    for group_id, _label, members in MODULE_GROUPS:
        if group_id == module_group:
            return members
    raise UsageFilterError("Invalid module_group")


def module_groups(*, include_external: bool = True) -> list[dict]:
    from app.usage_catalog import catalog_modules

    available = {item["id"] for item in catalog_modules()
                 if include_external or item["coverage"] != "external"}
    # Empty sidebar modules stay selectable and return empty reads. Historical
    # modules without a reliable sidebar attribution remain in unfiltered reads.
    return [{"id": group_id, "label": label,
             "modules": [module for module in members if module in available]}
            for group_id, label, members in MODULE_GROUPS]
