import { assertSessionEpoch, getSessionEpoch, onSessionRetired, privateFetch } from "../auth/session";
import type { PolymerMdArtifact } from "../types/polymerMd";
import { POLYMER_MD_API_BASE, POLYMER_MD_MEMORY_DOWNLOAD_LIMIT, PolymerMdApiError } from "./polymerMdApi";

type DownloadWriter = { write(chunk: Uint8Array): Promise<void>; close(): Promise<void>; abort(): Promise<void> };
type SavePicker = (options: { suggestedName: string; types: Array<{ description: string; accept: Record<string, string[]> }> }) => Promise<{ createWritable(): Promise<DownloadWriter> }>;
const encoder = new TextEncoder();
const MAX_UINT64 = 0xffffffffffffffffn;
const ZIP_END_SIZE = 98n; // ZIP64 end (56), locator (20), classic end (22).
const memoryMessage = "此浏览器仅支持最多 256 MiB 的整包下载，请使用支持流式文件保存的桌面 Chrome 或 Edge。";
const crcTable = Uint32Array.from({ length: 256 }, (_, index) => {
  let value = index;
  for (let bit = 0; bit < 8; bit++) value = value & 1 ? 0xedb88320 ^ value >>> 1 : value >>> 1;
  return value >>> 0;
});
function crcUpdate(crc: number, bytes: Uint8Array) {
  for (const byte of bytes) crc = crcTable[(crc ^ byte) & 255] ^ crc >>> 8;
  return crc >>> 0;
}
function record(length: number, signature: number) {
  const bytes = new Uint8Array(length), view = new DataView(bytes.buffer);
  view.setUint32(0, signature, true);
  return { bytes, view };
}

