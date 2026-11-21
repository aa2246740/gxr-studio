// 128pt 单带里的边框视图：款式卡横条（13 种样式）与选项子页（颜色、粗细、标志/参数/日期/更多/时间戳）。

import {
  COLOR_HEX,
  COLOR_NAMES,
  FRAME_NAMES,
  FRAME_ORDER,
  STYLE_OPTIONS,
  WEIGHT_NAMES,
  type FrameColor,
  type FrameLayoutData,
  type FrameStyle,
  type FrameWeight,
} from '../lib/frame_spec';
import { button, el, icon } from './dom';
import { drawDecor } from './finish_draw';
import { openFrameMoreSheet } from './frame_more_sheet';
import {
  autoMonoMessage,
  checkMissingLogo,
  commitFrame,
  commitSelectStyle,
  ensureCroppedPhotoSize,
  ensureFrameMeta,
  isPhotoCropped,
  photoExportSize,
  photoSourceSize,
  resolveCardLayouts,
} from './frame_state';
import { openLogoSheet } from './logo_sheet';
import { logoStore } from './logo_store';
import type { Photo } from './model';
import { cancelHostSheets } from './sheet_lifecycle';
import { openStampSheet } from './stamp_sheet';
import { showToast } from './toast';

const CHECK_ICON = 'M5 12.5l4.5 4.5L19 7.5';
const CARD_EDGE = 192;

export interface FrameHost {
  ew: () => [number, number];
  backdrop: () => HTMLCanvasElement | null;
  backdropKey?: () => string;
  onChange: () => void;
  isAlive?: () => boolean;
  onOptionsChange?: (open: boolean) => void;
  onBack?: () => void;
}

export class FrameView {
  private readonly shell: HTMLElement;
  private readonly photo: Photo;
  private readonly host: FrameHost;
  private readonly head: HTMLElement;
  private readonly headTitle: HTMLElement;
  private readonly optsBtn: HTMLButtonElement;
  private readonly ipadTitle: HTMLElement;
  private readonly ipadOptsBtn: HTMLButtonElement;
  private readonly root: HTMLElement;
  private readonly strip: HTMLElement;
  private readonly controls: HTMLElement;
  private readonly holders = new Map<FrameStyle, { card: HTMLButtonElement; thumb: HTMLElement }>();
  private readonly unsubLogo: () => void;
  private tabOpen = false;
  private optionsOpen = false;
  private cardLayouts = new Map<FrameStyle, FrameLayoutData | null>();
  private layoutSig = '';
  private drawnSig = '';
  private paintingSig = '';
  private layoutToken = 0;
  private paintToken = 0;
  private frameQueue = 0;
  private destroyed = false;

  constructor(shell: HTMLElement, photo: Photo, host: FrameHost) {
    this.shell = shell;
    this.photo = photo;
    this.host = host;

    this.head = el('div', 'band-frame-hd');
    this.headTitle = el('b', 'frame-hd-name', FRAME_NAMES[photo.border.style] || '无');
    this.optsBtn = button('band-act frame-opts-btn', '设置');
    this.optsBtn.dataset.act = 'frame-opts';
    this.optsBtn.addEventListener('click', () => {
      if (this.photo.border.style !== 'none') this.setOptionsOpen(true);
    });
    this.head.append(this.headTitle, this.optsBtn);

    const subHd = el('div', 'ipad-sub-hd');
    const subBack = button('ipad-sub-back', '‹ 更多');
    subBack.dataset.act = 'more-back';
    subBack.addEventListener('click', () => this.host.onBack?.());
    this.ipadTitle = el('b', 'ipad-sub-title', FRAME_NAMES[photo.border.style] || '无');
    this.ipadOptsBtn = button('ipad-sub-act', '设置');
    this.ipadOptsBtn.dataset.act = 'frame-opts-ipad';
    this.ipadOptsBtn.addEventListener('click', () => {
      if (this.photo.border.style !== 'none') this.setOptionsOpen(true);
    });
    subHd.append(subBack, this.ipadTitle, this.ipadOptsBtn);

    this.root = el('div', 'panel frame');
    this.root.dataset.panel = 'frame';
    this.strip = el('div', 'strip');
    this.controls = el('div', 'fcontrols');
    this.controls.hidden = true;

    for (const style of FRAME_ORDER) {
      const card = button('rc', '');
      card.dataset.style = style;
      const thumb = el('div', 'th fr skel');
      thumb.setAttribute('aria-busy', 'true');
      const name = el('div', 'nm', FRAME_NAMES[style]);
      card.append(thumb, name);
      card.addEventListener('click', () => {
        if (!this.aliveNow()) return;
        if (this.photo.border.style === style) {
          if (style !== 'none') this.setOptionsOpen(true);
          return;
        }
        commitSelectStyle(this.photo, style, () => {
          const monoMsg = autoMonoMessage(this.photo.border);
          if (monoMsg) showToast(this.shell, monoMsg);
          this.refresh();
          this.host.onChange();
        });
      });
      this.holders.set(style, { card, thumb });
      this.strip.append(card);
    }

    this.unsubLogo = logoStore.onChange(() => {
      queueMicrotask(() => {
        queueMicrotask(() => {
          if (!this.aliveNow()) return;
          if (checkMissingLogo(this.photo, (msg) => showToast(this.shell, msg))) {
            this.photo.rev += 1;
            this.changed();
          } else if (this.tabOpen) {
            void this.paintCards();
          }
        });
      });
    });

    this.root.append(subHd, this.strip, this.controls);
    this.syncSelection();
    this.syncSubView();
  }

