// 校准数据存在这台 iPhone 的浏览器里（IndexedDB），加到主屏幕后重开还在。只存用户自己导入的那个 zip。

const DB = 'gxr-mobile';
const STORE = 'calib';
const KEY = 'pack';

export interface StoredPack {
  zip: ArrayBuffer;
  name: string;
  savedAt: number;
}

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB, 1);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE)) request.result.createObjectStore(STORE);
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

export async function loadPack(): Promise<StoredPack | null> {
  if (typeof indexedDB === 'undefined') return null;
  try {
    const row = (await run('readonly', (store) => store.get(KEY))) as StoredPack | undefined;
    return row && row.zip instanceof ArrayBuffer && typeof row.name === 'string' ? row : null;
  } catch {
    return null;
  }
}

export async function savePack(pack: StoredPack): Promise<void> {
  await run('readwrite', (store) => store.put(pack, KEY));
  // 主屏幕网页的存储一般不会被清；再请求一次长期保留，被拒也没关系。
  void navigator.storage?.persist?.().catch(() => false);
}

export async function deletePack(): Promise<void> {
  if (typeof indexedDB === 'undefined') return;
  await run('readwrite', (store) => store.delete(KEY));
}
