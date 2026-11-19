// 128pt 单带里的裁剪页：比例片（原始..小红书 3:4）、显式「横竖对调」键、参考线选择、±45° 标尺拉直与 CoreError 提示。

import { geomOutputSizes } from '../core/api';
import {
  centeredMaxRect,
  clamp,
  cropIsPortrait,
  croppedPixelSize,
  GUIDES,
  guideLabel,
  loadGuidePrefs,
  MAX_ANGLE,
  ORIENTABLE,
  PRESETS,
  presetLabel,
  presetMatchesSpec,
  rectFromArray,
  saveGuidePrefs,
  sideSubset,
  type CanvasDims,
  type CropState,
  type GuideId,
  type Orientation,
} from '../lib/crop';
import { button, el, icon } from './dom';
import type { Photo } from './model';
import { registerSheet } from './sheet_lifecycle';

const CHECK = 'M5 12.5l4.5 4.5L19 7.5';
const FLIP_ICON = 'M7 16V4m0 0L3 8m4-4l4 4M17 8v12m0 0l4-4m-4 4l-4-4';
const GUIDE_ICON = 'M3 9h18M3 15h18M9 3v18M15 3v18';
const PX_PER_DEG = 18;

export interface CropPanelHooks {
  photo(): Photo | null;
  canvas(): CanvasDims;
  liveStraighten(): number;
  liveCrop(): CropState | null;
  pickPreset(preset: string, orient?: Orientation): void;
  pickOrientation(orient: Orientation): void;
  guidesChanged(): void;
  beginStraighten(): void;
  previewStraighten(angle: number): void;
  endStraighten(): void;
  cancelStraighten(): void;
  resetStraighten(): void;
  onExportSize?: (photo: Photo, width: number, height: number) => void;
}

function fmtDeg(v: number): string {
  const rounded = Math.round(v * 10) / 10;
  const clean = Math.abs(rounded) < 0.05 ? 0 : rounded;
  if (clean < 0) return `−${Math.abs(clean).toFixed(1)}°`;
  return `${clean.toFixed(1)}°`;
}

export class CropPanel {
  private readonly shell: HTMLElement;
  private readonly hooks: CropPanelHooks;
  private readonly head = el('div', 'band-crop-hd');
  private readonly panel = el('div', 'panel crop-panel');
  private readonly chipsWrap = el('div', 'chips');
  private readonly chips: Array<{ spec: (typeof PRESETS)[number]; btn: HTMLButtonElement }> = [];
  private readonly flipBtn = button('crop-act crop-flip', '横竖对调');
  private readonly guideBtn = button('crop-act guide-btn', '');
  private readonly guideText = el('span', 'guide-txt', '参考线');
  private readonly straightenBox = el('div', 'pad straighten');
  private readonly angleVal = el('span', 'crop-angle num', '0.0°');
  private readonly ruler = el('div', 'ruler');
  private readonly tape = el('div', 'ruler-tape');
  private readonly sizeLine = el('span', 'dim small num crop-size-line', '');
  private readonly errBanner = el('div', 'bn crop-err');
  private readonly errText = el('span', 'tx', '');
  private currentPortrait = false;
  private sizeSeq = 0;
  private destroyed = false;
  private lastTap = { at: 0, x: 0, y: 0 };
  private unregGuide: (() => void) | null = null;

  constructor(shell: HTMLElement, hooks: CropPanelHooks) {
    this.shell = shell;
    this.hooks = hooks;
    this.build();
  }

  headEl(): HTMLElement {
    return this.head;
  }

  panelEl(): HTMLElement {
    return this.panel;
  }

  showError(message: string | null): void {
    if (!message) {
      this.errBanner.hidden = true;
      this.errText.textContent = '';
      return;
    }
    this.errText.textContent = message;
    this.errBanner.hidden = false;
  }

  private build(): void {
    this.panel.dataset.tab = 'crop';
    this.head.append(this.sizeLine, this.angleVal);

    for (const spec of PRESETS) {
      const chip = button('chip', spec.label);
      chip.dataset.preset = spec.id;
      if (spec.orient) chip.dataset.orient = spec.orient;
      chip.addEventListener('click', () => {
        this.showError(null);
        this.hooks.pickPreset(spec.id, spec.orient);
      });
      this.chipsWrap.append(chip);
      this.chips.push({ spec, btn: chip });
    }

    this.flipBtn.dataset.act = 'crop-flip';
    this.flipBtn.prepend(icon(FLIP_ICON));
    this.flipBtn.addEventListener('click', () => {
      if (this.flipBtn.disabled) return;
      this.showError(null);
      this.hooks.pickOrientation(this.currentPortrait ? 'landscape' : 'portrait');
    });

    this.guideBtn.type = 'button';
    this.guideBtn.dataset.act = 'guides';
    this.guideBtn.append(icon(GUIDE_ICON), this.guideText);
    this.guideBtn.addEventListener('click', () => this.openGuideSheet());

    const actions = el('div', 'crop-acts');
    actions.append(this.flipBtn, this.guideBtn);

    const topRow = el('div', 'crop-top-row');
    topRow.append(this.chipsWrap, actions);

    this.ruler.setAttribute('role', 'slider');
    this.ruler.setAttribute('aria-label', '拉直');
    this.ruler.setAttribute('aria-valuemin', String(-MAX_ANGLE));
    this.ruler.setAttribute('aria-valuemax', String(MAX_ANGLE));
    this.ruler.setAttribute('aria-valuenow', '0');
    this.ruler.tabIndex = 0;
    this.buildTicks();
    const needle = el('i', 'ruler-needle');
    this.ruler.append(this.tape, needle);
    this.bindRuler();

    this.straightenBox.append(this.ruler);

    this.errBanner.hidden = true;
    this.errBanner.dataset.role = 'crop-error';
    this.errBanner.append(this.errText);

    this.panel.append(topRow, this.straightenBox, this.errBanner);
    this.sync();
  }

