from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import PROJECT_ROOT


DEFAULT_REPO_ID = "yujieq/MolScribe"
DEFAULT_FILENAME = "swin_base_char_aux_1m.pth"
DEFAULT_OUTPUT = PROJECT_ROOT / "model" / "ocsr" / DEFAULT_FILENAME


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download the default MolScribe checkpoint.")
    parser.add_argument("--repo-id", default=DEFAULT_REPO_ID)
    parser.add_argument("--filename", default=DEFAULT_FILENAME)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise SystemExit("huggingface_hub is required. Install backend requirements first.") from exc

    args = parse_args()
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    cached_path = Path(hf_hub_download(repo_id=args.repo_id, filename=args.filename))
    if cached_path.resolve() != output:
        shutil.copy2(cached_path, output)

    print(f"MolScribe checkpoint ready: {output}")


if __name__ == "__main__":
    main()
