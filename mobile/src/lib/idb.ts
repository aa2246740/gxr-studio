// 校准数据存在这台 iPhone 的浏览器里（IndexedDB），加到主屏幕后重开还在。只存用户自己导入的那个 zip。

const DB = 'gxr-mobile';
const DB_VERSION = 2;
const STORE = 'calib';
const LOGOS_STORE = 'logos';
const KEY = 'pack';

export interface StoredPack {
  zip: ArrayBuffer;
  name: string;
  savedAt: number;
}

export interface StoredLogo {
  id: string;
  name: string;
  w: number;
  h: number;
  coverage: number;
  opaque: boolean;
  lum: number;
  rgba: ArrayBuffer;
  createdAt: number;
}

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB, DB_VERSION);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE)) request.result.createObjectStore(STORE);
      if (!request.result.objectStoreNames.contains(LOGOS_STORE)) {
        request.result.createObjectStore(LOGOS_STORE, { keyPath: 'id' });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('IndexedDB'));
  });
}

async function run<T>(mode: IDBTransactionMode, work: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await open();
  try {
    return await new Promise<T>((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const request = work(tx.objectStore(STORE));
      tx.oncomplete = () => resolve(request.result);
      tx.onerror = () => reject(tx.error ?? new Error('IndexedDB'));
      tx.onabort = () => reject(tx.error ?? new Error('IndexedDB'));
    });
  } finally {
    db.close();
  }
}

/** 删除也记一个时间（墓碑）。晚到的旧导入看见更新的记录就不写，删掉的包不会在下次打开时回来。 */
interface Tomb {
  zip: null;
  name: '';
  savedAt: number;
}

export async function loadPack(): Promise<StoredPack | null> {
  if (typeof indexedDB === 'undefined') return null;
  try {
    const row = (await run('readonly', (store) => store.get(KEY))) as StoredPack | Tomb | undefined;
    return row && row.zip instanceof ArrayBuffer && typeof row.name === 'string' ? (row as StoredPack) : null;
  } catch {
    return null;
  }
}

/**
 * 版本水位：见过的最高版本（导入和删除墓碑都算）单独存一个键。新会话从它之上开始编号，
 * 时钟回拨或者墓碑比现在的时钟还新，都不会让新的导入被当成旧的（Sol r2）。
 */
const WATERMARK = 'watermark';

