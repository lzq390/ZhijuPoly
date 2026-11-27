"""Build one local M0 verification image from the reviewed 2026-11-19 image."""

import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from uuid import uuid4


SOURCE = Path(__file__).resolve().parents[3] / "ops/openscience-mcp-dev"
BASE_TAG = "openscience-backend-poc:53293fc-release-20261119"
BASE_ID = "sha256:a828b3723cdf0ebe067339b88bad1ef9384ad596f50f1d448b7dfa45f28ec853"
OLD_ID = "sha256:7e4c055b4641bfcc0d329cbb2b61ef473b89e8435b3505dabc7b2895d11f0435"
OLD_PROMPT = "c6909f7ac353466cbb78c65916dff4343afbf979462b7c9730af3da8d6f1b705"
NEW_PROMPT = "e46ab01515c64c97682dd4a78a79ef79c1dc0bcb156329811b6531b0433b417f"
REVIEWED_INPUTS = {
    "Dockerfile": "f566e66531c65b81d764d1633eb3f299a1cb33a5a561c5229e0c749649b61501",
    "patch.mjs": "3fe9223367ac34f0d8b55d40848a6427a7b41358c497df8b5b65b5ecf73d5553",
    "dev-boundary.mjs": "0e0624a7b93666c34584b1f841eb85b29acd0f0a44026437e35f223c34983cfb",
    "capability-form-bridge.mjs": "1a4f06aa1cf13e56f409db4bd9aa11c04cb9f9c6df9f70931e4e8ee8be883889",
    "polytao-form-bridge.mjs": "c249d3ab9d2615d51713fa10ab9a37f4252eda9f1bcbe56088b2a0556c9a7ebe",
    "retrosynthesis-form-bridge.mjs": "1c1a9f626a4dc1b819c29263b7f8b2a26a0134f5c3f823e86e8fa29781965d93",
}
INPUT_STAGE = "m0-reviewed-input"
LABEL = "org.nexpoly.m0.candidate=20261119"


def capture(*args):
    return subprocess.check_output(args, text=True).strip()


def replace_once(data, before, after):
    if data.count(before) != 1:
        raise ValueError(f"Expected one reviewed value in build input: {before}")
    return data.replace(before, after)


def prepare_context(context):
    context.mkdir()
    for name, digest in REVIEWED_INPUTS.items():
        data = (SOURCE / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"Unreviewed build input: {name}")
        if name in ("Dockerfile", "patch.mjs"):
            text = replace_once(data.decode(), OLD_ID, BASE_ID)
            if name == "Dockerfile":
                text = replace_once(text, "openscience-backend-poc:release-20261029", INPUT_STAGE)
            else:
                text = replace_once(text, OLD_PROMPT, NEW_PROMPT)
            data = text.encode()
        (context / name).write_bytes(data)


def export_base(directory):
    # Use a local OCI input so that even base resolution needs no registry.
    if capture("docker", "image", "inspect", "--format", "{{.Id}}", BASE_TAG) != BASE_ID:
        raise ValueError("Local base image differs from the reviewed ID")
    layout = directory / "layout"
    layout.mkdir()
    archive = directory / "image.tar"
    subprocess.run(["docker", "image", "save", "-o", str(archive), BASE_ID], check=True)
    subprocess.run(["tar", "-xf", str(archive), "-C", str(layout), "index.json", "oci-layout", "blobs"], check=True)
    archive.unlink()
    descriptors = json.loads((layout / "index.json").read_text())["manifests"]
    if len(descriptors) != 1:
        raise ValueError("Expected a single descriptor in the local base export")
    return f"oci-layout://{layout}@{descriptors[0]['digest']}"


def main():
    with TemporaryDirectory(prefix="nexpoly-m0-build-") as temp:
        directory = Path(temp)
        context = directory / "context"
        prepare_context(context)
        base_source = export_base(directory)
        tag = f"nexpoly-m0-candidate:53293fc-{uuid4().hex}"
        subprocess.run([
            "docker", "buildx", "build", "--builder", "default", "--network=none",
            "--pull=false", "--no-cache", "--load", "--progress=plain", "--provenance=false",
            "--build-context", f"{INPUT_STAGE}={base_source}", "--label", LABEL,
            "--tag", tag, str(context),
        ], check=True)
    image_id = capture("docker", "image", "inspect", "--format", "{{.Id}}", tag)
    print(json.dumps({"tag": tag, "image_id": image_id, "base_image_id": BASE_ID, "label": LABEL}))


if __name__ == "__main__":
    main()