  private aliveNow(): boolean {
    return !this.destroyed && (this.host.isAlive?.() ?? true);
  }

  headEl(): HTMLElement {
    return this.head;
  }

  panelEl(): HTMLElement {
    return this.root;
  }

  isOptionsOpen(): boolean {
    return this.optionsOpen;
  }

  private setOptionsOpen(open: boolean): void {
    const next = open && this.photo.border.style !== 'none';
    if (this.optionsOpen === next) return;
    this.optionsOpen = next;
    this.syncSubView();
    this.host.onOptionsChange?.(this.optionsOpen);
  }

  tabOpened(): void {
    if (!this.aliveNow()) return;
    this.tabOpen = true;
    const version = this.photo.version;
    void ensureFrameMeta(this.photo).then(() => {
      if (this.aliveNow() && this.tabOpen && this.photo.version === version) void this.paintCards();
    });
    if (!logoStore.loaded()) {
      void logoStore.ensureLoaded().then(() => {
        if (this.aliveNow() && this.tabOpen && this.photo.version === version) void this.paintCards();
      });
    }
    void this.paintCards();
  }

  tabClosed(): void {
    this.tabOpen = false;
    this.layoutToken += 1;
    this.paintToken += 1;
    this.paintingSig = '';
    cancelAnimationFrame(this.frameQueue);
    this.frameQueue = 0;
  }

  backdropUpdated(): void {
    if (this.aliveNow() && this.tabOpen) void this.paintCards();
  }

  refresh(): void {
    if (!this.aliveNow()) return;
    this.syncSelection();
    this.syncSubView();
    if (this.tabOpen) void this.paintCards();
  }

  destroy(): void {
    cancelHostSheets(this.shell);
    this.destroyed = true;
    this.unsubLogo();
    this.tabOpen = false;
    this.layoutToken += 1;
    this.paintToken += 1;
    this.paintingSig = '';
    cancelAnimationFrame(this.frameQueue);
    this.frameQueue = 0;
    for (const { thumb } of this.holders.values()) {
      for (const cv of thumb.querySelectorAll('canvas')) {
        cv.width = 0;
        cv.height = 0;
        cv.remove();
      }
    }
    this.cardLayouts.clear();
    this.holders.clear();
  }

  private changed(): void {
    if (!this.aliveNow()) return;
    this.refresh();
    this.host.onChange();
  }

  private syncSubView(): void {
    const style = this.photo.border.style;
    if (style === 'none' && this.optionsOpen) {
      this.optionsOpen = false;
      this.host.onOptionsChange?.(false);
    }
    const name = FRAME_NAMES[style] || '无';
    this.headTitle.textContent = name;
    this.ipadTitle.textContent = name;
    const hideOpts = style === 'none' || this.optionsOpen;
    this.optsBtn.hidden = hideOpts;
    this.ipadOptsBtn.hidden = hideOpts;
    this.strip.hidden = this.optionsOpen;
    this.controls.hidden = !this.optionsOpen;
    this.renderControls();
  }

  private syncSelection(): void {
    const active = this.photo.border.style;
    for (const [style, { card, thumb }] of this.holders) {
      const on = style === active;
      card.classList.toggle('on', on);
      card.setAttribute('aria-pressed', on ? 'true' : 'false');
      const existingCheck = thumb.querySelector('.ckk');
      if (on && !existingCheck) {
        const badge = el('span', 'ckk');
        badge.append(icon(CHECK_ICON));
        thumb.append(badge);
      } else if (!on && existingCheck) {
        existingCheck.remove();
      }
    }
  }

