// 编辑器里的 LUT 页签（设计稿 03g）：卡片条（不用 LUT + 已导入的 LUT）、输入色彩空间行、说明文字、导入按钮。
// 后台按单队列调用短命 Worker（lut_table）生成卡片小图与主舞台 3D LUT 纹理；拖动或导出时暂停后台小图。

import type { Engine, Geometry } from '../../../frontend/src/index';
import {
  LUT_ERRORS,
  LUT_FOOTNOTE,
  SOURCE_TAGS,
  computeInputChoice,
  isDisplaySpace,
  lutRenderToken,
  spaceShortLabel,
  type LutRecord,
} from '../lib/lut';
import { NEUTRAL_BAKE_KEY } from '../lib/neutral';
import { button, el, icon } from './dom';
import { exportPaused } from './export_pause';
import { lookCache } from './looks';
import { closeLutSheets, openLutActionSheet, openLutSpaceSheet } from './lut_sheet';
import type { AppApi, Photo } from './model';

const CHECK = 'M5 12.5l4.5 4.5L19 7.5';
const PLUS = 'M12 5v14M5 12h14';
const CHEVRON = 'M9 6l6 6-6 6';
const GEOMETRY: Geometry = { straighten: 0, rotate90: 0, flip: false, vertical: 0, horizontal: 0, distortion: 0, box: null };
const ADJUST = {
  exposure_ev: 0,
  wb: { warm: 0, tint: 0 },
  contrast: 0,
  highlight: 0,
  shadow: 0,
  black_point: 0,
  saturation: 0,
  vibrance: 0,
  vignette: 0,
};

export interface LutStageHooks {
  mode(): 'raw' | 'jpg' | 'still';
  /** 当前舞台用的烘焙表键（RAW 模式为配方或中性键，JPG 模式不用）。 */
  recipeGpuKey(): string;
  /** 把准备好的 LUT 纹理令牌交到舞台（未准备好前不清空上一张）。 */
  stageLut(token: string | null, label: string | null): void;
  dragging(): boolean;
  lutTab(): boolean;
  /** 通知编辑器 LUT 选中状态变了（更新 LUT tab 蓝点指示）。 */
  lutChanged(): void;
}

interface Slot {
  record: LutRecord | null;
  node: HTMLButtonElement;
  thumb: HTMLElement;
  nameEl: HTMLElement;
  sub: HTMLElement;
}

export class LutView {
  private token = 0;
  private thumbToken = 0;
  private sourceKey: string | null = null;
  private readonly panel = el('div', 'panel lut');
  private readonly strip = el('div', 'strip inpanel');
  private readonly missingBanner = el('div', 'bn lut-missing');
  private readonly spaceRow = button('lrow pad', '');
  private readonly spaceSmall = el('small', '', '');
  private readonly foot = el('p', 'pad faint small lut-foot', LUT_FOOTNOTE);
  private readonly statusEl = el('p', 'pad small lut-status', '');
  private readonly addRow = el('div', 'pad lut-add-row');
  private stripKeys: string[] = [];
  private slots: Slot[] = [];
  private statusMsg = '';
  private seenNotice = 0;
  private stageWanted = '';
  private stageReadyToken: string | null = null;
  private readonly off: () => void;

  constructor(
    private readonly shell: HTMLElement,
    private readonly api: AppApi,
    private readonly engine: Engine | null,
    private readonly photo: Photo,
    private readonly hooks: LutStageHooks,
  ) {
    this.panel.dataset.panel = 'lut';
    this.strip.dataset.luts = '1';

    this.missingBanner.dataset.state = 'lut-deleted';
    const missTx = el('div', 'tx');
    missTx.append(el('b', '', LUT_ERRORS.cube_not_found), el('div', 'dim small', '已选的 LUT 不在本地库里；点「不用 LUT」或重新导入后再导出。'));
    this.missingBanner.append(missTx);

    this.spaceRow.dataset.act = 'lut-space';
    const spaceTx = el('div', 'grow');
    spaceTx.append(el('b', '', '输入色彩空间'), this.spaceSmall);
    const arrow = el('span', 'lut-chev');
    arrow.append(icon(CHEVRON));
    this.spaceRow.append(spaceTx, arrow);
    this.spaceRow.addEventListener('click', () => {
      const rec = this.photo.lut ? this.api.lutBook().get(this.photo.lut) : undefined;
      if (rec) this.openSpaceFor(rec, false);
    });

    this.statusEl.dataset.state = 'lut-status';

    const add = button('btn block', '导入 LUT（.cube 文件）');
    add.dataset.act = 'lut-import';
    add.prepend(icon(PLUS));
    add.addEventListener('click', () => this.api.pickLut());
    this.addRow.append(add);

    this.panel.append(this.strip, this.spaceRow, this.foot, this.addRow);

    this.seenNotice = api.lutBook().lastNotice().seq;
    this.off = api.lutBook().onChange(() => {
      const notice = api.lutBook().lastNotice();
      if (notice.seq !== this.seenNotice) {
        this.seenNotice = notice.seq;
        this.statusMsg = notice.text;
      }
      this.refresh();
    });
    this.build();
  }

