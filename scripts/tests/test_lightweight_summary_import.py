"""The standalone browsing-recording entrypoint must not require an AI SDK."""
from pathlib import Path
import subprocess
import sys


def test_summary_entrypoint_imports_without_openai_or_scientific_dependencies():
    root = Path(__file__).resolve().parents[2]
    code = f"""
import importlib.abc
import sys
sys.path[:0] = {[str(root), str(root / 'backend')]!r}
class NoHeavyImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {{'openai', 'torch', 'rdkit'}}:
            raise AssertionError('lightweight summary imported ' + fullname)
sys.meta_path.insert(0, NoHeavyImports())
from app.knowledge_poc import create_app
from app.services.knowledge_poc_summary import generate_knowledge_summary, stream_knowledge_summary
print('lightweight summary imports passed')
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code], text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "lightweight summary imports passed"
