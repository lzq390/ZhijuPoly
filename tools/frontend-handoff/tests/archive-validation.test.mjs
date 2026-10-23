import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { inspectZip } from "../overlay/scripts/handoff-archive.mjs";
const bytes = name => readFileSync(join(process.env.HANDOFF_UNIT_ZIP_DIR, name));
test("stored unit archive retains member name and size",()=>assert.deepEqual(inspectZip(bytes("stored.zip")),[{name:"unit.txt",bytes:17}]));
test("deflated unit archive retains nested member name and size",()=>assert.deepEqual(inspectZip(bytes("deflated.zip")),[{name:"nested/unit.txt",bytes:17}]));
for (const [label,name] of [["relative traversal","relative.zip"],["absolute path","absolute.zip"],["Windows backslash path","backslash.zip"],["drive-relative Windows path","drive.zip"],["CRC corruption","crc.zip"],["truncation","truncated.zip"],["empty archive","empty.zip"],["unsupported compression","unsupported.zip"],["NUL member name","nul.zip"]]) {
  test(`archive validation rejects ${label}`,()=>assert.throws(()=>inspectZip(bytes(name))));
}
