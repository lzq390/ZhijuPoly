from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "frontend/scripts/verify-motion.mjs").read_text()
NAVIGATION = SOURCE[SOURCE.index("async function navigation("):SOURCE.index("async function checkDrag(")]
FAKE_PAGE = r'''
function fakePage(width, { collapsed = false, more = false, mobileOpen = false, groupId } = {}) {
  const events = [];
  const navRoot = width < 1024 ? "#np-mobile-navigation" : ".np-sidebar-desktop";
  let selected, idle = false, heading = false;
  function locator(selector, scope) {
    if (scope === navRoot) return { async click() {
      const id = selector.match(/^\[data-module-id="([^"]+)"\]$/)?.[1];
      assert.ok(["home", "researchTools", "structureWorkbench"].includes(id), "Persistent sidebar entry");
      if (width < 1024) assert.ok(mobileOpen, "Mobile navigation is open");
      selected = id; idle = false; events.push(["nav", id]);
    } };
    if (scope?.startsWith("main ")) {
      assert.ok(idle && selected === "researchTools" && heading, "Directory must be idle and active");
      if (selector === '.np-research-tools__collapse[aria-expanded="false"]') return {
        async count() { return Number(collapsed); },
        async click() { assert.ok(collapsed); collapsed = false; events.push(["reopen"]); }
      };
      assert.equal(collapsed, false, "Reopen collapsed category before inspecting cards");
      if (selector === '.np-research-tools__expand:not(.np-research-tools__collapse)[aria-expanded="false"]') return {
        async count() { return Number(more); },
        async click() { assert.ok(more); more = false; events.push(["more"]); }
      };
      return { async click() {
        assert.equal(more, false, "Reveal remaining cards before selecting the target");
        selected = selector.match(/^\[data-module-id="([^"]+)"\]$/)?.[1];
        assert.ok(selected); idle = false; events.push(["card", selected]);
      } };
    }
    if (selector === navRoot) return {
      async count() { return Number(mobileOpen); }, locator: child => locator(child, selector)
    };
    if (selector.startsWith("main ")) {
      assert.equal(selector, `main [data-research-group="${groupId}"]`);
      assert.ok(idle && selected === "researchTools" && heading);
      return { locator: child => locator(child, selector) };
    }
    if (selector === ".np-sidebar-mobile-layer") return { async waitFor(options) {
      assert.deepEqual(options, { state: "detached" }); assert.ok(idle);
      mobileOpen = false; events.push(["detached"]);
    } };
    const id = selector.match(/^\[data-module-content="([^"]+)"\]\[data-module-phase="idle"\]:not\(\[inert\]\)$/)?.[1];
    assert.ok(id, `Require idle and non-inert content guard: ${selector}`);
    return { async waitFor() { assert.equal(selected, id); idle = true; events.push(["idle", id]); } };
  }
  return { events, page: { locator, getByRole(role, options) {
    if (role === "button") return { async click() {
      assert.deepEqual(options, { name: "打开导航", exact: true });
      assert.ok(width < 1024 && !mobileOpen); mobileOpen = true; events.push(["open"]);
    } };
    assert.equal(role, "heading"); assert.deepEqual(options, { name: "实验工具", exact: true });
    return { async waitFor() {
      assert.ok(idle && selected === "researchTools"); heading = true; events.push(["heading"]);
    } };
  } } };
}
'''


@unittest.skipUnless(shutil.which("node"), "Node is required for the motion navigation regression")
class MotionVerifierNavigationTests(unittest.TestCase):
    def run_node(self, program):
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-"],
            input='import assert from "node:assert/strict";\n' + NAVIGATION + FAKE_PAGE + program,
            text=True, capture_output=True, cwd=ROOT, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_research_cards_wait_for_directory_and_expand_their_category(self):
        self.run_node(r'''
for (const width of [1440, 390]) {
  for (const [id, groupId] of [["knowledge", "discover"], ["polytaoGeneration", "discover"],
      ["monomerDft", "build"], ["reverseDesign", "optimize"]]) {
    for (const [collapsed, more] of [[false, false], [true, false], [false, true], [true, true]]) {
      const { page, events } = fakePage(width, { collapsed, more, groupId });
      await navigation(page, width, id);
      const detached = width < 1024 ? [["detached"]] : [];
      assert.deepEqual(events, [
        ...(width < 1024 ? [["open"]] : []), ["nav", "researchTools"], ["idle", "researchTools"],
        ...detached, ["heading"], ...(collapsed ? [["reopen"]] : []), ...(more ? [["more"]] : []),
        ["card", id], ["idle", id], ...detached
      ]);
    }
  }
}
''')

    def test_persistent_navigation_and_unknown_module_rejection(self):
        self.run_node(r'''
for (const width of [1440, 390]) {
  for (const id of ["home", "researchTools", "structureWorkbench"]) {
    for (const mobileOpen of [false, true]) {
      const { page, events } = fakePage(width, { mobileOpen });
      await navigation(page, width, id);
      assert.deepEqual(events, [
        ...(width < 1024 && !mobileOpen ? [["open"]] : []), ["nav", id], ["idle", id],
        ...(width < 1024 ? [["detached"]] : [])
      ]);
    }
  }
  const { page, events } = fakePage(width);
  await assert.rejects(navigation(page, width, "missingModule"), /Known research category/);
  assert.deepEqual(events, []);
}
''')

    def test_rapid_navigation_uses_persistent_entries_and_checks_retained_editor(self):
        start = SOURCE.index('      if (viewport.width < 1024)', SOURCE.index('await phase(page, "rapid-navigation");'))
        rapid = SOURCE[start:SOURCE.index('\n      await phase(page, "workbench-drawer");', start)]
        self.assertIn("assert.equal(result.retainedEditor, true);", rapid)
        self.run_node(r'''
for (const width of [1440, 390]) for (const retain of [true, false]) {
  const viewport = { width }, result = {}, events = [];
  let editor = {}, selected;
  const originalEditor = editor;
  globalThis.window = {};
  globalThis.document = { querySelector(selector) {
    assert.equal(selector, "[data-structure-editor]"); return editor;
  } };
  const page = {
    getByRole(role, options) {
      assert.equal(role, "button"); assert.deepEqual(options, { name: "打开导航", exact: true });
      return { async click() { events.push("open"); } };
    },
    locator(selector) {
      if (selector === "[data-module-content]") return { async getAttribute(name) {
        assert.equal(name, "data-module-content"); return selected;
      } };
      assert.equal(selector, width < 1024 ? "#np-mobile-navigation" : ".np-sidebar-desktop");
      return { async evaluate(callback) { callback({ querySelector(selector) {
        const id = selector.match(/^\[data-module-id="([^"]+)"\]$/)?.[1];
        assert.ok(["researchTools", "home", "structureWorkbench"].includes(id));
        return { click() { events.push(id); selected = id; if (!retain) editor = {}; } };
      } }); } };
    },
    async waitForTimeout(ms) { assert.equal(ms, 1650); }, async evaluate(callback) { return callback(); }
  };
  async function runRapid() {
''' + rapid + r'''
  }
  if (retain) await runRapid(); else await assert.rejects(runRapid(), { name: "AssertionError" });
  assert.deepEqual(events, [...(width < 1024 ? ["open"] : []), "researchTools", "home", "structureWorkbench"]);
  assert.equal(window.__editorBefore, originalEditor); assert.equal(result.retainedEditor, retain);
}
''')
