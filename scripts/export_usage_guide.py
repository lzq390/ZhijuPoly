#!/usr/bin/env python3
"""Export the shared platform guide as a self-contained, offline HTML document.

Run from any directory:
  python3 scripts/export_usage_guide.py --output /absolute/path/智聚万物_使用说明.html

The source JSON is shared with the in-app page. Image files are read from the
frontend public directory, validated, and embedded without altering their pixels.
The offline export contains no platform links or external network dependencies.
"""
from __future__ import annotations

import argparse
import base64
from html import escape
import itertools
import json
import mimetypes
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "frontend/src/help/usage-guide.json"
PUBLIC = ROOT / "frontend/public"
EXPECTED_IDS = (
    "structureWorkbench", "home", "knowledge", "polytaoGeneration",
    "monomerRetrosynthesis", "explorer", "databaseQuery", "databaseFilter",
    "database", "homopolymerPrediction", "monomerPolymerization",
    "mdSimulationDemo", "monomerMdSimulation", "monomerDft",
    "conditionalGeneration", "reverseDesign",
)
EXCLUDED = re.compile(r"9000|生产环境|截图日期|账号|用量|演示|未实现|20\d{2}[-/]\d{1,2}[-/]\d{1,2}|https?://")

CSS = '\n:root{color-scheme:light;--ink:#17343a;--muted:#5a6f76;--accent:#08766a;--paper:#fff;--line:#dce6e4;--pale:#eef6f3;--shadow:0 10px 36px #203c4110}\n*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:30px}body{margin:0;background:#f3f6f5;color:var(--ink);font:15px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}a{color:var(--accent);text-underline-offset:4px}button{font:inherit}a:focus-visible,button:focus-visible{outline:3px solid #30b9a5;outline-offset:4px}.skip{position:absolute;left:18px;top:-80px;z-index:5;background:white;padding:10px 16px}.skip:focus{top:10px}.hero{background:#123f44;color:#fff;border-bottom:5px solid #40bca4}.hero-inner{max-width:1440px;margin:auto;padding:42px 48px 34px}.brand{margin:0 0 12px;color:#9bdace;font-size:12px;font-weight:750;letter-spacing:.14em}.hero h1{font-size:clamp(28px,3.4vw,42px);line-height:1.3;margin:0 0 12px;letter-spacing:.01em}.intro{max-width:880px;margin:0;color:#d5e9e6;font-size:15px}.hero-actions{display:flex;flex-wrap:wrap;align-items:center;gap:14px 24px;margin-top:24px}.prod-tag{display:inline-flex;align-items:center;gap:9px;padding:6px 13px;border:1px solid #70b8a980;border-radius:6px;color:#e2fff6;background:#1d5455;font-size:13px;font-weight:600}.prod-tag:before{content:"";height:7px;width:7px;border-radius:100%;background:#77e2b4}.source{color:#e2fff6;font-size:13px}.print{padding:7px 14px;border:1px solid #70b8a9;border-radius:6px;background:transparent;color:#fff;cursor:pointer;font-size:13px}.capture-meta{margin:18px 0 0;color:#b7d7d1;font-size:12px}.layout{display:grid;grid-template-columns:242px minmax(0,1fr);gap:34px;max-width:1440px;margin:0 auto;padding:30px 48px 50px}.toc{position:sticky;top:24px;align-self:start;max-height:calc(100vh - 48px);overflow:auto;padding:19px 18px 20px;background:#fff;border:1px solid var(--line);border-radius:12px;font-size:12px}.toc-label{margin:0 0 12px;font-size:11px;font-weight:750;color:#718680;letter-spacing:.12em}.toc-group{margin-top:13px}.toc-group:first-of-type{margin-top:0}.toc-group-title{display:block;margin:0 0 5px;color:#4e6767;font-size:12px;font-weight:700}.toc ol{padding:0;margin:0;list-style:none}.toc li+li{margin-top:2px}.toc a{display:flex;gap:8px;padding:5px 8px;border-radius:6px;text-decoration:none;color:#415d61;line-height:1.5}.toc a:hover{background:var(--pale);color:var(--accent)}.toc-number{font-size:10px;color:#8b9e9a;min-width:18px;padding-top:2px;font-variant-numeric:tabular-nums}.content{min-width:0}.reading-note{margin:0 0 26px;padding:12px 16px;border-left:3px solid var(--accent);background:#e8f2ee;color:#49645f;border-radius:0 6px 6px 0;font-size:13px}.group{margin:0 0 34px}.group-heading{display:flex;align-items:center;gap:12px;margin:0 0 17px;font-size:19px;font-weight:750;line-height:1.5}.group-heading:after{content:"";height:1px;background:#d2e0da;flex:1}.module{background:var(--paper);border:1px solid var(--line);border-radius:14px;margin:0 0 24px;box-shadow:var(--shadow);overflow:hidden;scroll-margin-top:24px}.module-header{display:flex;align-items:center;gap:12px;padding:22px 26px 0}.module-number{display:flex;align-items:center;justify-content:center;flex-shrink:0;width:36px;height:36px;border-radius:9px;background:#e4f2ed;color:var(--accent);font-size:13px;font-weight:750;font-variant-numeric:tabular-nums}.module h3{margin:0;font-size:22px;line-height:1.4}.module-body{padding:17px 26px 23px}.purpose{margin:0 0 19px;padding-bottom:18px;border-bottom:1px solid #e9eeee;color:#4b6266;font-size:14px}.eyebrow{display:block;color:var(--accent);font-size:11px;font-weight:750;letter-spacing:.07em;margin-bottom:5px}.steps{list-style:none;counter-reset:instruction;margin:0 0 24px;padding:0;display:grid;gap:9px}.steps li{counter-increment:instruction;position:relative;padding-left:32px;min-height:24px;font-size:14px;line-height:1.8}.steps li:before{content:counter(instruction);position:absolute;left:0;top:2px;background:#edf5f2;border:1px solid #dbe9e2;border-radius:100%;width:23px;height:23px;text-align:center;line-height:21px;font-size:11px;font-weight:700;color:var(--accent)}.screenshots{display:grid;gap:20px}.screenshot{margin:0}.screenshot-button{display:block;width:100%;padding:0;cursor:zoom-in;background:#f0f4f3;border:1px solid #d4dedc;border-radius:8px;overflow:hidden;color:inherit;line-height:0}.screenshot-button img{display:block;width:100%;height:auto;max-height:1100px;object-fit:contain}.screenshot figcaption{display:flex;gap:9px;margin-top:8px;color:#5c7376;font-size:12px;line-height:1.6}.figure-label{flex-shrink:0;font-weight:700;color:#2f6e64}.module-link{display:inline-block;margin:18px 0 0;font-size:12px;text-decoration:none;color:var(--accent)}.module-link:hover{text-decoration:underline}.footer{border-top:1px solid var(--line);padding-top:18px;display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;color:#748780;font-size:11px}.footer a{color:inherit}dialog{padding:0;border:1px solid #a1b0ac;border-radius:12px;width:min(96vw,1800px);max-width:96vw;max-height:96vh;overflow:auto;box-shadow:0 20px 100px #0007}dialog::backdrop{background:#0e2429d9;backdrop-filter:blur(3px)}.lightbox-head{display:flex;justify-content:space-between;gap:16px;align-items:center;position:sticky;top:0;z-index:1;background:#fff;padding:12px 18px;border-bottom:1px solid var(--line)}.lightbox-head p{margin:0;font-size:14px}.close{border:1px solid var(--line);border-radius:6px;background:#f3f7f6;padding:5px 14px;cursor:pointer;color:var(--ink);font-size:13px;white-space:nowrap}.lightbox-image{display:block;width:100%;height:auto;background:#f5f7f6}.lightbox-caption{padding:12px 18px;margin:0;color:#5a7170;font-size:13px}\n@media(min-width:1500px){.layout{gap:42px}.module-body{padding:18px 30px 26px}.module-header{padding:26px 30px 0}}\n@media(max-width:1080px){.hero-inner{padding:32px 26px}.layout{grid-template-columns:196px minmax(0,1fr);gap:22px;padding:24px 26px 40px}.toc{padding:16px 12px}.module h3{font-size:20px}.module-header{padding:19px 20px 0}.module-body{padding:15px 20px 20px}}\n@media(max-width:760px){html{scroll-padding-top:16px}.hero-inner{padding:29px 20px 25px}.brand{font-size:11px}.hero h1{font-size:29px}.intro{font-size:14px}.hero-actions{margin-top:20px;gap:13px}.capture-meta{line-height:1.7}.layout{display:block;padding:20px 14px 32px}.toc{position:static;max-height:none;margin-bottom:22px;padding:15px 16px}.toc-label{margin-bottom:8px}.toc-groups{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.toc-group{margin:0}.toc a{padding:4px 0;font-size:11px}.toc-number{min-width:14px}.reading-note{font-size:12px;padding:10px 13px;margin-bottom:23px}.group{margin-bottom:26px}.group-heading{font-size:17px;margin-bottom:13px}.module{border-radius:11px;margin-bottom:19px}.module-header{padding:18px 16px 0;gap:9px}.module-number{width:31px;height:31px;border-radius:7px;font-size:11px}.module h3{font-size:20px}.module-body{padding:14px 16px 18px}.purpose{font-size:13px;margin-bottom:16px;padding-bottom:14px}.steps li{font-size:13px;padding-left:29px}.steps{gap:9px;margin-bottom:20px}.screenshots{gap:17px}.screenshot figcaption{font-size:11px}.screenshot-button{border-radius:5px}.footer{padding:16px 3px;font-size:10px}.lightbox-head{padding:10px 12px}.lightbox-head p{font-size:12px}.lightbox-caption{padding:10px 12px;font-size:12px}}\n@page{size:A4;margin:12mm}\n@media print{html{scroll-behavior:auto}body{font-size:9pt;background:#fff;color:#17343a;-webkit-print-color-adjust:exact;print-color-adjust:exact}.hero{background:#fff;color:#17343a;border-bottom:2pt solid #08766a}.hero-inner{padding:0 0 13pt;max-width:none}.brand{font-size:8pt;color:#08766a;margin:0 0 5pt}.hero h1{font-size:23pt;margin-bottom:6pt}.intro{font-size:9pt;color:#4a6668;max-width:none}.hero-actions{margin-top:10pt;gap:13pt}.prod-tag{font-size:8pt;background:#e9f3ef;color:#185548;border-color:#b6d3c8;padding:3pt 7pt}.source{font-size:8pt;color:#185548}.capture-meta{color:#5c7774;font-size:8pt;margin-top:7pt}.print,.toc,.skip,.module-link,dialog{display:none!important}.layout{display:block;max-width:none;padding:13pt 0 0}.reading-note{font-size:8pt;padding:7pt 10pt;margin-bottom:14pt}.group{margin-bottom:14pt}.group-heading{font-size:13pt;margin-bottom:10pt;break-after:avoid}.module{box-shadow:none;border:0;border-radius:0;overflow:visible;margin-bottom:20pt;break-inside:auto}.module-header{padding:0;gap:8pt;break-after:avoid}.module-number{width:24pt;height:24pt;border-radius:6pt;font-size:9pt}.module h3{font-size:15pt}.module-body{padding:8pt 0 0}.purpose{font-size:9pt;margin-bottom:10pt;padding-bottom:9pt;break-after:avoid}.eyebrow{font-size:7pt;margin-bottom:3pt}.steps{gap:5pt;margin:0 0 12pt;break-inside:avoid}.steps li{font-size:9pt;line-height:1.7;padding-left:23pt}.steps li:before{top:1pt;width:17pt;height:17pt;line-height:15pt;font-size:8pt}.screenshots{display:block}.screenshot{margin:0 0 13pt;break-inside:avoid}.screenshot-button{border-color:#cad6d2;border-radius:4pt;background:white}.screenshot-button img{width:100%;max-height:155mm;object-fit:contain}.screenshot figcaption{font-size:8pt;line-height:1.5;margin-top:5pt}.footer{font-size:7pt;padding-top:10pt}}\n\n.screenshot-details{margin:20px 0 0;border:1px solid #d4e2dc;border-radius:8px;background:#f8fbf9;overflow:hidden}\n.screenshot-details>summary{display:flex;align-items:center;gap:10px;padding:12px 15px;list-style:none;cursor:pointer;color:#225d52;font-size:13px;font-weight:650;user-select:none}\n.screenshot-details>summary::-webkit-details-marker{display:none}\n.screenshot-details>summary:hover{background:#edf6f1}\n.screenshot-details>summary:focus-visible{outline:3px solid #30b9a5;outline-offset:-3px;border-radius:7px}\n.disclosure-arrow{display:inline-block;font-size:22px;line-height:1;font-weight:500;transition:transform .15s ease}\n.screenshot-details[open]>summary .disclosure-arrow{transform:rotate(90deg)}\n.screenshot-count{padding:1px 7px;border-radius:4px;background:#e6f1ec;color:#53756b;font-size:11px;font-weight:500}\n.disclosure-state{margin-left:auto;color:#6b8279;font-size:11px;font-weight:400}\n.when-open{display:none}.screenshot-details[open] .when-closed{display:none}.screenshot-details[open] .when-open{display:inline}\n.screenshot-details[open]>summary{border-bottom:1px solid #dce8e1}\n.screenshot-details .screenshots{padding:14px;background:#fff}\n@media(max-width:760px){.screenshot-details{margin-top:16px}.screenshot-details>summary{padding:11px 12px;gap:7px;font-size:12px}.screenshot-details .screenshots{padding:9px}.disclosure-state{font-size:10px}}\n@media print{.screenshot-details{margin-top:9pt;border-color:#d0ddd7;border-radius:4pt;overflow:visible;background:#fff}.screenshot-details>summary{padding:6pt 8pt;font-size:8pt;break-after:avoid}.screenshot-details .screenshots{padding:8pt}.disclosure-state{display:none}.screenshot-count{font-size:7pt}.disclosure-arrow{font-size:13pt}}\n\n.hero-inner{padding-top:30px;padding-bottom:26px}.hero h1{font-size:32px}.hero-actions{margin-top:16px}.screenshot-details>summary{min-height:46px}@media print{.hero-inner{padding:0 0 13pt}}\n'