/** Pure records permit ZIP64 boundary checks without allocating gigabytes. */
export function createPolymerMdZip64Entry(name: string, size: bigint, offset: bigint, crc: number) {
  const filename = encoder.encode(name);
  if (filename.length > 65535 || size < 0n || size > MAX_UINT64 || offset < 0n || offset > MAX_UINT64) throw new Error("产物过大或文件名过长，无法生成 ZIP 归档。");
  const local = record(50 + filename.length, 0x04034b50);
  local.view.setUint16(4, 45, true);
  local.view.setUint16(6, 0x0808, true); // UTF-8 + data descriptor; STORE method is zero.
  local.view.setUint16(12, 0x21, true); // Stable DOS date: 1980-01-01.
  local.view.setUint32(18, 0xffffffff, true);
  local.view.setUint32(22, 0xffffffff, true);
  local.view.setUint16(26, filename.length, true);
  local.view.setUint16(28, 20, true);
  local.bytes.set(filename, 30);
  const extra = 30 + filename.length;
  local.view.setUint16(extra, 1, true);
  local.view.setUint16(extra + 2, 16, true);
  local.view.setBigUint64(extra + 4, size, true);
  local.view.setBigUint64(extra + 12, size, true);

  const descriptor = record(24, 0x08074b50);
  descriptor.view.setUint32(4, crc, true);
  descriptor.view.setBigUint64(8, size, true);
  descriptor.view.setBigUint64(16, size, true);

  const central = record(74 + filename.length, 0x02014b50);
  central.view.setUint16(4, 45, true);
  central.view.setUint16(6, 45, true);
  central.view.setUint16(8, 0x0808, true);
  central.view.setUint16(14, 0x21, true);
  central.view.setUint32(16, crc, true);
  central.view.setUint32(20, 0xffffffff, true);
  central.view.setUint32(24, 0xffffffff, true);
  central.view.setUint16(28, filename.length, true);
  central.view.setUint16(30, 28, true);
  central.view.setUint32(42, 0xffffffff, true);
  central.bytes.set(filename, 46);
  const centralExtra = 46 + filename.length;
  central.view.setUint16(centralExtra, 1, true);
  central.view.setUint16(centralExtra + 2, 24, true);
  central.view.setBigUint64(centralExtra + 4, size, true);
  central.view.setBigUint64(centralExtra + 12, size, true);
  central.view.setBigUint64(centralExtra + 20, offset, true);
  return { local: local.bytes, descriptor: descriptor.bytes, central: central.bytes };
}
function zipEnd(count: number, centralSize: bigint, centralOffset: bigint) {
  const end = record(Number(ZIP_END_SIZE), 0x06064b50);
  end.view.setBigUint64(4, 44n, true);
  end.view.setUint16(12, 45, true);
  end.view.setUint16(14, 45, true);
  end.view.setBigUint64(24, BigInt(count), true);
  end.view.setBigUint64(32, BigInt(count), true);
  end.view.setBigUint64(40, centralSize, true);
  end.view.setBigUint64(48, centralOffset, true);
  end.view.setUint32(56, 0x07064b50, true);
  end.view.setBigUint64(64, centralOffset + centralSize, true);
  end.view.setUint32(72, 1, true);
  end.view.setUint32(76, 0x06054b50, true);
  end.view.setUint16(84, 0xffff, true);
  end.view.setUint16(86, 0xffff, true);
  end.view.setUint32(88, 0xffffffff, true);
  end.view.setUint32(92, 0xffffffff, true);
  return end.bytes;
}
function safeSegment(raw: string, fallback: string) {
  let value = raw.normalize("NFC").replace(/[\\/<>:"|?*\x00-\x1f\x7f]/g, "_").replace(/^[. ]+|[. ]+$/g, "").slice(0, 1000);
  if (!value) value = fallback;
  if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(value)) value = `_${value}`;
  return value;
}
function prepareEntries(artifacts: PolymerMdArtifact[]) {
  const used = new Set<string>();
  return artifacts.map((artifact, index) => {
    if (!artifact.artifact_id || !Number.isSafeInteger(artifact.size_bytes) || artifact.size_bytes < 0) throw new Error("产物大小记录不完整，请刷新任务后重试。");
    const folder = safeSegment(artifact.stage_run_id, "stage"), filename = safeSegment(artifact.filename, `artifact-${index + 1}`);
    let name = `${folder}/${filename}`, suffix = 1;
    while (used.has(name.toLowerCase())) {
      const dot = filename.lastIndexOf("."), split = dot > 0 ? dot : filename.length;
      name = `${folder}/${filename.slice(0, split)} (${++suffix})${filename.slice(split)}`;
    }
    used.add(name.toLowerCase());
    return { artifact: { ...artifact }, name, size: BigInt(artifact.size_bytes) };
  });
}

/** Saves one complete ZIP64 archive; no backend bundle endpoint or compression. */
export async function downloadPolymerMdArtifacts(id: string, artifacts: PolymerMdArtifact[], onProgress?: (completed: number, total: number) => void, signal?: AbortSignal): Promise<void> {
  if (!artifacts.length) throw new Error("当前没有可下载的产物。");
  const epoch = getSessionEpoch(), entries = prepareEntries(artifacts);
  const filename = `polymer-md-${safeSegment(id, "job")}-artifacts.zip`;
  const picker = (window as Window & { showSaveFilePicker?: SavePicker }).showSaveFilePicker;
  const expectedSize = entries.reduce((total, entry) => total + entry.size + BigInt(148 + 2 * encoder.encode(entry.name).length), ZIP_END_SIZE);
  if (expectedSize > MAX_UINT64) throw new Error("产物总大小超出 ZIP64 支持范围。");
  if (!picker && expectedSize > BigInt(POLYMER_MD_MEMORY_DOWNLOAD_LIMIT)) throw new Error(memoryMessage);
  const controller = new AbortController();
  let writer: DownloadWriter | null = null, reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  let writerAbort: Promise<void> | undefined, writerAborted = false;
  const stopWriter = (): Promise<void> => {
    if (!writer || writerAborted) return Promise.resolve();
    if (writerAbort) return writerAbort;
    const stopping = writer.abort().then(() => { writerAborted = true; }).catch(() => {}).finally(() => { writerAbort = undefined; });
    writerAbort = stopping;
    return stopping;
  };
  const abort = () => { controller.abort(); if (reader) void reader.cancel().catch(() => {}); if (writer) void stopWriter(); };
  const retire = onSessionRetired(abort);
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) abort();
  const check = () => { assertSessionEpoch(epoch); if (controller.signal.aborted) throw new DOMException("下载已取消。", "AbortError"); };
  const wait = <T,>(operation: Promise<T>) => new Promise<T>((resolve, reject) => {
    const stopped = () => reject(new DOMException("下载已取消。", "AbortError"));
    controller.signal.addEventListener("abort", stopped, { once: true });
    if (controller.signal.aborted) stopped();
    operation.then(resolve, reject).finally(() => controller.signal.removeEventListener("abort", stopped));
  });
  const chunks: Uint8Array<ArrayBuffer>[] = [], directory: Uint8Array[] = [];
  let offset = 0n;
  const write = async (bytes: Uint8Array) => {
    check();
    const nextOffset = offset + BigInt(bytes.byteLength);
    if (nextOffset > MAX_UINT64) throw new Error("产物总大小超出 ZIP64 支持范围。");
    if (writer) {
      const writing = writer.write(bytes);
      // FileSystemWritableFileStream.write temporarily locks its stream. An
      // abort during that interval can reject; retry after this write settles.
      const finish = async () => {
        if (controller.signal.aborted) { await stopWriter(); if (!writerAborted) await stopWriter(); }
      };
      void writing.then(finish, finish);
      await wait(writing);
    }
    else {
      if (nextOffset > BigInt(POLYMER_MD_MEMORY_DOWNLOAD_LIMIT)) throw new Error(memoryMessage);
      chunks.push(Uint8Array.from(bytes));
    }
    check();
    offset = nextOffset;
  };
  try {
    check();
    // Keep this invocation before the first await/network call: picker activation
    // belongs to the original user click, including when many files are saved.
    if (picker) {
      const handle = await wait(picker.call(window, { suggestedName: filename, types: [{ description: "ZIP 归档", accept: { "application/zip": [".zip"] } }] }));
      check();
      const opening = handle.createWritable().then(value => {
        writer = value;
        if (controller.signal.aborted) void stopWriter();
        return value;
      });
      writer = await wait(opening);
      check();
    }
    onProgress?.(0, entries.length);
    for (let index = 0; index < entries.length; index++) {
      check();
      const { artifact, name, size } = entries[index], start = offset;
      const response = await wait(privateFetch(`${POLYMER_MD_API_BASE}/jobs/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(artifact.artifact_id)}`, { signal: controller.signal }));
      check();
      // Acquiring the reader also gives the private transport a cleanup path for
      // rejected HTTP responses, without buffering an untrusted error body.
      if (response.body) reader = response.body.getReader();
      if (!response.ok || response.status === 206) throw new PolymerMdApiError(response.status, "产物下载失败，请刷新任务后重试。");
      if (!reader) throw new Error("产物下载内容为空，请刷新任务后重试。");
      const length = response.headers.get("Content-Length");
      if (length !== null && !response.headers.get("Content-Encoding") && (!/^\d+$/.test(length) || BigInt(length) !== size)) throw new Error("下载大小与产物记录不符，请刷新任务后重试。");
      await write(createPolymerMdZip64Entry(name, size, start, 0).local);
      let received = 0n, crc = 0xffffffff;
      while (true) {
        check();
        const { done, value } = await wait(reader.read());
        check();
        if (done) break;
        received += BigInt(value.byteLength);
        // Enforce the archive budget as well as each recorded file length, even
        // if a server sends more bytes than its advertised Content-Length.
        if (!writer && offset + BigInt(value.byteLength) > BigInt(POLYMER_MD_MEMORY_DOWNLOAD_LIMIT)) throw new Error(memoryMessage);
        if (received > size) throw new Error("下载大小与产物记录不符，请刷新任务后重试。");
        crc = crcUpdate(crc, value);
        await write(value);
      }
      if (received !== size) throw new Error("下载未完整接收，请刷新任务后重试。");
      reader.releaseLock();
      reader = null;
      const records = createPolymerMdZip64Entry(name, size, start, (crc ^ 0xffffffff) >>> 0);
      await write(records.descriptor);
      directory.push(records.central);
      if (index + 1 < entries.length) onProgress?.(index + 1, entries.length);
    }
    const centralOffset = offset;
    for (const entry of directory) await write(entry);
    await write(zipEnd(entries.length, offset - centralOffset, centralOffset));
    check();
    if (writer) {
      await wait(writer.close());
      check();
      writer = null;
    } else {
      const blob = new Blob(chunks, { type: "application/zip" });
      check();
      const url = URL.createObjectURL(blob);
      try {
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.append(link);
        try { check(); link.click(); } finally { link.remove(); }
      } finally { URL.revokeObjectURL(url); }
    }
    check();
    onProgress?.(entries.length, entries.length);
  } catch (error) {
    abort();
    if (writer) await stopWriter();
    throw error;
  } finally {
    retire();
    signal?.removeEventListener("abort", abort);
    if (reader) { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  }
}
