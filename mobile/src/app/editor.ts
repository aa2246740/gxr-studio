// 编辑一张。拖滑块时只改 setLook 和滑块上的数字，不重建页面。

import { Engine, type Adjust } from '../../../frontend/src/index';
import { describeDng } from '../core/api';
import { NEUTRAL_BAKE_KEY } from '../dev/neutral_bake';
import { formatClock } from '../lib/group';
import { button, el, icon, ICON } from './dom';
import { photoMs, zeroKnobs, type AppApi, type Knobs, type Photo } from './model';

const SLIDERS: Array<{ key: keyof Knobs; label: string; min: number; max: number; step: number }> = [
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

const TABS = ['配方', '调整', '裁剪', '边框', 'LUT', '曲线'] as const;

function formatKnob(key: keyof Knobs, value: number): string {
  if (value === 0) return key === 'exposure_ev' ? '0.0' : '0';
  if (key === 'exposure_ev') return value > 0 ? `+${value.toFixed(1)}` : value.toFixed(1);
  return value > 0 ? `+${value}` : String(value);
}

function toAdjust(knobs: Knobs): Adjust {
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

function isGone(err: unknown): boolean {
  const name = err instanceof DOMException ? err.name : err instanceof Error ? err.name : '';
  return name === 'NotFoundError' || name === 'NotReadableError';
}

export class EditorView {
  private token = 0;
  private photo: Photo | null = null;
  private mode: 'raw' | 'jpg' | 'still' = 'still';
  private wantNeighbor = '';
  private key: keyof Knobs = 'exposure_ev';
  private dragging = false;
  private holding = false;
  private histLoop = 0;
  private input: HTMLInputElement | null = null;
  private valueEl: HTMLElement | null = null;
  private labelEl: HTMLElement | null = null;
  private stageLabel: HTMLElement | null = null;
  private gpuCanvas: HTMLCanvasElement | null = null;
  private still: HTMLCanvasElement | null = null;
  private hist: HTMLCanvasElement | null = null;
  private caption = '';
  private restLabel = '';

  constructor(
    private readonly host: HTMLElement,
    private readonly shell: HTMLElement,
    private readonly api: AppApi,
    private readonly engine: Engine | null,
  ) {}

  open(photo: Photo): void {
    const index = this.api.photos().findIndex((item) => item.id === photo.id);
    this.build(photo, index);
    void this.load(photo);
  }

  /** 模拟器没有显卡，滑块是停用的。截图时只把拖动中的版式摆出来。 */
  forceDrag(): void {
    this.enterDrag();
  }

  destroy(): void {
    this.token += 1;
    this.dragging = false;
    this.holding = false;
    this.wantNeighbor = '';
    if (this.histLoop) cancelAnimationFrame(this.histLoop);
    this.shell.dataset.drag = '0';
    this.engine?.detach();
  }

  private build(photo: Photo, index: number): void {
    this.photo = photo;
    this.host.replaceChildren();
    const photos = this.api.photos();
    const screen = el('section', 'screen editor');
    screen.dataset.photo = photo.stem;
    const nav = el('header', 'nav');
    const back = button('nb', '照片');
    back.prepend(icon(ICON.back));
    back.addEventListener('click', () => this.api.back());
    const title = el('div', 't');
    title.append(el('b', '', photo.stem), el('small', '', `${index + 1} / ${photos.length} · ${formatClock(photoMs(photo))}`));
    nav.append(back, title, button('nb plain', '…'));

    const stage = el('div', 'stage');
    const pv = el('div', 'pv');
    this.gpuCanvas = document.createElement('canvas');
    this.gpuCanvas.className = 'gpu';
    this.still = document.createElement('canvas');
    this.still.className = 'still';
    this.still.hidden = true;
    pv.append(this.gpuCanvas, this.still);
    this.stageLabel = el('div', 'lbl');
    stage.append(pv, this.stageLabel);
    this.bindGesture(stage);

    const strip = el('div', 'strip');
    for (const name of ['GR 正片', 'GR 负片', 'GR 高对比黑白', '原厂标准', 'Kodachrome 25']) {
      const card = button('rc', '');
      const thumb = el('div', 'th', photo.dng ? '中性转换' : '相机 JPG');
      card.append(thumb, el('div', 'nm', name));
      card.addEventListener('click', () => {
        for (const other of strip.querySelectorAll('.rc')) other.classList.remove('on');
        card.classList.add('on');
      });
      strip.append(card);
    }

    const sheet = el('div', 'sheet');
    sheet.append(el('div', 'grab'));
    const tabs = el('div', 'tabs');
    const panels: HTMLElement[] = [];
    TABS.forEach((name, tabIndex) => {
      const tab = button('', name);
      tab.setAttribute('role', 'tab');
      if (tabIndex === 1) tab.classList.add('on');
      tab.addEventListener('click', () => {
        for (const item of tabs.children) item.classList.remove('on');
        tab.classList.add('on');
        panels.forEach((panel, panelIndex) => {
          panel.hidden = panelIndex !== tabIndex;
        });
      });
      tabs.append(tab);
    });
    panels.push(this.recipePanel(photo), this.adjustPanel(), this.later('裁剪', ['原始', '自由', '1:1', '4:5', '3:2', '16:9', 'XPan 65:24', '小红书 3:4']), this.later('边框', ['无', '徕卡水印', '模糊背景', '白边', '哈苏水印', '拍立得', '胶片', '极简细线']), this.later('LUT', ['不用 LUT', '胶片暖调', '青橙电影', '黑金']), this.later('曲线', ['主曲线', '红', '绿', '蓝']));
    panels.forEach((panel, panelIndex) => {
      panel.hidden = panelIndex !== 1;
    });
    const copy = button('btn', '复制调整 · 下一步做');
    copy.disabled = true;
    const save = button('btn primary', '导出这张 · 下一步做');
    save.disabled = true;
    const acts = el('div', 'actbar');
    acts.append(copy, save);
    sheet.append(tabs, ...panels, acts);
    screen.append(nav, stage, strip, sheet);
    this.host.append(screen);
    if (this.engine && this.gpuCanvas) this.engine.attach(this.gpuCanvas);
    this.paintStill(photo.bitmap);
  }

  private recipePanel(photo: Photo): HTMLElement {
    const panel = el('div', 'panel');
    if (!photo.dng) {
      const note = el('div', 'bn grey');
      note.append(el('div', '', '配方需要 RAW 文件'), el('div', 'dim', '拍摄时将画质设为 RAW+JPEG'));
      panel.append(note, el('p', 'pad dim', '这张只有相机 JPG。调整、裁剪、边框、曲线照常能用。'));
      panel.append(el('p', 'pad faint', 'HDR 需要 RAW 文件。这张会存普通版本。'));
      return panel;
    }
    const note = el('div', 'bn');
    note.append(el('div', '', '还没导入校准数据。现在所有配方都只显示中性转换，和相机里的颜色不同。导出也一样。'));
    const bring = button('btn primary block', '导入校准数据 · 下一步做');
    bring.disabled = true;
    panel.append(note, el('div', 'pad'), bring, el('p', 'faint pad', '在 Mac 的 GXR Studio 里导出校准数据，用隔空投送发到这台 iPhone。只需一次。'));
    return panel;
  }

  private adjustPanel(): HTMLElement {
    const panel = el('div', 'panel adjust');
    const hist = el('div', 'hist');
    this.hist = document.createElement('canvas');
    this.hist.width = 408;
    this.hist.height = 64;
    hist.append(this.hist);
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
      if (!this.photo) return;
      this.photo.knobs = zeroKnobs();
      this.showKnob();
      this.push();
    });
    hint.append(reset);
    panel.append(hist, chips, big, hint);
    this.showKnob();
    this.input.addEventListener('input', () => {
      if (!this.photo || !this.input) return;
      const value = Number(this.input.value);
      if (!Number.isFinite(value)) return;
      this.photo.knobs[this.key] = value;
      this.paintSlider();
      this.push();
    });
    this.input.addEventListener('pointerdown', () => this.enterDrag());
    const end = () => {
      if (this.dragging) this.leaveDrag();
    };
    this.input.addEventListener('pointerup', end);
    this.input.addEventListener('pointercancel', end);
    this.input.addEventListener('dblclick', () => {
      if (!this.photo) return;
      this.photo.knobs[this.key] = 0;
      this.showKnob();
      this.push();
    });
    return panel;
  }

  private later(title: string, names: string[]): HTMLElement {
    const panel = el('div', 'panel');
    panel.append(el('p', 'later', '这项下一步做'));
    const chips = el('div', 'chips');
    for (const name of names) {
      const chip = button('chip', name);
      chip.disabled = true;
      chips.append(chip);
    }
    panel.append(chips, el('p', 'pad faint', title));
    return panel;
  }

  private showKnob(): void {
    const spec = SLIDERS.find((item) => item.key === this.key);
    if (!spec || !this.input || !this.labelEl || !this.photo) return;
    this.labelEl.textContent = spec.label;
    this.input.min = String(spec.min);
    this.input.max = String(spec.max);
    this.input.step = String(spec.step);
    this.input.value = String(this.photo.knobs[this.key]);
    this.input.dataset.key = spec.key;
    this.paintSlider();
  }

  private paintSlider(): void {
    const spec = SLIDERS.find((item) => item.key === this.key);
    if (!spec || !this.input || !this.valueEl || !this.photo) return;
    const value = this.photo.knobs[this.key];
    this.valueEl.textContent = formatKnob(this.key, value);
    const span = spec.max - spec.min || 1;
    const pct = ((value - spec.min) / span) * 100;
    const zero = ((0 - spec.min) / span) * 100;
    this.input.style.setProperty('--a', `${Math.min(pct, zero)}%`);
    this.input.style.setProperty('--b', `${Math.max(pct, zero)}%`);
  }

  private push(): void {
    if (!this.engine || !this.photo || this.mode === 'still') return;
    const knobs = this.holding ? zeroKnobs() : this.photo.knobs;
    this.engine.setLook({
      recipe: this.mode === 'raw' ? NEUTRAL_BAKE_KEY : null,
      lut: null,
      lutStrength: 1,
      curve: null,
      adjust: toAdjust(knobs),
    });
  }

  private setLabel(text: string): void {
    this.caption = text;
    if (!this.stageLabel) return;
    this.stageLabel.replaceChildren();
    if (!text) return;
    this.stageLabel.append(el('span', 'tag neutral', text));
  }

  private paintStill(bitmap: ImageBitmap | null): void {
    if (!this.still) return;
    if (!bitmap) {
      this.still.hidden = true;
      return;
    }
    this.still.hidden = false;
    this.still.width = bitmap.width;
    this.still.height = bitmap.height;
    this.still.getContext('2d')?.drawImage(bitmap, 0, 0);
  }

  private enterDrag(): void {
    if (this.dragging) return;
    this.dragging = true;
    this.shell.dataset.drag = '1';
    this.api.watch('drag', true);
    this.loopHist();
    requestAnimationFrame(() => this.engine?.redraw());
  }

  private leaveDrag(): void {
    this.dragging = false;
    this.shell.dataset.drag = '0';
    this.api.watch('drag', false);
    if (this.histLoop) cancelAnimationFrame(this.histLoop);
    this.histLoop = 0;
    requestAnimationFrame(() => this.engine?.redraw());
  }

  private loopHist = (): void => {
    if (!this.dragging) return;
    this.histLoop = requestAnimationFrame(this.loopHist);
    void this.engine?.histogram().then((data) => {
      if (this.dragging && data) this.drawHist(data.counts);
    });
  };

  private drawHist(counts: Uint32Array): void {
    const canvas = this.hist;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const luma = counts.subarray(768, 1024);
    let max = 1;
    for (let i = 0; i < luma.length; i++) if (luma[i] > max) max = luma[i];
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#6aa6ff';
    const slot = canvas.width / 256;
    for (let i = 0; i < 256; i++) {
      const height = (luma[i] / max) * (canvas.height - 4);
      ctx.fillRect(i * slot, canvas.height - height, Math.ceil(slot), height);
    }
  }

  private bindGesture(stage: HTMLElement): void {
    stage.addEventListener('pointerdown', (event) => {
      if (event.button !== 0 || event.clientX < 24) return;
      const startX = event.clientX;
      const startY = event.clientY;
      const pointer = event.pointerId;
      let moved = false;
      let held = false;
      const timer = window.setTimeout(() => {
        if (moved || !this.photo) return;
        held = true;
        this.holding = true;
        this.restLabel = this.caption;
        this.setLabel('原图');
        this.push();
      }, 450);
      const move = (ev: PointerEvent) => {
        if (ev.pointerId !== pointer) return;
        if (Math.hypot(ev.clientX - startX, ev.clientY - startY) > 12) moved = true;
      };
      const up = (ev: PointerEvent) => {
        if (ev.pointerId !== pointer) return;
        window.clearTimeout(timer);
        stage.removeEventListener('pointermove', move);
        stage.removeEventListener('pointerup', up);
        stage.removeEventListener('pointercancel', up);
        if (held) {
          this.holding = false;
          this.setLabel(this.restLabel);
          this.push();
          return;
        }
        const dx = ev.clientX - startX;
        if (moved && Math.abs(dx) > 56 && Math.abs(dx) > Math.abs(ev.clientY - startY) && this.photo) {
          this.api.swipe(this.photo.id, dx < 0 ? 1 : -1);
        }
      };
      stage.addEventListener('pointermove', move);
      stage.addEventListener('pointerup', up);
      stage.addEventListener('pointercancel', up);
    });
  }

  private nextPhoto(dir: -1 | 1): Photo | null {
    if (!this.photo) return null;
    const photos = this.api.photos();
    const index = photos.findIndex((item) => item.id === this.photo?.id);
    return photos[index + dir] ?? null;
  }

  private async take(photo: Photo): Promise<ArrayBuffer> {
    const warm = this.api.takeWarm(photo.id);
    if (warm) return warm;
    if (!photo.dng) throw new DOMException('没有 RAW', 'NotFoundError');
    return photo.dng.arrayBuffer();
  }

  private prefetch(next: Photo | null): void {
    if (!next?.dng) {
      this.wantNeighbor = '';
      return;
    }
    const id = next.id;
    const file = next.dng;
    const token = this.token;
    this.wantNeighbor = id;
    void file.arrayBuffer().then((bytes) => {
      if (this.token !== token || this.wantNeighbor !== id) return;
      this.api.keepWarm(id, bytes);
    }).catch(() => undefined);
  }

  private async load(photo: Photo): Promise<void> {
    const token = ++this.token;
    if (!photo.dng) {
      this.showStillPhoto(photo, photo.jpg ? '相机 JPG' : '只有 RAW');
      if (this.engine && photo.bitmap) {
        const key = `jpg:${photo.id}`;
        this.engine.addJpg(key, photo.bitmap);
        this.engine.show(key);
        this.mode = 'jpg';
        if (this.gpuCanvas) this.gpuCanvas.hidden = false;
        if (this.still) this.still.hidden = true;
        this.push();
        const frame = this.engine.frame();
        if (frame) this.shell.dataset.frame = '1';
        if (this.input) this.input.disabled = false;
      }
      return;
    }
    let bytes: ArrayBuffer;
    try {
      bytes = await this.take(photo);
    } catch (err) {
      if (token !== this.token) return;
      if (isGone(err)) this.api.markGone(photo.id);
      else this.api.markBad(photo.id);
      return;
    }
    if (token !== this.token) return;
    if (!this.engine) {
      this.showStillPhoto(photo, '这个浏览器现在用不了显卡预览');
      if (this.input) this.input.disabled = true;
      this.prefetch(this.nextPhoto(1));
      return;
    }
    try {
      const described = await describeDng(bytes);
      if (token !== this.token) return;
      if (!described.layout || described.reason) {
        this.api.markBad(photo.id);
        return;
      }
      const key = `raw:${photo.id}`;
      await this.engine.addRaw(key, described.layout, bytes, described.scene);
      if (token !== this.token) return;
      this.engine.show(key);
      this.mode = 'raw';
      this.push();
      const frame = this.engine.frame();
      if (frame) this.shell.dataset.frame = '1';
      this.setLabel('开发用中性预览，不是配方');
      if (this.gpuCanvas) this.gpuCanvas.hidden = false;
      if (this.still) this.still.hidden = true;
      this.prefetch(this.nextPhoto(1));
    } catch {
      if (token !== this.token) return;
      this.api.markBad(photo.id);
    }
  }

  private showStillPhoto(photo: Photo, label: string): void {
    this.mode = 'still';
    this.setLabel(label);
    this.paintStill(photo.bitmap);
    if (this.gpuCanvas) this.gpuCanvas.hidden = true;
    if (!this.engine && this.input) this.input.disabled = true;
  }
}
