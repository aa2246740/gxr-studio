// 编辑器 128pt 单带里的「调整」页：10 项参数片、全部重置、曲线 ›、浮窗直方图开关与单根大滑块。

import type { Adjust } from '../../../frontend/src/index';
import { curveJson } from '../lib/export_request';
import { button, el, icon } from './dom';
import { markControlDragEnd } from './drag_guard';
import { zeroKnobs, type Knobs, type Photo } from './model';

const HIST_ICON = 'M4 19V11M9 19V6M14 19V13M19 19V8M3 19h18';

export const SLIDERS: Array<{ key: keyof Knobs; label: string; min: number; max: number; step: number }> = [
  { key: 'exposure_ev', label: '曝光', min: -5, max: 5, step: 0.1 },
  { key: 'contrast', label: '对比度', min: -100, max: 100, step: 1 },
  { key: 'highlight', label: '高光', min: -100, max: 100, step: 1 },
  { key: 'shadow', label: '阴影', min: -100, max: 100, step: 1 },
  { key: 'black_point', label: '黑点', min: -100, max: 100, step: 1 },
  { key: 'warm', label: '色温', min: -100, max: 100, step: 1 },
  { key: 'tint', label: '色调', min: -100, max: 100, step: 1 },
  { key: 'saturation', label: '饱和度', min: -100, max: 100, step: 1 },
  { key: 'vibrance', label: '自然饱和度', min: -100, max: 100, step: 1 },
  { key: 'vignette', label: '暗角', min: -100, max: 100, step: 1 },
];

export function formatKnob(key: keyof Knobs, value: number): string {
  if (value === 0) return key === 'exposure_ev' ? '0.0' : '0';
  if (key === 'exposure_ev') return value > 0 ? `+${value.toFixed(1)}` : value.toFixed(1);
  return value > 0 ? `+${value}` : String(value);
}

export function toAdjust(knobs: Knobs): Adjust {
  return {
    exposure_ev: knobs.exposure_ev,
    wb: { warm: knobs.warm, tint: knobs.tint },
    contrast: knobs.contrast,
    highlight: knobs.highlight,
    shadow: knobs.shadow,
    black_point: knobs.black_point,
    saturation: knobs.saturation,
    vibrance: knobs.vibrance,
    vignette: knobs.vignette,
  };
}

export interface AdjustHost {
  photo(): Photo | null;
  push(): void;
  enterDrag(): void;
  leaveDrag(): void;
  dragging(): boolean;
  refreshCurves?(): void;
  openCurves?(): void;
  toggleHist?(): boolean;
  histVisible?(): boolean;
  onScrub?(text: string | null): void;
  onEditedChanged?(): void;
}

export class AdjustView {
  private key: keyof Knobs = 'exposure_ev';
  private input: HTMLInputElement | null = null;
  private valueEl: HTMLElement | null = null;
  private labelEl: HTMLElement | null = null;
  private ipadLabEl: HTMLElement | null = null;
  private ipadValEl: HTMLElement | null = null;
  private histBtn: HTMLButtonElement | null = null;
  private chipsEl: HTMLElement | null = null;
  private committed = 0;
  private readonly host: AdjustHost;
  private readonly head: HTMLElement;
  private readonly panel: HTMLElement;

  constructor(host: AdjustHost, _legacyHist?: HTMLElement) {
    void _legacyHist;
    this.host = host;
    this.head = el('div', 'band-adjust-hd');
    this.labelEl = el('b', 'adj-lab', '曝光');
    this.valueEl = el('span', 'val num', '0.0');
    this.head.append(this.labelEl, this.valueEl);
    this.panel = this.adjustPanel();
  }

  headEl(): HTMLElement {
    return this.head;
  }

  panelEl(): HTMLElement {
    return this.panel;
  }

  refresh(): void {
    this.showKnob();
    this.syncHistBtn();
  }

  setDisabled(disabled: boolean): void {
    if (this.input) this.input.disabled = disabled;
  }

  syncHistBtn(): void {
    if (!this.histBtn) return;
    const on = Boolean(this.host.histVisible?.());
    this.histBtn.classList.toggle('on', on);
    this.histBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
  }

  destroy(): void {
    this.input = null;
    this.valueEl = null;
    this.labelEl = null;
    this.ipadLabEl = null;
    this.ipadValEl = null;
    this.histBtn = null;
    this.chipsEl = null;
  }

  private resetAll(): void {
    const photo = this.host.photo();
    if (!photo) return;
    // 滑块全是 0 且没有曲线：没有修改，不加 rev（不然撤销套用时会当成套用之后又改过）。
    const knobsZero = Object.values(photo.knobs).every((value) => value === 0);
    const hasCurve = curveJson(photo.curve) !== null;
    if (knobsZero && !hasCurve) return;
    photo.knobs = zeroKnobs();
    photo.curve = null;
    photo.rev += 1;
    this.showKnob();
    this.host.push();
    this.host.refreshCurves?.();
    this.host.onEditedChanged?.();
  }

