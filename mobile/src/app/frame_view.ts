// 03f 边框标签页：13 种样式缩略卡、颜色色块、粗细、标志/参数/日期/更多 chip、时间戳行与底部工作表联动。

import {
  COLOR_HEX,
  COLOR_NAMES,
  FRAME_NAMES,
  FRAME_ORDER,
  stampSummary,
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
  commitFrame,
  commitSelectStyle,
  commitStamp,
  ensureFrameMeta,
  photoExportSize,
  resolveCardLayouts,
} from './frame_state';
import { openLogoSheet } from './logo_sheet';
import { logoStore } from './logo_store';
import type { Photo } from './model';
import { openStampSheet } from './stamp_sheet';
import { showToast } from './toast';

const CHECK_ICON = 'M5 12.5l4.5 4.5L19 7.5';
const CARD_EDGE = 192;

export interface FrameHost {
  ew: () => [number, number];
  backdrop: () => HTMLCanvasElement | null;
  onChange: () => void;
}

export class FrameView {
  private readonly root: HTMLElement;
  private readonly strip: HTMLElement;
  private readonly controls: HTMLElement;
  private readonly stampTitleSummary: HTMLElement;
  private readonly stampSwitch: HTMLInputElement;
  private readonly holders = new Map<FrameStyle, { card: HTMLButtonElement; thumb: HTMLElement }>();
  private tabOpen = false;
  private cardLayouts = new Map<FrameStyle, FrameLayoutData | null>();
  private layoutSig = '';
  private drawnSig = '';
  private cardToken = 0;
  private frameQueue = 0;
  private destroyed = false;