  panelEl(): HTMLElement {
    return this.panel;
  }

  destroy(): void {
    this.token += 1;
    this.thumbToken += 1;
    this.off();
    closeLutSheets(this.shell);
  }

  setStatus(message: string): void {
    this.statusMsg = message;
    this.build();
  }

  /** 源图已经上传显卡：立即把当前选中的 LUT 推上舞台，并在需要时生成缩略图。 */
  start(sourceKey: string): void {
    this.sourceKey = sourceKey;
    this.refresh();
  }

  /** 配方变了（主舞台或配方小图）：刷新卡片小图。 */
  recipeUpdated(): void {
    if (this.hooks.lutTab()) {
      void this.renderCards(++this.thumbToken);
    }
  }

  tabOpened(): void {
    if (this.sourceKey && this.engine) {
      void this.renderCards(++this.thumbToken);
    }
  }

  /** 确保当前选中的 LUT 在 3 槽 GPU 缓存里（比如被后台缩略图挤掉后重新建回来）。 */
  ensureActive(): void {
    void this.applyStageLut();
  }

  refresh(): void {
    this.token += 1;
    this.build();
    this.hooks.lutChanged();
    void this.applyStageLut();
    if (this.hooks.lutTab() && this.sourceKey && this.engine) {
      void this.renderCards(++this.thumbToken);
    }
  }

  private hasRaw(): boolean {
    return this.photo.dng !== null;
  }

  private isUsable(record: LutRecord): boolean {
    if (!record.input_space) return false;
    if (!this.hasRaw() && !isDisplaySpace(record.input_space)) return false;
    return true;
  }

  private spaceLine(record: LutRecord | undefined): { text: string; warn: boolean } {
    if (!record) return { text: '未选用 LUT', warn: false };
    const choice = computeInputChoice(record);
    if (!record.input_space) return { text: LUT_ERRORS.lut_space_required, warn: true };
    const srcTag = SOURCE_TAGS[choice.default_input_source === 'required' ? 'manual' : choice.default_input_source];
    const extras: string[] = [];
    if (record.output_gamut) extras.push(record.output_gamut === 'rec2020' ? 'Rec.2020' : record.output_gamut.toUpperCase());
    if (record.input_range === 'legal' || record.output_range === 'legal') extras.push('法律范围');
    return {
      text: `${spaceShortLabel(record.input_space)} · ${srcTag}${extras.length ? ` · ${extras.join(' · ')}` : ''}`,
      warn: false,
    };
  }

