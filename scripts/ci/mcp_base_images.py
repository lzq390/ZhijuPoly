#!/usr/bin/env python3
"""Validate reviewed MCP base identities and emit bounded public CI build arguments."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOCK = ROOT / "ops/capability-gateway-dev/base-images.lock.json"
SOURCE = "https://github.com/lzq390/ZhijuPoly"
BASES = {
    "gateway": ("zhijupoly-mcp-gateway-base", "fb7fdd70e46d7544d0816f042bd0a4324cd7a6c1e86a4a4606031f5108199df0", "CAPABILITY_GATEWAY_BASE_IMAGE"),
    "backend": ("zhijupoly-mcp-openscience-backend-base", "7e4c055b4641bfcc0d329cbb2b61ef473b89e8435b3505dabc7b2895d11f0435", "OPENSCIENCE_BASE_IMAGE"),
    "ui": ("zhijupoly-mcp-openscience-ui-base", "fea8a3c294bb324a98041f8f8354448e6ca5c6502dedc565c4b884994c567bb0", "OPENSCIENCE_UI_BASE_IMAGE"),
}
UI_PATCH_IMAGE = "node:22-bookworm-slim@sha256:d9f850096136edbc402debdd8729579a288aac64574ada0ff4db26b6ae58b0b2"


def load_lock(path: Path = DEFAULT_LOCK) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"version", "source", "images"} or payload["version"] != 1 or payload["source"] != SOURCE:
        raise ValueError("Unexpected MCP base lock format or repository source")
    images = payload["images"]
    if not isinstance(images, dict) or set(images) != set(BASES):
        raise ValueError("MCP base lock must contain exactly gateway, backend and ui")
    result = {}
    for name, (repository, reviewed, variable) in BASES.items():
        image = images[name]
        if not isinstance(image, dict) or set(image) != {"reference", "reviewed_image_id"}:
            raise ValueError("Unexpected MCP base image fields")
        reference = image["reference"]
        if not isinstance(reference, str) or re.fullmatch(r"ghcr\.io/lzq390/" + repository + r"@sha256:[0-9a-f]{64}", reference) is None:
            raise ValueError("MCP base reference must pin its reviewed GHCR repository digest")
        if reference.endswith("0" * 64) or image["reviewed_image_id"] != "sha256:" + reviewed:
            raise ValueError("MCP base identity does not match its reviewed local image")
        result[variable] = reference
    result["OPENSCIENCE_UI_PATCH_IMAGE"] = UI_PATCH_IMAGE
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    args = parser.parse_args()
    for key, value in load_lock(args.lock).items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
