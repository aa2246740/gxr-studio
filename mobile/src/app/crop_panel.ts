// 03e 裁剪页：比例片（原始..小红书 3:4）、横竖切换、参考线选择（gxr-studio.crop-guides）、
// ±45° 标尺拉直（每次 0.1°、双按归零、拖动时进入 03d 收起版式）、成品像素与比例行、CoreError 内联提示。

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

const CHECK = 'M5 12.5l4.5 4.5L19 7.5';
const CHEVRON_DOWN = 'M6 9l6 6 6-6';
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
  private readonly panel = el('div', 'panel crop-panel');
  private readonly chipsWrap = el('div', 'chips');
  private readonly chips: Array<{ spec: (typeof PRESETS)[number]; btn: HTMLButtonElement }> = [];
  private readonly controlRow = el('div', 'pad row gap8 crop-row');
  private readonly seg = el('div', 'seg');
  private readonly landBtn = button('on', '横向');
  private readonly portBtn = button('', '竖向');
  private readonly guideBtn = button('btn guide-btn', '');
  private readonly guideText = el('span', '', '');
  private readonly straightenBox = el('div', 'pad straighten');
  private readonly angleVal = el('span', 'dim small num', '0.0°');
  private readonly ruler = el('div', 'ruler');
  private readonly tape = el('div', 'ruler-tape');
  private readonly sizeLine = el('p', 'pad dim small num crop-size-line', '');
  private readonly errBanner = el('div', 'bn crop-err');
  private readonly errText = el('span', 'tx', '');
  private sizeSeq = 0;
  private destroyed = false;
  private lastTap = { at: 0, x: 0, y: 0 };

  constructor(
    private readonly shell: HTMLElement,
    private readonly hooks: CropPanelHooks,
  ) {
    this.build();
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

    this.seg.setAttribute('role', 'group');
    this.seg.setAttribute('aria-label', '方向');
    this.landBtn.dataset.orient = 'landscape';
    this.portBtn.dataset.orient = 'portrait';
    this.landBtn.addEventListener('click', () => {
      this.showError(null);
      this.hooks.pickOrientation('landscape');
    });
    this.portBtn.addEventListener('click', () => {
      this.showError(null);
      this.hooks.pickOrientation('portrait');
    });
    this.seg.append(this.landBtn, this.portBtn);

    this.guideBtn.type = 'button';
    this.guideBtn.dataset.act = 'guides';
    this.guideBtn.append(this.guideText, icon(CHEVRON_DOWN));
    this.guideBtn.addEventListener('click', () => this.openGuideSheet());
    this.controlRow.append(this.seg, this.guideBtn);

    const head = el('div', 'row between');
    head.append(el('span', 'dim small', '拉直'), this.angleVal);

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

    this.straightenBox.append(head, this.ruler);

    this.errBanner.hidden = true;
    this.errBanner.dataset.role = 'crop-error';
    this.errBanner.append(this.errText);

    this.panel.append(this.chipsWrap, this.controlRow, this.straightenBox, this.sizeLine, this.errBanner);
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
    this.shell.append(scrim, sheet);
  }

  private closeGuideSheet(): void {
    this.shell.querySelector('.guide-scrim')?.remove();
    this.shell.querySelector('.guide-sheet')?.remove();
  }

  private syncGuideLabel(): void {
    const prefs = loadGuidePrefs();
    this.guideText.textContent = `参考线：${guideLabel(prefs.guide)}`;
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
    this.seg.hidden = !canOrient;
    const portrait = cropIsPortrait(rect, canvas);
    this.landBtn.classList.toggle('on', !portrait);
    this.portBtn.classList.toggle('on', portrait);
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
        this.sizeLine.textContent = `成品 ${px.w} × ${px.h} 像素 · ${label}`;
      }
      return;
    }
    if (!this.sizeLine.textContent) {
      const est = croppedPixelSize(
        crop ? rectObj : centeredMaxRect({ w: frame.width, h: frame.height }, null, this.hooks.liveStraighten()),
        { w: frame.width, h: frame.height },
      );
      this.sizeLine.textContent = `成品 ${est.w} × ${est.h} 像素 · ${label}`;
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
        this.sizeLine.textContent = `成品 ${w} × ${h} 像素 · ${label}`;
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
