// 手机端「导出关机图」底部工作表：把当前带编辑的照片（SDR）或本机另选的任意图片做成 640×480 的 GXROFF.JPG，
// 展示最终字节的 640×480 预览与文件大小/品质；只有 Rust WASM 校验通过后才启用「导出」，并通过系统共享（保存到“文件” / 存储到“文件”）或下载交出。

import { ExportCore, calibPack } from '../core/export_api';
import { buildRequest, usedLogoId } from '../lib/export_request';
import { fontsNeeded } from '../lib/fonts';
import { isComputer } from '../lib/save_mode';
import { button, el } from './dom';
import { adoptCameraRecipe, cardsFor, requestPhoto } from './export_request_prep';
import { CALIB_WAIT_MS } from './export_calib_wait';
import { ensureFontPack } from './font_loader';
import { logoStore } from './logo_store';
import { ensureCameraRecipes, needsCameraRecipe, type AppApi, type Photo } from './model';
import type { OffImageReply, OffImageRequest } from './off_image_worker';
import { registerSheet } from './sheet_lifecycle';

declare const __GXR_BUILD__: string;

const NOTE_CAMERA = '相机会把当天张数叠在关机画面上，下部别放重要内容。';
const NOTE_CARD = '把 GXROFF.JPG 拷到存储卡最外层（和 GXRCE.DAT 放在一起）。';
const NOTE_FILES = '存的时候选「保存到“文件”」（有的系统叫「存储到“文件”」），不要存进相册（相册会改动文件，相机就不认了）。';

interface DevOffImageHook {
  corruptTail?: boolean;
  workersSpawned: number;
  workersAlive: number;
  liveUrls: number;
}

