import assert from "node:assert/strict";
import { inflateRawSync } from "node:zlib";

export function inspectZip(bytes) {
  const end = bytes.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
  assert.ok(end >= 0 && end + 22 <= bytes.length, "Download contains a complete ZIP directory");
  const count = bytes.readUInt16LE(end + 10);
  assert.ok(count > 0, "ZIP contains at least one file");
  const files = [];
  let offset = bytes.readUInt32LE(end + 16);
  for (let index = 0; index < count; index++) {
    assert.equal(bytes.readUInt32LE(offset), 0x02014b50, "Valid ZIP central directory entry");
    const method = bytes.readUInt16LE(offset + 10);
    const crc = bytes.readUInt32LE(offset + 16);
    const compressedSize = bytes.readUInt32LE(offset + 20);
    const size = bytes.readUInt32LE(offset + 24);
    const nameLength = bytes.readUInt16LE(offset + 28);
    const name = bytes.subarray(offset + 46, offset + 46 + nameLength).toString("utf8");
    assert.ok(!name.startsWith("/") && !name.includes("\\") && !/^[A-Za-z]:/.test(name) && !name.includes("\0") && !name.split("/").includes(".."), "Safe download archive path");
    const local = bytes.readUInt32LE(offset + 42);
    assert.equal(bytes.readUInt32LE(local), 0x04034b50, "Valid ZIP local file entry");
    const start = local + 30 + bytes.readUInt16LE(local + 26) + bytes.readUInt16LE(local + 28);
    const compressed = bytes.subarray(start, start + compressedSize);
    assert.ok([0, 8].includes(method), "Supported ZIP compression");
    const content = method === 8 ? inflateRawSync(compressed) : compressed;
    assert.equal(content.length, size, `Complete ZIP member ${name}`);
    let actualCrc = 0xffffffff;
    for (const byte of content) {
      actualCrc ^= byte;
      for (let bit = 0; bit < 8; bit++) actualCrc = (actualCrc >>> 1) ^ (actualCrc & 1 ? 0xedb88320 : 0);
    }
    assert.equal((actualCrc ^ 0xffffffff) >>> 0, crc, `ZIP CRC matches ${name}`);
    files.push({ name, bytes: content.length });
    offset += 46 + nameLength + bytes.readUInt16LE(offset + 30) + bytes.readUInt16LE(offset + 32);
  }
  return files;
}