  private buildTicks(): void {
    const mid = MAX_ANGLE * PX_PER_DEG;
    this.tape.style.width = `${MAX_ANGLE * 2 * PX_PER_DEG + 1}px`;
    for (let deg = -MAX_ANGLE; deg <= MAX_ANGLE; deg += 1) {
      const major = deg % 5 === 0;
      const tick = document.createElement('i');
      tick.className = major ? 'tick major' : 'tick';
      tick.style.left = `${mid + deg * PX_PER_DEG}px`;
      this.tape.append(tick);
      if (major) {
        const label = document.createElement('span');
        label.className = 'tnum num';
        label.style.left = `${mid + deg * PX_PER_DEG}px`;
        label.textContent = deg < 0 ? `−${Math.abs(deg)}°` : `${deg}°`;
        this.tape.append(label);
      }
    }
  }

  private bindRuler(): void {
    let activeId = -1;
    let startX = 0;
    let startAngle = 0;
    let moved = false;

    this.ruler.addEventListener('pointerdown', (ev) => {
      if (ev.button !== 0 || activeId !== -1) return;
      ev.preventDefault();
      this.showError(null);
      activeId = ev.pointerId;
      startX = ev.clientX;
      startAngle = this.hooks.liveStraighten();
      moved = false;
      try {
        this.ruler.setPointerCapture(ev.pointerId);
      } catch {
        /* ignore */
      }
      this.hooks.beginStraighten();
    });

    this.ruler.addEventListener('pointermove', (ev) => {
      if (ev.pointerId !== activeId) return;
      const dx = ev.clientX - startX;
      if (Math.abs(dx) > 2) moved = true;
      const raw = startAngle - dx / PX_PER_DEG;
      const next = clamp(Math.round(raw * 10) / 10, -MAX_ANGLE, MAX_ANGLE);
      this.paintAngle(next);
      this.hooks.previewStraighten(next);
    });

    const finish = (ev: PointerEvent) => {
      if (ev.pointerId !== activeId) return;
      activeId = -1;
      if (this.ruler.hasPointerCapture?.(ev.pointerId)) {
        try {
          this.ruler.releasePointerCapture(ev.pointerId);
        } catch {
          /* ignore */
        }
      }
      if (ev.type === 'pointercancel') {
        this.hooks.cancelStraighten();
        this.paintAngle(this.hooks.liveStraighten());
        return;
      }
      this.hooks.endStraighten();
      if (!moved) {
        const now = performance.now();
        const close = Math.hypot(ev.clientX - this.lastTap.x, ev.clientY - this.lastTap.y) < 16;
        if (now - this.lastTap.at < 320 && close) {
          this.lastTap = { at: 0, x: 0, y: 0 };
          this.hooks.resetStraighten();
          return;
        }
        this.lastTap = { at: now, x: ev.clientX, y: ev.clientY };
      }
    };

    this.ruler.addEventListener('pointerup', finish);
    this.ruler.addEventListener('pointercancel', finish);
    this.ruler.addEventListener('dblclick', () => {
      this.showError(null);
      this.hooks.resetStraighten();
    });
    this.ruler.addEventListener('keydown', (ev) => {
      if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
      ev.preventDefault();
      const step = ev.shiftKey ? 0.5 : 0.1;
      const delta = ev.key === 'ArrowRight' ? step : -step;
      const next = clamp(Math.round((this.hooks.liveStraighten() + delta) * 10) / 10, -MAX_ANGLE, MAX_ANGLE);
      this.paintAngle(next);
      this.hooks.previewStraighten(next);
      this.hooks.endStraighten();
    });
  }

  private paintAngle(angle: number): void {
    this.angleVal.textContent = fmtDeg(angle);
    this.ruler.setAttribute('aria-valuenow', String(angle));
    const mid = MAX_ANGLE * PX_PER_DEG;
    this.tape.style.transform = `translate3d(${-mid - angle * PX_PER_DEG}px, 0, 0)`;
  }

