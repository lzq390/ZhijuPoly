import { randomId } from "./randomId";

const LOCK_NAME = "nexpoly-auth-mutation";
export const AUTH_MUTEX_DATABASE = "nexpoly-auth-coordination";
const STORE = "locks";
const WAIT_LIMIT_MS = 15_000;
const POLL_MS = 100;
const unavailable = () => new Error("浏览器无法协调账号切换，请允许此站点使用浏览器存储后重试。");
const occupied = () => new Error("另一页面的账号操作尚未完成，请稍后重试。若所有页面都已关闭后仍出现此提示，请关闭本站全部页面，清除此站点的浏览器数据后重新打开。");
const cancelled = () => new DOMException("账号操作已取消。", "AbortError");
function checkSignal(signal?: AbortSignal) { if (signal?.aborted) throw cancelled(); }

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") { reject(unavailable()); return; }
    let request: IDBOpenDBRequest;
    try { request = indexedDB.open(AUTH_MUTEX_DATABASE, 1); }
    catch { reject(unavailable()); return; }
    let settled = false;
    const fail = () => { if (!settled) { settled = true; clearTimeout(timer); reject(unavailable()); } };
    const timer = setTimeout(fail, 5_000);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE);
    request.onerror = fail;
    request.onblocked = fail;
    request.onsuccess = () => {
      clearTimeout(timer);
      if (settled) { request.result.close(); return; }
      settled = true;
      const db = request.result;
      db.onversionchange = () => db.close();
      resolve(db);
    };
  });
}

/** An IDB readwrite transaction is serialized across tabs, unlike localStorage. */
function claim(db: IDBDatabase, token: string, release: boolean): Promise<boolean> {
  return new Promise((resolve, reject) => {
    let transaction: IDBTransaction;
    try { transaction = db.transaction(STORE, "readwrite"); }
    catch { reject(unavailable()); return; }
    let changed = false;
    transaction.oncomplete = () => resolve(changed);
    transaction.onabort = () => reject(unavailable());
    transaction.onerror = () => { /* onabort reports the final transaction result. */ };
    const store = transaction.objectStore(STORE);
    const request = store.get(LOCK_NAME);
    request.onsuccess = () => {
      if (release ? request.result === token : request.result === undefined) {
        if (release) store.delete(LOCK_NAME);
        else store.put(token, LOCK_NAME);
        changed = true;
      }
    };
  });
}

function pause(signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const complete = () => { signal?.removeEventListener("abort", abort); resolve(); };
    const timer = setTimeout(complete, POLL_MS);
    const abort = () => { clearTimeout(timer); signal?.removeEventListener("abort", abort); reject(cancelled()); };
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) abort();
  });
}

/**
 * Serialize Cookie-changing requests across all tabs of this origin.
 *
 * HTTP does not expose Web Locks. Its fallback commits an IDB claim BEFORE
 * sending credentials and deletes it only AFTER the complete request settles.
 * Claims deliberately have no expiry: a frozen/crashed tab must never let a
 * second login race its late Set-Cookie. An orphan claim requires closing all
 * site tabs and clearing site data. Only opaque random IDs are stored here.
 *
 * Cancellation stops queued work. After dispatch the caller must await the
 * actual network response before settling `work`, even when its UI is retired.
 */
export async function withAuthMutationLock<T>(work: () => Promise<T>, signal?: AbortSignal): Promise<T> {
  checkSignal(signal);
  if (typeof navigator !== "undefined" && navigator.locks?.request) {
    return navigator.locks.request(LOCK_NAME, { signal }, async () => {
      checkSignal(signal);
      return work();
    });
  }
  const token = randomId();
  const db = await openDatabase();
  const deadline = Date.now() + WAIT_LIMIT_MS;
  let acquired = false;
  try {
    while (!acquired) {
      checkSignal(signal);
      acquired = await claim(db, token, false);
      if (!acquired) {
        if (Date.now() >= deadline) throw occupied();
        await pause(signal);
      }
    }
    checkSignal(signal);
    return await work();
  } finally {
    try {
      if (acquired && !await claim(db, token, true)) throw unavailable();
    } finally { db.close(); }
  }
}