function devHook(): DevOffImageHook | null {
  if (!import.meta.env.DEV) return null;
  const scope = window as unknown as { __devOffImage?: DevOffImageHook };
  scope.__devOffImage ??= { workersSpawned: 0, workersAlive: 0, liveUrls: 0 };
  return scope.__devOffImage;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB（${bytes} B）`;
}

/** 用短命 ExportCore 把当前照片按当前编辑渲染为 SDR JPEG，立刻释放导出 WASM 内存。 */
async function renderPhotoSdr(api: AppApi, photo: Photo, cancelled: () => boolean): Promise<ArrayBuffer> {
  const book = api.book();
  // 默认校准还在路上时先等它落定（和照片导出同一个截止时间，最多几秒），不然关机图会是中性转换。
  await book.calibSettled(CALIB_WAIT_MS);
  if (cancelled()) throw new Error('已取消');
  const dat = book.dat;
  const lutBook = api.lutBook();
  if (needsCameraRecipe(photo, dat, { checkMissingDat: true })) {
    await ensureCameraRecipes([photo], book, { checkMissingDat: true });
  }
  if (cancelled()) throw new Error('已取消');

  const reqPhoto = requestPhoto(photo, lutBook, book);
  const logoId = usedLogoId(reqPhoto.border);
  const cubeIds = reqPhoto.lut ? [reqPhoto.lut] : [];
  const fontKeys = fontsNeeded(reqPhoto.border, reqPhoto.stamp);

  const [luts, fonts, loadedLogos] = await Promise.all([
    cubeIds.length ? lutBook.exportSnapshot(cubeIds).catch(() => ({})) : Promise.resolve({}),
    fontKeys.length ? ensureFontPack(fontKeys).catch(() => ({ fontIndex: '', fontPack: null })) : Promise.resolve({ fontIndex: '', fontPack: null }),
    logoId && !logoStore.loaded() ? logoStore.ensureLoaded().catch(() => []) : Promise.resolve([]),
  ]);
  void loadedLogos;
  if (cancelled()) throw new Error('已取消');

  if (logoId) reqPhoto.logoInfo = logoStore.info(logoId);
  let cards = cardsFor(book, photo);
  if (!photo.choice && !photo.recipeOff) cards = adoptCameraRecipe(reqPhoto, cards, photo, dat) ?? cards;

  const req = buildRequest(reqPhoto, cards, { size: 'original', quality: 'high', hdr: false }, __GXR_BUILD__);
  const pack = calibPack(book.pack?.files ?? null);
  const packedLogos = logoStore.packLogos(logoId ? [logoId] : []);

  const core = new ExportCore();
  try {
    core.begin({
      dat: dat ? dat.read.bytes.slice(0) : null,
      calibIndex: pack.calibIndex,
      calibBlob: pack.calibBlob,
      luts,
      fontIndex: fonts.fontIndex,
      fontPack: fonts.fontPack,
      logoIndex: packedLogos.logoIndex,
      logoBlob: packedLogos.logoBlob,
    });
    const res = await core.run({
      dng: photo.dng,
      jpg: photo.jpg,
      req,
    });
    if (!res.ok) {
      throw new Error(res.message || '无法渲染当前照片。');
    }
    return res.file;
  } finally {
    core.stop();
  }
}

/** 兜底解码：Worker 无 OffscreenCanvas 或无法直接解码时，在主线程经 img + canvas 铺黑底转成 sRGB RGB。 */
async function decodeBlobOnMain(blob: Blob): Promise<{ rgb: ArrayBuffer; width: number; height: number }> {
  const url = URL.createObjectURL(blob);
  try {
    const img = new Image();
    img.decoding = 'async';
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve();
      img.onerror = () => reject(new Error('无法读取这张图片。'));
      img.src = url;
    });
    const width = img.naturalWidth;
    const height = img.naturalHeight;
    if (!width || !height) throw new Error('图片尺寸无效。');
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext('2d', { colorSpace: 'srgb', willReadFrequently: true });
    if (!ctx) throw new Error('浏览器不支持画布解码。');
    ctx.fillStyle = '#000000';
    ctx.fillRect(0, 0, width, height);
    ctx.drawImage(img, 0, 0);
    const rgba = ctx.getImageData(0, 0, width, height).data;
    const rgb = new Uint8Array(width * height * 3);
    for (let i = 0, j = 0; i < rgba.length; i += 4, j += 3) {
      rgb[j] = rgba[i];
      rgb[j + 1] = rgba[i + 1];
      rgb[j + 2] = rgba[i + 2];
    }
    return { rgb: rgb.buffer, width, height };
  } finally {
    URL.revokeObjectURL(url);
  }
}

interface ActiveWorker {
  promise: Promise<OffImageReply>;
  cancel(): void;
}

function runShortWorker(req: OffImageRequest, transfer: Transferable[] = []): ActiveWorker {
  let finishRef: ((reply: OffImageReply) => void) | null = null;
  const hook = devHook();
  const promise = new Promise<OffImageReply>((resolve) => {
    const worker = new Worker(new URL('./off_image_worker.ts', import.meta.url), { type: 'module' });
    if (hook) {
      hook.workersSpawned += 1;
      hook.workersAlive += 1;
    }
    let done = false;
    const finish = (reply: OffImageReply) => {
      if (done) return;
      done = true;
      try {
        worker.terminate();
      } catch {
        /* 已关闭 */
      }
      if (hook) hook.workersAlive = Math.max(0, hook.workersAlive - 1);
      resolve(reply);
    };
    finishRef = finish;
    worker.onmessage = (ev: MessageEvent<OffImageReply>) => finish(ev.data);
    worker.onerror = (ev) =>
      finish({
        ok: false,
        code: 'worker',
        message: ev.message || '关机图编码器出错。',
        dat_state: 'error',
        errors: [],
        wasmBytes: 0,
      });
    worker.onmessageerror = () =>
      finish({
        ok: false,
        code: 'worker',
        message: '关机图消息损坏。',
        dat_state: 'error',
        errors: [],
        wasmBytes: 0,
      });
    try {
      worker.postMessage(req, transfer);
    } catch (err) {
      finish({
        ok: false,
        code: 'worker',
        message: err instanceof Error ? err.message : '发送图片失败。',
        dat_state: 'error',
        errors: [],
        wasmBytes: 0,
      });
    }
  });
  return {
    promise,
    cancel: () =>
      finishRef?.({
        ok: false,
        code: 'cancelled',
        message: '已取消。',
        dat_state: 'cancelled',
        errors: [],
        wasmBytes: 0,
      }),
  };
}

interface ReadyOffImage {
  bytes: Uint8Array<ArrayBuffer>;
  quality: number;
  width: number;
  height: number;
  datState: string;
}

/** 打开手机端「导出关机图」底部工作表。 */
export function openOffImageSheet(host: HTMLElement, api: AppApi, photo: Photo): void {
  for (const old of host.querySelectorAll('.off-image-scrim, .off-image-sheet')) old.remove();

  const scrim = el('div', 'scrim off-image-scrim');
  const sheet = el('div', 'ms off-image-sheet');
  sheet.dataset.state = 'off-image';
  sheet.setAttribute('role', 'dialog');
  sheet.setAttribute('aria-modal', 'true');
  sheet.setAttribute('aria-label', '导出关机图');

  const head = el('div', 'hd');
  head.append(el('h2', '', '导出关机图'));
  const closeBtn = button('nb', '关闭');
  closeBtn.dataset.act = 'off-image-close';
  head.append(closeBtn);

  const stage = el('div', 'oi-stage');
  const preview = document.createElement('img');
  preview.className = 'oi-preview';
  preview.alt = '关机图预览';
  preview.hidden = true;
  const placeholder = el('div', 'oi-placeholder', '正在生成关机图…');
  stage.append(preview, placeholder);

  const metaLine = el('p', 'oi-meta num', '640×480 · 生成中…');
  metaLine.dataset.role = 'off-image-meta';

  const errBox = el('p', 'oi-error');
  errBox.dataset.role = 'off-image-error';
  errBox.setAttribute('role', 'alert');
  errBox.hidden = true;

  const pickInput = document.createElement('input');
  pickInput.type = 'file';
  pickInput.accept = 'image/*';
  pickInput.className = 'filepick';
  pickInput.dataset.role = 'off-image-file';

  const pickBtn = button('btn lg block oi-pick', '换一张图…');
  pickBtn.dataset.act = 'off-image-pick';

  const notes = el('div', 'oi-notes');
  const noteCamera = el('p', '', NOTE_CAMERA);
  const noteCard = el('p', '', NOTE_CARD);
  const noteFiles = el('p', 'oi-note-files', isComputer() ? '下载后把 GXROFF.JPG 拷到存储卡最外层。' : NOTE_FILES);
  notes.append(noteCamera, noteCard, noteFiles);

  const fallbackRow = el('div', 'oi-fallback');
  fallbackRow.hidden = true;

  const exportBtn = button('btn primary lg block oi-go', '导出');
  exportBtn.dataset.act = 'off-image-export';
  exportBtn.disabled = true;

  const body = el('div', 'oi-body');
  body.append(stage, metaLine, errBox, pickBtn, pickInput, notes, fallbackRow);
  const foot = el('div', 'oi-foot');
  foot.append(exportBtn);

  sheet.append(el('div', 'grab'), head, body, foot);
  host.append(scrim, sheet);

  let closed = false;
  let gen = 0;
  const openSession = api.session();
  const photoId = photo.id;
  const photoVersion = photo.version;
  const photoRev = photo.rev;
  let current: ReadyOffImage | null = null;
  let activeWorker: ActiveWorker | null = null;
  const urls = new Set<string>();

  const isStale = (token: number): boolean => closed || token !== gen || api.session() !== openSession;
  const isPhotoStale = (token: number): boolean => {
    if (isStale(token)) return true;
    const live = api.photo(photoId);
    return !live || live !== photo || live.version !== photoVersion || live.rev !== photoRev;
  };

  const syncLiveUrls = (): void => {
    const hook = devHook();
    if (hook) hook.liveUrls = urls.size;
  };

  const trackUrl = (url: string): string => {
    urls.add(url);
    syncLiveUrls();
    return url;
  };

  const revokeAllUrls = (): void => {
    for (const url of urls) URL.revokeObjectURL(url);
    urls.clear();
    syncLiveUrls();
  };

  const stopWorker = (): void => {
    activeWorker?.cancel();
    activeWorker = null;
  };

  const setLoading = (text: string): void => {
    current = null;
    exportBtn.disabled = true;
    errBox.hidden = true;
    errBox.textContent = '';
    fallbackRow.hidden = true;
    fallbackRow.replaceChildren();
    preview.hidden = true;
    preview.removeAttribute('src');
    revokeAllUrls();
    placeholder.hidden = false;
    placeholder.textContent = text;
    metaLine.textContent = '640×480 · 生成中…';
    sheet.dataset.ready = '0';
  };

  const setError = (message: string, datState = 'error'): void => {
    current = null;
    exportBtn.disabled = true;
    preview.hidden = true;
    preview.removeAttribute('src');
    revokeAllUrls();
    placeholder.hidden = false;
    placeholder.textContent = '未生成可导出的关机图';
    metaLine.textContent = `校验未通过（${datState}）`;
    errBox.textContent = message;
    errBox.hidden = false;
    sheet.dataset.ready = '0';
    sheet.dataset.datState = datState;
  };

  const setReady = (reply: Extract<OffImageReply, { ok: true }>): void => {
    revokeAllUrls();
    const bytes = new Uint8Array(reply.file);
    current = {
      bytes,
      quality: reply.quality,
      width: reply.width,
      height: reply.height,
      datState: reply.dat_state,
    };
    const url = trackUrl(URL.createObjectURL(new Blob([bytes], { type: 'image/jpeg' })));
    preview.src = url;
    preview.hidden = false;
    placeholder.hidden = true;
    errBox.hidden = true;
    errBox.textContent = '';
    fallbackRow.hidden = true;
    fallbackRow.replaceChildren();
    metaLine.textContent = `${formatSize(reply.bytes)} · ${reply.width}×${reply.height} · 品质 ${reply.quality}`;
    exportBtn.disabled = false;
    sheet.dataset.ready = '1';
    sheet.dataset.datState = reply.dat_state;
  };

  const encodeBlob = async (blob: Blob, token: number, checkPhoto: boolean): Promise<void> => {
    const stale = () => (checkPhoto ? isPhotoStale(token) : isStale(token));
    const corruptTail = Boolean(devHook()?.corruptTail);
    stopWorker();
    const job = runShortWorker({ op: 'blob', blob, ...(corruptTail ? { corruptTail: true } : {}) });
    activeWorker = job;
    let reply = await job.promise;
    if (activeWorker === job) activeWorker = null;
    if (stale()) return;

    if (!reply.ok && reply.code === 'decode_needed') {
      try {
        const decoded = await decodeBlobOnMain(blob);
        if (stale()) return;
        const job2 = runShortWorker(
          {
            op: 'rgb',
            rgb: decoded.rgb,
            width: decoded.width,
            height: decoded.height,
            ...(corruptTail ? { corruptTail: true } : {}),
          },
          [decoded.rgb],
        );
        activeWorker = job2;
        reply = await job2.promise;
        if (activeWorker === job2) activeWorker = null;
        if (stale()) return;
      } catch (err) {
        if (stale()) return;
        setError(err instanceof Error ? err.message : '图片格式不受支持。', 'decode_error');
        return;
      }
    }

    if (!reply.ok) {
      setError(reply.message || '关机图校验未通过。', reply.dat_state);
      return;
    }
    setReady(reply);
  };

  const generateFromCurrentPhoto = async (): Promise<void> => {
    const token = ++gen;
    setLoading('正在渲染并生成关机图…');
    try {
      const sdrJpeg = await renderPhotoSdr(api, photo, () => isPhotoStale(token));
      if (isPhotoStale(token)) return;
      await encodeBlob(new Blob([sdrJpeg], { type: 'image/jpeg' }), token, true);
    } catch (err) {
      if (isPhotoStale(token)) return;
      setError(err instanceof Error ? err.message : '无法处理当前照片。', 'render_error');
    }
  };

  const generateFromFile = async (file: File): Promise<void> => {
    const token = ++gen;
    setLoading('正在生成关机图…');
    await encodeBlob(file, token, false);
  };

  const onKey = (ev: KeyboardEvent): void => {
    if (ev.key === 'Escape') {
      ev.preventDefault();
      close();
    }
  };

  const unregister = registerSheet(host, () => close());

  const close = (): void => {
    if (closed) return;
    closed = true;
    gen += 1;
    stopWorker();
    revokeAllUrls();
    document.removeEventListener('keydown', onKey);
    unregister();
    scrim.remove();
    sheet.remove();
  };

  scrim.addEventListener('click', close);
  closeBtn.addEventListener('click', close);
  document.addEventListener('keydown', onKey);

  pickBtn.addEventListener('click', () => {
    pickInput.click();
  });

  pickInput.addEventListener('change', () => {
    const picked = pickInput.files?.[0] ?? null;
    pickInput.value = '';
    if (!picked) return;
    void generateFromFile(picked).catch(() => undefined);
  });

  // 导出必须在点击事件里同步触发 navigator.share，前面不能有 await，保留用户手势激活。电脑浏览器直接下载。
  exportBtn.addEventListener('click', () => {
    if (!current || exportBtn.disabled) return;
    const file = new File([current.bytes], 'GXROFF.JPG', { type: 'image/jpeg' });
    if (isComputer()) {
      offerDownload(file, true);
      return;
    }
    const nav = navigator as Navigator & { canShare?: (data: ShareData) => boolean };
    const canShare = typeof nav.share === 'function' && typeof nav.canShare === 'function' && nav.canShare({ files: [file] });
    if (canShare) {
      void nav.share({ files: [file] }).catch((err: unknown) => {
        // 用户自己关掉共享面板（AbortError）就留在面板里；别的失败给一个下载链接，不能什么都不发生。
        if (err instanceof DOMException && err.name === 'AbortError') return;
        offerDownload(file, false);
      });
      return;
    }
    offerDownload(file, true);
  });

  /** 不能共享时退回下载；autoClick 只在还有用户点击手势时用。 */
  function offerDownload(file: File, autoClick: boolean): void {
    const url = trackUrl(URL.createObjectURL(file));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'GXROFF.JPG';
    link.textContent = '下载 GXROFF.JPG';
    link.className = 'oi-download-link';
    link.dataset.act = 'off-image-download';
    fallbackRow.replaceChildren(link);
    fallbackRow.hidden = false;
    if (autoClick) link.click();
  }

  void generateFromCurrentPhoto().catch(() => undefined);
}