JS = r"""
(function(){
  'use strict';
  const dialog=document.getElementById('image-dialog');
  const image=document.getElementById('lightbox-image');
  const title=document.getElementById('lightbox-title');
  const caption=document.getElementById('lightbox-caption');
  document.querySelectorAll('.screenshot-button').forEach(function(button){
    button.addEventListener('click',function(){
      const source=button.querySelector('img');
      image.src=source.src;
      image.alt=source.alt;
      title.textContent=button.dataset.module;
      caption.textContent=button.dataset.caption;
      dialog.showModal();
    });
  });
  document.getElementById('close-lightbox').addEventListener('click',function(){dialog.close();});
  dialog.addEventListener('click',function(event){
    if(event.target!==dialog){return;}
    const bounds=dialog.getBoundingClientRect();
    if(event.clientX<bounds.left||event.clientX>bounds.right||event.clientY<bounds.top||event.clientY>bounds.bottom){dialog.close();}
  });
  const openImages=function(){return Array.from(document.querySelectorAll('.screenshot-details[open] .screenshot img'));};
  const eagerImages=function(){openImages().forEach(function(item){item.loading='eager';});};
  window.addEventListener('beforeprint',eagerImages);
  document.getElementById('print-guide').addEventListener('click',async function(){
    eagerImages();
    await Promise.all(openImages().map(function(item){return item.decode?item.decode().catch(function(){}):Promise.resolve();}));
    window.print();
  });
})();
"""


