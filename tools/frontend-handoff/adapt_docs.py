#!/usr/bin/env python3
"""Adapt selected historical documentation inside a staged frontend handoff.

The source repository is never an input/output target of the packager call.
Run: python -B tools/frontend-handoff/adapt_docs.py /path/to/staged-package
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


HISTORICAL_DOCS = (
    "visual-motion-round-1.md",
    "ketcher-componentization.md",
    "ketcher-sdk-compatibility.md",
    "browsing-recording-ui.md",
)
NOTICE_MARKER = "<!-- frontend-handoff:historical-reference -->"
NOTICE = (
    NOTICE_MARKER + "\n"
    "> **交付包中的历史参考文档。** 本文保留原项目的设计、接口和历次验收说明；"
    "其中日期、测试数量、性能样本、部署与回退记录属于当时环境，不代表本包的验证结果。\n"
    "> 本包使用本地 Mock，不要求后端、数据库或 GPU 服务。安装与运行以 "
    "[交付说明](../README.md) 为准，页面修改和复验以 "
    "[页面统一验收说明](page-unification.md) 为准；本次实际结果见 "
    "[VALIDATION.json](../VALIDATION.json)。标注未交付的历史资料仅保留名称供追溯。\n"
)
PACKAGE_VERIFICATION = (
    "本节原有命令依赖原项目的服务器、后端测试环境或历史脚本，仅属于历史验证过程，"
    "不作为本包的操作步骤。请从 [交付说明](../README.md) 安装依赖并启动本地 Mock，"
    "再按 [页面统一验收说明](page-unification.md) 运行本包提供的检查；"
    "本次验证范围及结果见 [VALIDATION.json](../VALIDATION.json)。\n"
)
LINK_RE = re.compile(r"!?\[([^\]\n]+)\]\(([^\s)]+)(?:\s+[\"'][^\n]*?[\"'])?\)")


def _replace_section(text: str, heading: str, body: str) -> str:
    """Replace one explicit operational section without touching sibling sections."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines(keepends=True)
    headings: list[tuple[int, str]] = []
    fence = ""
    for index, line in enumerate(lines):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            continue
        if not fence and re.match(r"^#{1,6} ", line):
            headings.append((index, line.strip()))
    matches = [index for index, title in headings if title == heading]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one historical section: {heading!r}; found {len(matches)}")
    start = matches[0]
    end = next((index for index, title in headings
                if index > start and len(title) - len(title.lstrip("#")) <= level), len(lines))
    return "".join(lines[:start]) + heading + "\n\n" + body.strip() + "\n\n" + "".join(lines[end:])


def _staged_path(stage: Path, relative: str) -> Path:
    path = Path(os.path.abspath(stage / relative))
    if not path.is_relative_to(stage):
        raise ValueError("Document path escaped the staged handoff")
    for component in (path, *path.parents):
        if component == stage:
            break
        if component.is_symlink():
            raise ValueError("Symlink is not a staged handoff document")
    return path


def _missing_links_as_history(text: str, document: Path, stage: Path) -> str:
    # VALIDATION.json is written later by the generator. These package entry points
    # stay live even on an idempotence check before that metadata file is created.
    generated_targets = {stage / "VALIDATION.json"}

    def rewrite(match: re.Match[str]) -> str:
        label, destination = match.groups()
        parsed = urlsplit(destination.strip("<>"))
        if parsed.scheme or parsed.netloc or not parsed.path:
            return match.group(0)
        absolute = Path(os.path.abspath(document.parent / unquote(parsed.path)))
        if absolute.is_relative_to(stage):
            try:
                target = _staged_path(stage, str(absolute.relative_to(stage)))
            except ValueError:
                target = None
            if target is not None and (target.is_file() or target in generated_targets):
                return match.group(0)
        return f"{label}（历史资料，未随包交付）"

    # Preserve backtick and tilde fences, including longer closing markers.
    output, fence = [], ""
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            output.append(line)
        else:
            output.append(line if fence else LINK_RE.sub(rewrite, line))
    return "".join(output)



