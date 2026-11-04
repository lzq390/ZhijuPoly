from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from scripts import prepare_openscience_static_release as release


class StaticReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.current = self.root / "current"
        self.previous = self.root / "previous"
        self.output = self.root / "output"
        self.write(self.current, "index.html", '<script type="module" src="/assets/new.js"></script>')
        self.write(self.current, "assets/new.js", 'import "./common.js";export const version="new";')
        self.write(self.current, "assets/common.js", 'export const common=true;')
        self.write(self.current, "healthz", "ok")
        self.write(self.previous, "index.html", '<script type="module" src="/assets/old.js"></script>')
        self.write(self.previous, "assets/old.js", 'import "./common.js";export const load=()=>import("./old-lazy.js");')
        self.write(self.previous, "assets/common.js", 'export const common=true;')
        self.write(self.previous, "assets/old-lazy.js", 'export { common } from "./common.js";')

    @staticmethod
    def write(root, path, value):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)

    def prepare(self, **kwargs):
        return release.prepare(self.current, [self.previous], self.output, **kwargs)

    def test_current_and_old_lazy_graph_keep_exact_original_bytes(self):
        manifest = self.root / "manifest.json"
        result = self.prepare(manifest=manifest, required_assets=["assets/old.js"])
        self.assertEqual((self.output / "index.html").read_bytes(), (self.current / "index.html").read_bytes())
        for path in ["assets/old.js", "assets/old-lazy.js", "assets/common.js"]:
            self.assertEqual((self.output / path).read_bytes(), (self.previous / path).read_bytes())
        self.assertEqual((self.output / "healthz").read_text(), "ok")
        self.assertEqual(json.loads(manifest.read_text()), result)
        self.assertNotIn(str(self.root), manifest.read_text())
        self.assertEqual(result["releases"][1]["validated_dependencies"],
                         ["assets/common.js", "assets/old-lazy.js", "assets/old.js"])

    def test_private_operation_umask_keeps_nested_assets_readable_by_nginx(self):
        self.write(self.previous, "assets/old-lazy.js", 'export {value} from "./nested/legacy/dependency.js";')
        self.write(self.previous, "assets/nested/legacy/dependency.js", 'export const value="retained";')
        original_umask = os.umask(0o077)
        try:
            self.prepare()
        finally:
            os.umask(original_umask)
        for path in [self.output, *self.output.rglob("*")]:
            expected = 0o755 if path.is_dir() else 0o644
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected, str(path.relative_to(self.output)))
        self.assertEqual((self.output / "assets/nested/legacy/dependency.js").read_bytes(),
                         (self.previous / "assets/nested/legacy/dependency.js").read_bytes())

    def test_old_importmap_resolves_lazy_import_to_its_own_entry(self):
        self.write(self.previous, "index.html", '<script type="importmap">{"imports":{"/assets/base.js":"/assets/old.js"}}</script>'
                   '<script type="module" src="/assets/old.js"></script><link rel="modulepreload" href="/assets/old-lazy.js">')
        self.write(self.previous, "assets/old-lazy.js", 'import {load} from "/assets/base.js";')
        result = self.prepare()
        self.assertIn("assets/old-lazy.js", result["releases"][1]["validated_dependencies"])
        self.assertFalse((self.output / "assets/base.js").exists())

    def test_same_asset_path_with_different_bytes_fails_without_output(self):
        self.write(self.previous, "assets/common.js", 'export const common=false;')
        with self.assertRaisesRegex(release.ReleaseError, "Conflicting bytes"):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_missing_lazy_dependency_fails_before_any_output_or_manifest(self):
        (self.previous / "assets/old-lazy.js").unlink()
        manifest = self.root / "manifest.json"
        with self.assertRaisesRegex(release.ReleaseError, "Missing release dependency"):
            self.prepare(manifest=manifest)
        self.assertFalse(self.output.exists())
        self.assertFalse(manifest.exists())

    def test_existing_output_is_never_replaced(self):
        self.output.mkdir()
        marker = self.output / "keep"
        marker.write_text("original")
        with self.assertRaisesRegex(release.ReleaseError, "Output must be new"):
            self.prepare()
        self.assertEqual(marker.read_text(), "original")

    def test_symlink_files_directories_and_roots_are_rejected(self):
        link = self.previous / "assets/link.js"
        link.symlink_to(self.current / "assets/new.js")
        with self.assertRaisesRegex(release.ReleaseError, "symlinks"):
            self.prepare()
        link.unlink()
        link.symlink_to(self.current, target_is_directory=True)
        with self.assertRaisesRegex(release.ReleaseError, "symlinks"):
            self.prepare()
        link.unlink()
        linked_root = self.root / "linked-root"
        linked_root.symlink_to(self.previous, target_is_directory=True)
        with self.assertRaisesRegex(release.ReleaseError, "symlinks"):
            release.prepare(self.current, [linked_root], self.output)

    def test_only_previous_assets_are_retained_and_source_trees_are_unchanged(self):
        self.write(self.previous, "obsolete.html", "obsolete")
        before = release.inventory(self.previous)
        self.prepare()
        self.assertFalse((self.output / "obsolete.html").exists())
        self.assertEqual(release.inventory(self.previous), before)

    def test_required_asset_rejects_missing_and_escaped_paths(self):
        for required in ["assets/missing.js", "assets/../../escape.js", "/assets/old.js"]:
            with self.subTest(required=required), self.assertRaisesRegex(release.ReleaseError, "Required retained asset"):
                self.prepare(required_assets=[required])
        self.assertFalse(self.output.exists())

    def test_output_and_manifest_must_not_modify_inputs_or_public_tree(self):
        with self.assertRaisesRegex(release.ReleaseError, "Output must be new"):
            release.prepare(self.current, [self.previous], self.current / "output")
        with self.assertRaisesRegex(release.ReleaseError, "Manifest must be a new file"):
            self.prepare(manifest=self.output / "manifest.json")

    def test_import_lexer_ignores_code_examples_comments_and_regexes(self):
        source = '''const example='import "./not-a-module.js"';
          // import './comment.js';
          /* export {x} from './comment2.js'; */
          const regexp=/import ['"]missing/;
          import{thing}from"./real.js";
          export{thing}from"./export.js";
          const lazy=()=>import('./lazy.js');'''
        self.assertEqual(release.module_imports(source), ["./real.js", "./export.js", "./lazy.js"])

    def test_css_font_dependency_and_missing_module_preload_are_checked(self):
        self.write(self.current, "index.html", '<link rel="stylesheet" href="/assets/app.css"><script type="module" src="/assets/new.js"></script>')
        self.write(self.current, "assets/app.css", '@font-face{src:url("./font.woff2")}')
        with self.assertRaisesRegex(release.ReleaseError, "font.woff2"):
            self.prepare()
        self.write(self.current, "assets/font.woff2", "font")
        self.write(self.previous, "index.html", '<link rel="modulepreload" href="/assets/missing-preload.js">')
        with self.assertRaisesRegex(release.ReleaseError, "missing-preload.js"):
            self.prepare()

    def test_scope_and_prefix_importmaps(self):
        mapping = {"imports": {"library/": "/assets/"}, "scopes": {"/assets/special/": {"library/": "/assets/legacy/"}}}
        self.assertEqual(release.mapped_import("library/common.js", "assets/new.js", mapping), "assets/common.js")
        self.assertEqual(release.mapped_import("library/common.js", "assets/special/new.js", mapping), "assets/legacy/common.js")


if __name__ == "__main__":
    unittest.main()