def text(value):
    return escape(str(value), quote=True)


def load_guide(source: Path):
    guide = json.loads(source.read_text(encoding="utf-8"))
    sections = guide.get("sections", [])
    if tuple(section.get("id") for section in sections) != EXPECTED_IDS:
        raise ValueError("手册必须按顺序包含 16 个功能模块")
    if not guide.get("title"):
        raise ValueError("手册标题不能为空")
    visible = [guide["title"], guide.get("introduction", "")]
    image_count = 0
    for number, section in enumerate(sections, 1):
        if section.get("number") != f"{number:02d}":
            raise ValueError("模块序号不连续")
        for key in ("group", "title", "purpose"):
            if not isinstance(section.get(key), str) or not section[key].strip():
                raise ValueError("模块字段不能为空：" + key)
            visible.append(section[key])
        steps = section.get("steps", [])
        if not 3 <= len(steps) <= 4 or any(not isinstance(step, str) or not step.strip() for step in steps):
            raise ValueError("每个模块须包含 3–4 条操作步骤：" + section["title"])
        visible.extend(steps)
        screenshots = section.get("screenshots", [])
        if not screenshots:
            raise ValueError("模块缺少截图：" + section["title"])
        for screenshot in screenshots:
            src = screenshot.get("src", "")
            if not src.startswith("/help/images/") or ".." in Path(src).parts:
                raise ValueError("截图须位于 /help/images/：" + src)
            path = (PUBLIC / src.lstrip("/")).resolve()
            if not path.is_relative_to(PUBLIC.resolve()) or not path.is_file():
                raise ValueError("截图不存在：" + str(path))
            mime = mimetypes.guess_type(path.name)[0]
            if mime not in ("image/png", "image/webp", "image/jpeg"):
                raise ValueError("截图格式不支持：" + str(path))
            if not screenshot.get("alt") or not screenshot.get("caption"):
                raise ValueError("截图须包含文字说明")
            visible.extend((screenshot["alt"], screenshot["caption"]))
            screenshot["data_url"] = "data:" + mime + ";base64," + base64.b64encode(path.read_bytes()).decode("ascii")
            image_count += 1
    match = EXCLUDED.search(" ".join(visible))
    if match:
        raise ValueError("手册含非用户说明或排除内容：" + match.group())
    return guide, image_count