def _adapt_document(name: str, text: str) -> str:
    if name == "visual-motion-round-1.md":
        text = _replace_section(text, "### 本地重现", PACKAGE_VERIFICATION)
    elif name == "monomer-retrosynthesis-module.md":
        text = _replace_section(
            text,
            "## 回归验证",
            PACKAGE_VERIFICATION + "\n"
            "原项目曾另行进行后端与数据库隔离回归；这些历史结果不要求接收方重建相应环境。"
            "本包中的结构图、反推候选和认证响应是界面验证夹具，不作为模型准确性证据。",
        )
    elif name == "ketcher-componentization.md":
        # Retain implementation guidance from the old mixed build/design section.
        paragraphs = text.split("\n\n")
        retained = [paragraph for paragraph in paragraphs if paragraph.startswith((
            "异步 SDK 条件使用显式轮询",
            "截至 `4793ce7`，",
        ))]
        text = _replace_section(
            text,
            "## 构建与复验",
            PACKAGE_VERIFICATION + "\n"
            "历史发布流程还包括双引擎、Docker 镜像与生产回退验证；这些流程不属于本包的验收范围。"
            "原设计要求构建输出 `structure-editor.json` 记录引擎、SDK、补丁、锁文件摘要及资源列表；"
            "React 运行时不请求旧静态应用，iframe 构建隔离原生 SDK 和其 CSS。"
            "原项目关于运行一个发布周期后再清理 iframe 的安排是历史发布策略，"
            "本包不据此执行部署或清理。\n\n" + "\n\n".join(retained),
        )
    elif name == "ketcher-sdk-compatibility.md":
        text = _replace_section(
            text,
            "## 复验和回退",
            PACKAGE_VERIFICATION + "\n"
            "本包默认使用原生 React 编辑器；本文的双引擎回退门禁属于原项目历史验收。"
            "维护与内容传递边界见 [组件化说明](ketcher-componentization.md) 与 "
            "[补丁维护说明](../frontend/patches/README.md)。",
        )
    elif name == "browsing-recording-ui.md":
        text = re.sub(r"(?m)^- `npm run test:(?:recording-browser|pages)`[^\n]*\n", "", text)
        text = re.sub(
            r"(?m)^真实环境验收还需检查[^\n]*$",
            "历史真实服务验收包含后端接口、检索与筛选记录及 AI 总结的集成检查；"
            "这些记录描述原项目联调边界，不是本包的后端配置或重启要求。"
            "本包只演示本地 Mock 交互，不能据此证明真实后端或模型已通过验收。\n\n"
            + PACKAGE_VERIFICATION.rstrip(),
            text,
        )
    return text


def adapt_historical_docs(stage: Path | str) -> list[str]:
    """Adapt four staged docs in place; return relative paths whose bytes changed.

    Missing expected files/sections fail before writing anything, so an updated
    source document cannot silently bypass its handoff adaptation.
    """
    stage = Path(stage)
    if stage.is_symlink():
        raise ValueError("Stage root must not be a symlink")
    stage = stage.resolve()
    if (stage / ".git").exists():
        raise ValueError("Refusing to edit a Git source worktree")
    if not _staged_path(stage, "README.md").is_file() or not _staged_path(stage, "docs/page-unification.md").is_file():
        raise ValueError("Expected a staged handoff with README.md and docs/page-unification.md")
    prepared: list[tuple[Path, str]] = []
    for name in HISTORICAL_DOCS:
        document = _staged_path(stage, "docs/" + name)
        original = document.read_text(encoding="utf-8")
        updated = _adapt_document(name, original)
        updated = _missing_links_as_history(updated, document, stage)
        if NOTICE_MARKER not in updated:
            title, separator, rest = updated.partition("\n")
            if not separator or not title.startswith("# "):
                raise ValueError(f"Expected a document title: {document}")
            updated = title + "\n\n" + NOTICE + "\n" + rest.lstrip("\n")
        if updated != original:
            prepared.append((document, updated))
    for document, updated in prepared:
        document.write_text(updated, encoding="utf-8")
    return [str(document.relative_to(stage)) for document, _ in prepared]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", type=Path)
    args = parser.parse_args()
    changed = adapt_historical_docs(args.stage)
    print(json.dumps({"historical_docs_adapted": changed}, ensure_ascii=False))


if __name__ == "__main__":
    main()
