// 窗口往大拉、跨过大屏线（layout.ts 的 BIG_SCREEN_QUERY）以后，网格上看得见、还是按小格子解的缩略图按大屏格子重解一次。
// 只解看得见的那几张（之后滚到的再解），走 ThumbQueue 的网格缩略图通道，宽高到真发给 Worker 时才定，受 BIG_THUMB_BYTES 管着。
// 往小拉不用管：大位图缩着画照样清楚。只在网格上换位图；编辑器开着时 photo.bitmap 还在显卡和裁剪里用，晚到的结果丢掉。

import { BIG_SCREEN_QUERY, currentGridThumbBox, isBigScreen } from './layout';
import type { Photo } from './model';
import { dropReply, type ThumbQueue } from './queue';

export interface UpsizeHost {
  photo(id: string): Photo | undefined;
  /** 读照片的那一轮（清空、重新选照片时加一）。 */
  session(): number;
  onGrid(): boolean;
  /** 还是同一个照片对象、同一个版本、同一个文件。 */
  current(photo: Photo, version: number, file: File): boolean;
  /** 网格上就地重画这一格。 */
  repaint(id: string): void;
}

const SETTLE_MS = 120;

export function watchThumbUpsize(queue: ThumbQueue, host: UpsizeHost): void {
  if (typeof matchMedia !== 'function' || typeof document === 'undefined') return;
  const pending = new Set<Photo>();
  /** 相机 JPG 解不出图（当初是 DNG 顶上的）：这张不再试。 */
  const noImage = new WeakSet<Photo>();
  let timer = 0;

  const upsize = async (photo: Photo, file: File): Promise<void> => {
    const version = photo.version;
    const session = host.session();
    pending.add(photo);
    const reply = await queue.run(file, photo.jpg ? 'jpg' : 'dng').finally(() => pending.delete(photo));
    const old = photo.bitmap;
    const fresh = reply.ok ? reply.bitmap : null;
    if (reply.ok && !fresh) noImage.add(photo);
    const bigger = Boolean(fresh && old && fresh.width > old.width && fresh.height >= old.height);
    if (!bigger || !old || session !== host.session() || !host.current(photo, version, file) || !host.onGrid()) {
      dropReply(reply);
      return;
    }
    photo.bitmap = fresh;
    old.close();
    host.repaint(photo.id);
  };

  const scan = (): void => {
    timer = 0;
    if (!isBigScreen() || !host.onGrid()) return;
    const scroller = document.querySelector<HTMLElement>('.gscroll');
    if (!scroller) return;
    const view = scroller.getBoundingClientRect();
    const box = currentGridThumbBox();
    for (const cell of scroller.querySelectorAll<HTMLElement>('.cell[data-id]')) {
      const r = cell.getBoundingClientRect();
      if (r.bottom <= view.top || r.top >= view.bottom || r.width <= 0) continue;
      const photo = host.photo(cell.dataset.id ?? '');
      const bmp = photo?.bitmap;
      const file = photo?.jpg ?? photo?.dng;
      if (!photo || !bmp || !file || photo.status !== 'ready' || pending.has(photo) || noImage.has(photo)) continue;
      if (bmp.width >= box.width && bmp.height >= box.height) continue;
      // 字节账满了就不排：照样只能按 360 解，白读一遍。
      if (!queue.budget.room(box.width)) return;
      upsize(photo, file).catch(() => undefined);
    }
  };

  const later = (): void => {
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(scan, SETTLE_MS);
  };

  matchMedia(BIG_SCREEN_QUERY).addEventListener('change', (event) => {
    if (event.matches) later();
  });
  // 滚动事件不冒泡，在 document 上用捕获接网格滚动区的。
  document.addEventListener(
    'scroll',
    (event) => {
      if (event.target instanceof HTMLElement && event.target.classList.contains('gscroll') && isBigScreen()) later();
    },
    { capture: true, passive: true },
  );
}
