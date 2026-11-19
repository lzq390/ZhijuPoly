"""Build the per-user development backend from its pinned local November base."""

import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from uuid import uuid4


HERE = Path(__file__).resolve().parent
SHARED_ADAPTERS = HERE.parent / "openscience-mcp-dev"
BASE_TAG = "openscience-backend-poc:53293fc-release-20261119"
BASE_ID = "sha256:a828b3723cdf0ebe067339b88bad1ef9384ad596f50f1d448b7dfa45f28ec853"
TEMPLATE_VERSION = "53293fc-m1-v1"
PURPOSE = "openscience-per-user-development"
ADAPTER_DIGESTS = {
    "patch.mjs": "3fe9223367ac34f0d8b55d40848a6427a7b41358c497df8b5b65b5ecf73d5553",
    "dev-boundary.mjs": "0e0624a7b93666c34584b1f841eb85b29acd0f0a44026437e35f223c34983cfb",
    "capability-form-bridge.mjs": "1a4f06aa1cf13e56f409db4bd9aa11c04cb9f9c6df9f70931e4e8ee8be883889",
    "polytao-form-bridge.mjs": "c249d3ab9d2615d51713fa10ab9a37f4252eda9f1bcbe56088b2a0556c9a7ebe",
    "retrosynthesis-form-bridge.mjs": "1c1a9f626a4dc1b819c29263b7f8b2a26a0134f5c3f823e86e8fa29781965d93",
}


def docker_output(*args):
    return subprocess.check_output(["docker", "--context", "default", *args], text=True).strip()


def prepare_context(context):
    context.mkdir()
    for name, digest in ADAPTER_DIGESTS.items():
        source = (SHARED_ADAPTERS / name).read_bytes()
        if hashlib.sha256(source).hexdigest() != digest:
            raise ValueError(f"Shared OpenScience adapter has changed since review: {name}")
        (context / name).write_bytes(source)
    for name in ("Dockerfile", "install_patch.mjs", "instance-auth.mjs", "activity.mjs", "patch_activity_instance.mjs"):
        (context / name).write_bytes((HERE / name).read_bytes())


def export_local_base(directory):
    if docker_output("image", "inspect", "--format", "{{.Id}}", BASE_TAG) != BASE_ID:
        raise ValueError("Local OpenScience base does not match the pinned November image")
    # FROM resolves to this local layout even if the registry is unreachable.
    archive = directory / "base.tar"
    layout = directory / "oci"
    layout.mkdir()
    subprocess.run(["docker", "--context", "default", "image", "save", "-o", str(archive), BASE_ID], check=True)
    subprocess.run(["tar", "-xf", str(archive), "-C", str(layout), "index.json", "oci-layout", "blobs"], check=True)
    archive.unlink()
    manifest, = json.loads((layout / "index.json").read_text())["manifests"]
    return f"oci-layout://{layout}@{manifest['digest']}"


def main():
    if not docker_output("context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}").startswith("unix://"):
        raise ValueError("The development image requires a local Docker Engine")
    tag = f"nexpoly-openscience-per-user-dev:53293fc-{uuid4().hex}"
    with TemporaryDirectory(prefix="nexpoly-openscience-dev-build-") as tmp:
        directory = Path(tmp)
        context = directory / "context"
        prepare_context(context)
        base = export_local_base(directory)
        subprocess.run([
            "docker", "--context", "default", "buildx", "build", "--builder", "default",
            "--pull=false", "--network=none", "--load", "--provenance=false",
            "--build-context", f"reviewed-openscience-base={base}", "--tag", tag, str(context),
        ], check=True)
    image_id, purpose, version, image_base = docker_output(
        "image", "inspect", "--format",
        '{{.Id}} {{index .Config.Labels "org.nexpoly.purpose"}} '
        '{{index .Config.Labels "org.nexpoly.template-version"}} {{index .Config.Labels "org.nexpoly.expected-base-image"}}',
        tag,
    ).split()
    if (purpose, version, image_base) != (PURPOSE, TEMPLATE_VERSION, BASE_ID):
        raise ValueError("Built image labels do not match the M1 development template")
    print(json.dumps({"tag": tag, "image_id": image_id, "base_image_id": BASE_ID, "template_version": TEMPLATE_VERSION}))


if __name__ == "__main__":
    main()
