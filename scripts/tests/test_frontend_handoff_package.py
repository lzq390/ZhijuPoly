"""Packaging-boundary checks; no frontend dependency installation needed."""
import importlib.util
import shutil
import subprocess
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

spec = importlib.util.spec_from_file_location('handoff_package', Path(__file__).resolve().parents[1] / 'package_frontend_handoff.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)

class HandoffPackageTests(unittest.TestCase):
    def owned_run(self, root):
        run = root / '.runtime/frontend-handoff/20300101-010203'
        run.mkdir(parents=True)
        package.write_json(run / 'run.json', {
            'schema_version': package.RUN_SCHEMA_VERSION,
            'source_root': str(root.resolve()),
            'name': 'nexpoly-frontend-handoff-' + run.name,
        })
        return run

    def test_selected_sources_keep_relative_json_imports(self):
        selected = package.selected_files(package.ROOT)
        imports = 0
        pattern = __import__('re').compile(r"(?:from|import)\s*(?:\(\s*)?[\"'](\.[^\"']+\.json)[\"']")
        for relative, path in selected.items():
            if path.suffix not in {'.ts', '.tsx', '.js', '.mjs'}:
                continue
            for dependency in pattern.findall(path.read_text()):
                target = (path.parent / dependency).resolve()
                if not target.is_relative_to(package.ROOT):
                    continue
                imports += 1
                with self.subTest(source=relative, dependency=dependency):
                    self.assertIn(target.relative_to(package.ROOT).as_posix(), selected)
        self.assertGreater(imports, 0)


    def test_page_fixture_routes_preserve_lower_auth_and_business_failures(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is required to execute the adapted browser route callbacks')
        with tempfile.TemporaryDirectory() as tmp:
            page_check = Path(tmp) / 'pages.mjs'
            source = Path(package.__file__).resolve().parents[1] / 'frontend/scripts/verify-page-consistency.mjs'
            page_check.write_text(source.read_text())
            package.adapt_page_fixture_routes(page_check)
            adapted = page_check.read_text()
            functions = []
            for name, following in (('taskStatusThemes', 'tabs'), ('serviceFailure', 'highThroughputStages')):
                start = adapted.index('async function ' + name + '(')
                end = adapted.index('\nasync function ' + following + '(', start)
                functions.append(adapted[start:end])
            harness = '\n'.join(functions) + r"""
const stop = new Error('captured route');
const base = 'http://127.0.0.1';
const taskThemeFixture = () => ({ response: () => undefined });
for (const probe of [taskStatusThemes, serviceFailure]) {
  let handler;
  const page = {
    route: async (_pattern, callback) => { handler = callback; },
    goto: async () => { throw stop; },
    unroute: async () => {},
  };
  await assert.rejects(probe(page, '/monomer-dft', '.fixture-status', 'fixture', [1920, 1080]), error => error === stop);
  assert.equal(typeof handler, 'function');
  for (const lower of [
    { status: 200, json: { authenticated: true, account_type: 'member', capabilities: { dev_gpu_control: false } } },
    { status: 200, json: { authenticated: false, user: null, capabilities: {} } },
    { status: 403, json: { detail: 'Synthetic forbidden session' } },
    { status: 'blocked', method: 'POST' },
  ]) {
    let delegated = 0;
    const actual = await handler({
      request: () => ({ url: () => base + '/api/v1/auth/session' }),
      fallback: () => { delegated += 1; return lower; },
      fulfill: () => { throw new Error('Auth must not be fulfilled by a business fixture'); },
    });
    assert.equal(delegated, 1);
    assert.equal(actual, lower, 'Preserve the lower transport identity, denial, and method guard');
  }
  let fulfilled;
  await handler({
    request: () => ({ url: () => base + '/api/v1/monomer-dft/capabilities' }),
    fallback: () => { throw new Error('Business failures must remain in the business fixture'); },
    fulfill: value => { fulfilled = value; },
  });
  assert.equal(fulfilled.status, 503);
  assert.match(fulfilled.json.detail, /^Local UI (state )?fixture$/);
}
"""
            javascript = 'import assert from "node:assert/strict";\n' + harness
            result = subprocess.run([node, '--input-type=module', '-'], input=javascript,
                                    cwd=tmp, env={'PATH': os.defpath, 'LANG': 'C.UTF-8'},
                                    text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)


    def test_drawer_verifiers_follow_guarded_navigation_without_weakening_checks(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is required to execute adapted browser navigation')
        with tempfile.TemporaryDirectory() as tmp:
            frontend = Path(tmp) / 'frontend'
            (frontend / 'scripts').mkdir(parents=True)
            original = {}
            for name in ('verify-workbench-drawer-focus.mjs', 'verify-motion.mjs', 'verify-high-throughput-states.mjs'):
                path = Path(package.__file__).resolve().parents[1] / 'frontend/scripts' / name
                original[name] = path.read_text()
                (frontend / 'scripts' / name).write_text(original[name])
            package.adapt_workbench_verifiers(frontend)
            self.assertEqual((frontend / 'scripts/verify-high-throughput-states.mjs').read_text(), original['verify-high-throughput-states.mjs'])
            snippets = []
            for name in ('verify-workbench-drawer-focus.mjs', 'verify-motion.mjs'):
                adapted = (frontend / 'scripts' / name).read_text()
                self.assertNotIn('设置单体逆合成反推参数', adapted)
                additions = {
                    'assert.ok(expected, "The exact focus-restoration trigger must exist");',
                    'assert.equal(identity.sameElement, true, JSON.stringify(identity));',
                    'assert.ok(submitTrigger, "Capture the exact form submitter before opening results");',
                }
                self.assertEqual([line.strip() for line in adapted.splitlines() if 'assert.' in line and line.strip() not in additions],
                                 [line.strip() for line in original[name].splitlines() if 'assert.' in line])
                start = adapted.index('await page.getByRole("button", { name: "打开单体逆合成反推"')
                end = adapted.index('await page.getByRole("button", { name: "运行反推", exact: true }).click();', start)
                end += len('await page.getByRole("button", { name: "运行反推", exact: true }).click();')
                snippets.append(adapted[start:end])
                if name == 'verify-workbench-drawer-focus.mjs':
                    tail = 'result.initial = await expectDrawerFocus(page);'
                    expected_tail = original[name][original[name].index(tail):].replace(
                        'result.closed = await expectClosed(page);', 'result.closed = await expectClosed(page, submitTrigger);').replace(
                        'result.error = String(error);', 'result.error = String(error);\n'
                        '          result.focusFailure = error.focusIdentity || await focusIdentity(page, submitTrigger).catch(() => null);')
                    self.assertEqual(adapted[adapted.index(tail):], expected_tail)
                else:
                    self.assertEqual(adapted.count('workspace: ".np-monomer-retrosynthesis .np-sw-workspace"'), 2)
                    tail = 'await phase(page, "module-navigation");'
                    self.assertEqual(adapted[adapted.index(tail):], original[name][original[name].index(tail):])
            javascript = 'import assert from "node:assert/strict";\nconst snippets = ' + __import__('json').dumps(snippets) + r""";
for (const [index, snippet] of snippets.entries()) {
  const calls = [];
  const page = {
    getByRole: (role, options) => ({
      click: async () => calls.push(['click', role, options.name, options.exact]),
      fill: async value => calls.push(['fill', role, options.name, value]),
      elementHandle: async () => { calls.push(['capture', role, options.name, options.exact]); return {}; },
    }),
    locator: selector => ({ waitFor: async () => calls.push(['ready', selector]) }),
  };
  await new Function('page', 'assert', 'return (async () => { let submitTrigger;' + snippet + '})();')(page, assert);
  assert.deepEqual(calls, [
    ['click', 'button', '打开单体逆合成反推', true],
    ['ready', '[data-module-content="monomerRetrosynthesis"][data-module-phase="idle"]:not([inert])'],
    ['fill', 'textbox', '目标单体 SMILES', 'CCO'],
    ...(index === 0 ? [['capture', 'button', '运行反推', true]] : []),
    ['click', 'button', '运行反推', true],
  ]);
}
"""
            result = subprocess.run([node, '--input-type=module', '-'], input=javascript, text=True,
                                    cwd=tmp, env={'PATH': os.defpath, 'LANG': 'C.UTF-8'}, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_drawer_fixtures_count_only_the_business_post(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is required to execute adapted browser fixture callbacks')
        with tempfile.TemporaryDirectory() as tmp:
            frontend = Path(tmp) / 'frontend'
            (frontend / 'scripts').mkdir(parents=True)
            for name in ('verify-workbench-drawer-focus.mjs', 'verify-motion.mjs'):
                source = Path(package.__file__).resolve().parents[1] / 'frontend/scripts' / name
                (frontend / 'scripts' / name).write_text(source.read_text())
            package.adapt_workbench_verifiers(frontend)
            blocks = []
            for name in ('verify-workbench-drawer-focus.mjs', 'verify-motion.mjs'):
                text = (frontend / 'scripts' / name).read_text()
                start = text.index('await context.route("**/*", ')
                end = text.index('const page = await context.newPage();', start)
                blocks.append(text[start:end])
            javascript = 'import assert from "node:assert/strict";\nconst blocks = ' + __import__('json').dumps(blocks) + r""";
const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor;
const base = 'http://127.0.0.1:19001';
for (const [index, block] of blocks.entries()) {
  let handler;
  const context = { route: async (_pattern, callback) => { handler = callback; } };
  const count = await new AsyncFunction('context', 'fixture', 'base', 'total',
    'let fixtureRequests = 0; let polytaoSubmits = 0;' + block + '\nreturn () => fixtureRequests;')(
      context, () => ({ total: 2, candidates: [] }), base, 2);
  const cases = [
    [base, '/api/v1/monomer-retrosynthesis', 'POST', 'fulfill'],
    [base, '/monomer-retrosynthesis', 'GET', 'continue'],
    [base, '/api/v1/monomer-retrosynthesis', 'GET', 'continue'],
    [base, '/api/v1/monomer-retrosynthesis', 'PUT', 'abort'],
    [base, '/api/v1/auth/session', 'GET', 'continue'],
    ['http://localhost:19001', '/api/v1/monomer-retrosynthesis', 'POST', 'abort'],
  ];
  let fulfilled = 0;
  for (const [origin, path, method, expected] of cases) {
    let actual;
    await handler({ request: () => ({ url: () => origin + path, method: () => method }),
      fulfill: data => { actual = 'fulfill'; fulfilled += 1; assert.ok(Array.isArray(data.json.candidates)); },
      continue: () => { actual = 'continue'; }, abort: () => { actual = 'abort'; } });
    assert.equal(actual, expected);
  }
  assert.equal(fulfilled, 1);
  if (index === 0) assert.equal(count(), 1);
}
"""
            result = subprocess.run([node, '--input-type=module', '-'], input=javascript, text=True,
                                    cwd=tmp, env={'PATH': os.defpath, 'LANG': 'C.UTF-8'}, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)


    def test_drawer_focus_requires_exact_submitted_and_remounted_trigger_identity(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is required for the exact DOM identity regression')
        with tempfile.TemporaryDirectory() as tmp:
            frontend = Path(tmp) / 'frontend'
            (frontend / 'scripts').mkdir(parents=True)
            for name in ('verify-workbench-drawer-focus.mjs', 'verify-motion.mjs'):
                source = package.ROOT / 'frontend/scripts' / name
                (frontend / 'scripts' / name).write_text(source.read_text())
            package.adapt_workbench_verifiers(frontend)
            adapted = (frontend / 'scripts/verify-workbench-drawer-focus.mjs').read_text()
            start = adapted.index('async function focusIdentity(')
            end = adapted.index('\ntry {', start)
            functions = adapted[start:end]
            self.assertEqual(adapted.count('result.closed = await expectClosed(page, submitTrigger);'), 1)
            self.assertEqual(adapted.count('await expectClosed(page);'), 2)
            self.assertLess(adapted.index('submitTrigger = await'), adapted.index('await page.getByRole("button", { name: "运行反推", exact: true }).click();'))
            javascript = 'import assert from "node:assert/strict";\n' + functions + r"""
const button = (text, ariaLabel = null) => ({ tagName: 'BUTTON', id: '', textContent: text, isConnected: true,
  getAttribute: name => name === 'aria-label' ? ariaLabel : name === 'type' ? 'submit' : null,
  closest: () => null });
const submit = button('运行反推');
const sameNameImpostor = button('运行反推');
const staleReopen = button('反推结果', '展开反推结果');
staleReopen.isConnected = false;
const currentReopen = button('反推结果', '展开反推结果');
globalThis.document = { activeElement: submit, querySelector: () => ({ dataset: { motionPhase: 'closed' } }) };
let reopenLookups = 0;
const page = {
  evaluate: async (callback, expected) => callback(expected),
  waitForFunction: async (callback, expected) => { if (!callback(expected)) throw new Error('Exact identity mismatch'); },
  waitForTimeout: async milliseconds => assert.equal(milliseconds, 150),
  getByRole: (role, options) => { reopenLookups++; assert.deepEqual([role, options], ['button', { name: '展开反推结果', exact: true }]);
    return { elementHandle: async () => currentReopen }; },
};
async function snapshot() { return { focusInDrawer: false, focusedInert: false }; }
assert.equal((await expectClosed(page, submit)).focusIdentity.sameElement, true);
assert.equal(reopenLookups, 0, 'First close must use the captured submitter, never a role/label approximation');
document.activeElement = sameNameImpostor;
await assert.rejects(expectClosed(page, submit), error => {
  assert.equal(error.focusIdentity.sameElement, false);
  assert.equal(error.focusIdentity.actual.text, '运行反推');
  assert.equal(error.focusIdentity.expected.text, '运行反推');
  return /Exact identity mismatch/.test(error.message);
});
document.activeElement = currentReopen;
assert.equal((await expectClosed(page)).focusIdentity.sameElement, true);
document.activeElement = staleReopen;
await assert.rejects(expectClosed(page), error => error.focusIdentity.sameElement === false && error.focusIdentity.actual.connected === false);
assert.equal(reopenLookups, 2, 'Each later close must compare with the exact newly rendered reopen control');
"""
            result = subprocess.run([node, '--input-type=module', '-'], input=javascript, text=True,
                                    cwd=tmp, env={'PATH': os.defpath, 'LANG': 'C.UTF-8'}, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)


    def test_drawer_snapshot_handles_unmounted_navigation_without_hiding_initial_failures(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is required for the navigation snapshot regression')
        with tempfile.TemporaryDirectory() as tmp:
            frontend = Path(tmp) / 'frontend'
            (frontend / 'scripts').mkdir(parents=True)
            for name in ('verify-workbench-drawer-focus.mjs', 'verify-motion.mjs'):
                source = package.ROOT / 'frontend/scripts' / name
                (frontend / 'scripts' / name).write_text(source.read_text())
            package.adapt_workbench_verifiers(frontend)
            text = (frontend / 'scripts/verify-workbench-drawer-focus.mjs').read_text()
            snapshot = text[text.index('async function snapshot('):text.index('\nasync function expectDrawerFocus(')]
            javascript = 'import assert from "node:assert/strict";\n' + snapshot + r"""
const active = { tagName: 'MAIN', getAttribute: () => null, closest: () => null };
const main = { getBoundingClientRect: () => ({ x: 4, y: 6 }) };
const title = { getBoundingClientRect: () => ({ width: 100, x: 20, y: 26 }) };
let titles = [], drawer = null;
globalThis.document = { activeElement: active,
  querySelectorAll: selector => { assert.equal(selector, '.np-module-page-title'); return titles; },
  querySelector: selector => selector === '.np-sw-drawer' ? drawer : selector === '.np-app-shell__body > main' ? main : null,
};
const page = { evaluate: async callback => callback() };
const navigated = await snapshot(page);
assert.equal(navigated.title, null);
assert.equal(navigated.focusInDrawer, false);
assert.equal(navigated.focusedInert, false);
assert.equal(navigated.focusedLabel, 'MAIN');
titles = [title];
const titleWithoutDrawer = await snapshot(page);
assert.deepEqual(titleWithoutDrawer.title, { x: 16, y: 20 });
assert.equal(titleWithoutDrawer.focusInDrawer, false);
drawer = { dataset: { motionPhase: 'open' }, contains: element => element === active };
const initial = await snapshot(page);
assert.equal(initial.focusInDrawer, true);
assert.deepEqual(initial.title, { x: 16, y: 20 });
titles = []; drawer = null;
const missingInitial = await snapshot(page);
assert.throws(() => assert.deepEqual(missingInitial.title, { x: 16, y: 20 }));
assert.throws(() => assert.equal(missingInitial.focusInDrawer, true));
"""
            result = subprocess.run([node, '--input-type=module', '-'], input=javascript, text=True,
                                    cwd=tmp, env={'PATH': os.defpath, 'LANG': 'C.UTF-8'}, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_env_filename_variants_are_rejected_by_audit(self):
        for name in ('.env', '.env.local', '.envrc', '.env-development', 'app.env',
                     'app.env.example', 'app.envrc', 'app.env-production', 'config.ENV.prod'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / name).write_text('SYNTHETIC_SETTING=placeholder\n')
                with self.assertRaises(package.PackagingError):
                    package.audit(root)

    def test_source_selection_excludes_environment_files_before_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'frontend/src'
            source.mkdir(parents=True)
            (root / 'design-system/polyprop').mkdir(parents=True)
            for name in ('.env.local', 'app.env', 'app.env.example', 'safe.ts', 'env.d.ts'):
                (source / name).write_text('synthetic fixture')
            with patch.object(package, 'TREE_DIRS', ('src',)), patch.object(package, 'ROOT_FILES', ()), \
                 patch.object(package, 'SCRIPTS', ()), patch.object(package, 'DOCS', ()), \
                 patch.object(package, 'EXTERNAL_FIXTURES', {}):
                selected = package.selected_files(root)
            self.assertEqual(set(selected), {'frontend/src/safe.ts', 'frontend/src/env.d.ts'})

    def test_large_text_and_chunk_crossing_credentials_are_rejected(self):
        for padding in (4_000_000, package.AUDIT_CHUNK_BYTES - 4):
            with self.subTest(padding=padding), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'source.ts').write_text(' ' * padding + 'sk-proj-' + 'x' * 48)
                with self.assertRaisesRegex(package.PackagingError, 'credential'):
                    package.audit(root)

    def test_unknown_file_suffix_does_not_bypass_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'payload.blob').write_bytes(b'\x00ghp_' + b'x' * 48)
            with self.assertRaisesRegex(package.PackagingError, 'credential'):
                package.audit(root)

    def test_only_exact_generated_handoff_environment_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'frontend/.env.handoff'
            path.parent.mkdir()
            path.write_text(package.HANDOFF_ENV)
            package.audit(root)
            path.write_text(package.HANDOFF_ENV + 'EXTRA_SETTING=synthetic\n')
            with self.assertRaisesRegex(package.PackagingError, 'unexpected-handoff-environment'):
                package.audit(root)

    def test_existing_snapshot_destination_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / 'snapshot'
            snapshot.mkdir()
            marker = snapshot / 'keep.txt'
            marker.write_text('preserve')
            with self.assertRaises(package.PackagingError):
                package.stable_snapshot(root, snapshot)
            self.assertEqual(marker.read_text(), 'preserve')

    def test_resume_outside_root_and_legacy_marker_cannot_trigger_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project'
            root.mkdir()
            outside = Path(tmp) / 'unrelated'
            outside.mkdir()
            with patch.object(package.shutil, 'rmtree') as remove:
                with self.assertRaises(package.PackagingError):
                    package.reset_run_directory(root, outside, 'stage')
                run = root / '.runtime/frontend-handoff/20300101-010203'
                run.mkdir(parents=True)
                package.write_json(run / 'run.json', {'name': 'legacy'})
                with self.assertRaises(package.PackagingError):
                    package.reset_run_directory(root, run, 'validation')
                remove.assert_not_called()

    def test_owned_cleanup_preserves_source_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keep = root / 'source.txt'
            keep.write_text('preserve')
            run = self.owned_run(root)
            (run / 'stage').mkdir()
            (run / 'stage/generated.txt').write_text('disposable')
            package.reset_run_directory(root, run, 'stage')
            self.assertFalse((run / 'stage').exists())
            outside = root / 'outside'
            outside.mkdir()
            (run / 'validation').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(package.PackagingError):
                package.reset_run_directory(root, run, 'validation')
            self.assertTrue(outside.is_dir())
            self.assertEqual(keep.read_text(), 'preserve')

    def test_run_marker_cannot_redirect_source_root_or_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self.owned_run(root)
            record = __import__('json').loads((run / 'run.json').read_text())
            record['name'] = '../outside'
            package.write_json(run / 'run.json', record)
            with self.assertRaises(package.PackagingError):
                package.owned_run(root, run)

    def test_non_object_run_marker_is_rejected_before_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self.owned_run(root)
            package.write_json(run / 'run.json', [])
            with patch.object(package.shutil, 'rmtree') as remove:
                with self.assertRaises(package.PackagingError):
                    package.reset_run_directory(root, run, 'stage')
                remove.assert_not_called()

    def test_validation_environment_does_not_inherit_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self.owned_run(root)
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-only', 'DATABASE_PASSWORD': 'synthetic-only'}):
                environment = package.validation_environment(run, Path('/synthetic/node/bin'))
            self.assertNotIn('OPENAI_API_KEY', environment)
            self.assertNotIn('DATABASE_PASSWORD', environment)
            self.assertEqual(environment['HOME'], str(run / 'home'))
            user = Path(environment['npm_config_userconfig'])
            global_config = Path(environment['npm_config_globalconfig'])
            self.assertNotEqual(user, global_config)
            self.assertEqual(user.parent, run / 'home')
            self.assertEqual(global_config.parent, run / 'home')
            self.assertEqual(user.read_bytes(), b'')
            self.assertEqual(global_config.read_bytes(), b'')

    def test_validation_environment_rejects_nonempty_npm_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self.owned_run(root)
            (run / 'home').mkdir()
            config = run / 'home' / 'empty-user.npmrc'
            config.write_text('registry=https://example.invalid\n')
            with self.assertRaises(package.PackagingError):
                package.validation_environment(run, Path('/synthetic/node/bin'))
            self.assertEqual(config.read_text(), 'registry=https://example.invalid\n')

    def test_archive_symlink_does_not_overwrite_unrelated_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / 'stage'
            stage.mkdir()
            target = root / 'keep.txt'
            target.write_text('preserve')
            archive = root / 'candidate.zip'
            archive.symlink_to(target)
            with self.assertRaises(package.PackagingError):
                package.make_zip(stage, archive, 'handoff')
            self.assertEqual(target.read_text(), 'preserve')

    def test_missing_adapter_kit_fails_before_run_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch('sys.argv', ['package_frontend_handoff.py', '--root', str(root), '--prepare-only']):
                with self.assertRaisesRegex(package.PackagingError, 'adapter kit'):
                    package.main()
            self.assertFalse((root / '.runtime').exists())

    def test_archive_is_reproducible_and_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / 'stage'
            stage.mkdir()
            (stage / 'source.ts').write_text('export const value = 42;\n')
            package.write_manifest(stage)
            package.make_zip(stage, root / 'first.zip', 'handoff')
            package.make_zip(stage, root / 'second.zip', 'handoff')
            self.assertEqual((root / 'first.zip').read_bytes(), (root / 'second.zip').read_bytes())
            entries = package.verify_zip(root / 'first.zip', 'handoff')
            self.assertEqual(set(entries), {'source.ts'})
            with ZipFile(root / 'first.zip', 'a') as archive:
                archive.writestr('handoff/undeclared.txt', 'unexpected data')
            with self.assertRaisesRegex(package.PackagingError, 'inventory'):
                package.verify_zip(root / 'first.zip', 'handoff')

    def test_extracted_source_tampering_cannot_be_reported_as_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'app.ts'
            source.write_text('original')
            manifest = {'app.ts': package.digest(source)}
            package.verify_extracted_payload(root, manifest)
            source.write_text('changed after extraction')
            with self.assertRaisesRegex(package.PackagingError, 'during validation'):
                package.verify_extracted_payload(root, manifest)

    def test_archive_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'unsafe.zip'
            with ZipFile(archive, 'w') as z:
                z.writestr('handoff/../outside', 'payload')
            with self.assertRaisesRegex(package.PackagingError, 'unsafe'):
                package.verify_zip(archive, 'handoff')

    def test_extracted_extra_source_is_rejected_but_build_outputs_are_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'frontend/build').mkdir(parents=True)
            source = root / 'frontend/build/plugin.ts'
            source.write_text('export const value = 1;')
            manifest = {'frontend/build/plugin.ts': package.digest(source)}
            (root / 'frontend/build/plugin.d.ts').write_text('export declare const value = 1;')
            (root / 'frontend/dist').mkdir()
            (root / 'frontend/dist/app.js').write_text('built')
            package.verify_extracted_payload(root, manifest)
            (root / 'frontend/build/plugin.js').write_text('unexpected module override')
            with self.assertRaisesRegex(package.PackagingError, 'Unexpected source'):
                package.verify_extracted_payload(root, manifest)

    def test_adaptation_record_excludes_final_validation_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'snapshot').mkdir()
            (root / 'stage').mkdir()
            (root / 'stage/VALIDATION.json').write_text('{"status":"candidate-not-validated"}')
            package.create_adaptation_record(root / 'snapshot', root / 'stage', root)
            self.assertEqual((root / 'stage/HANDOFF-CHANGES.json').read_text(), '[]\n')

    def test_rejects_symlinks_in_snapshot_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'secret').write_text('never copy this')
            (root / 'link').symlink_to(root / 'secret')
            with self.assertRaisesRegex(package.PackagingError, 'Symlink'):
                list(package.regular_files(root))

    def test_uncommitted_file_is_copied_and_racing_snapshot_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'new-untracked.ts'
            source.write_text('before')
            actual_copy = package.shutil.copyfile
            copies = 0
            def racing_copy(src, dst):
                nonlocal copies
                copies += 1
                result = actual_copy(src, dst)
                if copies == 1:
                    source.write_text('after')
                return result
            with patch.object(package, 'selected_files', return_value={'new-untracked.ts': source}), \
                 patch.object(package.shutil, 'copyfile', side_effect=racing_copy):
                result = package.stable_snapshot(root, root / 'snapshot')
            self.assertEqual(copies, 2)
            self.assertEqual((root / 'snapshot/new-untracked.ts').read_text(), 'after')
            self.assertEqual(result['new-untracked.ts'], package.digest(source))

    def test_audit_rejects_secrets_without_echoing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = 'sk-proj-' + 'a' * 48
            (root / 'accidental.ts').write_text('export const token = "' + token + '"')
            with self.assertRaises(package.PackagingError) as caught:
                package.audit(root)
            self.assertNotIn(token, str(caught.exception))
            self.assertIn('credential', str(caught.exception))

    def test_audit_rejects_env_and_build_artifacts(self):
        for name in ['.env.local', 'frontend/dist/index.html', 'backend/app.py']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('example')
                with self.assertRaises(package.PackagingError):
                    package.audit(root)

if __name__ == '__main__':
    unittest.main()