  constructor(
    private readonly shell: HTMLElement,
    private readonly photo: Photo,
    private readonly host: FrameHost,
  ) {
    this.root = el('div', 'panel frame');
    this.root.dataset.panel = 'frame';
    this.strip = el('div', 'strip inpanel');
    this.controls = el('div', 'fcontrols');

    for (const style of FRAME_ORDER) {
      const card = button('rc', '');
      card.dataset.style = style;
      const thumb = el('div', 'th fr skel');
      thumb.setAttribute('aria-busy', 'true');
      const name = el('div', 'nm', FRAME_NAMES[style]);
      card.append(thumb, name);
      card.addEventListener('click', () => {
        if (this.photo.border.style === style) return;
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

    const stampRow = el('div', 'stamp-row');
    const stampBtn = button('stamp-open', '');
    stampBtn.dataset.act = 'open-stamp';
    this.stampTitleSummary = el('small', '', stampSummary(this.photo.stamp, this.photo.border));
    stampBtn.append(el('b', '', '时间戳'), this.stampTitleSummary);
    stampBtn.addEventListener('click', () => {
      openStampSheet(this.shell, this.photo, () => this.changed());
    });

    this.stampSwitch = document.createElement('input');
    this.stampSwitch.type = 'checkbox';
    this.stampSwitch.className = 'switch';
    this.stampSwitch.checked = this.photo.stamp.enabled;
    this.stampSwitch.setAttribute('aria-label', '时间戳');
    this.stampSwitch.addEventListener('change', () => {
      commitStamp(this.photo, { enabled: this.stampSwitch.checked }, () => this.changed());
    });
    stampRow.append(stampBtn, this.stampSwitch);

    this.root.append(this.strip, this.controls, stampRow);
    this.syncSelection();
    this.renderControls();
  }

  panelEl(): HTMLElement {
    return this.root;
  }

  tabOpened(): void {
    this.tabOpen = true;
    void ensureFrameMeta(this.photo).then(() => {
      if (!this.destroyed && this.tabOpen) void this.paintCards();
    });
    if (!logoStore.loaded()) {
      void logoStore.ensureLoaded().then(() => {
        if (!this.destroyed && this.tabOpen) void this.paintCards();
      });
    }
    void this.paintCards();
  }

  tabClosed(): void {
    this.tabOpen = false;
    cancelAnimationFrame(this.frameQueue);
    this.frameQueue = 0;
  }

  backdropUpdated(): void {
    if (this.tabOpen) void this.paintCards();
  }

  refresh(): void {
    this.syncSelection();
    this.renderControls();
    this.stampSwitch.checked = this.photo.stamp.enabled;
    this.stampTitleSummary.textContent = stampSummary(this.photo.stamp, this.photo.border);
    if (this.tabOpen) void this.paintCards();
  }

  destroy(): void {
    this.destroyed = true;
    this.cardToken += 1;
    cancelAnimationFrame(this.frameQueue);
    this.frameQueue = 0;
  }

  private changed(): void {
    this.refresh();
    this.host.onChange();
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

    // 颜色色块 + 粗细分段
    if (opts.includes('color') || opts.includes('weight')) {
      const row = el('div', 'pad row fctl-row');
      if (opts.includes('color')) {
        row.append(el('span', 'dim fctl-lab', '颜色'));
        const colors: FrameColor[] = cur.style === 'slide_mount' ? ['cream', 'black'] : ['white', 'black', 'cream'];
        for (const color of colors) {
          const sw = button(cur.color === color ? 'swc on' : 'swc', '');
          sw.dataset.color = color;
          sw.setAttribute('aria-label', COLOR_NAMES[color]);
          const dot = el('i');
          dot.style.background = COLOR_HEX[color];
          sw.append(dot);
          sw.addEventListener('click', () => {
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
            commitFrame(this.photo, { weight }, () => this.changed());
          });
          seg.append(btn);
        });
        row.append(seg);
      }
      this.controls.append(row);
    }

    // 标志 / 参数 / 日期 / 更多
    const chips = el('div', 'chips');
    if (opts.includes('logo')) {
      const logoOn = cur.show_logo && cur.logo !== 'none';
      const logoChip = button(logoOn ? 'chip on' : 'chip', '标志');
      logoChip.dataset.act = 'frame-logo';
      logoChip.addEventListener('click', () => {
        openLogoSheet(this.shell, this.photo, () => this.changed());
      });
      chips.append(logoChip);
    }
    if (opts.includes('show_params')) {
      const paramChip = button(cur.show_params ? 'chip on' : 'chip', '参数');
      paramChip.dataset.act = 'frame-params';
      paramChip.addEventListener('click', () => {
        commitFrame(this.photo, { show_params: !this.photo.border.show_params }, () => this.changed());
      });
      chips.append(paramChip);
    }
    if (opts.includes('show_date')) {
      const dateChip = button(cur.show_date ? 'chip on' : 'chip', '日期');
      dateChip.dataset.act = 'frame-date';
      dateChip.addEventListener('click', () => {
        commitFrame(this.photo, { show_date: !this.photo.border.show_date }, () => this.changed());
      });
      chips.append(dateChip);
    }
    const moreChip = button('chip push', '更多');
    moreChip.dataset.act = 'frame-more';
    moreChip.addEventListener('click', () => {
      openFrameMoreSheet(this.shell, this.photo, () => this.changed());
    });
    chips.append(moreChip);
    this.controls.append(chips);
  }

  private async paintCards(): Promise<void> {
    if (this.destroyed || !this.tabOpen) return;
    const base = this.host.backdrop();
    if (!base) return;

    const [ew, eh] = this.host.ew();
    const cur = this.photo.border;
    const logoInfo = cur.logo !== 'ricoh' && cur.logo !== 'none' ? logoStore.info(cur.logo) : null;
    const layoutKey = JSON.stringify([
      this.photo.version,
      ew,
      eh,
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

    const token = ++this.cardToken;
    if (this.layoutSig !== layoutKey) {
      const layouts = await resolveCardLayouts(this.photo, ew, eh, () => {
        if (!this.destroyed && this.tabOpen) {
          this.drawnSig = '';
          void this.paintCards();
        }
      });
      if (this.destroyed || token !== this.cardToken) return;
      this.cardLayouts.clear();
      this.cardLayouts.set('none', null);
      for (const style of FRAME_ORDER) {
        if (style !== 'none') this.cardLayouts.set(style, layouts[style] ?? null);
      }
      this.layoutSig = layoutKey;
      this.drawnSig = '';
    }

    const drawKey = `${layoutKey}|${base.width}x${base.height}|${logoStore.revision()}|${this.photo.rev}`;
    if (this.drawnSig === drawKey) return;
    this.drawnSig = drawKey;

    cancelAnimationFrame(this.frameQueue);
    const styles = [...FRAME_ORDER];
    let index = 0;

    const drawOne = (style: FrameStyle): void => {
      const holder = this.holders.get(style);
      if (!holder) return;
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
            photo: base,
            backdrop: base,
            onLogo: () => {
              this.drawnSig = '';
              if (!this.destroyed && this.tabOpen) void this.paintCards();
            },
          },
        );
      }
      thumb.classList.remove('skel');
      thumb.removeAttribute('aria-busy');
      if (!canvas.parentElement) thumb.prepend(canvas);
    };

    const batch = (): void => {
      this.frameQueue = 0;
      if (this.destroyed || token !== this.cardToken || !this.tabOpen) return;
      const end = Math.min(styles.length, index + 3);
      while (index < end) {
        drawOne(styles[index++]);
      }
      if (index < styles.length) {
        this.frameQueue = requestAnimationFrame(batch);
      }
    };
    batch();
  }
}

export function cardSizeHint(photo: Photo): [number, number] {
  return photoExportSize(photo, null);
}