  private build(): void {
    const lutBook = this.api.lutBook();
    const list = lutBook.list();
    const selectedId = this.photo.lut;
    const selectedRec = selectedId ? lutBook.get(selectedId) : undefined;
    const deleted = Boolean(selectedId && !selectedRec);

    const nextKeys = ['none', ...(deleted ? ['deleted'] : []), ...list.map((r) => r.cube_id)];
    const sameStrip =
      nextKeys.length === this.stripKeys.length &&
      nextKeys.every((k, i) => k === this.stripKeys[i]) &&
      this.slots.length === 1 + list.length;

    if (sameStrip) {
      // 卡片键序列没变时原地更新类名与文字，避免异步 refresh() 拆卸正在点击或滚动中的 .rc 元素。
      this.syncMark(this.slots[0].node, this.slots[0].thumb, selectedId === null);
      for (let i = 0; i < list.length; i += 1) {
        const record = list[i];
        const slot = this.slots[i + 1];
        slot.record = record;
        slot.nameEl.textContent = record.name;
        const needsSpace = !record.input_space;
        const needsRaw = !this.hasRaw() && Boolean(record.input_space && !isDisplaySpace(record.input_space));
        slot.sub.textContent = needsSpace ? '请选择色彩空间' : needsRaw ? '需要 RAW 文件' : '';
        slot.node.classList.toggle('lockd', needsSpace || needsRaw);
        this.syncMark(slot.node, slot.thumb, selectedId === record.cube_id);
      }
    } else {
      const scrollLeft = this.strip.scrollLeft;
      this.strip.replaceChildren();
      this.slots = [];
      this.stripKeys = nextKeys;

      // 第一张卡：不用 LUT
      const noneBtn = button(selectedId === null ? 'rc on' : 'rc', '');
      noneBtn.dataset.lut = 'none';
      const noneThumb = el('div', 'th lut-none');
      noneThumb.textContent = '×';
      const noneName = el('span', 'nm', '不用 LUT');
      const noneSub = el('span', 'sub');
      noneBtn.append(noneThumb, noneName, noneSub);
      noneBtn.addEventListener('click', () => {
        if (this.photo.lut === null) return;
        this.photo.lut = null;
        this.photo.rev += 1;
        this.statusMsg = '';
        this.refresh();
      });
      this.strip.append(noneBtn);
      this.slots.push({ record: null, node: noneBtn, thumb: noneThumb, nameEl: noneName, sub: noneSub });

      // 若照片选过已被删除的 LUT：明显标出「LUT 已删除」提示卡片（导出也按桌面语义拒绝）。
      if (deleted) {
        const delCard = button('rc on lockd', '');
        delCard.dataset.lut = 'deleted';
        const delThumb = el('div', 'th lut-deleted-th');
        delThumb.textContent = '!';
        const delSub = el('span', 'sub', '找不到这个 LUT');
        delCard.append(delThumb, el('span', 'nm', 'LUT 已删除'), delSub);
        delCard.addEventListener('click', () => {
          this.statusMsg = LUT_ERRORS.cube_not_found;
          this.build();
        });
        this.strip.append(delCard);
      }

      for (const record of list) {
        const card = button('rc', '');
        card.dataset.lut = record.cube_id;
        const thumb = el('div', 'th');
        const needsSpace = !record.input_space;
        const needsRaw = !this.hasRaw() && Boolean(record.input_space && !isDisplaySpace(record.input_space));
        const subText = needsSpace ? '请选择色彩空间' : needsRaw ? '需要 RAW 文件' : '';
        const nameEl = el('span', 'nm', record.name);
        const sub = el('span', 'sub', subText);
        if (needsSpace || needsRaw) card.classList.add('lockd');
        this.syncMark(card, thumb, selectedId === record.cube_id);
        card.append(thumb, nameEl, sub);

        this.bindCardGestures(card, record.cube_id);
        this.strip.append(card);
        this.slots.push({ record, node: card, thumb, nameEl, sub });
      }
      if (scrollLeft > 0) this.strip.scrollLeft = scrollLeft;
    }

    if (deleted && !this.missingBanner.isConnected) {
      this.panel.insertBefore(this.missingBanner, this.spaceRow);
    } else if (!deleted && this.missingBanner.isConnected) {
      this.missingBanner.remove();
    }

    const sp = this.spaceLine(selectedRec);
    this.spaceSmall.className = sp.warn ? 'warn' : '';
    this.spaceSmall.textContent = sp.text;
    this.spaceRow.disabled = !selectedRec;

    if (this.statusMsg) {
      this.statusEl.textContent = this.statusMsg;
      if (!this.statusEl.isConnected) this.panel.insertBefore(this.statusEl, this.addRow);
    } else if (this.statusEl.isConnected) {
      this.statusEl.remove();
    }

    this.paintThumbs();
  }