function watermarkOf(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

/** 同一个事务里先读再写：水位（或者记录本身）已经不比它旧，就什么都不做。写了返回 true。 */
async function putIfNewer(row: StoredPack | Tomb): Promise<boolean> {
  const db = await open();
  try {
    return await new Promise<boolean>((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      const store = tx.objectStore(STORE);
      let wrote = false;
      const mark = store.get(WATERMARK);
      mark.onsuccess = () => {
        try {
          const record = store.get(KEY);
          record.onsuccess = () => {
            try {
              const now = record.result as { savedAt?: unknown } | undefined;
              const high = Math.max(watermarkOf(mark.result), watermarkOf(now?.savedAt));
              if (high >= row.savedAt) return;
              store.put(row, KEY);
              store.put(row.savedAt, WATERMARK);
              wrote = true;
            } catch (err) {
              try {
                tx.abort();
              } catch {
                /* transaction may already be aborted */
              }
              reject(err);
            }
          };
        } catch (err) {
          try {
            tx.abort();
          } catch {
            /* transaction may already be aborted */
          }
          reject(err);
        }
      };
      tx.oncomplete = () => resolve(wrote);
      tx.onerror = () => reject(tx.error ?? new Error('IndexedDB'));
      tx.onabort = () => reject(tx.error ?? new Error('IndexedDB'));
    });
  } finally {
    db.close();
  }
}

/** 已经存下来的最高版本；没有存过是 0。只读这个数字，不把整个 zip 读进主线程。 */
export async function loadWatermark(): Promise<number> {
  if (typeof indexedDB === 'undefined') return 0;
  return watermarkOf(await run('readonly', (store) => store.get(WATERMARK)));
}

/** 写进去返回 true；水位已经不比它旧、没写返回 false；事务失败抛错。 */
export async function savePack(pack: StoredPack): Promise<boolean> {
  const wrote = await putIfNewer(pack);
  // 主屏幕网页的存储一般不会被清；再请求一次长期保留，被拒也没关系。
  void navigator.storage?.persist?.().catch(() => false);
  return wrote;
}

/** 写进墓碑返回 true；已经有更新的记录（没写）返回 false；事务失败抛错。 */
export async function deletePack(at: number): Promise<boolean> {
  if (typeof indexedDB === 'undefined') return true;
  return putIfNewer({ zip: null, name: '', savedAt: at });
}

export async function loadAllLogos(): Promise<StoredLogo[]> {
  if (typeof indexedDB === 'undefined') return [];
  const db = await open();
  try {
    const rows = await new Promise<StoredLogo[]>((resolve, reject) => {
      const tx = db.transaction(LOGOS_STORE, 'readonly');
      const req = tx.objectStore(LOGOS_STORE).getAll();
      tx.oncomplete = () => resolve((req.result as StoredLogo[]) ?? []);
      tx.onerror = () => reject(tx.error ?? new Error('IndexedDB'));
      tx.onabort = () => reject(tx.error ?? new Error('IndexedDB'));
    });
    return rows.sort((a, b) => a.createdAt - b.createdAt);
  } catch {
    return [];
  } finally {
    db.close();
  }
}

export async function putStoredLogo(logo: StoredLogo, maxLogos = 12, signal?: AbortSignal): Promise<boolean> {
  if (typeof indexedDB === 'undefined' || signal?.aborted) return false;
  const db = await open();
  if (signal?.aborted) {
    db.close();
    return false;
  }
  try {
    return await new Promise<boolean>((resolve, reject) => {
      const tx = db.transaction(LOGOS_STORE, 'readwrite');
      const store = tx.objectStore(LOGOS_STORE);
      let wrote = false;
      let abortedBySignal = false;
      const onAbort = () => {
        abortedBySignal = true;
        try {
          tx.abort();
        } catch {
          /* transaction may already be finished */
        }
      };
      signal?.addEventListener('abort', onAbort, { once: true });
      const cleanup = () => {
        signal?.removeEventListener('abort', onAbort);
      };
      const allReq = store.getAll();
      allReq.onsuccess = () => {
        try {
          if (signal?.aborted || abortedBySignal) {
            onAbort();
            return;
          }
          const list = (allReq.result as StoredLogo[]) ?? [];
          const exists = list.some((row) => row.id === logo.id);
          if (!exists && list.length >= maxLogos) return;
          store.put(logo);
          wrote = true;
        } catch (err) {
          cleanup();
          try {
            tx.abort();
          } catch {
            /* transaction may already be aborted */
          }
          reject(err);
        }
      };
      tx.oncomplete = () => {
        cleanup();
        resolve(wrote);
      };
      tx.onerror = () => {
        cleanup();
        if (abortedBySignal || signal?.aborted) {
          reject(new DOMException('Aborted', 'AbortError'));
          return;
        }
        reject(tx.error ?? new Error('IndexedDB'));
      };
      tx.onabort = () => {
        cleanup();
        if (abortedBySignal || signal?.aborted) {
          reject(new DOMException('Aborted', 'AbortError'));
          return;
        }
        reject(tx.error ?? new Error('IndexedDB'));
      };
    });
  } finally {
    db.close();
  }
}

export async function renameStoredLogo(id: string, name: string): Promise<boolean> {
  if (typeof indexedDB === 'undefined') return false;
  const db = await open();
  try {
    return await new Promise<boolean>((resolve, reject) => {
      const tx = db.transaction(LOGOS_STORE, 'readwrite');
      const store = tx.objectStore(LOGOS_STORE);
      let wrote = false;
      const req = store.get(id);
      req.onsuccess = () => {
        try {
          const row = req.result as StoredLogo | undefined;
          if (!row) return;
          store.put({ ...row, name });
          wrote = true;
        } catch (err) {
          try {
            tx.abort();
          } catch {
            /* transaction may already be aborted */
          }
          reject(err);
        }
      };
      tx.oncomplete = () => resolve(wrote);
      tx.onerror = () => reject(tx.error ?? new Error('IndexedDB'));
      tx.onabort = () => reject(tx.error ?? new Error('IndexedDB'));
    });
  } finally {
    db.close();
  }
}

export async function deleteStoredLogo(id: string): Promise<void> {
  if (typeof indexedDB === 'undefined') return;
  const db = await open();
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(LOGOS_STORE, 'readwrite');
      tx.objectStore(LOGOS_STORE).delete(id);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error ?? new Error('IndexedDB'));
      tx.onabort = () => reject(tx.error ?? new Error('IndexedDB'));
    });
  } finally {
    db.close();
  }
}
