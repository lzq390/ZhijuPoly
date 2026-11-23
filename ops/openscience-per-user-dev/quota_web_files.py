"""Install the quota-aware file operations for the immutable development workspace."""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile

PREFIX = "/app/backend/cli/src/"
TARGETS = {
    "index.ts": PREFIX + "file/index.ts",
    "file.ts": PREFIX + "server/routes/file.ts",
    "quota-file-io.mjs": PREFIX + "file/nexpoly-quota-io.mjs",
}
ORIGINAL = {
    "index.ts": "1bf8e4300a702da5c1a68acb7e459c836758136dc1dc5084738aa667d63465b5",
    "file.ts": "822f436f84a1070bc03e92ec59e3fb1b88833f9d44c4ea96eba41f5b55d00216",
}
PATCHED = {
    "index.ts": "0f0b0d7e3799f1d1a636b92df5500c5db875543c01bacdc9556faf5df09adc35",
    "file.ts": "ab22c7522fe11ecadf8b4a68addce499a3dbd3360b5261ae5ab9997280226dfe",
}


def replace(source, before, after):
    if source.count(before) != 1:
        raise ValueError("Pinned November file operation changed")
    return source.replace(before, after)


def patch(name, source):
    if name == "index.ts":
        source = ('import { replaceFile, removeRegularFile } from "./nexpoly-quota-io.mjs"\n'
                  'import { workspacePath } from "../server/nexpoly-dev-boundary.mjs"\n' + source)
        source = replace(source, "await Bun.write(full, content)", "await replaceFile(await workspacePath(Instance.directory, full), content)")
        anchor = "  export async function list("
        return replace(source, anchor, '''  export async function remove(file: string) {
    const full = path.join(Instance.directory, file)
    if (!Instance.containsPath(full)) throw new Error("Access denied")
    await removeRegularFile(full)
    await Bus.publish(FileWatcher.Event.Updated, { file: full, event: "unlink" })
  }

''' + anchor)
    # The instance's existing devBoundary validates the selected workspace and
    # the JSON path for all non-GET methods, including these two routes.
    source = 'import { constants } from "node:os"\n' + source
    source = replace(source, '''        const content = await File.write(body.path, body.content)
        return c.json(content)''', '''        try {
          return c.json(await File.write(body.path, body.content))
        } catch (error) {
          if (error?.code === "EDQUOT" || Math.abs(error?.errno) === constants.errno.EDQUOT)
            return c.json({ error: "workspace_quota_exceeded" }, 507)
          if (error?.code === "ENOSPC") return c.json({ error: "workspace_disk_full" }, 507)
          throw error
        }''')
    anchor = '    .get(\n      "/file/status",'
    return replace(source, anchor, '''    .delete(
      "/file/content",
      validator("json", z.object({ path: z.string().min(1) })),
      async (c) => {
        try {
          await File.remove(c.req.valid("json").path)
          return c.json({ deleted: true })
        } catch (error) {
          if (error?.code === "ENOENT") return c.json({ error: "file_missing" }, 404)
          if (error?.code === "EINVAL") return c.json({ error: "regular_file_required" }, 400)
          throw error
        }
      },
    )
''' + anchor)


def overlay_path(registry_root, image_id):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise ValueError("Expected a local immutable image ID")
    root = Path(registry_root).resolve()
    target = root / ("quota-web-code-" + image_id.removeprefix("sha256:"))
    if not target.exists() and not target.is_symlink():
        # Keep active bind sources and the original rollback image addressable.
        legacy = root / "quota-web-code"
        if legacy.exists() or legacy.is_symlink():
            if overlay_image(legacy) == image_id:
                return legacy
    return target


def overlay_image(directory):
    marker = directory / "image-id"
    if directory.is_symlink() or marker.is_symlink() or not marker.is_file():
        raise ValueError("Unexpected quota overlay image marker")
    value = marker.read_text().strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
        raise ValueError("Invalid quota overlay image marker")
    return value


def prepare(registry_root, image_id):
    target = overlay_path(registry_root, image_id)
    if not target.exists() and not target.is_symlink():
        root = target.parent
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            code = Path(temporary) / "code"
            code.mkdir()
            for name, digest in ORIGINAL.items():
                raw = subprocess.check_output([
                    "docker", "run", "--rm", "--pull=never", "--network", "none", "--read-only",
                    "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user", "65534:65534",
                    "--entrypoint", "cat", image_id, TARGETS[name],
                ])
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise ValueError("Unexpected November source: " + name)
                (code / name).write_text(patch(name, raw.decode()))
            (code / "quota-file-io.mjs").write_bytes(Path(__file__).with_name("quota-file-io.mjs").read_bytes())
            (code / "image-id").write_text(image_id + "\n")
            for path in code.iterdir():
                path.chmod(0o444)
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            verified_mounts(code, image_id)
            sync_directory(code)
            code.rename(target)
            sync_directory(root)
    return verified_mounts(target, image_id)


def mounts(registry_root, image_id):
    return verified_mounts(overlay_path(registry_root, image_id), image_id)


def sync_directory(directory):
    fd = os.open(directory, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verified_mounts(root, image_id):
    if overlay_image(root) != image_id:
        raise ValueError("Quota file overlay belongs to a different workspace image")
    expected = {**PATCHED, "quota-file-io.mjs": hashlib.sha256(Path(__file__).with_name("quota-file-io.mjs").read_bytes()).hexdigest()}
    for name, digest in expected.items():
        path = root / name
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Unexpected quota file operation overlay: " + name)
    return {target: root / name for name, target in TARGETS.items()}
