// 编辑一张。拖滑块时只改 setLook 和滑块上的数字，不重建页面。

import { Engine, type Adjust } from '../../../frontend/src/index';
import { describeDng, type Described } from '../core/api';
import { NEUTRAL_BAKE_KEY } from '../dev/neutral_bake';
import { formatClock } from '../lib/group';
import { histPoint, histShape } from '../lib/hist';
import { button, el, icon, ICON } from './dom';
import { cropPanel, curvesPanel, framePanel, lutPanel } from './later_panels';
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
  /** 直方图：正在数、数完还要再数一次、上次画的是哪个内容版本。静止时不再读显卡。 */
  private histBusy = false;
  private histAgain = false;
  private histDrawn = -1;
  private clipLow: HTMLElement | null = null;
  private clipHigh: HTMLElement | null = null;
  private input: HTMLInputElement | null = null;
  private valueEl: HTMLElement | null = null;
  private labelEl: HTMLElement | null = null;
  private stageLabel: HTMLElement | null = null;
  private gpuCanvas: HTMLCanvasElement | null = null;
  private still: HTMLCanvasElement | null = null;
  private hist: HTMLCanvasElement | null = null;
  private caption = '';
  private sharp: ImageBitmap | null = null;
  private placeholder: HTMLElement | null = null;
  private cards: Array<{ thumb: HTMLElement; sub: HTMLElement }> = [];
  /** 这张读完、清楚的图也换上之后才 resolve。只给截图和测试等。 */
  settled: Promise<void> = Promise.resolve();
  private sharpening: Promise<void> = Promise.resolve();
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
    this.settled = this.load(photo).then(() => this.sharpening);
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
    this.histAgain = false;
    this.shell.dataset.drag = '0';
    this.sharp?.close();
    this.sharp = null;
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
    this.placeholder = el('div', 'ph', '只有 RAW');
    this.placeholder.hidden = true;
    pv.append(this.gpuCanvas, this.still, this.placeholder);
    this.stageLabel = el('div', 'lbl');
    stage.append(pv, this.stageLabel);
    this.bindGesture(stage);

    const strip = el('div', 'strip');
    this.cards = [];
    for (const name of ['GR 正片', 'GR 负片', 'GR 高对比黑白', '原厂标准', 'Kodachrome 25']) {
      const card = button('rc', '');
      const thumb = el('div', 'th');
      const sub = el('div', 'sub');
      card.append(thumb, el('div', 'nm', name), sub);
      this.cards.push({ thumb, sub });
      if (!photo.dng) card.disabled = true;
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
    panels.push(this.recipePanel(photo), this.adjustPanel(), cropPanel(), framePanel(), lutPanel(), curvesPanel());
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
    this.paintSource(photo);
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
    this.hist.width = 816;
    this.hist.height = 128;
    this.clipLow = el('span', 'clip l');
    this.clipHigh = el('span', 'clip r');
    this.clipLow.setAttribute('aria-label', '暗部压死');
    this.clipHigh.setAttribute('aria-label', '高光溢出');
    hist.append(this.hist, this.clipLow, this.clipHigh);
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
    this.requestHist();
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
    if (this.placeholder) this.placeholder.hidden = true;
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
    requestAnimationFrame(() => this.engine?.redraw());
  }

  private leaveDrag(): void {
    this.dragging = false;
    this.shell.dataset.drag = '0';
    this.api.watch('drag', false);
    requestAnimationFrame(() => this.engine?.redraw());
  }

  /**
   * 要一次直方图：RAW 第一帧、换片、每次调整之后。正在数就只记一笔，数完看内容版本变没变再补一次，
   * 所以拖滑块时自然合并，停下来以后不会一直读显卡。
   */
  private requestHist(): void {
    if (!this.engine || this.mode === 'still') return;
    if (this.histBusy) {
      this.histAgain = true;
      return;
    }
    this.histBusy = true;
    this.histAgain = false;
    const token = this.token;
    void this.countHist(token).finally(() => {
      this.histBusy = false;
      if (token !== this.token) return;
      const engine = this.engine;
      if (this.histAgain || (engine && engine.contentVersion !== this.histDrawn)) this.requestHist();
    });
  }

  private async countHist(token: number): Promise<void> {
    const engine = this.engine;
    if (!engine) return;
    // 等这一帧画完，成片纹理才是新的。
    for (let i = 0; i < 30 && engine.pending; i++) await new Promise((done) => requestAnimationFrame(done));
    if (token !== this.token) return;
    const data = await engine.histogram();
    if (!data || token !== this.token) return;
    this.histDrawn = data.version;
    this.drawHist(data.counts, data.samples, data.version);
  }

  private drawHist(counts: Uint32Array, samples: number, version: number): void {
    const canvas = this.hist;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const shape = histShape(counts, samples);
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    const area = (values: Float64Array, color: string) => {
      ctx.beginPath();
      ctx.moveTo(0, h);
      for (let i = 0; i < 256; i++) {
        const [x, y] = histPoint(i, values[i], w, h);
        ctx.lineTo(x, y);
      }
      ctx.lineTo(w, h);
      ctx.closePath();
      ctx.fillStyle = color;
      ctx.fill();
    };
    ctx.globalCompositeOperation = 'screen';
    area(shape.r, 'rgba(255,70,70,.62)');
    area(shape.g, 'rgba(60,220,110,.62)');
    area(shape.b, 'rgba(70,130,255,.7)');
    ctx.globalCompositeOperation = 'source-over';
    ctx.beginPath();
    for (let i = 0; i < 256; i++) {
      const [x, y] = histPoint(i, shape.l[i], w, h);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = 'rgba(255,255,255,.85)';
    ctx.lineWidth = 2;
    ctx.stroke();
    canvas.dataset.version = String(version);
    this.clipLow?.classList.toggle('lit', shape.low);
    this.clipHigh?.classList.toggle('lit', shape.high);
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
    const warm = this.api.takeWarm(photo.id, photo.version);
    if (warm) return warm;
    if (!photo.dng) throw new DOMException('没有 RAW', 'NotFoundError');
    const reply = await this.api.readFull(photo);
    if (reply.ok) return reply.bytes;
    if (reply.stale) throw new DOMException('重新选过', 'AbortError');
    throw new DOMException('读不了', reply.gone ? 'NotReadableError' : 'EncodingError');
  }

  private prefetch(next: Photo | null): void {
    if (!next?.dng) {
      this.wantNeighbor = '';
      return;
    }
    const id = next.id;
    const version = next.version;
    const token = this.token;
    this.wantNeighbor = id;
    void this.api.readFull(next).then((reply) => {
      if (!reply.ok || this.token !== token || this.wantNeighbor !== id) return;
      this.api.keepWarm(id, version, reply.bytes);
    });
  }

  private async load(photo: Photo): Promise<void> {
    const token = ++this.token;
    const version = photo.version;
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
    // 这个版本的完整源已经在显卡里：不读卡，直接显示。卡被拔出后照样能开、能调。
    const key = `raw:${photo.id}`;
    if (this.engine && photo.cached === version && this.engine.hasSource(key)) {
      this.showRaw(key);
      return;
    }
    // 没有显卡、但这个版本描述过：直接放相机 JPG 或内嵌预览。
    if (!this.engine && photo.checked === version) {
      this.showStillPhoto(photo, '这个浏览器现在用不了显卡预览');
      return;
    }
    let bytes: ArrayBuffer;
    try {
      bytes = await this.take(photo);
    } catch (err) {
      if (token !== this.token) return;
      if (err instanceof DOMException && err.name === 'AbortError') return;
      if (isGone(err)) this.api.markGone(photo.id, version);
      else this.api.markBad(photo.id, version);
      return;
    }
    if (token !== this.token) return;
    // 先用 WASM 描述，坏文件的判断和有没有显卡无关。
    let described: Described;
    try {
      described = await describeDng(bytes);
    } catch {
      if (token === this.token) this.api.markBad(photo.id, version);
      return;
    }
    if (token !== this.token) return;
    if (!described.layout || described.reason) {
      this.api.markBad(photo.id, version);
      return;
    }
    if (photo.version === version) photo.checked = version;
    if (!this.engine) {
      this.showStillPhoto(photo, '这个浏览器现在用不了显卡预览');
      if (this.input) this.input.disabled = true;
      this.prefetch(this.nextPhoto(1));
      return;
    }
    try {
      // 每张照片一个固定的键：再加一次会替换（并销毁）原来那份纹理，不会同一张占两份。
      // 上传由 App 按照片排队，提交前核对版本；过期的请求不会碰显卡里的源。
      const installed = await this.api.uploadRaw(photo, version, described.layout, bytes, described.scene);
      if (!installed || token !== this.token) return;
      this.showRaw(key);
      this.prefetch(this.nextPhoto(1));
    } catch {
      if (token !== this.token) return;
      this.api.markBad(photo.id, version);
    }
  }

  /**
   * 配方卡上画的是什么，就写什么。有显卡的 RAW：中性转换（显卡出的小图，好了再画）；
   * 否则是相机 JPG 或 DNG 内嵌的预览，不能叫中性转换。
   */
  private paintSource(photo: Photo): void {
    if (photo.dng && this.engine) {
      const neutral = photo.neutral && photo.neutral.version === photo.version ? photo.neutral.bitmap : null;
      this.paintCards(neutral, '中性转换');
      return;
    }
    const label = photo.jpg ? '相机 JPG' : photo.bitmap ? '内嵌预览' : '只有 RAW';
    this.paintCards(photo.bitmap, label);
  }

  private paintCards(bitmap: ImageBitmap | null, label: string): void {
    for (const card of this.cards) {
      card.sub.textContent = label;
      card.thumb.replaceChildren();
      card.thumb.dataset.source = bitmap ? label : '';
      if (!bitmap) continue;
      const mini = document.createElement('canvas');
      mini.width = bitmap.width;
      mini.height = bitmap.height;
      mini.getContext('2d')?.drawImage(bitmap, 0, 0);
      card.thumb.append(mini);
    }
  }

  /** 这张 RAW 的中性小图：每个版本只请显卡出一次，之后换片回来直接用。 */
  private async neutralCards(photo: Photo, key: string): Promise<void> {
    if (!this.engine) return;
    const version = photo.version;
    if (photo.neutral && photo.neutral.version === version) {
      this.paintCards(photo.neutral.bitmap, '中性转换');
      return;
    }
    const token = this.token;
    const session = this.api.session();
    // 照片还是这次会话里的同一张、同一个版本：清空、去掉、重新选过都算过期。
    const alive = () => this.api.photo(photo.id) === photo && photo.version === version && this.api.session() === session;
    const look = { recipe: NEUTRAL_BAKE_KEY, lut: null, lutStrength: 1, curve: null, adjust: toAdjust(zeroKnobs()) };
    const geometry = { straighten: 0, rotate90: 0, flip: false, vertical: 0, horizontal: 0, distortion: 0, box: null };
    const out = await this.engine.thumb({ source: key, look, geometry, aspect: null, maxEdge: 192 });
    if (!out || !alive()) return;
    const bitmap = await createImageBitmap(new ImageData(new Uint8ClampedArray(out.data), out.w, out.h));
    if (!alive()) {
      bitmap.close();
      return;
    }
    photo.neutral?.bitmap.close();
    photo.neutral = { version, bitmap };
    if (token === this.token) this.paintCards(bitmap, '中性转换');
  }

  private showRaw(key: string): void {
    if (!this.engine) return;
    this.engine.show(key);
    this.mode = 'raw';
    this.push();
    const frame = this.engine.frame();
    if (frame) this.shell.dataset.frame = '1';
    this.setLabel('开发用中性预览，不是配方');
    if (this.gpuCanvas) this.gpuCanvas.hidden = false;
    if (this.still) this.still.hidden = true;
    if (this.input) this.input.disabled = false;
    if (this.photo) void this.neutralCards(this.photo, key);
  }

  private showStillPhoto(photo: Photo, label: string): void {
    this.mode = 'still';
    this.setLabel(label);
    this.paintStill(photo.bitmap);
    if (this.gpuCanvas) this.gpuCanvas.hidden = true;
    if (!this.engine && this.input) this.input.disabled = true;
    // 没有相机 JPG、DNG 里也没有预览：照片区写「只有 RAW」，不留一块空灰。
    if (this.placeholder) this.placeholder.hidden = !!photo.bitmap || !!photo.jpg;
    this.sharpening = this.sharpen(photo);
  }

  /** 先放缩略图，Worker 解好屏幕宽度的那张再换上。主线程只画一次。 */
  private async sharpen(photo: Photo): Promise<void> {
    const token = this.token;
    const width = Math.min(2048, Math.round((this.host.clientWidth || 390) * (window.devicePixelRatio || 1)));
    const bitmap = await this.api.still(photo, width);
    if (!bitmap) return;
    if (token !== this.token || this.mode === 'raw') {
      bitmap.close();
      return;
    }
    if (this.mode === 'jpg' && this.engine) {
      // 只有 JPG 又有显卡：换成清楚的纹理。复制进显卡后位图就能关掉。
      const key = `jpg:${photo.id}`;
      this.engine.addJpg(key, bitmap);
      bitmap.close();
      this.engine.show(key);
      this.push();
      return;
    }
    this.sharp?.close();
    this.sharp = bitmap;
    this.paintStill(bitmap);
  }
}