  private openGuideSheet(): void {
    this.closeGuideSheet();
    const cur = loadGuidePrefs().guide;
    const scrim = el('div', 'scrim guide-scrim');
    const sheet = el('div', 'ms guide-sheet');
    const head = el('div', 'hd');
    head.append(el('h2', '', '参考线'));
    sheet.append(el('div', 'grab'), head);
    for (const [id, label] of GUIDES) {
      const row = button('lrow guide-item', '');
      row.dataset.guide = id;
      const text = el('div', 'grow');
      text.append(el('b', '', label));
      const mark = el('span', 'gchk');
      if (id === cur) {
        row.classList.add('on');
        mark.append(icon(CHECK));
      }
      row.append(text, mark);
      row.addEventListener('click', () => {
        saveGuidePrefs({ guide: id as GuideId });
        this.closeGuideSheet();
        this.syncGuideLabel();
        this.hooks.guidesChanged();
      });
      sheet.append(row);
    }
    scrim.addEventListener('click', () => this.closeGuideSheet());
    this.unregGuide = registerSheet(this.shell, () => this.closeGuideSheet());
    this.shell.append(scrim, sheet);
  }

  private closeGuideSheet(): void {
    const unreg = this.unregGuide;
    this.unregGuide = null;
    unreg?.();
    this.shell.querySelector('.guide-scrim')?.remove();
    this.shell.querySelector('.guide-sheet')?.remove();
  }

  private syncGuideLabel(): void {
    const prefs = loadGuidePrefs();
    this.guideBtn.setAttribute('aria-label', `参考线：${guideLabel(prefs.guide)}`);
    this.guideText.textContent = prefs.guide === 'off' ? '参考线' : guideLabel(prefs.guide);
  }

  sync(): void {
    const crop = this.hooks.liveCrop();
    const preset = crop?.preset ?? 'original';
    const canvas = this.hooks.canvas();
    const rect = rectFromArray(crop?.rect);
    for (const item of this.chips) {
      const active = presetMatchesSpec(item.spec, preset, rect, canvas);
      item.btn.classList.toggle('on', active);
      if (active) item.btn.scrollIntoView?.({ block: 'nearest', inline: 'center' });
    }
    const canOrient = ORIENTABLE.has(preset);
    this.currentPortrait = cropIsPortrait(rect, canvas);
    this.flipBtn.disabled = !canOrient;
    this.flipBtn.dataset.orient = this.currentPortrait ? 'portrait' : 'landscape';
    this.syncGuideLabel();
    this.paintAngle(this.hooks.liveStraighten());
    this.refreshSize();
  }

  /** 只在拖动拉直角度时更新数字和尺子，不动 Worker，不重算尺寸行。 */
  syncAngleOnly(): void {
    this.paintAngle(this.hooks.liveStraighten());
  }

  refreshSize(): void {
    if (this.destroyed) return;
    const photo = this.hooks.photo();
    if (!photo) return;
    const crop = this.hooks.liveCrop();
    const preset = crop?.preset ?? 'original';
    const rectArr = crop ? crop.rect : ([0, 0, 1, 1] as [number, number, number, number]);
    const canvas = this.hooks.canvas();
    const rectObj = rectFromArray(rectArr);
    const label = presetLabel(preset, rectObj, canvas);
    const frame = photo.frame;
    if (!frame) {
      if (!this.sizeLine.textContent && canvas.w > 1 && canvas.h > 1) {
        const px = croppedPixelSize(rectObj, canvas);
        this.sizeLine.textContent = `${px.w} × ${px.h}`;
      }
      return;
    }
    if (!this.sizeLine.textContent) {
      const est = croppedPixelSize(
        crop ? rectObj : centeredMaxRect({ w: frame.width, h: frame.height }, null, this.hooks.liveStraighten()),
        { w: frame.width, h: frame.height },
      );
      this.sizeLine.textContent = `${est.w} × ${est.h}`;
    }
    const seq = ++this.sizeSeq;
    const version = photo.version;
    const rev = photo.rev;
    const side = sideSubset({ frame, geometry: { straighten: this.hooks.liveStraighten() }, crop });
    void geomOutputSizes(frame.width, frame.height, side, rectArr, preset)
      .then((reply) => {
        if (
          this.destroyed ||
          seq !== this.sizeSeq ||
          this.hooks.photo() !== photo ||
          photo.version !== version ||
          photo.rev !== rev
        ) {
          return;
        }
        const [w, h] = reply.sizes.original;
        this.sizeLine.textContent = `${w} × ${h}`;
        this.sizeLine.dataset.label = label;
        this.hooks.onExportSize?.(photo, w, h);
      })
      .catch(() => {
        /* 算不出来就留着上一次的值，不置空。 */
      });
  }

  destroy(): void {
    this.destroyed = true;
    this.sizeSeq += 1;
    this.closeGuideSheet();
  }
}
