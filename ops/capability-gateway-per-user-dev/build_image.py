"""Build the M3 gateway offline from the verified local 11 November release."""

import json
from pathlib import Path
import subprocess
import tempfile
from uuid import uuid4

BASE = "sha256:fb7fdd70e46d7544d0816f042bd0a4324cd7a6c1e86a4a4606031f5108199df0"
HERE = Path(__file__).resolve().parent
DOCKER = ["docker", "--context", "default"]


def output(*args):
    return subprocess.check_output([*DOCKER, *args], text=True).strip()


def main():
    if not output("context", "inspect", "default", "--format", "{{.Endpoints.docker.Host}}").startswith(
        "unix://"
    ):
        raise ValueError("The M3 gateway requires a local Docker Engine")
    tag = "nexpoly-openscience-per-user-gateway:91f5d81-" + uuid4().hex
    with tempfile.TemporaryDirectory(prefix="np-m3-gateway-") as tmp:
        directory = Path(tmp)
        base_tar = directory / "source.tar"
        subprocess.run([*DOCKER, "image", "save", "-o", str(base_tar), BASE], check=True)
        subprocess.run(
            ["tar", "-xf", str(base_tar), "-C", tmp, "index.json", "oci-layout", "blobs"], check=True
        )
        (manifest,) = json.loads((directory / "index.json").read_text())["manifests"]
        subprocess.run(
            [
                *DOCKER,
                "buildx",
                "build",
                "--builder",
                "default",
                "--pull=false",
                "--network=none",
                "--load",
                "--provenance=false",
                "--build-context",
                f"reviewed-gateway-base=oci-layout://{directory}@{manifest['digest']}",
                "-t",
                tag,
                str(HERE),
            ],
            check=True,
        )
    image_id = output("image", "inspect", "--format", "{{.Id}}", tag)
    layout = "{{json .RootFS.Layers}}"
    base_layers = json.loads(output("image", "inspect", "--format", layout, BASE))
    layers = json.loads(output("image", "inspect", "--format", layout, image_id))
    if layers[: len(base_layers)] != base_layers:
        raise ValueError("Development gateway does not inherit the reviewed base")
    print(json.dumps({"image_id": image_id, "tag": tag, "base_image_id": BASE}))


if __name__ == "__main__":
    main()