  private bindCardGestures(card: HTMLButtonElement, cubeId: string): void {
    let suppressTap = false;
    let pressTimer = 0;
    let startX = 0;
    let startY = 0;
    let moved = false;

    card.addEventListener('pointerdown', (ev) => {
      if (ev.button !== 0) return;
      startX = ev.clientX;
      startY = ev.clientY;
      moved = false;
      suppressTap = false;
      window.clearTimeout(pressTimer);
      pressTimer = window.setTimeout(() => {
        if (moved) return;
        const record = this.api.lutBook().get(cubeId);
        if (!record) return;
        suppressTap = true;
        openLutActionSheet(this.shell, this.api.lutBook(), record, {
          onChanged: () => this.refresh(),
          onDeleted: (deletedId) => {
            if (this.photo.lut === deletedId) {
              this.statusMsg = '';
            }
            this.refresh();
          },
        });
      }, 450);
    });

    card.addEventListener('pointermove', (ev) => {
      if (pressTimer && Math.hypot(ev.clientX - startX, ev.clientY - startY) > 10) {
        moved = true;
        window.clearTimeout(pressTimer);
        pressTimer = 0;
      }
    });

    const cancelPress = () => {
      if (pressTimer) {
        window.clearTimeout(pressTimer);
        pressTimer = 0;
      }
    };
    card.addEventListener('pointerup', cancelPress);
    card.addEventListener('pointercancel', cancelPress);

    card.addEventListener('click', () => {
      if (suppressTap) {
        suppressTap = false;
        return;
      }
      const record = this.api.lutBook().get(cubeId);
      if (!record) return;
      if (!record.input_space) {
        this.openSpaceFor(record, true);
        return;
      }
      if (!this.hasRaw() && !isDisplaySpace(record.input_space)) {
        this.statusMsg = LUT_ERRORS.lut_needs_raw;
        this.build();
        return;
      }
      if (this.photo.lut === record.cube_id) return;
      this.photo.lut = record.cube_id;
      this.photo.rev += 1;
      this.statusMsg = '';
      this.refresh();
    });
  }

  private openSpaceFor(record: LutRecord, applyIfUsable: boolean): void {
    openLutSpaceSheet(this.shell, this.api.lutBook(), record, {
      hasRaw: this.hasRaw(),
      onUpdated: (updated) => {
        if (applyIfUsable) {
          if (this.isUsable(updated)) {
            this.photo.lut = updated.cube_id;
            this.photo.rev += 1;
            this.statusMsg = '';
          } else if (updated.input_space && !this.hasRaw() && !isDisplaySpace(updated.input_space)) {
            this.statusMsg = LUT_ERRORS.lut_needs_raw;
          }
        }
        this.refresh();
      },
    });
  }

  private syncMark(node: HTMLElement, thumb: HTMLElement, on: boolean): void {
    node.classList.toggle('on', on);
    if (thumb.classList.contains('lut-none')) return;
    const existing = thumb.querySelector('.ckk');
    if (on && !existing) {
      const tick = el('span', 'ckk');
      tick.append(icon(CHECK));
      thumb.append(tick);
    } else if (!on && existing) {
      existing.remove();
    }
  }

  private thumbCacheKey(record: LutRecord): string {
    const token = lutRenderToken(record);
    const mode = this.hooks.mode();
    const recKey = mode === 'raw' ? this.hooks.recipeGpuKey() : '';
    return `lut:${token}:${this.sourceKey ?? ''}:${recKey}`;
  }

  private paintThumbs(): void {
    const mode = this.hooks.mode();
    for (const slot of this.slots) {
      const rec = slot.record;
      if (!rec) continue;
      const usable = this.isUsable(rec);
      let bitmap: ImageBitmap | null = null;
      if (usable && this.engine && this.sourceKey && (mode === 'raw' || mode === 'jpg')) {
        bitmap = lookCache.get(this.photo, this.thumbCacheKey(rec));
      }
      if (!bitmap && (mode === 'still' || !this.engine)) {
        bitmap = this.photo.bitmap;
      }
      const want = bitmap ? `bmp:${bitmapId(bitmap)}` : '';
      if (slot.thumb.dataset.drawn === want) continue;
      slot.thumb.querySelector('canvas')?.remove();
      slot.thumb.dataset.drawn = want;
      if (!bitmap) continue;
      const mini = document.createElement('canvas');
      mini.width = bitmap.width;
      mini.height = bitmap.height;
      mini.getContext('2d')?.drawImage(bitmap, 0, 0);
      slot.thumb.prepend(mini);
    }
  }

