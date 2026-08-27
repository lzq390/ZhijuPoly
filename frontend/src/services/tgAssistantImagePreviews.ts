const DATABASE_NAME = "nexpoly-assistant-previews";
const DATABASE_VERSION = 1;
const STORE_NAME = "tg-message-images";
const SESSION_INDEX = "sessionId";
const PREVIEW_MAX_DIMENSION = 640;
const PREVIEW_MAX_BYTES = 512 * 1024;
const PREVIEW_RETENTION_MS = 7 * 24 * 60 * 60 * 1000;

type StoredPreview = {
  key: string;
  sessionId: string;
  messageId: string;
  thumbnail: Blob;
  createdAt: number;
};

type DecodedImage = {
  source: CanvasImageSource;
  width: number;
  height: number;
  dispose: () => void;
};

let databasePromise: Promise<IDBDatabase> | null = null;

function previewKey(sessionId: string, messageId: string) {
  return `${sessionId}:${messageId}`;
}

function openDatabase() {
  if (databasePromise) return databasePromise;
  databasePromise = new Promise<IDBDatabase>((resolve, reject) => {
    if (typeof indexedDB === "undefined") {
      reject(new Error("IndexedDB is unavailable"));
      return;
    }
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    let settled = false;
    const rejectOnce = (error: Error) => {
      if (settled) return;
      settled = true;
      reject(error);
    };
    request.onupgradeneeded = () => {
      const database = request.result;
      const store = database.objectStoreNames.contains(STORE_NAME)
        ? request.transaction!.objectStore(STORE_NAME)
        : database.createObjectStore(STORE_NAME, { keyPath: "key" });
      if (!store.indexNames.contains(SESSION_INDEX)) {
        store.createIndex(SESSION_INDEX, "sessionId", { unique: false });
      }
    };
    request.onerror = () => rejectOnce(request.error ?? new Error("Unable to open preview database"));
    request.onblocked = () => rejectOnce(new Error("Preview database upgrade is blocked"));
    request.onsuccess = () => {
      const database = request.result;
      if (settled) {
        database.close();
        return;
      }
      settled = true;
      database.onversionchange = () => {
        database.close();
        databasePromise = null;
      };
      resolve(database);
    };
  }).catch((error) => {
    databasePromise = null;
    throw error;
  });
  return databasePromise;
}

function transactionDone(transaction: IDBTransaction) {
  return new Promise<void>((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error ?? new Error("Preview transaction failed"));
    transaction.onabort = () => reject(transaction.error ?? new Error("Preview transaction was aborted"));
  });
}

function requestResult<T>(request: IDBRequest<T>) {
  return new Promise<T>((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("Preview request failed"));
  });
}

async function decodeImage(blob: Blob): Promise<DecodedImage> {
  if (typeof createImageBitmap === "function") {
    try {
      const bitmap = await createImageBitmap(blob);
      if (bitmap.width > 0 && bitmap.height > 0) {
        return {
          source: bitmap,
          width: bitmap.width,
          height: bitmap.height,
          dispose: () => bitmap.close()
        };
      }
      bitmap.close();
    } catch {
      // Older browser engines may expose createImageBitmap but reject valid files.
    }
  }

  const objectUrl = URL.createObjectURL(blob);
  const image = new Image();
  try {
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve();
      image.onerror = () => reject(new Error("Image preview decoding failed"));
      image.src = objectUrl;
    });
    if (!image.naturalWidth || !image.naturalHeight) {
      throw new Error("Image preview has invalid dimensions");
    }
    return {
      source: image,
      width: image.naturalWidth,
      height: image.naturalHeight,
      dispose: () => URL.revokeObjectURL(objectUrl)
    };
  } catch (error) {
    URL.revokeObjectURL(objectUrl);
    throw error;
  }
}

function encodeCanvas(canvas: HTMLCanvasElement, quality: number) {
  return new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob?.size) resolve(blob);
      else reject(new Error("Image preview encoding failed"));
    }, "image/webp", quality);
  });
}

export async function createTgAssistantImageThumbnail(source: Blob) {
  const decoded = await decodeImage(source);
  try {
    let scale = Math.min(1, PREVIEW_MAX_DIMENSION / Math.max(decoded.width, decoded.height));
    let quality = 0.9;
    for (let attempt = 0; attempt < 6; attempt += 1) {
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(decoded.width * scale));
      canvas.height = Math.max(1, Math.round(decoded.height * scale));
      const context = canvas.getContext("2d");
      if (!context) throw new Error("Canvas is unavailable for image preview");
      context.fillStyle = "#ffffff";
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(decoded.source, 0, 0, canvas.width, canvas.height);
      const thumbnail = await encodeCanvas(canvas, quality);
      if (thumbnail.size <= PREVIEW_MAX_BYTES) return thumbnail;
      scale *= 0.78;
      quality = Math.max(0.62, quality - 0.08);
    }
    throw new Error("Image preview exceeds the storage limit");
  } finally {
    decoded.dispose();
  }
}

export async function saveTgAssistantImagePreview(
  sessionId: string,
  messageId: string,
  source: Blob
) {
  const thumbnail = await createTgAssistantImageThumbnail(source);
  const database = await openDatabase();
  const transaction = database.transaction(STORE_NAME, "readwrite");
  transaction.objectStore(STORE_NAME).put({
    key: previewKey(sessionId, messageId),
    sessionId,
    messageId,
    thumbnail,
    createdAt: Date.now()
  } satisfies StoredPreview);
  await transactionDone(transaction);
}

export async function loadTgAssistantImagePreview(sessionId: string, messageId: string) {
  const database = await openDatabase();
  const transaction = database.transaction(STORE_NAME, "readonly");
  const record = await requestResult(
    transaction.objectStore(STORE_NAME).get(previewKey(sessionId, messageId))
  ) as StoredPreview | undefined;
  await transactionDone(transaction);
  return record?.thumbnail instanceof Blob ? record.thumbnail : null;
}

export async function deleteTgAssistantImagePreview(sessionId: string, messageId: string) {
  const database = await openDatabase();
  const transaction = database.transaction(STORE_NAME, "readwrite");
  transaction.objectStore(STORE_NAME).delete(previewKey(sessionId, messageId));
  await transactionDone(transaction);
}

export async function clearTgAssistantImagePreviews(sessionId: string) {
  const database = await openDatabase();
  const transaction = database.transaction(STORE_NAME, "readwrite");
  const request = transaction.objectStore(STORE_NAME).index(SESSION_INDEX).openCursor(IDBKeyRange.only(sessionId));
  request.onsuccess = () => {
    const cursor = request.result;
    if (!cursor) return;
    cursor.delete();
    cursor.continue();
  };
  await transactionDone(transaction);
}

export async function pruneExpiredTgAssistantImagePreviews(now = Date.now()) {
  const database = await openDatabase();
  const transaction = database.transaction(STORE_NAME, "readwrite");
  const request = transaction.objectStore(STORE_NAME).openCursor();
  const cutoff = now - PREVIEW_RETENTION_MS;
  request.onsuccess = () => {
    const cursor = request.result;
    if (!cursor) return;
    const record = cursor.value as Partial<StoredPreview>;
    if (typeof record.createdAt !== "number" || record.createdAt < cutoff) cursor.delete();
    cursor.continue();
  };
  await transactionDone(transaction);
}
