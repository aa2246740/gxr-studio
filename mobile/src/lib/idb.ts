// 校准数据、相机配方文件、自定义标志与用户 LUT 存在这台 iPhone 的浏览器里（IndexedDB），加到主屏幕后重开还在。

import { MAX_LUT_LIBRARY_BYTES, type LutRecord } from './lut';

const DB = 'gxr-mobile';
const DB_VERSION = 4;
const STORE = 'calib';
const CAMFILES_STORE = 'camfiles';
const CAMFILES_KEY = 'current';
const LOGOS_STORE = 'logos';
const KEY = 'pack';
const LUT_RECORDS = 'lut_records';
const LUT_BLOBS = 'lut_blobs';
const LUT_META = 'lut_meta';
const LUT_ORDER_KEY = 'order';
const LUT_WATERMARK_KEY = 'watermark';

export interface StoredPack {
  zip: ArrayBuffer;
  name: string;
  savedAt: number;
}

export interface StoredCamFiles {
  dat: ArrayBuffer;
  datName: string;
  library: ArrayBuffer | null;
  generation: number | null;
  libraryHash: string | null;
  catalogId: string | null;
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
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
      if (!db.objectStoreNames.contains(CAMFILES_STORE)) db.createObjectStore(CAMFILES_STORE);
      if (!db.objectStoreNames.contains(LOGOS_STORE)) db.createObjectStore(LOGOS_STORE, { keyPath: 'id' });
      if (!db.objectStoreNames.contains(LUT_RECORDS)) db.createObjectStore(LUT_RECORDS, { keyPath: 'cube_id' });
      if (!db.objectStoreNames.contains(LUT_BLOBS)) db.createObjectStore(LUT_BLOBS);
      if (!db.objectStoreNames.contains(LUT_META)) db.createObjectStore(LUT_META);
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

interface CamFilesTomb {
  dat: null;
  datName: '';
  library: null;
  generation: null;
  libraryHash: null;
  catalogId: null;
  savedAt: number;
}

async function runCamFiles<T>(mode: IDBTransactionMode, work: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await open();
  try {
    return await new Promise<T>((resolve, reject) => {
      const tx = db.transaction(CAMFILES_STORE, mode);
      const request = work(tx.objectStore(CAMFILES_STORE));
      tx.oncomplete = () => resolve(request.result);
      tx.onerror = () => reject(tx.error ?? new Error('IndexedDB'));
      tx.onabort = () => reject(tx.error ?? new Error('IndexedDB'));
    });
  } finally {
    db.close();
  }
}

async function putCamFilesIfNewer(row: StoredCamFiles | CamFilesTomb): Promise<boolean> {
  const db = await open();
  try {
    return await new Promise<boolean>((resolve, reject) => {
      const tx = db.transaction(CAMFILES_STORE, 'readwrite');
      const store = tx.objectStore(CAMFILES_STORE);
      let wrote = false;
      const mark = store.get(WATERMARK);
      mark.onsuccess = () => {
        try {
          const record = store.get(CAMFILES_KEY);
          record.onsuccess = () => {
            try {
              const now = record.result as { savedAt?: unknown } | undefined;
              const high = Math.max(watermarkOf(mark.result), watermarkOf(now?.savedAt));
              if (high >= row.savedAt) return;
              store.put(row, CAMFILES_KEY);
              store.put(row.savedAt, WATERMARK);
              wrote = true;
            } catch (err) {
              try { tx.abort(); } catch { /* transaction may already be aborted */ }
              reject(err);
            }
          };
        } catch (err) {
          try { tx.abort(); } catch { /* transaction may already be aborted */ }
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

export async function loadCamFiles(): Promise<StoredCamFiles | null> {
  if (typeof indexedDB === 'undefined') return null;
  try {
    const row = (await runCamFiles('readonly', (store) => store.get(CAMFILES_KEY))) as StoredCamFiles | CamFilesTomb | undefined;
    if (!row || !(row.dat instanceof ArrayBuffer) || typeof row.datName !== 'string') return null;
    if (row.library !== null && !(row.library instanceof ArrayBuffer)) return null;
    return row as StoredCamFiles;
  } catch {
    return null;
  }
}

export async function loadCamFilesWatermark(): Promise<number> {
  if (typeof indexedDB === 'undefined') return 0;
  try {
    return watermarkOf(await runCamFiles('readonly', (store) => store.get(WATERMARK)));
  } catch {
    return 0;
  }
}

export async function saveCamFiles(files: StoredCamFiles): Promise<boolean> {
  if (typeof indexedDB === 'undefined') return false;
  const wrote = await putCamFilesIfNewer(files);
  void navigator.storage?.persist?.().catch(() => false);
  return wrote;
}

export async function deleteCamFiles(at: number): Promise<boolean> {
  if (typeof indexedDB === 'undefined') return true;
  return putCamFilesIfNewer({ dat: null, datName: '', library: null, generation: null, libraryHash: null, catalogId: null, savedAt: at });
}

export const camFilesIo = {
  loadCamFiles,
  saveCamFiles,
  deleteCamFiles,
  camFilesWatermark: loadCamFilesWatermark,
};

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

export async function renameStoredLogo(id: string, name: string, signal?: AbortSignal): Promise<boolean> {
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
      const req = store.get(id);
      req.onsuccess = () => {
        try {
          if (signal?.aborted || abortedBySignal) {
            onAbort();
            return;
          }
          const row = req.result as StoredLogo | undefined;
          if (!row) return;
          store.put({ ...row, name });
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

export async function deleteStoredLogo(id: string, signal?: AbortSignal): Promise<boolean> {
  if (signal?.aborted) return false;
  if (typeof indexedDB === 'undefined') return true;
  const db = await open();
  if (signal?.aborted) {
    db.close();
    return false;
  }
  try {
    return await new Promise<boolean>((resolve, reject) => {
      const tx = db.transaction(LOGOS_STORE, 'readwrite');
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
      if (signal?.aborted || abortedBySignal) {
        onAbort();
      } else {
        tx.objectStore(LOGOS_STORE).delete(id);
        wrote = true;
      }
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

// ---- LUT 库 IndexedDB 存储（256 MiB 上限、去重、版本水位、确认删除） --------------------

export type SaveLutResult =
  | { ok: true; duplicate: boolean; record: LutRecord; totalBytes: number }
  | { ok: false; reason: 'full' | 'stale' };

export type LutPatchFields = Partial<
  Pick<
    LutRecord,
    | 'name'
    | 'input_space'
    | 'output_space'
    | 'input_gamut'
    | 'output_gamut'
    | 'input_range'
    | 'output_range'
    | 'input_space_manual'
    | 'input_space_state'
    | 'starred'
  >
>;

export async function loadLutWatermark(): Promise<number> {
  if (typeof indexedDB === 'undefined') return 0;
  const db = await open();
  try {
    return await new Promise<number>((resolve, reject) => {
      const tx = db.transaction(LUT_META, 'readonly');
      const req = tx.objectStore(LUT_META).get(LUT_WATERMARK_KEY);
      tx.oncomplete = () => resolve(watermarkOf(req.result));
      tx.onerror = () => reject(tx.error ?? new Error('IndexedDB'));
      tx.onabort = () => reject(tx.error ?? new Error('IndexedDB'));
    });
  } finally {
    db.close();
  }
}

export async function loadLutLibraryMeta(): Promise<{ records: LutRecord[]; order: string[]; totalBytes: number; watermark: number }> {
  if (typeof indexedDB === 'undefined') return { records: [], order: [], totalBytes: 0, watermark: 0 };
  const db = await open();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction([LUT_RECORDS, LUT_META], 'readonly');
      const recReq = tx.objectStore(LUT_RECORDS).getAll();
      const ordReq = tx.objectStore(LUT_META).get(LUT_ORDER_KEY);
      const wmReq = tx.objectStore(LUT_META).get(LUT_WATERMARK_KEY);
      tx.oncomplete = () => {
        const all = (Array.isArray(recReq.result) ? recReq.result : []) as LutRecord[];
        const byId = new Map(all.map((r) => [r.cube_id, r]));
        const savedOrder = Array.isArray(ordReq.result) ? (ordReq.result as string[]) : [];
        const ordered: LutRecord[] = [];
        const seen = new Set<string>();
        for (const id of savedOrder) {
          const item = byId.get(id);
          if (item && !seen.has(id)) {
            seen.add(id);
            ordered.push(item);
          }
        }
        for (const item of all) {
          if (!seen.has(item.cube_id)) ordered.push(item);
        }
        const totalBytes = ordered.reduce((sum, r) => sum + (Number.isFinite(r.bytes) ? r.bytes : 0), 0);
        resolve({ records: ordered, order: ordered.map((r) => r.cube_id), totalBytes, watermark: watermarkOf(wmReq.result) });
      };
      tx.onerror = () => reject(tx.error ?? new Error('IndexedDB'));
      tx.onabort = () => reject(tx.error ?? new Error('IndexedDB'));
    });
  } finally {
    db.close();
  }
}

export async function loadLutTotalBytes(): Promise<number> {
  if (typeof indexedDB === 'undefined') return 0;
  const meta = await loadLutLibraryMeta();
  return meta.totalBytes;
}

export async function loadLutBlob(cubeId: string): Promise<ArrayBuffer | null> {
  if (typeof indexedDB === 'undefined') return null;
  const db = await open();
  try {
    return await new Promise<ArrayBuffer | null>((resolve, reject) => {
      const tx = db.transaction(LUT_BLOBS, 'readonly');
      const req = tx.objectStore(LUT_BLOBS).get(cubeId);
      tx.oncomplete = () => resolve(req.result instanceof ArrayBuffer ? req.result : null);
      tx.onerror = () => reject(tx.error ?? new Error('IndexedDB'));
      tx.onabort = () => reject(tx.error ?? new Error('IndexedDB'));
    });
  } finally {
    db.close();
  }
}

export interface LutSnapshotRow {
  cubeId: string;
  record: LutRecord | null;
  blob: ArrayBuffer | null;
}

/** 在单个 readonly 事务里一次读完一批 LUT 的记录和 .cube 字节，后续 patch/delete 写事务会排在它后面。 */
export async function loadLutBatchSnapshot(cubeIds: readonly string[]): Promise<LutSnapshotRow[]> {
  const unique = [...new Set(cubeIds)];
  if (!unique.length || typeof indexedDB === 'undefined') return [];
  const db = await open();
  try {
    return await new Promise<LutSnapshotRow[]>((resolve, reject) => {
      const tx = db.transaction([LUT_RECORDS, LUT_BLOBS], 'readonly');
      const recs = tx.objectStore(LUT_RECORDS);
      const blobs = tx.objectStore(LUT_BLOBS);
      const pending = unique.map((cubeId) => ({
        cubeId,
        recReq: recs.get(cubeId),
        blobReq: blobs.get(cubeId),
      }));
      tx.oncomplete = () =>
        resolve(
          pending.map(({ cubeId, recReq, blobReq }) => ({
            cubeId,
            record: (recReq.result as LutRecord | undefined) ?? null,
            blob: blobReq.result instanceof ArrayBuffer ? blobReq.result : null,
          })),
        );
      tx.onerror = () => reject(tx.error ?? new Error('IndexedDB'));
      tx.onabort = () => reject(tx.error ?? new Error('IndexedDB'));
    });
  } finally {
    db.close();
  }
}

/** 原子写入新 LUT：同事务校验版本水位、去重（不覆盖用户改名与手动空间）及 256 MiB 上限。 */
export async function saveLut(record: LutRecord, blob: ArrayBuffer, version: number): Promise<SaveLutResult> {
  const db = await open();
  try {
    const out = await new Promise<SaveLutResult>((resolve, reject) => {
      const tx = db.transaction([LUT_RECORDS, LUT_BLOBS, LUT_META], 'readwrite');
      const recs = tx.objectStore(LUT_RECORDS);
      const blobs = tx.objectStore(LUT_BLOBS);
      const meta = tx.objectStore(LUT_META);
      let result: SaveLutResult = { ok: false, reason: 'stale' };

      const wmReq = meta.get(LUT_WATERMARK_KEY);
      const ordReq = meta.get(LUT_ORDER_KEY);
      const allReq = recs.getAll();

      allReq.onsuccess = () => {
        const high = watermarkOf(wmReq.result);
        const all = (Array.isArray(allReq.result) ? allReq.result : []) as LutRecord[];
        const totalBytes = all.reduce((sum, r) => sum + (Number.isFinite(r.bytes) ? r.bytes : 0), 0);
        const existing = all.find((r) => r.cube_id === record.cube_id);
        if (existing) {
          // 已经导入过：确保原始字节完好，但不覆盖用户的改名或已选色彩空间。
          const blobReq = blobs.get(record.cube_id);
          blobReq.onsuccess = () => {
            if (!(blobReq.result instanceof ArrayBuffer) || blobReq.result.byteLength !== blob.byteLength) {
              blobs.put(blob, record.cube_id);
            }
            result = { ok: true, duplicate: true, record: existing, totalBytes };
          };
          return;
        }
        if (high >= version) {
          result = { ok: false, reason: 'stale' };
          return;
        }
        if (totalBytes + blob.byteLength > MAX_LUT_LIBRARY_BYTES) {
          result = { ok: false, reason: 'full' };
          return;
        }
        const order = Array.isArray(ordReq.result) ? ([...ordReq.result] as string[]) : all.map((r) => r.cube_id);
        if (!order.includes(record.cube_id)) order.push(record.cube_id);
        const stored: LutRecord = { ...record, savedAt: version };
        recs.put(stored);
        blobs.put(blob, record.cube_id);
        meta.put(order, LUT_ORDER_KEY);
        meta.put(version, LUT_WATERMARK_KEY);
        result = { ok: true, duplicate: false, record: stored, totalBytes: totalBytes + blob.byteLength };
      };

      tx.oncomplete = () => resolve(result);
      tx.onerror = () => reject(tx.error ?? new Error('IndexedDB'));
      tx.onabort = () => reject(tx.error ?? new Error('IndexedDB'));
    });
    if (out.ok && !out.duplicate) {
      void navigator.storage?.persist?.().catch(() => false);
    }
    return out;
  } finally {
    db.close();
  }
}

export async function patchLutRecord(
  cubeId: string,
  patch: LutPatchFields,
  version: number,
): Promise<{ ok: true; record: LutRecord } | { ok: false; reason: 'not_found' | 'stale' }> {
  const db = await open();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction([LUT_RECORDS, LUT_META], 'readwrite');
      const recs = tx.objectStore(LUT_RECORDS);
      const meta = tx.objectStore(LUT_META);
      let result: { ok: true; record: LutRecord } | { ok: false; reason: 'not_found' | 'stale' } = { ok: false, reason: 'not_found' };

      const wmReq = meta.get(LUT_WATERMARK_KEY);
      const itemReq = recs.get(cubeId);
      itemReq.onsuccess = () => {
        const current = itemReq.result as LutRecord | undefined;
        if (!current) {
          result = { ok: false, reason: 'not_found' };
          return;
        }
        const high = watermarkOf(wmReq.result);
        if (high >= version || current.savedAt >= version) {
          result = { ok: false, reason: 'stale' };
          return;
        }
        const next: LutRecord = {
          ...current,
          ...patch,
          version: current.version + 1,
          savedAt: version,
        };
        recs.put(next);
        meta.put(version, LUT_WATERMARK_KEY);
        result = { ok: true, record: next };
      };
      tx.oncomplete = () => resolve(result);
      tx.onerror = () => reject(tx.error ?? new Error('IndexedDB'));
      tx.onabort = () => reject(tx.error ?? new Error('IndexedDB'));
    });
  } finally {
    db.close();
  }
}

/** 确认在 IndexedDB 删完记录、字节与顺序再返回 true，调用方据此清内存。 */
export async function deleteLutRecord(cubeId: string, version: number): Promise<boolean> {
  if (typeof indexedDB === 'undefined') return true;
  const db = await open();
  try {
    return await new Promise<boolean>((resolve, reject) => {
      const tx = db.transaction([LUT_RECORDS, LUT_BLOBS, LUT_META], 'readwrite');
      const recs = tx.objectStore(LUT_RECORDS);
      const blobs = tx.objectStore(LUT_BLOBS);
      const meta = tx.objectStore(LUT_META);
      let deleted = false;
      const wmReq = meta.get(LUT_WATERMARK_KEY);
      const ordReq = meta.get(LUT_ORDER_KEY);
      ordReq.onsuccess = () => {
        const high = watermarkOf(wmReq.result);
        if (high >= version) return;
        recs.delete(cubeId);
        blobs.delete(cubeId);
        const order = Array.isArray(ordReq.result) ? (ordReq.result as string[]).filter((id) => id !== cubeId) : [];
        meta.put(order, LUT_ORDER_KEY);
        meta.put(version, LUT_WATERMARK_KEY);
        deleted = true;
      };
      tx.oncomplete = () => resolve(deleted);
      tx.onerror = () => reject(tx.error ?? new Error('IndexedDB'));
      tx.onabort = () => reject(tx.error ?? new Error('IndexedDB'));
    });
  } finally {
    db.close();
  }
}

