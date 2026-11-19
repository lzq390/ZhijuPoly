// Match the upload control in the pinned November build to the formats indexed by the private service.
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

export const knowledgeChunk = "/assets/session-DxSaVjPe.js";
export async function privateKnowledgeChunk(uiRoot) {
  const original = await readFile(resolve(uiRoot, "." + knowledgeChunk), "utf8");
  if (
    createHash("sha256").update(original).digest("hex") !==
    "8c8ee4ab20b67c9476428ab865bb6b7809ff9145aec17b238be6f42a1f3718df"
  )
    throw Error("Unexpected development knowledge interface");
  return original
    .replace("accept=.txt,.pdf", "accept=.txt,.md")
    .replace("txt|pdf", "txt|md")
    .replaceAll("仅支持 .txt 和 .pdf 文件", "仅支持 UTF-8 编码的 .txt 和 .md 文件")
    .replace("支持 .txt 和 .pdf 格式", "支持 UTF-8 编码的 .txt 和 .md，每个文件最多 256 KiB");
}