  private adjustPanel(): HTMLElement {
    const panel = el('div', 'panel adjust');
    const row = el('div', 'adjust-row');
    const chips = el('div', 'chips');
    this.chipsEl = chips;

    for (const spec of SLIDERS) {
      const chip = button('chip', spec.label);
      chip.dataset.key = spec.key;
      if (spec.key === this.key) chip.classList.add('on');
      chip.addEventListener('click', () => {
        this.key = spec.key;
        for (const item of chips.querySelectorAll('.chip[data-key]')) item.classList.toggle('on', item === chip);
        this.showKnob();
      });
      chips.append(chip);
    }

    const reset = button('chip reset-chip', '全部重置');
    reset.dataset.act = 'adjust-reset';
    reset.addEventListener('click', () => this.resetAll());
    chips.append(reset);

    const curves = button('chip curves-chip deeper', '曲线 ›');
    curves.dataset.act = 'open-curves';
    curves.addEventListener('click', () => this.host.openCurves?.());
    chips.append(curves);

    this.histBtn = button('hist-toggle', '');
    this.histBtn.dataset.act = 'hist-toggle';
    this.histBtn.setAttribute('aria-label', '直方图');
    this.histBtn.setAttribute('aria-pressed', 'false');
    this.histBtn.append(icon(HIST_ICON));
    this.histBtn.addEventListener('click', () => {
      this.host.toggleHist?.();
      this.syncHistBtn();
    });

    row.append(chips, this.histBtn);

    const big = el('div', 'big');
    const l1 = el('div', 'adj-l1');
    this.ipadLabEl = el('b', '', '曝光');
    this.ipadValEl = el('span', 'val num', '0.0');
    l1.append(this.ipadLabEl, this.ipadValEl);
    this.input = document.createElement('input');
    this.input.type = 'range';
    this.input.dataset.key = 'exposure_ev';
    const slider = el('div', 'slider');
    slider.append(this.input);
    big.append(l1, slider);

    const hintRow = el('div', 'adj-hint');
    const hintReset = button('fbtn', '全部重置');
    hintReset.dataset.act = 'adjust-reset-all';
    hintReset.addEventListener('click', () => this.resetAll());
    hintRow.append(el('span', 'faint', '点两下圆点回到 0'), hintReset);

    panel.append(row, big, hintRow);
    this.showKnob();
    this.syncHistBtn();

    const scrubText = (value: number): string => {
      const spec = SLIDERS.find((item) => item.key === this.key);
      return `${spec?.label ?? ''} ${formatKnob(this.key, value)}`.trim();
    };
    this.input.addEventListener('input', () => {
      const photo = this.host.photo();
      if (!photo || !this.input) return;
      const value = Number(this.input.value);
      if (!Number.isFinite(value)) return;
      photo.knobs[this.key] = value;
      this.paintSlider();
      this.paintChipDots();
      this.host.onScrub?.(scrubText(value));
      this.host.push();
      this.host.onEditedChanged?.();
    });
    // 松手（change）才算落定的修改，编辑状态的版本加一（撤销套用时据此判断改没改过）。
    this.input.addEventListener('change', () => {
      markControlDragEnd();
      if (!this.host.dragging()) this.host.onScrub?.(null);
      const photo = this.host.photo();
      if (!photo) return;
      const value = photo.knobs[this.key];
      if (value === this.committed) return;
      this.committed = value;
      photo.rev += 1;
      this.host.onEditedChanged?.();
    });
    this.input.addEventListener('blur', () => {
      if (!this.host.dragging()) this.host.onScrub?.(null);
    });
    const end = () => {
      window.removeEventListener('pointerup', end, true);
      window.removeEventListener('pointercancel', end, true);
      markControlDragEnd();
      this.host.onScrub?.(null);
      if (this.host.dragging()) this.host.leaveDrag();
    };
    this.input.addEventListener('pointerdown', () => {
      this.host.enterDrag();
      const photo = this.host.photo();
      if (photo) this.host.onScrub?.(scrubText(photo.knobs[this.key]));
      window.addEventListener('pointerup', end, true);
      window.addEventListener('pointercancel', end, true);
    });
    this.input.addEventListener('pointerup', end);
    this.input.addEventListener('pointercancel', end);
    this.input.addEventListener('dblclick', () => {
      const photo = this.host.photo();
      if (!photo || photo.knobs[this.key] === 0) return;
      photo.knobs[this.key] = 0;
      this.committed = 0;
      photo.rev += 1;
      this.showKnob();
      this.host.push();
      this.host.onEditedChanged?.();
    });
    return panel;
  }

  private paintChipDots(): void {
    const photo = this.host.photo();
    if (!photo || !this.chipsEl) return;
    for (const node of this.chipsEl.querySelectorAll<HTMLElement>('.chip[data-key]')) {
      const key = node.dataset.key as keyof Knobs | undefined;
      if (key) node.classList.toggle('edited', photo.knobs[key] !== 0);
    }
  }

  private showKnob(): void {
    const spec = SLIDERS.find((item) => item.key === this.key);
    const photo = this.host.photo();
    if (!spec || !this.input || !this.labelEl || !photo) return;
    this.labelEl.textContent = spec.label;
    if (this.ipadLabEl) this.ipadLabEl.textContent = spec.label;
    this.input.min = String(spec.min);
    this.input.max = String(spec.max);
    this.input.step = String(spec.step);
    this.input.value = String(photo.knobs[this.key]);
    this.committed = photo.knobs[this.key];
    this.input.dataset.key = spec.key;
    this.paintSlider();
    this.paintChipDots();
  }

  private paintSlider(): void {
    const spec = SLIDERS.find((item) => item.key === this.key);
    const photo = this.host.photo();
    if (!spec || !this.input || !this.valueEl || !photo) return;
    const value = photo.knobs[this.key];
    const text = formatKnob(this.key, value);
    this.valueEl.textContent = text;
    if (this.ipadValEl) this.ipadValEl.textContent = text;
    const span = spec.max - spec.min || 1;
    const pct = ((value - spec.min) / span) * 100;
    const zero = ((0 - spec.min) / span) * 100;
    this.input.style.setProperty('--a', `${Math.min(pct, zero)}%`);
    this.input.style.setProperty('--b', `${Math.max(pct, zero)}%`);
  }
}