def render(guide):
    toc = []
    body = []
    for group, group_sections in itertools.groupby(guide["sections"], key=lambda section: section["group"]):
        group_sections = list(group_sections)
        group_id = "group-" + group_sections[0]["number"]
        toc.append('<div class="toc-group"><span class="toc-group-title">' + text(group) + '</span><ol>')
        body.append('<section class="group" aria-labelledby="' + group_id + '"><h2 class="group-heading" id="' + group_id + '">' + text(group) + '</h2>')
        for section in group_sections:
            anchor = "module-" + section["number"]
            toc.append('<li><a href="#' + anchor + '"><span class="toc-number">' + text(section["number"]) + '</span><span>' + text(section["title"]) + '</span></a></li>')
            body.append('<article class="module" id="' + anchor + '" aria-labelledby="' + anchor + '-title"><div class="module-header"><span class="module-number" aria-hidden="true">' + text(section["number"]) + '</span><h3 id="' + anchor + '-title">' + text(section["title"]) + '</h3></div><div class="module-body"><p class="purpose"><span class="eyebrow">做什么</span>' + text(section["purpose"]) + '</p><span class="eyebrow">怎么用</span><ol class="steps">')
            body.extend('<li>' + text(step) + '</li>' for step in section["steps"])
            body.append('</ol><details class="screenshot-details"><summary><span class="disclosure-arrow" aria-hidden="true">›</span><span>操作截图</span><span class="screenshot-count">' + str(len(section["screenshots"])) + ' 张</span><span class="disclosure-state"><span class="when-closed">点击展开</span><span class="when-open">点击收起</span></span></summary><div class="screenshots">')
            for index, screenshot in enumerate(section["screenshots"], 1):
                label = '图 ' + section["number"] + '-' + str(index)
                body.append('<figure class="screenshot"><button type="button" class="screenshot-button" aria-haspopup="dialog" aria-controls="image-dialog" aria-label="放大查看：' + text(screenshot["alt"]) + '" data-module="' + text(section["title"]) + '" data-caption="' + text(label + '　' + screenshot["caption"]) + '"><img src="' + screenshot["data_url"] + '" alt="' + text(screenshot["alt"]) + '" loading="lazy" decoding="async"></button><figcaption><span class="figure-label">' + text(label) + '</span><span>' + text(screenshot["caption"]) + '</span></figcaption></figure>')
            body.append('</div></details></div></article>')
        toc.append('</ol></div>')
        body.append('</section>')
    title = text(guide["title"])
    introduction = text(guide.get("introduction", ""))
    return '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="智聚万物功能用途与操作步骤，附操作截图。"><title>智聚万物 · ' + title + '</title><style>' + CSS + '</style></head><body><a class="skip" href="#content">跳到功能说明</a><header class="hero"><div class="hero-inner"><p class="brand">NEXPOLY · 智聚万物</p><h1>' + title + '</h1><p class="intro">' + introduction + '</p><div class="hero-actions"><button class="print" id="print-guide" type="button">打印 / 保存为 PDF</button></div></div></header><div class="layout"><nav class="toc" aria-label="功能目录"><p class="toc-label">功能目录</p><div class="toc-groups">' + ''.join(toc) + '</div></nav><main class="content" id="content">' + ''.join(body) + '</main></div><dialog id="image-dialog" aria-labelledby="lightbox-title" aria-describedby="lightbox-caption"><div class="lightbox-head"><p id="lightbox-title">操作截图</p><button type="button" class="close" id="close-lightbox" autofocus>关闭</button></div><img class="lightbox-image" id="lightbox-image" alt=""><p class="lightbox-caption" id="lightbox-caption"></p></dialog><script>' + JS + '</script></body></html>'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="与平台共用的手册 JSON")
    parser.add_argument("--output", type=Path, help="单文件 HTML 输出路径")
    parser.add_argument("--check", action="store_true", help="仅校验内容及图片，不写出文件")
    args = parser.parse_args()
    if not args.check and args.output is None:
        parser.error("导出须指定 --output")
    try:
        guide, image_count = load_guide(args.source)
        if args.check:
            print(json.dumps({"modules": len(guide["sections"]), "screenshots": image_count, "valid": True}, ensure_ascii=False))
            return
        document = render(guide)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".building")
        temporary.write_text(document, encoding="utf-8")
        temporary.replace(args.output)
        print(json.dumps({"output": str(args.output.resolve()), "modules": len(guide["sections"]), "screenshots": image_count, "bytes": args.output.stat().st_size}, ensure_ascii=False))
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, "导出失败：" + str(error) + "\n")


if __name__ == "__main__":
    main()
