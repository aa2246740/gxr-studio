// 编辑器里的「调整」页：十个基础调整滑块、双指/长按重置、拖动中状态和 rev 记录。

import type { Adjust } from '../../../frontend/src/index';
import { button, el } from './dom';
import { zeroKnobs, type Knobs, type Photo } from './model';

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
}

export class AdjustView {
  private key: keyof Knobs = 'exposure_ev';
  private input: HTMLInputElement | null = null;
  private valueEl: HTMLElement | null = null;
  private labelEl: HTMLElement | null = null;
  private committed = 0;
  private readonly host: AdjustHost;
  private readonly panel: HTMLElement;

  constructor(host: AdjustHost, hist: HTMLElement) {
    this.host = host;
    this.panel = this.adjustPanel(hist);
  }

  panelEl(): HTMLElement {
    return this.panel;
  }

  refresh(): void {
    this.showKnob();
  }

  setDisabled(disabled: boolean): void {
    if (this.input) this.input.disabled = disabled;
  }

  destroy(): void {
    this.input = null;
    this.valueEl = null;
    this.labelEl = null;
  }

  private adjustPanel(hist: HTMLElement): HTMLElement {
    const panel = el('div', 'panel adjust');
    const chips = el('div', 'chips');
    for (const spec of SLIDERS) {
      const chip = button('chip', spec.label);
      chip.dataset.key = spec.key;
      if (spec.key === this.key) chip.classList.add('on');
      chip.addEventListener('click', () => {
        this.key = spec.key;
        for (const item of chips.querySelectorAll('.chip')) item.classList.toggle('on', item === chip);
        this.showKnob();
      });
      chips.append(chip);
    }
    const big = el('div', 'big');
    const line = el('div', 'l1');
    this.labelEl = el('b', '', '曝光');
    this.valueEl = el('span', 'val', '0.0');
    line.append(this.labelEl, this.valueEl);
    this.input = document.createElement('input');
    this.input.type = 'range';
    this.input.dataset.key = 'exposure_ev';
    const slider = el('div', 'slider');
    slider.append(this.input);
    big.append(line, slider);
    const hint = el('div', 'hint-row');
    hint.append(el('span', 'faint', '点两下圆点回到 0'));
    const reset = button('fbtn', '全部重置');
    reset.addEventListener('click', () => {
      const photo = this.host.photo();
      if (!photo) return;
      // 全都已经是 0：没有修改，不加 rev（不然撤销套用时会当成套用之后又改过）。
      if (Object.values(photo.knobs).every((value) => value === 0)) return;
      photo.knobs = zeroKnobs();
      photo.rev += 1;
      this.showKnob();
      this.host.push();
    });
    hint.append(reset);
    panel.append(hist, chips, big, hint);
    this.showKnob();
    this.input.addEventListener('input', () => {
      const photo = this.host.photo();
      if (!photo || !this.input) return;
      const value = Number(this.input.value);
      if (!Number.isFinite(value)) return;
      photo.knobs[this.key] = value;
      this.paintSlider();
      this.host.push();
    });
    // 松手（change）才算落定的修改，编辑状态的版本加一（撤销套用时据此判断改没改过）。
    this.input.addEventListener('change', () => {
      const photo = this.host.photo();
      if (!photo) return;
      // 松手时的值和上次落定的一样（拖出去又拖回来）：没有修改。
      const value = photo.knobs[this.key];
      if (value === this.committed) return;
      this.committed = value;
      photo.rev += 1;
    });
    this.input.addEventListener('pointerdown', () => this.host.enterDrag());
    const end = () => {
      if (this.host.dragging()) this.host.leaveDrag();
    };
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
    });
    return panel;
  }

  private showKnob(): void {
    const spec = SLIDERS.find((item) => item.key === this.key);
    const photo = this.host.photo();
    if (!spec || !this.input || !this.labelEl || !photo) return;
    this.labelEl.textContent = spec.label;
    this.input.min = String(spec.min);
    this.input.max = String(spec.max);
    this.input.step = String(spec.step);
    this.input.value = String(photo.knobs[this.key]);
    this.committed = photo.knobs[this.key];
    this.input.dataset.key = spec.key;
    this.paintSlider();
  }

  private paintSlider(): void {
    const spec = SLIDERS.find((item) => item.key === this.key);
    const photo = this.host.photo();
    if (!spec || !this.input || !this.valueEl || !photo) return;
    const value = photo.knobs[this.key];
    this.valueEl.textContent = formatKnob(this.key, value);
    const span = spec.max - spec.min || 1;
    const pct = ((value - spec.min) / span) * 100;
    const zero = ((0 - spec.min) / span) * 100;
    this.input.style.setProperty('--a', `${Math.min(pct, zero)}%`);
    this.input.style.setProperty('--b', `${Math.max(pct, zero)}%`);
  }
}