  private async applyStageLut(): Promise<void> {
    const mode = this.hooks.mode();
    const token = this.token;
    const cubeId = this.photo.lut;
    const appShell = this.panel.closest('.app');
    if (!cubeId) {
      this.stageWanted = '';
      this.stageReadyToken = null;
      appShell?.removeAttribute('data-lut-token');
      this.hooks.stageLut(null, null);
      return;
    }
    const record = this.api.lutBook().get(cubeId);
    if (!record || !this.isUsable(record)) {
      this.stageWanted = '';
      this.stageReadyToken = null;
      appShell?.removeAttribute('data-lut-token');
      this.hooks.stageLut(null, null);
      return;
    }
    if (!this.engine || mode === 'still') {
      // 无 GPU 时保留牌标签提示已选 LUT。
      this.stageReadyToken = null;
      this.hooks.stageLut(null, record.name);
      return;
    }
    const renderToken = lutRenderToken(record);
    this.stageWanted = renderToken;
    const gpu = this.api.lutGpu();
    if (gpu.has(renderToken)) {
      this.stageReadyToken = renderToken;
      appShell?.setAttribute('data-lut-token', renderToken);
      this.hooks.stageLut(renderToken, record.name);
      return;
    }
    // 没好之前保持上一帧，不把照片置空。
    const res = await gpu.ensure(record);
    if (token !== this.token || this.stageWanted !== renderToken || !this.panel.isConnected) return;
    if (!res.ok) {
      this.statusMsg = res.message || LUT_ERRORS.cube_unreadable;
      this.build();
      return;
    }
    this.stageReadyToken = res.token;
    appShell?.setAttribute('data-lut-token', res.token);
    this.hooks.stageLut(res.token, record.name);
  }

  private async renderCards(thumbToken: number): Promise<void> {
    const engine = this.engine;
    const source = this.sourceKey;
    const mode = this.hooks.mode();
    if (!engine || !source || (mode !== 'raw' && mode !== 'jpg')) return;
    const photo = this.photo;
    const version = photo.version;
    const session = this.api.session();
    const alive = () =>
      thumbToken === this.thumbToken &&
      this.api.photo(photo.id) === photo &&
      photo.version === version &&
      this.api.session() === session &&
      this.panel.isConnected;

    const list = [...this.api.lutBook().list()];
    const gpu = this.api.lutGpu();

    for (const record of list) {
      if (!alive()) return;
      if (!this.isUsable(record)) continue;
      const cacheKey = this.thumbCacheKey(record);
      if (lookCache.get(photo, cacheKey)) continue;

      while (alive() && (this.hooks.dragging() || exportPaused())) {
        await new Promise((done) => setTimeout(done, 120));
      }
      if (!alive()) return;

      const built = await gpu.ensure(record);
      if (!alive()) return;
      if (!built.ok) continue;

      const recKey = mode === 'raw' ? this.hooks.recipeGpuKey() || NEUTRAL_BAKE_KEY : null;
      const out = await engine.thumb({
        source,
        look: { recipe: recKey, lut: built.token, lutStrength: 1, curve: null, adjust: ADJUST },
        geometry: GEOMETRY,
        aspect: null,
        maxEdge: 192,
      });
      if (!out || !alive()) return;
      const bitmap = await createImageBitmap(new ImageData(new Uint8ClampedArray(out.data), out.w, out.h));
      if (!alive()) {
        bitmap.close();
        return;
      }
      if (lookCache.get(photo, cacheKey)) {
        bitmap.close();
        continue;
      }
      lookCache.put(photo, cacheKey, version, bitmap, `lut:${record.cube_id}`);
      this.paintThumbs();
    }

    // 如果后台生成小图超过 3 槽、把舞台当前用的 LUT 挤出显卡缓存了，马上给舞台补回来。
    if (alive() && this.stageReadyToken && !gpu.has(this.stageReadyToken)) {
      await this.applyStageLut();
    }
  }
}

const ids = new WeakMap<ImageBitmap, number>();
let nextId = 1;

function bitmapId(bitmap: ImageBitmap): number {
  let id = ids.get(bitmap);
  if (!id) {
    id = nextId++;
    ids.set(bitmap, id);
  }
  return id;
}
