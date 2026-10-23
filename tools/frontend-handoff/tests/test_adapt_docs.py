import importlib.util
import pathlib
import tempfile
import unittest
spec = importlib.util.spec_from_file_location('staged_doc_adapter', pathlib.Path(__file__).resolve().parents[1] / 'adapt_docs.py')
adapter = importlib.util.module_from_spec(spec); spec.loader.exec_module(adapter)
def fixture(base):
    root = pathlib.Path(base) / 'stage'; (root / 'docs').mkdir(parents=True)
    (root / 'README.md').write_text('# Unit-only package\n')
    (root / 'docs/page-unification.md').write_text('# Unit-only entry\n')
    headings = {'visual-motion-round-1.md': '### 本地重现', 'ketcher-componentization.md': '## 构建与复验', 'ketcher-sdk-compatibility.md': '## 复验和回退'}
    for name in adapter.HISTORICAL_DOCS:
        text = '# Unit-only historical document\n\n' + headings.get(name, '## Unit-only section') + '\n\nUnit-only old instructions.\n\n'
        text += '[entry](../README.md) [future](../VALIDATION.json) [old](missing-unit-file.md)\n'
        (root / 'docs' / name).write_text(text)
    return root
class StagedDocsTests(unittest.TestCase):
    def test_complete_adaptation_marks_history_and_preserves_live_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fixture(tmp); changed = adapter.adapt_historical_docs(root)
            self.assertEqual(len(changed), 4)
            for name in adapter.HISTORICAL_DOCS:
                text = (root / 'docs' / name).read_text()
                self.assertEqual(text.count(adapter.NOTICE_MARKER), 1)
                self.assertIn('../README.md', text); self.assertIn('../VALIDATION.json', text)
                self.assertNotIn('](missing-unit-file.md)', text)
    def test_adaptation_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fixture(tmp); adapter.adapt_historical_docs(root); self.assertEqual(adapter.adapt_historical_docs(root), [])
    def test_late_missing_document_does_not_partially_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = fixture(tmp); (root/'docs'/adapter.HISTORICAL_DOCS[-1]).unlink()
            before = {p: p.read_bytes() for p in (root/'docs').glob('*.md')}
            with self.assertRaises(FileNotFoundError): adapter.adapt_historical_docs(root)
            self.assertTrue(all(p.read_bytes() == data for p,data in before.items()))
    def test_duplicate_operational_heading_fails(self):
        with self.assertRaises(ValueError): adapter._replace_section('# Unit\n## Target\na\n## Target\nb\n', '## Target', 'replacement')
    def test_section_replacement_preserves_fenced_and_sibling_headings(self):
        text = '# Unit\n~~~md\n## Target\n~~~\n## Target\nold\n## Next\nkeep\n'
        result = adapter._replace_section(text, '## Target', 'replacement')
        self.assertIn('~~~md\n## Target\n~~~', result); self.assertIn('## Next\nkeep', result)
    def test_tilde_and_backtick_fenced_links_are_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=fixture(tmp); text='~~~md\n[unit](missing-unit-file.md)\n~~~\n```md\n[unit](missing-unit-file.md)\n```\n'
            self.assertEqual(adapter._missing_links_as_history(text, root/'docs/unit.md', root), text)
    def test_git_source_roots_are_rejected_before_writes(self):
        for marker_dir in (False, True):
            with self.subTest(directory=marker_dir), tempfile.TemporaryDirectory() as tmp:
                root=fixture(tmp); marker=root/'.git'
                marker.mkdir() if marker_dir else marker.write_text('unit-only marker')
                before=(root/'docs'/adapter.HISTORICAL_DOCS[0]).read_bytes()
                with self.assertRaisesRegex(ValueError, 'Git source'): adapter.adapt_historical_docs(root)
                self.assertEqual((root/'docs'/adapter.HISTORICAL_DOCS[0]).read_bytes(), before)
    def test_document_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=fixture(tmp); original=root/'docs'/adapter.HISTORICAL_DOCS[0]; outside=pathlib.Path(tmp)/'outside.md'
            outside.write_bytes(original.read_bytes()); original.unlink(); original.symlink_to(outside)
            before=outside.read_bytes()
            with self.assertRaisesRegex(ValueError, 'Symlink'): adapter.adapt_historical_docs(root)
            self.assertEqual(outside.read_bytes(), before)
    def test_document_directory_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=fixture(tmp); (root/'docs').rename(root/'docs-original'); (root/'docs').symlink_to(root/'docs-original', target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'Symlink'): adapter.adapt_historical_docs(root)
    def test_stage_root_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=fixture(tmp); link=pathlib.Path(tmp)/'stage-link'; link.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'symlink'): adapter.adapt_historical_docs(link)
    def test_outside_links_are_deactivated_without_reading_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=fixture(tmp); text='[unit](../../never-open-unit-target.md)\n'
            result=adapter._missing_links_as_history(text, root/'docs/unit.md', root)
            self.assertNotIn('](../../never-open-unit-target.md)', result)
if __name__=='__main__': unittest.main()
