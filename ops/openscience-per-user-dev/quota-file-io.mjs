import { randomUUID } from "node:crypto";
import { chmod, lstat, mkdir, open, rename, unlink } from "node:fs/promises";
import { dirname, join } from "node:path";

async function regularFile(path) {
  const info = await lstat(path);
  if (!info.isFile()) throw Object.assign(new Error("Expected a regular file"), {code: "EINVAL"});
  return info;
}

// The caller has resolved this path against the authenticated workspace. Stage
// bytes on the same quota filesystem. EDQUOT during write or fsync leaves the
// old directory entry (and any concurrent reader's open file) intact.
export async function replaceFile(path, content) {
  let info;
  try {
    info = await regularFile(path);
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
    await mkdir(dirname(path), {recursive: true});
  }
  const staged = join(dirname(path), ".np-write-" + randomUUID());
  const handle = await open(staged, "wx", 0o600);
  try {
    try {
      await handle.writeFile(content);
      await chmod(staged, info ? info.mode & 0o777 : 0o666 & ~process.umask());
      await handle.sync();
    } finally {
      await handle.close();
    }
    await rename(staged, path);
  } finally {
    await unlink(staged).catch(error => { if (error.code !== "ENOENT") throw error; });
  }
}

export async function removeRegularFile(path) {
  await regularFile(path);
  await unlink(path);
}