  private renderControls(): void {
    this.controls.replaceChildren();
    const cur = this.photo.border;
    if (cur.style === 'none') return;
    const opts = STYLE_OPTIONS[cur.style];

    // 第一行：‹ 边框 + 颜色色块 + 粗细分段
    const row = el('div', 'row fctl-row');
    const backBtn = button('chip frame-back', '‹ 边框');
    backBtn.dataset.act = 'frame-back';
    backBtn.addEventListener('click', () => this.setOptionsOpen(false));
    row.append(backBtn);

    if (opts.includes('color')) {
      const colors: FrameColor[] = cur.style === 'slide_mount' ? ['cream', 'black'] : ['white', 'black', 'cream'];
      for (const color of colors) {
        const sw = button(cur.color === color ? 'swc on' : 'swc', '');
        sw.dataset.color = color;
        sw.setAttribute('aria-label', COLOR_NAMES[color]);
        const dot = el('i');
        dot.style.background = COLOR_HEX[color];
        sw.append(dot);
        sw.addEventListener('click', () => {
          if (!this.aliveNow()) return;
          commitFrame(this.photo, { color }, () => {
            const monoMsg = autoMonoMessage(this.photo.border);
            if (monoMsg) showToast(this.shell, monoMsg);
            this.changed();
          });
        });
        row.append(sw);
      }
    }
    if (opts.includes('weight')) {
      const seg = el('div', 'seg push');
      seg.setAttribute('role', 'group');
      seg.setAttribute('aria-label', '粗细');
      (['thin', 'medium', 'thick'] as FrameWeight[]).forEach((weight) => {
        const btn = button(cur.weight === weight ? 'on' : '', WEIGHT_NAMES[weight]);
        btn.dataset.weight = weight;
        btn.addEventListener('click', () => {
          if (!this.aliveNow()) return;
          commitFrame(this.photo, { weight }, () => this.changed());
        });
        seg.append(btn);
      });
      row.append(seg);
    }
    this.controls.append(row);

    // 第二行：标志 / 参数 / 日期 / 更多 / 时间戳
    const chips = el('div', 'chips');
    if (opts.includes('logo')) {
      const logoOn = cur.show_logo && cur.logo !== 'none';
      const logoChip = button(logoOn ? 'chip on' : 'chip', '标志');
      logoChip.dataset.act = 'frame-logo';
      logoChip.addEventListener('click', () => {
        if (!this.aliveNow()) return;
        openLogoSheet(this.shell, this.photo, () => this.changed(), () => this.aliveNow());
      });
      chips.append(logoChip);
    }
    if (opts.includes('show_params')) {
      const paramChip = button(cur.show_params ? 'chip on' : 'chip', '参数');
      paramChip.dataset.act = 'frame-params';
      paramChip.addEventListener('click', () => {
        if (!this.aliveNow()) return;
        commitFrame(this.photo, { show_params: !this.photo.border.show_params }, () => this.changed());
      });
      chips.append(paramChip);
    }
    if (opts.includes('show_date')) {
      const dateChip = button(cur.show_date ? 'chip on' : 'chip', '日期');
      dateChip.dataset.act = 'frame-date';
      dateChip.addEventListener('click', () => {
        if (!this.aliveNow()) return;
        commitFrame(this.photo, { show_date: !this.photo.border.show_date }, () => this.changed());
      });
      chips.append(dateChip);
    }
    const moreChip = button('chip', '更多');
    moreChip.dataset.act = 'frame-more';
    moreChip.addEventListener('click', () => {
      if (!this.aliveNow()) return;
      openFrameMoreSheet(this.shell, this.photo, () => this.changed(), () => this.aliveNow());
    });
    chips.append(moreChip);

    const stampChip = button(this.photo.stamp.enabled ? 'chip on' : 'chip', '时间戳');
    stampChip.dataset.act = 'open-stamp';
    stampChip.addEventListener('click', () => {
      if (!this.aliveNow()) return;
      openStampSheet(this.shell, this.photo, () => this.changed(), () => this.aliveNow());
    });
    chips.append(stampChip);

    this.controls.append(chips);
  }

