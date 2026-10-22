#!/usr/bin/env python3
"""Create a verified, backend-free frontend handoff from a stable working-tree snapshot.

The production frontend is never changed. --prepare-only creates an unverified
candidate for diagnosis; only the default complete validation publishes a ZIP.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
FIXED_TIME = (2000, 1, 1, 0, 0, 0)
MANIFEST = 'FILE-MANIFEST.sha256'
TREE_DIRS = ('src', 'public', 'build', 'sdk', 'shared', 'patches')
ROOT_FILES = ('package.json', 'package-lock.json', 'index.html', 'ketcher-compat.html',
              'tsconfig.json', 'tsconfig.node.json', 'tailwind.config.ts',
              'postcss.config.js', 'vite.config.ts', 'vite.ketcher-compat.config.ts')
SCRIPTS = (
    'verify-ketcher-patches.mjs', 'verify-ketcher-runtime-http.mjs', 'verify-ketcher-worker.mjs',
    'verify-ketcher-compat.mjs', 'verify-ketcher-deferred.mjs',
    'verify-page-consistency.mjs', 'page-consistency-fixtures.mjs',
    'verify-workbench-drawer-focus.mjs', 'verify-high-throughput-states.mjs',

    'browser-poll.mjs',
    'fixtures/entitlements.json', 'fixtures/usage-catalog.json',
    'verify-structure-resources.mjs',
    'verify-motion.mjs', 'drawer-motion-probe.mjs', 'verify-module-entrance.mjs',

)
DOCS = ('page-unification.md', 'visual-motion-round-1.md', 'browsing-recording-ui.md',
        'ketcher-componentization.md',
        'ketcher-sdk-compatibility.md')
EXTERNAL_FIXTURES = {
    'backend/tests/fixtures/monomer_dft_scientific_result_v1_legacy.json':
        'frontend/src/test/fixtures/monomer_dft_scientific_result_v1_legacy.json',
    'contracts/monomer_dft_api_contract_v1.json':
        'frontend/src/test/fixtures/monomer_dft_api_contract_v1.json',
}
TEXT_SUFFIXES = {'.ts', '.tsx', '.js', '.mjs', '.cjs', '.css', '.json', '.html', '.md', '.txt', '.csv', '.patch'}
HANDOFF_ENV = ('VITE_HANDOFF_MOCK=true\nVITE_API_BASE_URL=/api/v1\nVITE_AGENT_WORKSPACE_URL=\n'
               'VITE_STRUCTURE_EDITOR_ENGINE=react\nVITE_DEV_GPU_SESSION_CONTROL=false\n'
               'VITE_ONLINE_KNOWLEDGE_BASE_URL=https://example.invalid/v1\n'
               'VITE_ONLINE_KNOWLEDGE_MODEL=synthetic-model\n')
AUDIT_CHUNK_BYTES = 65536
RUN_SCHEMA_VERSION = 1

class PackagingError(RuntimeError):
    pass

def environment_file(path: Path) -> bool:
    # Vite type declarations are source, rather than dotenv values.
    if path.name.lower() in {'env.d.ts', 'vite-env.d.ts'}:
        return False
    return (path.name.lower().startswith('.env')
            or bool(re.search(r'(^|[._-])env($|[._-])|(^|[._-])envrc$', path.name, re.I)))

def private_input(path: Path) -> bool:
    return (environment_file(path) or path.name.lower() in {'.npmrc', '.netrc', '.pypirc', 'id_rsa', 'id_ed25519'}
            or path.suffix.lower() in {'.pem', '.key', '.p12', '.pfx'}
            or bool(set(path.parts) & {'.git', '.ssh', '.aws', 'credentials', 'secrets'}))

def contained_path(path: Path, boundary: Path) -> Path:
    boundary = boundary.resolve()
    absolute = Path(os.path.abspath(path))
    if absolute == boundary or not absolute.is_relative_to(boundary):
        raise PackagingError('Path is outside its managed boundary')
    for component in (absolute, *absolute.parents):
        if component == boundary:
            break
        if component.is_symlink():
            raise PackagingError('Symlink is not allowed in a managed path')
    if not absolute.resolve().is_relative_to(boundary):
        raise PackagingError('Resolved path escaped its managed boundary')
    return absolute

def owned_run(root: Path, run: Path) -> dict:
    boundary = contained_path(root / '.runtime/frontend-handoff', root)
    run = contained_path(run, boundary)
    if run.parent != boundary or not re.fullmatch(r'\d{8}-\d{6}', run.name):
        raise PackagingError('Resume must name one direct managed run directory')
    marker = run / 'run.json'
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 8192:
        raise PackagingError('Resume has no supported ownership marker')
    try:
        value = json.loads(marker.read_text())
    except (ValueError, OSError) as exc:
        raise PackagingError('Resume ownership marker is invalid') from exc
    if (not isinstance(value, dict) or value.get('schema_version') != RUN_SCHEMA_VERSION
            or value.get('source_root') != str(root.resolve())
            or value.get('name') != 'nexpoly-frontend-handoff-' + run.name):
        raise PackagingError('Resume ownership does not match this source root; preserve it and start a new run')
    return value

def reset_run_directory(root: Path, run: Path, name: str) -> Path:
    owned_run(root, run)
    if name not in {'stage', 'validation'}:
        raise PackagingError('Directory is not a disposable run output')
    target = contained_path(run / name, run)
    if target.exists():
        if not target.is_dir():
            raise PackagingError('Run output is not a directory')
        shutil.rmtree(target)
    return target

def check_kit(kit: Path) -> None:
    if kit.is_symlink() or not kit.is_dir():
        raise PackagingError('Required frontend handoff adapter kit is missing')
    for name in ('overlay', 'assets/public', 'templates', 'adapt_docs.py'):
        path = contained_path(kit / name, kit)
        if not path.exists():
            raise PackagingError('Required frontend handoff adapter input is missing: ' + name)
    for path in regular_files(kit):
        if private_input(path.relative_to(kit)):
            raise PackagingError('Adapter kit contains an excluded configuration file')

def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def regular_files(root: Path):
    if root.is_symlink():
        raise PackagingError(f'Symlink is not allowed: {root}')
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            if path.is_symlink():
                raise PackagingError(f'Symlink is not allowed: {path}')
        for name in files:
            path = Path(current) / name
            if not path.is_file():
                raise PackagingError(f'Non-regular file: {path}')
            yield path

def selected_files(root: Path) -> dict[str, Path]:
    selected: dict[str, Path] = {}
    def add(path: Path):
        if private_input(path.relative_to(root)):
            return
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root.parent):
            raise PackagingError(f'Symlink is not allowed: {path}')
        if not path.is_file():
            raise PackagingError(f'Required file missing: {path}')
        selected[path.relative_to(root).as_posix()] = path
    for directory in TREE_DIRS:
        for path in regular_files(root / 'frontend' / directory):
            rel = path.relative_to(root / 'frontend')
            if any(part in {'node_modules', 'dist', '__pycache__', '.vite', '.ketcher-runtime'} for part in rel.parts):
                continue
            if path.name.endswith(('.tsbuildinfo', '.map', '.log', '.pyc')):
                continue
            # tsc emits these next to TS sources; do not ship stale generated copies.
            if directory in {'build', 'shared'} and path.suffix in {'.js', '.ts'}:
                source = path.with_suffix('.ts')
                if path.suffix == '.js' and source.exists():
                    continue
                if path.name.endswith('.d.ts') and path.with_name(path.name[:-5] + '.ts').exists():
                    continue
            if directory == 'public' and ('pdf-similarity' in rel.parts or 'demo-data' in rel.parts
                                          or 'samples' in rel.parts):
                continue
            add(path)
    for name in ROOT_FILES:
        add(root / 'frontend' / name)
    for name in SCRIPTS:
        add(root / 'frontend/scripts' / name)
    for path in regular_files(root / 'design-system/polyprop'):
        if path.suffix == '.md':
            add(path)
    for name in DOCS:
        add(root / 'docs' / name)
    for name in EXTERNAL_FIXTURES:
        add(root / name)
    return selected

def stable_snapshot(root: Path, destination: Path, attempts: int = 3) -> dict:
    if destination.exists() or destination.is_symlink():
        raise PackagingError('Snapshot destination must be new; existing contents are preserved')
    parent = destination.parent.resolve()
    contained_path(destination, parent)
    created_inode = None
    for attempt in range(attempts):
        first = selected_files(root)
        before = {key: digest(path) for key, path in first.items()}
        if created_inode is not None:
            if destination.is_symlink() or destination.lstat().st_ino != created_inode:
                raise PackagingError('Snapshot destination identity changed')
            shutil.rmtree(destination)
        destination.mkdir(parents=True, exist_ok=False)
        created_inode = destination.stat().st_ino
        for name, source in first.items():
            output = destination / name
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, output)
        second = selected_files(root)
        after = {key: digest(path) for key, path in second.items()}
        copied = {key: digest(destination / key) for key in first}
        if before == after == copied:
            return before
        print(f'Source changed during snapshot; retry {attempt + 1}/{attempts}', flush=True)
    raise PackagingError('Working-tree files changed during all snapshot attempts; no archive published.')

def replace_exact(path: Path, old: str, new: str) -> None:
    value = path.read_text()
    if value.count(old) != 1:
        raise PackagingError(f'Adaptation anchor must occur once: {path.name}: {old[:100]!r}')
    path.write_text(value.replace(old, new))

def remove_png_metadata(path: Path) -> None:
    original = path.read_bytes()
    if not original.startswith(b'\x89PNG\r\n\x1a\n'):
        raise PackagingError(f'Invalid PNG: {path}')
    out, offset = bytearray(original[:8]), 8
    while offset < len(original):
        length = struct.unpack('>I', original[offset:offset+4])[0]
        chunk = original[offset+4:offset+8]
        end = offset + 12 + length
        if end > len(original):
            raise PackagingError(f'Truncated PNG: {path}')
        if chunk not in {b'tEXt', b'zTXt', b'iTXt', b'eXIf', b'tIME'}:
            out.extend(original[offset:end])
        offset = end
    path.write_bytes(out)

def adapt_page_fixture_routes(page_check: Path) -> None:
    # Business-state fixtures delegate auth to the existing local GET/HEAD guard
    # and isolated HTTP Mock; they must not manufacture an authenticated session.
    replace_exact(page_check,
                  'const failure = route => route.fulfill({ status: 503, json: { detail: "Local UI state fixture" } });',
                  'const failure = route => new URL(route.request().url()).pathname.endsWith("/auth/session")'
                  ' ? route.fallback()'
                  ' : route.fulfill({ status: 503, json: { detail: "Local UI state fixture" } });')
    replace_exact(page_check,
                  'const url = new URL(route.request().url());\n    const payload = fixture.response(url);',
                  'const url = new URL(route.request().url());\n'
                  '    if (url.pathname.endsWith("/auth/session")) return route.fallback();\n'
                  '    const payload = fixture.response(url);')


def adapt_workbench_verifiers(frontend: Path) -> None:
    # The retained editor navigates through App to the standalone result module.
    # Keep the original focus, reversal, animation, and request-count assertions.
    for filename, indent in (('verify-workbench-drawer-focus.mjs', '          '),
                             ('verify-motion.mjs', '      ')):
        path = frontend / 'scripts' / filename
        replace_exact(path,
                      'await page.getByRole("button", { name: "设置单体逆合成反推参数" }).click();',
                      'await page.getByRole("button", { name: "打开单体逆合成反推", exact: true }).click();\n'
                      + indent + "await page.locator('[data-module-content=\"monomerRetrosynthesis\"][data-module-phase=\"idle\"]:not([inert])').waitFor();")
        # A frontend navigation GET must never count as the fixture business POST.
        replace_exact(path, 'url.pathname.endsWith("/monomer-retrosynthesis")',
                      'url.pathname === "/api/v1/monomer-retrosynthesis" && request.method() === "POST"')
    focus = frontend / 'scripts/verify-workbench-drawer-focus.mjs'
    # Standalone result modules unmount on navigation. Absence means focus is
    # outside the old drawer; initial geometry/drawer assertions remain strict.
    replace_exact(focus, 'const titleBox = title.getBoundingClientRect();',
                  'const titleBox = title?.getBoundingClientRect();')
    replace_exact(focus, 'focusInDrawer: drawer?.contains(active),',
                  'focusInDrawer: Boolean(drawer?.contains(active)),')
    replace_exact(focus, 'title: { x: titleBox.x - main.x, y: titleBox.y - main.y }',
                  'title: titleBox ? { x: titleBox.x - main.x, y: titleBox.y - main.y } : null')
    # Shell restores the original connected submitter on first close; subsequent
    # reopen controls are remounted and must match their new exact DOM identity.
    replace_exact(focus, 'async function expectClosed(page) {\n  await page.waitForFunction(() => document.querySelector(".np-sw-drawer")?.dataset.motionPhase === "closed");\n  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "展开反推结果");\n  await page.waitForTimeout(150);\n  const state = await snapshot(page);\n  assert.equal(state.focusInDrawer, false);\n  assert.equal(state.focusedInert, false);\n  return state;\n}', 'async function focusIdentity(page, expected) {\n  return page.evaluate(expected => {\n    const describe = element => element ? {\n      tag: element.tagName, id: element.id, type: element.getAttribute("type"),\n      ariaLabel: element.getAttribute("aria-label"), text: element.textContent.trim().replace(/\\s+/g, " ").slice(0, 120),\n      connected: element.isConnected, inert: Boolean(element.closest("[inert], [aria-hidden=\'true\']")),\n      module: element.closest("[data-module]")?.getAttribute("data-module") || null,\n    } : null;\n    return { sameElement: document.activeElement === expected,\n      actual: describe(document.activeElement), expected: describe(expected) };\n  }, expected);\n}\n\nasync function expectClosed(page, submittedTrigger = null) {\n  let expected = submittedTrigger;\n  try {\n    await page.waitForFunction(() => document.querySelector(".np-sw-drawer")?.dataset.motionPhase === "closed");\n    // The first close restores the still-connected form submitter. Later opens\n    // unmount the reopen control; restore the exact newly rendered reopen node.\n    expected ||= await page.getByRole("button", { name: "展开反推结果", exact: true }).elementHandle();\n    assert.ok(expected, "The exact focus-restoration trigger must exist");\n    await page.waitForFunction(expected => expected.isConnected && document.activeElement === expected, expected);\n    await page.waitForTimeout(150);\n    const identity = await focusIdentity(page, expected);\n    assert.equal(identity.sameElement, true, JSON.stringify(identity));\n    const state = await snapshot(page);\n    assert.equal(state.focusInDrawer, false);\n    assert.equal(state.focusedInert, false);\n    return { ...state, focusIdentity: identity };\n  } catch (error) {\n    error.focusIdentity = await focusIdentity(page, expected).catch(() => null);\n    throw error;\n  }\n}')
    replace_exact(focus, 'const result = { viewport, reducedMotion, total, passed: false };',
                  'const result = { viewport, reducedMotion, total, passed: false };\n        let submitTrigger = null;')
    replace_exact(focus, 'await page.getByRole("button", { name: "运行反推", exact: true }).click();',
                  'submitTrigger = await page.getByRole("button", { name: "运行反推", exact: true }).elementHandle();\n'
                  '          assert.ok(submitTrigger, "Capture the exact form submitter before opening results");\n'
                  '          await page.getByRole("button", { name: "运行反推", exact: true }).click();')
    replace_exact(focus, 'result.closed = await expectClosed(page);',
                  'result.closed = await expectClosed(page, submitTrigger);')
    replace_exact(focus, 'result.error = String(error);',
                  'result.error = String(error);\n'
                  '          result.focusFailure = error.focusIdentity || await focusIdentity(page, submitTrigger).catch(() => null);')
    motion = frontend / 'scripts/verify-motion.mjs'
    # The original editor stays mounted. Measure the active result workspace.
    replace_exact(motion, 'const workbenchSlide = { workspace: ".np-sw-workspace",',
                  'const workbenchSlide = { workspace: ".np-monomer-retrosynthesis .np-sw-workspace",')
    replace_exact(motion, 'checkDrawerReversal(page, { workspace: ".np-sw-workspace", drawer: ".np-sw-drawer",',
                  'checkDrawerReversal(page, { workspace: ".np-monomer-retrosynthesis .np-sw-workspace", drawer: ".np-sw-drawer",')


def adapt(stage: Path, kit: Path, run: Path) -> None:
    frontend = stage / 'frontend'
    # --resume can retain an earlier snapshot from before the handoff allowlist
    # was refined. Keep its UI baseline, but enforce the current script boundary.
    for path in regular_files(frontend / 'scripts'):
        if path.relative_to(frontend / 'scripts').as_posix() not in SCRIPTS:
            path.unlink()
    for folder in ('backend', 'contracts'):
        for original, new in EXTERNAL_FIXTURES.items():
            if original.startswith(folder + '/'):
                target = stage / new
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(stage / original, target)
        shutil.rmtree(stage / folder)
    replace_exact(frontend / 'src/lib/monomerDftPresentation.test.ts',
                  '../../../backend/tests/fixtures/monomer_dft_scientific_result_v1_legacy.json',
                  '../test/fixtures/monomer_dft_scientific_result_v1_legacy.json')
    replace_exact(frontend / 'src/services/monomerDftContract.test.ts',
                  '../../../contracts/monomer_dft_api_contract_v1.json',
                  '../test/fixtures/monomer_dft_api_contract_v1.json')
    for path in regular_files(kit / 'overlay'):
        target = frontend / path.relative_to(kit / 'overlay')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    for path in regular_files(kit / 'assets/public'):
        target = frontend / 'public' / path.relative_to(kit / 'assets/public')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    source_assets = kit / 'assets/source'
    if source_assets.exists():
        for path in [source_assets / 'highThroughputDoePiRows.ts']:
            target = frontend / 'src/constants' / path.relative_to(source_assets)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    for name in ('src/index.css', 'src/styles/polymer-desktop.css'):
        path = frontend / name
        value = path.read_text()
        value = re.sub(r'@import url\("https://fonts.googleapis.com/[^\n]+\);',
                       '@import url("/fonts/fonts.css");' if name == 'src/index.css' else '', value)
        path.write_text(value)
    # Keep current UI markup; replace only its synthetic bibliography and paper image URLs.
    paper = frontend / 'src/components/PdfSimilarityDemoPanel.tsx'
    value = paper.read_text()
    cards = [{'id': f'similar-paper-{i}', 'title': f'Similar Paper {i}', 'authors': 'Synthetic authors',
              'year': '2030', 'journal': 'Synthetic UI examples', 'doi': f'SYNTHETIC-{i}',
              'reason': '合成示例，仅用于前端布局和交互。', 'image': f'/images/pdf-similarity/similar_paper_{i}.svg'}
             for i in range(1, 7)]
    value, count = re.subn(r'const curatedPaperCards: SimilarPaperCard\[\] = \[.*?\n\];',
                           'const curatedPaperCards: SimilarPaperCard[] = ' + json.dumps(cards, ensure_ascii=False, indent=2) + ';', value, flags=re.S)
    if count != 1:
        raise PackagingError('PDF synthetic card replacement no longer matches current source.')
    paper.write_text(value)
    replace_exact(frontend / 'src/constants/onlineKnowledgeDefaults.ts',
                  'https://api.relayrouter.ai/v1', 'https://example.invalid/v1')
    agent = frontend / 'src/components/AgentWorkspaceHomePage.tsx'
    replace_exact(agent, '正在同步', '智能体工作区：交付包本地占位')
    home_test = frontend / 'src/App.home.test.tsx'
    home_test.write_text(home_test.read_text().replace('getByText("正在同步",', 'getByText("智能体工作区：交付包本地占位",'))
    # Production CSS chunks can load the shared workbench rules after the page
    # rules. Preserve this page's existing whole-module scrolling in either order.
    polymerization_css = frontend / 'src/styles/monomer-polymerization.css'
    css = polymerization_css.read_text()
    for selector, expected in (('.np-mp-page {', 3), ('.np-sw-layout,', 1), ('.np-sw-workspace {', 2)):
        original = '.np-monomer-polymerization ' + selector
        if css.count(original) != expected:
            raise PackagingError('Polymerization scroll adaptation no longer matches current CSS: ' + selector)
        css = css.replace(original, '.np-module-page.np-monomer-polymerization ' + selector)
    polymerization_css.write_text(css)
    adapt_page_fixture_routes(frontend / 'scripts/verify-page-consistency.mjs')
    adapt_workbench_verifiers(frontend)
    main = frontend / 'src/main.tsx'
    original = main.read_text()
    imports, body = original.split('// Start transport', 1)
    main.write_text(imports + '''async function bootstrapHandoff() {
  if (import.meta.env.MODE === "handoff") {
    const { installHandoffMockFetch } = await import("./mocks/handoffMockFetch");
    installHandoffMockFetch({ apiBaseUrl: "/api/v1" });
  }
// Start transport''' + body + '\n}\nvoid bootstrapHandoff();\n')
    vite = frontend / 'vite.config.ts'
    value = vite.read_text()
    value = 'import { handoffPlugin } from "./build/handoff";\n' + value
    value = value.replace('const env = loadEnv(', 'const handoffMode = mode === "handoff" || mode === "handoff-fixtures";\n  const env = loadEnv(', 1)
    value = value.replace('plugins: [react(),', 'plugins: [handoffMode && handoffPlugin({ httpFixtures: mode === "handoff-fixtures" }), react(),', 1)
    value = value.replace('      proxy\n', '      proxy: handoffMode ? undefined : proxy\n')
    # Define literals make caller environment variables unable to re-enable remote services.
    value = value.replace('    resolve: { alias:', '''    define: handoffMode ? {
      "import.meta.env.VITE_API_BASE_URL": JSON.stringify("/api/v1"),
      "import.meta.env.VITE_AGENT_WORKSPACE_URL": JSON.stringify(""),
      "import.meta.env.VITE_DEV_GPU_SESSION_CONTROL": JSON.stringify("false"),
      "import.meta.env.VITE_ONLINE_KNOWLEDGE_BASE_URL": JSON.stringify("https://example.invalid/v1"),
      "import.meta.env.VITE_ONLINE_KNOWLEDGE_MODEL": JSON.stringify("synthetic-model")
    } : undefined,
    resolve: { alias:''', 1)
    vite.write_text(value)
    package = json.loads((frontend / 'package.json').read_text())
    keep = {'postinstall', 'verify:ketcher-patches', 'test', 'test:pages', 'test:pages:focus',
            'test:pages:states', 'test:retrosynthesis-browser', 'test:structure-resources', 'test:ketcher-worker',
            'test:ketcher-runtime', 'test:auth-browser', 'test:guest-structure-import',
            'build:ketcher-runtime'}
    package['scripts'] = {k: v for k, v in package['scripts'].items() if k in keep}
    package['scripts'].update({
        'dev': 'npm run dev:handoff', 'build': 'npm run build:handoff', 'preview': 'npm run preview:handoff',
        'dev:handoff': 'vite --config vite.config.ts --mode handoff --host 127.0.0.1',
        'dev:fixtures': 'vite --config vite.config.ts --mode handoff-fixtures --host 127.0.0.1',
        'prebuild:handoff': 'npm run verify:ketcher-patches',
        'build:handoff': 'tsc -b && vite build --config vite.config.ts --mode handoff',
        'preview:handoff': 'vite preview --config vite.config.ts --mode handoff --host 127.0.0.1',
        'test:handoff': 'node scripts/verify-handoff.mjs',
        'test:motion': 'node scripts/verify-motion.mjs',
    })
    package['engines'] = {'node': '>=22.12.0 <23', 'npm': '>=10'}
    write_json(frontend / 'package.json', package)
    # Engine metadata is kept in sync without changing any locked dependency.
    lock = json.loads((frontend / 'package-lock.json').read_text())
    lock['packages']['']['engines'] = package['engines']
    write_json(frontend / 'package-lock.json', lock)
    (frontend / '.env.handoff').write_text(HANDOFF_ENV)
    (frontend / '.gitignore').write_text('node_modules/\ndist/\n.vite/\n.ketcher-runtime/\n*.tsbuildinfo\nbuild/*.d.ts\nshared/*.d.ts\nvite*.d.ts\n.env.local\n')
    (stage / '.gitignore').write_text('frontend/node_modules/\nfrontend/dist/\nfrontend/.vite/\nfrontend/.ketcher-runtime/\n*.tsbuildinfo\n')
    for path in regular_files(kit / 'templates'):
        target = stage / path.relative_to(kit / 'templates')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    # Repository documentation contains historical local evidence; do not export machine paths.
    for directory in (stage / 'docs', stage / 'design-system'):
        for path in directory.rglob('*.md'):
            text = path.read_text()
            text = re.sub(r'/(?:data|home|tmp)/[^\s`\)<>\]，；。]+', '[本地证据未随包交付]', text)
            text = re.sub(r'(?m)^.*(?:本地证据：|\.runtime/).*$\n?', '', text)
            text = text.replace('15个模块', '16个模块').replace('15页×12档', '16页×13档')
            path.write_text(text)
    subprocess.run([sys.executable, '-I', '-S', '-B', str(kit / 'adapt_docs.py'), str(stage)],
                   check=True, env=validation_environment(run, Path(sys.executable).parent))
    for path in (frontend / 'public/images').rglob('*.png'):
        remove_png_metadata(path)
    direct = {**package.get('dependencies', {}), **package.get('devDependencies', {})}
    rows = []
    for name in sorted(direct):
        info = lock['packages'].get('node_modules/' + name, {})
        rows.append(f"| {name} | {info.get('version', direct[name])} | {info.get('license', '见上游依赖内 LICENSE')} |")
    notices = stage / 'THIRD_PARTY_NOTICES.md'
    notices.write_text(notices.read_text() + '\n| 依赖 | 锁定版本 | 许可证 |\n|---|---|---|\n' + '\n'.join(rows) + '\n')
    write_json(stage / 'VALIDATION.json', {'status': 'candidate-not-validated', 'checks': []})
    create_adaptation_record(run / 'snapshot', stage, run)

def create_adaptation_record(snapshot: Path, stage: Path, run: Path) -> None:
    original = {p.relative_to(snapshot).as_posix(): p for p in regular_files(snapshot)}
    final = {p.relative_to(stage).as_posix(): p for p in regular_files(stage)}
    changes, patches = [], []
    for name in sorted(original.keys() | final.keys()):
        # Generated acceptance metadata changes after validation; its authoritative
        # final checksum is in FILE-MANIFEST, not in the source adaptation record.
        if name in {'VALIDATION.json', 'HANDOFF-CHANGES.json', MANIFEST}:
            continue
        before, after = original.get(name), final.get(name)
        if before and after and digest(before) == digest(after):
            continue
        changes.append({'path': name, 'action': 'added' if not before else 'removed' if not after else 'adapted',
                        'source_sha256': digest(before) if before else None,
                        'handoff_sha256': digest(after) if after else None})
        if Path(name).suffix in TEXT_SUFFIXES:
            try:
                left = before.read_text().splitlines(keepends=True) if before else []
                right = after.read_text().splitlines(keepends=True) if after else []
                patches.extend(difflib.unified_diff(left, right, fromfile='source/' + name, tofile='handoff/' + name))
            except UnicodeDecodeError:
                pass
    write_json(run / 'adaptations.json', changes)
    (run / 'adaptations.patch').write_text(''.join(patches))
    write_json(stage / 'HANDOFF-CHANGES.json', changes)

def audit(stage: Path) -> dict:
    files = list(regular_files(stage))
    issues = []
    denied = {'node_modules', '.git', 'backend', 'contracts', 'workers', 'ops', 'dist', '.runtime', '.ketcher-runtime', '__pycache__'}
    secret = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:sk-(?:proj-)?|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{20,}')
    private_ip = re.compile(rb'\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|114\.214\.255\.154)\b')
    for path in files:
        name = path.relative_to(stage).as_posix()
        if set(Path(name).parts) & denied or path.name in {'Dockerfile', 'nginx.conf'}:
            issues.append({'path': name, 'kind': 'excluded-path'})
        if private_input(Path(name)) and name != 'frontend/.env.handoff':
            issues.append({'path': name, 'kind': 'environment-file'})
        if name == 'frontend/.env.handoff' and (path.stat().st_size != len(HANDOFF_ENV.encode())
                                              or path.read_bytes() != HANDOFF_ENV.encode()):
            issues.append({'path': name, 'kind': 'unexpected-handoff-environment'})
        # Scan every byte, including large files and unknown suffixes. The retained
        # overlap exceeds each pattern's minimum match width across chunk edges.
        found, tail = set(), b''
        with path.open('rb') as stream:
            while chunk := stream.read(AUDIT_CHUNK_BYTES):
                window = tail + chunk
                if secret.search(window): found.add('credential')
                if private_ip.search(window): found.add('private-or-deployment-address')
                if b'/data/lzq/' in window or b'/home/devuser/' in window: found.add('machine-path')
                tail = window[-512:]
        issues.extend({'path': name, 'kind': kind} for kind in sorted(found))
    if issues:
        raise PackagingError('Archive audit failed (values redacted): ' + json.dumps(issues, ensure_ascii=False))
    return {'status': 'passed', 'files': len(files), 'credential_values_logged': False}

def verify_extracted_payload(directory: Path, manifest: dict[str, str]) -> None:
    for name, expected in manifest.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise PackagingError("Extracted source changed during validation: " + name)
    # New source files can alter module resolution even if every original byte is
    # unchanged. Permit only outputs produced by the documented install/build.
    generated_dirs = {'frontend/node_modules', 'frontend/dist', 'frontend/.vite', 'frontend/.ketcher-runtime'}
    for parent, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = [name for name in dirs if (Path(parent) / name).relative_to(directory).as_posix() not in generated_dirs]
        for name in dirs + files:
            path = Path(parent) / name
            if path.is_symlink():
                raise PackagingError('Unexpected validation symlink: ' + path.relative_to(directory).as_posix())
        for name in files:
            path = Path(parent) / name
            relative = path.relative_to(directory).as_posix()
            if relative in manifest or relative == MANIFEST:
                continue
            if relative in {'frontend/tsconfig.tsbuildinfo', 'frontend/tsconfig.node.tsbuildinfo'}:
                continue
            if relative.startswith(('frontend/build/', 'frontend/shared/')) or relative in {
                'frontend/vite.config.d.ts', 'frontend/vite.ketcher-compat.config.d.ts'
            }:
                source = relative[:-5] + '.ts' if relative.endswith('.d.ts') else ''
                if source in manifest:
                    continue
            raise PackagingError('Unexpected source added during validation: ' + relative)


def write_manifest(stage: Path) -> None:
    lines = [f'{digest(path)}  {path.relative_to(stage).as_posix()}'
             for path in sorted(regular_files(stage)) if path.name != MANIFEST]
    (stage / MANIFEST).write_text('\n'.join(lines) + '\n')

def make_zip(stage: Path, archive: Path, name: str) -> None:
    if archive.is_symlink():
        raise PackagingError('Archive output must not be a symlink')
    archive.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(archive, 'w', ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(regular_files(stage)):
            info = ZipInfo(name + '/' + path.relative_to(stage).as_posix(), FIXED_TIME)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = ZIP_DEFLATED
            z.writestr(info, path.read_bytes())

def verify_zip(archive: Path, name: str) -> dict[str, str]:
    with ZipFile(archive) as z:
        if z.testzip():
            raise PackagingError('ZIP CRC check failed')
        names = z.namelist()
        if len(set(names)) != len(names) or any(not n.startswith(name + '/') or '..' in Path(n).parts for n in names):
            raise PackagingError('ZIP inventory is unsafe')
        manifest = z.read(name + '/' + MANIFEST).decode()
        entries = dict((line.split('  ', 1)[1], line.split('  ', 1)[0]) for line in manifest.splitlines())
        actual = {n[len(name)+1:] for n in names if n != name + '/' + MANIFEST}
        if actual != entries.keys():
            raise PackagingError('Manifest inventory differs from archive')
        for rel, checksum in entries.items():
            if hashlib.sha256(z.read(name + '/' + rel)).hexdigest() != checksum:
                raise PackagingError('Manifest mismatch: ' + rel)
        return entries

def command(args: list[str], cwd: Path, env: dict, log: Path, timeout: int = 2400) -> dict:
    print('RUN ' + ' '.join(args), flush=True)
    start = time.monotonic()
    with log.open('w') as output:
        result = subprocess.run(args, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
    check = {'command': args, 'exit_code': result.returncode, 'seconds': round(time.monotonic()-start, 2), 'log': log.name}
    if result.returncode:
        print(log.read_text(errors='replace')[-9000:], flush=True)
        raise PackagingError('Validation failed: ' + ' '.join(args) + ' (see ' + str(log) + ')')
    return check

def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]

@contextmanager
def server(frontend: Path, env: dict, run: Path, preview: bool, fixtures: bool = False):
    port = free_port()
    mode = 'fixtures' if fixtures else 'preview' if preview else 'dev'
    with (run / f'{mode}-server.log').open('w') as log:
        process = subprocess.Popen(['npm', 'run', 'dev:fixtures' if fixtures else f'{mode}:handoff', '--', '--port', str(port), '--strictPort'],
                                   cwd=frontend, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            base = f'http://127.0.0.1:{port}'
            for _ in range(180):
                if process.poll() is not None:
                    raise PackagingError(f'{mode} server exited; see {mode}-server.log')
                try:
                    with urllib.request.urlopen(base, timeout=2) as response:
                        if response.status == 200:
                            break
                except (OSError, TimeoutError):
                    time.sleep(1)
            else:
                raise PackagingError(f'{mode} server did not start')
            yield base
        finally:
            import signal
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=15)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()

def preserve_previous_attempt(root: Path, run: Path) -> None:
    owned_run(root, run)
    artifacts = [*run.glob("*.log"), *[run / name for name in
                 ("browser-dev", "browser-preview", "pages", "focus", "states", "motion") if (run / name).exists()]]
    if not artifacts:
        return
    attempts = contained_path(run / "attempts", run)
    attempts.mkdir(exist_ok=True)
    destination = attempts / f"{len(list(attempts.iterdir())) + 1:03d}"
    destination.mkdir()
    for artifact in artifacts:
        contained_path(artifact, run)
        shutil.move(str(artifact), destination / artifact.name)


def validation_environment(run: Path, node_bin: Path) -> dict:
    # Package installation/build scripts do not inherit developer credentials.
    for name in ('home', 'tmp', 'npm-cache', 'browsers'):
        contained_path(run / name, run).mkdir(exist_ok=True)
    # npm rejects loading the same file as both global and user config.
    # Separate empty, run-owned files also exclude ambient registry credentials.
    npm_configs = {}
    for scope in ('user', 'global'):
        config = contained_path(run / 'home' / f'empty-{scope}.npmrc', run)
        if config.exists():
            if not config.is_file() or config.stat().st_size:
                raise PackagingError('Validation npm config must be an empty run-owned file')
        else:
            config.open('x').close()
        npm_configs[scope] = str(config)
    return {'PATH': str(node_bin) + os.pathsep + os.defpath,
            'HOME': str(run / 'home'), 'TMPDIR': str(run / 'tmp'),
            'LANG': 'C.UTF-8', 'CI': '1',
            'npm_config_userconfig': npm_configs['user'], 'npm_config_globalconfig': npm_configs['global'],
            'npm_config_cache': str(run / 'npm-cache'),
            'PLAYWRIGHT_BROWSERS_PATH': str(run / 'browsers')}

def validate(root: Path, run: Path, name: str, node_bin: Path) -> list[dict]:
    owned_run(root, run)
    verify_zip(run / 'candidate.zip', name)
    validation = reset_run_directory(root, run, 'validation')
    validation.mkdir()
    with ZipFile(run / 'candidate.zip') as z:
        z.extractall(validation)
    frontend = validation / name / 'frontend'
    env = validation_environment(run, node_bin)
    checks = []
    node_version = subprocess.check_output(['node', '--version'], env=env, text=True).strip()
    npm_version = subprocess.check_output(['npm', '--version'], env=env, text=True).strip()
    major, minor, *_ = map(int, node_version.lstrip('v').split('.'))
    if major != 22 or minor < 12:
        raise PackagingError('Validation requires Node 22.12+ of 22.x; got ' + node_version)
    if int(npm_version.split('.')[0]) < 10:
        raise PackagingError('Validation requires npm 10+; got ' + npm_version)
    write_json(run / 'validation-environment.json', {'node': node_version, 'npm': npm_version,
               'candidate_sha256': digest(run / 'candidate.zip'), 'fresh_extraction': True,
               'dependencies': 'npm ci in extracted candidate; isolated packaging-run cache'})
    for label, args in (
        ('install', ['npm', 'ci', '--no-audit', '--no-fund']),
        ('unit', ['npm', 'test', '--', '--maxWorkers=2']),
        ('ketcher-runtime', ['npm', 'run', 'test:ketcher-runtime']),
        ('build', ['npm', 'run', 'build:handoff']),
        ('browser-install', ['npx', '--no-install', 'playwright', 'install', 'chromium']),
    ):
        checks.append(command(args, frontend, env, run / f'{label}.log'))
    for preview in (False, True):
        with server(frontend, env, run, preview) as base:
            label = 'preview' if preview else 'dev'
            browser_env = {**env, 'HANDOFF_BASE_URL': base, 'HANDOFF_ARTIFACT_DIR': str(run / f'browser-{label}')}
            checks.append(command(['npm', 'run', 'test:handoff'], frontend, browser_env, run / f'browser-{label}.log', 3600))
    with server(frontend, env, run, False, fixtures=True) as base:
        for key, script, extra in (
            ('pages', 'test:pages', {'PAGE_BASE_URL': base, 'PAGE_ARTIFACT_DIR': str(run/'pages')}),
            ('focus', 'test:pages:focus', {'DRAWER_BASE_URL': base, 'DRAWER_ARTIFACT_DIR': str(run/'focus')}),
            ('states', 'test:pages:states', {'PAGE_BASE_URL': base, 'PAGE_ARTIFACT_DIR': str(run/'states')}),
            ('motion', 'test:motion', {'MOTION_BASE_URL': base, 'MOTION_ARTIFACT_DIR': str(run/'motion')}),
        ):
            checks.append(command(['npm', 'run', script], frontend, {**env, **extra}, run/f'{key}.log', 3600))
    return checks

def validation_summary(run: Path) -> dict:
    summary = {}
    unit = re.sub(r"\x1b\[[0-9;]*m", "", (run / "unit.log").read_text(errors="replace"))
    passed = re.search(r"Tests\s+(\d+) passed", unit)
    files = re.search(r"Test Files\s+(\d+) passed", unit)
    summary["unit"] = {"passed_tests": int(passed.group(1)) if passed else None,
                       "passed_files": int(files.group(1)) if files else None}
    for mode in ("dev", "preview"):
        payload = json.loads((run / f"browser-{mode}/results.json").read_text())
        summary[f"handoff_{mode}"] = {"cases": payload["total"], "failed": payload["failed"],
                                       "actual_browser_mock": True, "external_requests": sum(len(r["externalRequests"]) for r in payload["results"])}
    for name, filename in (("pages", "report.json"), ("focus", "results.json"),
                           ("states", "report.json"), ("motion", "results.json")):
        payload = json.loads((run / name / filename).read_text())
        summary[name] = {"checks": len(payload) if isinstance(payload, list) else len(payload.get("results", [])),
                         "failed": sum(bool(r.get("error")) or r.get("passed") is False for r in payload) if isinstance(payload, list) else len(payload.get("failures", []))}
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--resume', type=Path, help='Reuse a frozen source snapshot; reapply latest handoff overlay and repeat validation.')
    parser.add_argument('--prepare-only', action='store_true', help='Create an unverified candidate in .runtime only; publish nothing.')
    parser.add_argument('--node-bin', type=Path, default=Path('/home/devuser/.nvm/versions/node/v22.23.2/bin'))
    args = parser.parse_args()
    if args.root.is_symlink():
        raise PackagingError('Source root must not be a symlink')
    root = args.root.resolve(strict=True)
    kit = root / 'tools/frontend-handoff'
    check_kit(kit)
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')
    boundary = contained_path(root / '.runtime/frontend-handoff', root)
    run = contained_path(args.resume if args.resume else boundary / stamp, boundary)
    if args.resume:
        owned_run(root, run)
    if not args.resume:
        run.mkdir(parents=True, exist_ok=False)
        manifest = stable_snapshot(root, run / 'snapshot')
        write_json(run / 'source-snapshot.json', {'captured_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                   'source_kind': 'working-tree-including-uncommitted-files', 'files': manifest})
        write_json(run / 'run.json', {'schema_version': RUN_SCHEMA_VERSION, 'source_root': str(root),
                   'name': f'nexpoly-frontend-handoff-{stamp}', 'created_at': stamp})
    snapshot_record = contained_path(run / 'source-snapshot.json', run)
    if not snapshot_record.is_file():
        raise PackagingError('Resume directory has no frozen source snapshot')
    frozen = json.loads((run/'source-snapshot.json').read_text())['files']
    actual = {p.relative_to(run/'snapshot').as_posix(): digest(p) for p in regular_files(run/'snapshot')}
    if actual != frozen:
        raise PackagingError('Frozen source snapshot was modified; begin a new packaging run.')
    name = owned_run(root, run)['name']
    stage = reset_run_directory(root, run, 'stage')
    shutil.copytree(run / 'snapshot', stage)
    print('RUN_DIRECTORY=' + str(run), flush=True)
    adapter_inputs = {p.relative_to(kit).as_posix(): digest(p) for p in regular_files(kit)}
    adapt(stage, kit, run)
    if adapter_inputs != {p.relative_to(kit).as_posix(): digest(p) for p in regular_files(kit)}:
        raise PackagingError('Handoff adapter changed during preparation; resume after edits settle.')
    write_json(run/'adapter-snapshot.json', adapter_inputs)
    write_json(run/'audit.json', audit(stage))
    write_manifest(stage)
    make_zip(stage, contained_path(run/'candidate.zip', run), name)
    candidate = verify_zip(run/'candidate.zip', name)
    if args.prepare_only:
        print('UNVERIFIED_CANDIDATE=' + str(run/'candidate.zip'), flush=True)
        return
    preserve_previous_attempt(root, run)
    checks = validate(root, run, name, args.node_bin)
    verify_extracted_payload(run/"validation"/name, candidate)
    report = {'status': 'passed', 'validated_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
              'source_kind': 'working-tree snapshot', 'checks': checks, 'summary': validation_summary(run),
              'source_captured_at': json.loads((run/'source-snapshot.json').read_text())['captured_at'],
              'environment': json.loads((run/'validation-environment.json').read_text()),
              'audit': json.loads((run/'audit.json').read_text()),
              'limitations': ['Synthetic UI responses are not scientific validation or backend integration tests.',
                              'OpenScience is represented by a local placeholder.']}
    write_json(stage/'VALIDATION.json', report)
    # Runtime validation metadata becomes part of the published payload too.
    audit(stage)
    write_manifest(stage)
    final = contained_path(run/'verified.zip', run)
    make_zip(stage, final, name)
    verified = verify_zip(final, name)
    if {k:v for k,v in candidate.items() if k != 'VALIDATION.json'} != {k:v for k,v in verified.items() if k != 'VALIDATION.json'}:
        raise PackagingError('Final payload differs from the tested candidate')
    destination = contained_path(root/'release/frontend-handoff'/f'{name}.zip', root)
    sidecar = destination.with_suffix('.zip.sha256')
    if destination.exists() or destination.is_symlink() or sidecar.exists() or sidecar.is_symlink():
        raise PackagingError('Will not overwrite an existing handoff archive: ' + str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as output, final.open('rb') as source:
        shutil.copyfileobj(source, output)
    checksum = digest(destination)
    with sidecar.open('x') as output:
        output.write(f'{checksum}  {destination.name}\n')
    write_json(run/'published.json', {'archive': str(destination), 'sha256': checksum, 'bytes': destination.stat().st_size})
    print('PUBLISHED=' + str(destination), flush=True)
    print('SHA256=' + checksum, flush=True)

if __name__ == '__main__':
    try:
        main()
    except (PackagingError, subprocess.TimeoutExpired) as exc:
        print('HANDOFF FAILED: ' + str(exc), file=sys.stderr)
        sys.exit(1)