  private async paintCards(): Promise<void> {
    if (!this.aliveNow() || !this.tabOpen) return;
    const initialBase = this.host.backdrop();
    if (!initialBase || initialBase.width <= 0 || initialBase.height <= 0) return;

    const version = this.photo.version;
    if (isPhotoCropped(this.photo) && photoSourceSize(this.photo)) {
      const myCheck = this.layoutToken;
      await ensureCroppedPhotoSize(this.photo);
      if (!this.aliveNow() || !this.tabOpen || myCheck !== this.layoutToken || this.photo.version !== version) return;
    }
    const [ew, eh] = this.host.ew();
    const cur = this.photo.border;
    const logoInfo = cur.logo !== 'ricoh' && cur.logo !== 'none' ? logoStore.info(cur.logo) : null;
    const layoutKey = JSON.stringify([
      version,
      ew,
      eh,
      this.photo.crop ?? null,
      this.photo.geometry?.straighten ?? 0,
      this.photo.frameMeta?.frame ?? null,
      cur.color,
      cur.weight,
      cur.show_logo,
      cur.show_params,
      cur.show_date,
      cur.text,
      cur.output_ratio,
      cur.fmt,
      cur.table,
      cur.logo,
      cur.logo_mode,
      cur.logo_size,
      cur.model,
      logoInfo,
    ]);

    if (this.layoutSig !== layoutKey) {
      const myLayout = ++this.layoutToken;
      let layouts: Record<string, FrameLayoutData | null>;
      try {
        layouts = await resolveCardLayouts(
          this.photo,
          ew,
          eh,
          () => {
            if (this.aliveNow() && this.tabOpen && this.photo.version === version) {
              this.drawnSig = '';
              this.paintingSig = '';
              void this.paintCards();
            }
          },
          () => this.aliveNow() && this.tabOpen && myLayout === this.layoutToken && this.photo.version === version,
        );
      } catch {
        return;
      }
      if (!this.aliveNow() || !this.tabOpen || myLayout !== this.layoutToken || this.photo.version !== version) return;
      this.cardLayouts.clear();
      this.cardLayouts.set('none', null);
      for (const style of FRAME_ORDER) {
        if (style !== 'none') this.cardLayouts.set(style, layouts[style] ?? null);
      }
      this.layoutSig = layoutKey;
      this.drawnSig = '';
      this.paintingSig = '';
    }

    const base = this.host.backdrop();
    if (!base || base.width <= 0 || base.height <= 0) return;
    const backdropKey = this.host.backdropKey?.() ?? '';
    const drawKey = `${layoutKey}|${base.width}x${base.height}|${backdropKey}|${logoStore.revision()}|${this.photo.rev}`;
    if (this.drawnSig === drawKey || (this.frameQueue && this.paintingSig === drawKey)) return;

    const myPaint = ++this.paintToken;
    this.paintingSig = drawKey;
    this.drawnSig = '';
    cancelAnimationFrame(this.frameQueue);
    this.frameQueue = 0;

    const styles = [...FRAME_ORDER];
    let index = 0;

    const drawOne = (style: FrameStyle): boolean => {
      const activeBase = this.host.backdrop();
      if (!activeBase || activeBase.width <= 0 || activeBase.height <= 0) return false;
      const holder = this.holders.get(style);
      if (!holder) return true;
      const { thumb } = holder;
      const frameLayout = style === 'none' ? null : (this.cardLayouts.get(style) ?? null);
      const dw = frameLayout ? frameLayout.width : ew;
      const dh = frameLayout ? frameLayout.height : eh;
      const dpr = Math.min(2, globalThis.devicePixelRatio || 1);
      const scale = (CARD_EDGE * dpr) / Math.max(dw, dh);
      const canvas = thumb.querySelector('canvas') ?? document.createElement('canvas');
      canvas.setAttribute('aria-hidden', 'true');
      canvas.width = Math.max(1, Math.round(dw * scale));
      canvas.height = Math.max(1, Math.round(dh * scale));
      const ctx = canvas.getContext('2d');
      if (ctx) {
        drawDecor(
          ctx,
          {
            key: `card:${style}`,
            frame: frameLayout,
            stamp: null,
            stampOrigin: null,
            ew,
            eh,
            width: dw,
            height: dh,
          },
          scale,
          {
            photo: activeBase,
            backdrop: activeBase,
            onLogo: () => {
              this.drawnSig = '';
              this.paintingSig = '';
              if (this.aliveNow() && this.tabOpen && this.photo.version === version) void this.paintCards();
            },
          },
        );
      }
      thumb.classList.remove('skel');
      thumb.removeAttribute('aria-busy');
      if (!canvas.parentElement) thumb.prepend(canvas);
      return true;
    };

    const batch = (): void => {
      this.frameQueue = 0;
      if (!this.aliveNow() || myPaint !== this.paintToken || !this.tabOpen || this.photo.version !== version) return;
      const end = Math.min(styles.length, index + 3);
      while (index < end) {
        if (!drawOne(styles[index++])) {
          this.paintingSig = '';
          return;
        }
      }
      if (index < styles.length) {
        this.frameQueue = requestAnimationFrame(batch);
      } else {
        this.paintingSig = '';
        this.drawnSig = drawKey;
      }
    };
    batch();
  }
}

export function cardSizeHint(photo: Photo): [number, number] {
  return photoExportSize(photo, null);
}
