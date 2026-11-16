// 编辑一张。拖滑块时只改 setLook 和滑块上的数字，不重建页面。

import type { Engine } from '../../../frontend/src/index';
import { describeDng, type Described } from '../core/api';
import { NEUTRAL_BAKE_KEY } from '../lib/neutral';
import { formatClock } from '../lib/group';
import { histPoint, histShape } from '../lib/hist';
import { AdjustView, toAdjust } from './adjust_view';
import { CropStage, DEFAULT_GEOMETRY } from './crop_stage';
import { button, el, icon, ICON } from './dom';
import { curvesPanel, framePanel } from './later_panels';
import { LutView } from './lut_view';
import { photoMs, zeroKnobs, type AppApi, type Photo } from './model';
import { lookCache } from './looks';
import { pasteState } from './paste_state';
import { openCopySheet, PASTE_ICON } from './paste_ui';
import { RecipeView } from './recipe_view';

/**
 * 显卡在忙（上一张的直方图还在读回）时 `histogram()` 立刻给 null，于是每帧再试一次。
 * 连着这么多帧都拿不到（显卡丢了之类）就放慢到每 HIST_SLOW_MS 试一次，一直试到画上、换片或关掉，
 * 不会停下来等用户再调一下，也不会每帧都去问。
 */
const HIST_FAST_MISSES = 300;
const HIST_SLOW_MS = 250;

const TABS = ['配方', '调整', '裁剪', '边框', 'LUT', '曲线'] as const;

function isGone(err: unknown): boolean {
  const name = err instanceof DOMException ? err.name : err instanceof Error ? err.name : '';
  return name === 'NotFoundError' || name === 'NotReadableError';
}

export class EditorView {
  private token = 0;
  private photo: Photo | null = null;
  private mode: 'raw' | 'jpg' | 'still' = 'still';
  private wantNeighbor = '';
  private dragging = false;
  private holding = false;
  /** 直方图：正在数、数完还要再数一次、上次画的是哪个内容版本。静止时不再读显卡。 */
  private histBusy = false;
  private histAgain = false;
  private histDrawn = -1;
  /** 排在下一帧（或放慢后排在定时器上）的重试和连着拿不到的次数。 */
  private histFrame = 0;
  private histTimer = 0;
  private histMisses = 0;
  private clipLow: HTMLElement | null = null;
  private clipHigh: HTMLElement | null = null;
  private stageLabel: HTMLElement | null = null;
  private pv: HTMLElement | null = null;
  private gpuCanvas: HTMLCanvasElement | null = null;
  private still: HTMLCanvasElement | null = null;
  private hist: HTMLCanvasElement | null = null;
  private caption = '';
  private lookCaption = '';
  private sharp: ImageBitmap | null = null;
  private placeholder: HTMLElement | null = null;
  private recipes: RecipeView | null = null;
  private adjust: AdjustView | null = null;
  private cropStage: CropStage | null = null;
  private pvObserver: ResizeObserver | null = null;
  private luts: LutView | null = null;
  private lutTabBtn: HTMLButtonElement | null = null;
  /** 舞台上套的烘焙表（中性转换或某个配方）。 */
  private recipeKey = NEUTRAL_BAKE_KEY;
  /** 舞台上套的 LUT 渲染令牌和名称。 */
  private lutToken: string | null = null;
  private lutLabel: string | null = null;
  private tab = 1;
  /** 这张读完、清楚的图也换上之后才 resolve。只给截图和测试等。 */
  settled: Promise<void> = Promise.resolve();
  private sharpening: Promise<void> = Promise.resolve();
  /** 套用或撤销改了正在看的这张：重新打开，照片和面板都按新的编辑状态画。 */
  private offPaste: (() => void) | null = null;

  constructor(
    private readonly host: HTMLElement,
    private readonly shell: HTMLElement,
    private readonly api: AppApi,
    private readonly engine: Engine | null,
  ) {}

  open(photo: Photo): void {
    const index = this.api.photos().findIndex((item) => item.id === photo.id);
    // 正在看的这张的配方卡小图不会被全局上限挤掉。
    lookCache.setOpen(photo);
    pasteState.gridLeft();
    this.offPaste?.();
    this.offPaste = pasteState.onEdit((ids) => {
      if (this.photo && ids.has(this.photo.id) && this.api.photo(this.photo.id) === this.photo) this.api.open(this.photo.id);
    });
    this.build(photo, index);
    this.shell.dataset.settled = '0';
    this.settled = this.load(photo)
      .then(() => this.sharpening)
      .finally(() => {
        if (this.photo === photo) this.shell.dataset.settled = photo.stem;
      });
  }

  /** 模拟器没有显卡，滑块是停用的。截图时只把拖动中的版式摆出来。 */
  forceDrag(): void {
    this.enterDrag();
  }

  destroy(): void {
    this.token += 1;
    this.photo = null;
    lookCache.setOpen(null);
    this.offPaste?.();
    this.offPaste = null;
    this.dragging = false;
    this.holding = false;
    this.cropStage?.destroy();
    this.cropStage = null;
    this.wantNeighbor = '';
    this.histAgain = false;
    this.cancelHistRetry();
    this.pvObserver?.disconnect();
    this.pvObserver = null;
    this.shell.dataset.drag = '0';
    this.shell.dataset.settled = '0';
    this.sharp?.close();
    this.sharp = null;
    this.recipes?.destroy();
    this.recipes = null;
    this.adjust?.destroy();
    this.adjust = null;
    this.luts?.destroy();
    this.luts = null;
    this.lutTabBtn = null;
    this.engine?.detach();
  }

  private build(photo: Photo, index: number): void {
    this.photo = photo;
    this.tab = 1;
    this.lookCaption = '';
    this.pvObserver?.disconnect();
    this.pvObserver = null;
    this.cropStage?.destroy();
    this.cropStage = new CropStage(this.shell, this.api, this.engine, {
      photo: () => this.photo,
      mode: () => this.mode,
      active: () => this.tab === 2,
      sharp: () => this.sharp,
      stillCanvas: () => this.still,
      placeholderEl: () => this.placeholder,
      pvEl: () => this.pv,
      caption: () => this.caption,
      holding: () => this.holding,
      setHolding: (on) => {
        this.holding = on;
        this.renderStageLabel();
        this.push();
      },
      setLabel: (text) => this.setLabel(text),
      enterDrag: () => this.enterDrag(),
      leaveDrag: () => this.leaveDrag(),
      dragging: () => this.dragging,
      requestHist: () => this.requestHist(),
      swipe: (id, dir) => this.api.swipe(id, dir),
      onCommitted: (target, changed) => {
        if (changed && this.photo === target) {
          if (this.mode === 'raw') {
            this.recipes?.neutral(null);
            void this.neutralCards(target, `raw:${target.id}`);
          }
          this.recipes?.refresh();
        }
      },
      restLabelText: () => this.restLabelText(),
    });

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
    this.pv = pv;
    this.gpuCanvas = document.createElement('canvas');
    this.gpuCanvas.className = 'gpu';
    this.still = document.createElement('canvas');
    this.still.className = 'still';
    this.still.hidden = true;
    this.placeholder = el('div', 'ph', '只有 RAW');
    this.placeholder.hidden = true;

    pv.append(this.gpuCanvas, this.still, this.placeholder);
    this.cropStage.mountOverlay(pv);
    this.stageLabel = el('div', 'lbl');
    stage.append(pv, this.stageLabel);
    if (typeof ResizeObserver === 'function') {
      this.pvObserver = new ResizeObserver(() => {
        this.engine?.redraw();
        if (this.tab === 2) this.cropStage?.renderOverlay();
      });
      this.pvObserver.observe(pv);
    }
    this.cropStage.bindGesture(stage);

    this.recipeKey = NEUTRAL_BAKE_KEY;
    this.lutToken = null;
    this.lutLabel = null;
    this.recipes = new RecipeView(this.api, this.engine, photo, {
      mode: () => this.mode,
      look: (key, label) => this.useLook(key, label),
      dragging: () => this.dragging,
      recipeTab: () => this.tab === 0,
      geometry: () => this.cropStage?.resolveBoxGeometry(photo) ?? Promise.resolve({ geometry: { ...DEFAULT_GEOMETRY }, cropped: false }),
    });
    this.luts = new LutView(this.shell, this.api, this.engine, photo, {
      mode: () => this.mode,
      recipeGpuKey: () => this.recipeKey,
      stageLut: (token, label) => this.useLut(token, label),
      dragging: () => this.dragging,
      lutTab: () => this.tab === 4,
      lutChanged: () => this.updateLutTabDot(),
    });
    this.adjust?.destroy();
    this.adjust = new AdjustView(
      {
        photo: () => this.photo,
        push: () => this.push(),
        enterDrag: () => this.enterDrag(),
        leaveDrag: () => this.leaveDrag(),
        dragging: () => this.dragging,
      },
      this.histEl(),
    );
    const strip = this.recipes.stripEl();
    const cropPanelEl = this.cropStage.createPanel();

    const sheet = el('div', 'sheet');
    sheet.append(el('div', 'grab'));
    const tabs = el('div', 'tabs');
    const panels: HTMLElement[] = [];
    this.lutTabBtn = null;
    TABS.forEach((name, tabIndex) => {
      const tab = button('', name);
      tab.dataset.tab = String(tabIndex);
      tab.setAttribute('role', 'tab');
      if (tabIndex === 4) {
        tab.dataset.tab = 'lut';
        this.lutTabBtn = tab;
        this.updateLutTabDot();
      }
      if (tabIndex === 1) tab.classList.add('on');
      if (tabIndex === 2) this.cropStage?.attachTabButton(tab);
      tab.addEventListener('click', () => {
        if (this.tab === tabIndex) return;
        for (const item of tabs.children) item.classList.remove('on');
        tab.classList.add('on');
        this.tab = tabIndex;
        if (tabIndex === 0) this.recipes?.tabOpened();
        if (tabIndex === 4) this.luts?.tabOpened();
        panels.forEach((panel, panelIndex) => {
          panel.hidden = panelIndex !== tabIndex;
        });
        this.cropStage?.syncTabMode();
      });
      tabs.append(tab);
    });
    panels.push(this.recipes.panelEl(), this.adjust.panelEl(), cropPanelEl, framePanel(), this.luts.panelEl(), curvesPanel());
    panels.forEach((panel, panelIndex) => {
      panel.hidden = panelIndex !== 1;
    });
    const copy = button('btn', '复制调整');
    copy.prepend(icon(PASTE_ICON.copy));
    copy.dataset.act = 'copy';
    copy.addEventListener('click', () => {
      if (this.photo) openCopySheet(this.shell, this.api, this.photo, () => undefined);
    });
    const save = button('btn primary', '导出这张');
    save.dataset.act = 'export';
    save.addEventListener('click', () => this.api.exportPhotos([photo.id]));
    const acts = el('div', 'actbar');
    acts.append(copy, save);
    sheet.append(tabs, ...panels, acts);
    screen.append(nav, stage, strip, sheet);
    this.host.append(screen);
    if (this.engine && this.gpuCanvas) this.engine.attach(this.gpuCanvas);
    this.cropStage.paintTabDot();
    this.cropStage.paintStill(photo.bitmap);
  }

  private updateLutTabDot(): void {
    this.lutTabBtn?.classList.toggle('dot', this.photo?.lut !== null);
  }

  /** 配方那边定下了舞台该用哪张烘焙表。 */
  private useLook(key: string, label: string): void {
    this.recipeKey = key;
    this.lookCaption = label;
    if (this.tab !== 2) this.setLabel(label);
    this.push();
    this.luts?.recipeUpdated();
  }

  /** LUT 这边定下了舞台该用哪张 3D LUT 纹理。 */
  private useLut(token: string | null, label: string | null): void {
    this.lutToken = token;
    this.lutLabel = label;
    this.renderStageLabel();
    this.push();
  }

  private histEl(): HTMLElement {
    const hist = el('div', 'hist');
    this.hist = document.createElement('canvas');
    this.hist.width = 816;
    this.hist.height = 128;
    this.clipLow = el('span', 'clip l');
    this.clipHigh = el('span', 'clip r');
    this.clipLow.setAttribute('aria-label', '暗部压死');
    this.clipHigh.setAttribute('aria-label', '高光溢出');
    hist.append(this.hist, this.clipLow, this.clipHigh);
    return hist;
  }

  private push(): void {
    if (!this.engine || !this.photo || this.mode === 'still') return;
    const knobs = this.holding ? zeroKnobs() : this.photo.knobs;
    const lut = this.holding ? null : this.lutToken;
    if (lut && !this.engine.hasLut(lut)) {
      this.luts?.ensureActive();
    }
    this.engine.setLook({
      // 按住看原图：中性转换、不用 LUT、调整归零。
      recipe: this.mode === 'raw' ? (this.holding ? NEUTRAL_BAKE_KEY : this.recipeKey) : null,
      lut,
      lutStrength: 1,
      curve: null,
      adjust: toAdjust(knobs),
    });
    this.requestHist();
  }

  private setLabel(text: string): void {
    this.caption = text;
    this.renderStageLabel();
  }

  private renderStageLabel(): void {
    if (!this.stageLabel) return;
    this.stageLabel.replaceChildren();
    const text = this.holding
      ? '原图'
      : this.lutLabel
        ? `LUT · ${this.lutLabel}${this.caption ? ` · ${this.caption}` : ''}`
        : this.caption;
    if (!text) return;
    this.stageLabel.append(el('span', 'tag neutral', text));
  }

  private enterDrag(): void {
    if (this.dragging) return;
    this.dragging = true;
    this.shell.dataset.drag = '1';
    this.api.watch('drag', true);
    const token = this.token;
    requestAnimationFrame(() => {
      if (token === this.token) this.engine?.redraw();
    });
  }

  private leaveDrag(): void {
    this.dragging = false;
    this.shell.dataset.drag = '0';
    this.api.watch('drag', false);
    const token = this.token;
    requestAnimationFrame(() => {
      if (token === this.token) this.engine?.redraw();
    });
  }

  /**
   * 要一次直方图：RAW 第一帧、换片、每次调整之后。正在数就只记一笔，数完看内容版本变没变再补一次，
   * 所以拖滑块时自然合并，停下来以后不会一直读显卡。
   */
  private requestHist(retry = false): void {
    if (!this.engine || this.mode === 'still') return;
    if (!retry) {
      // 新的调整：重新按帧试，排着的慢重试不要了。
      this.histMisses = 0;
      this.cancelHistRetry();
    }
    if (this.histBusy) {
      this.histAgain = true;
      return;
    }
    this.histBusy = true;
    this.histAgain = false;
    const token = this.token;
    void this.countHist(token)
      .catch(() => false)
      .then((drawn) => {
        this.histBusy = false;
        if (token !== this.token) return;
        this.histMisses = drawn ? 0 : this.histMisses + 1;
        const engine = this.engine;
        // 只有画上了、而且没有更新的版本才停；没画上（忙、这一帧还没画完、显卡丢了）都接着按预算重试。
        if (drawn && !this.histAgain && !(engine && engine.contentVersion !== this.histDrawn)) return;
        // 排到下一帧（连着拿不到太久就排到慢定时器）再数。不能在这里直接再调：显卡忙时 histogram() 马上给 null，
        // 一串微任务接着跑，页面的定时器、显卡回调和手势都轮不到，换片时整页就卡死了。
        this.cancelHistRetry();
        const again = (): void => {
          this.histFrame = 0;
          this.histTimer = 0;
          if (token === this.token) this.requestHist(true);
        };
        if (this.histMisses < HIST_FAST_MISSES) this.histFrame = requestAnimationFrame(again);
        else this.histTimer = window.setTimeout(again, HIST_SLOW_MS);
      });
  }

  private cancelHistRetry(): void {
    if (this.histFrame) cancelAnimationFrame(this.histFrame);
    if (this.histTimer) window.clearTimeout(this.histTimer);
    this.histFrame = 0;
    this.histTimer = 0;
  }

  /** 数到并画上了才是 true；显卡在忙、这一帧还没画完、换了片都是 false。 */
  private async countHist(token: number): Promise<boolean> {
    const engine = this.engine;
    // 这一帧还没画完（pending）成片纹理就不是新的：这次算没拿到，和显卡忙一样占同一个重试预算，
    // 每次调度只看一次。显卡丢了时 pending 可能一直是 true，这样 300 帧后也只是每 250 ms 看一次。
    if (!engine || token !== this.token || engine.pending) return false;
    const data = await engine.histogram();
    if (!data || token !== this.token) return false;
    this.histDrawn = data.version;
    this.drawHist(data.counts, data.samples, data.version);
    return true;
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

  private restLabelText(): string {
    return this.mode === 'raw' ? this.lookCaption || '中性转换' : this.mode === 'jpg' ? '相机 JPG' : this.photo?.jpg ? '相机 JPG' : '只有 RAW';
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

  private same(token: number, photo: Photo, version: number, session: number, rev?: number): boolean {
    return (
      token === this.token &&
      this.photo === photo &&
      this.api.session() === session &&
      this.api.photo(photo.id) === photo &&
      photo.version === version &&
      (rev === undefined || photo.rev === rev)
    );
  }

  private prefetch(next: Photo | null): void {
    if (!next?.dng) {
      this.wantNeighbor = '';
      return;
    }
    const id = next.id;
    const version = next.version;
    const session = this.api.session();
    const token = this.token;
    this.wantNeighbor = id;
    void this.api.readFull(next).then((reply) => {
      if (!reply.ok || this.token !== token || this.wantNeighbor !== id || this.api.session() !== session || this.api.photo(id) !== next || next.version !== version) return;
      this.api.keepWarm(id, version, reply.bytes);
    });
  }

  private async load(photo: Photo): Promise<void> {
    const token = ++this.token;
    const version = photo.version;
    const session = this.api.session();
    if (!photo.dng) {
      this.showStillPhoto(photo, photo.jpg ? '相机 JPG' : '只有 RAW');
      if (this.engine && photo.bitmap) {
        const key = `jpg:${photo.id}`;
        this.engine.addJpg(key, photo.bitmap);
        this.engine.show(key);
        this.mode = 'jpg';
        this.luts?.start(key);
        if (this.gpuCanvas) this.gpuCanvas.hidden = false;
        if (this.still) this.still.hidden = true;
        this.push();
        await this.cropStage?.applyPreviewGeometry();
        if (!this.same(token, photo, version, session)) return;
        const frame = this.engine.frame();
        if (frame) this.shell.dataset.frame = '1';
        this.adjust?.setDisabled(false);
        this.cropStage?.syncLoaded();
      }
      return;
    }
    // 这个版本的完整源已经在显卡里：不读卡，直接显示。卡被拔出后照样能开、能调。
    const key = `raw:${photo.id}`;
    if (this.engine && photo.cached === version && this.engine.hasSource(key)) {
      await this.showRaw(key);
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
      if (!this.same(token, photo, version, session)) return;
      if (err instanceof DOMException && err.name === 'AbortError') return;
      if (isGone(err)) this.api.markGone(photo.id, version);
      else this.api.markBad(photo.id, version);
      return;
    }
    if (!this.same(token, photo, version, session)) return;
    // 先用 WASM 描述，坏文件的判断和有没有显卡无关。
    let described: Described;
    try {
      described = await describeDng(bytes);
    } catch {
      if (this.same(token, photo, version, session)) this.api.markBad(photo.id, version);
      return;
    }
    if (!this.same(token, photo, version, session)) return;
    if (!described.layout || described.reason) {
      this.api.markBad(photo.id, version);
      return;
    }
    photo.checked = version;
    // 镜头名和 ISO：相机 JPG 里没有时用 DNG 里的（选镜头单元和模型）。
    photo.dngMeta = described.meta;
    if (!this.engine) {
      this.showStillPhoto(photo, '这个浏览器现在用不了显卡预览');
      this.adjust?.setDisabled(true);
      this.recipes?.refresh();
      this.prefetch(this.nextPhoto(1));
      return;
    }
    try {
      // 每张照片一个固定的键：再加一次会替换（并销毁）原来那份纹理，不会同一张占两份。
      // 上传由 App 按照片排队，提交前核对版本；过期的请求不会碰显卡里的源。
      const installed = await this.api.uploadRaw(photo, version, described.layout, bytes, described.scene);
      if (!installed || !this.same(token, photo, version, session)) return;
      await this.showRaw(key);
      if (!this.same(token, photo, version, session)) return;
      this.prefetch(this.nextPhoto(1));
    } catch {
      if (!this.same(token, photo, version, session)) return;
      this.api.markBad(photo.id, version);
    }
  }

  /** 这张 RAW 的中性小图：每个版本只请显卡出一次，之后换片回来直接用。 */
  private async neutralCards(photo: Photo, key: string): Promise<void> {
    if (!this.engine || !this.cropStage) return;
    const version = photo.version;
    const rev = photo.rev;
    const token = this.token;
    const session = this.api.session();
    // 照片还是这次会话里的同一张、同一个编辑实例、版本和编辑版本：离开重开、清空、去掉、重新选过或改过裁剪都算过期。
    const alive = () => this.same(token, photo, version, session, rev);
    if (photo.neutral && photo.neutral.version === version) {
      if (alive()) this.recipes?.neutral(photo.neutral.bitmap);
      return;
    }
    const look = { recipe: NEUTRAL_BAKE_KEY, lut: null, lutStrength: 1, curve: null, adjust: toAdjust(zeroKnobs()) };
    const { geometry } = await this.cropStage.resolveBoxGeometry(photo);
    if (!alive()) return;
    const out = await this.engine.thumb({ source: key, look, geometry, aspect: null, maxEdge: 192 });
    if (!out || !alive()) return;
    const bitmap = await createImageBitmap(new ImageData(new Uint8ClampedArray(out.data), out.w, out.h));
    if (!alive()) {
      bitmap.close();
      return;
    }
    this.recipes?.neutral(null);
    photo.neutral?.bitmap.close();
    photo.neutral = { version, bitmap };
    this.recipes?.neutral(bitmap);
  }

  private async showRaw(key: string): Promise<void> {
    if (!this.engine || !this.photo) return;
    const photo = this.photo;
    const token = this.token;
    const version = photo.version;
    const session = this.api.session();
    this.engine.show(key);
    this.mode = 'raw';
    this.push();
    await this.cropStage?.applyPreviewGeometry();
    if (!this.same(token, photo, version, session)) return;
    const frame = this.engine.frame();
    if (frame) this.shell.dataset.frame = '1';
    if (!this.lookCaption) this.lookCaption = '中性转换';
    if (this.tab !== 2 && !this.caption) this.setLabel(this.lookCaption);
    this.cropStage?.syncLoaded();
    this.recipes?.start(key);
    this.luts?.start(key);
    if (this.gpuCanvas) this.gpuCanvas.hidden = false;
    if (this.still) this.still.hidden = true;
    this.adjust?.setDisabled(false);
    void this.neutralCards(photo, key);
  }

  private showStillPhoto(photo: Photo, label: string): void {
    this.mode = 'still';
    this.lookCaption = label;
    if (this.tab !== 2) this.setLabel(label);
    this.cropStage?.paintStill(photo.bitmap);
    if (this.gpuCanvas) this.gpuCanvas.hidden = true;
    if (!this.engine) this.adjust?.setDisabled(true);
    // 没有相机 JPG、DNG 里也没有预览：照片区写「只有 RAW」，不留一块空灰。
    if (this.placeholder) this.placeholder.hidden = !!photo.bitmap || !!photo.jpg;
    this.sharpening = this.sharpen(photo);
  }

  /** 先放缩略图，Worker 解好屏幕宽度的那张再换上。主线程只画一次。 */
  private async sharpen(photo: Photo): Promise<void> {
    const token = this.token;
    const version = photo.version;
    const session = this.api.session();
    const alive = () => this.same(token, photo, version, session);
    const fullJpg = !photo.dng && !!photo.jpg && !!this.engine;
    const width = fullJpg ? Number.POSITIVE_INFINITY : Math.min(2048, Math.round((this.host.clientWidth || 390) * (window.devicePixelRatio || 1)));
    const bitmap = await this.api.still(photo, width);
    if (!bitmap) return;
    if (!alive() || this.mode === 'raw') {
      bitmap.close();
      return;
    }
    if (this.mode === 'jpg' && this.engine) {
      // 只有 JPG 又有显卡：换成完整纹理，使 layerSize 和成片尺寸与导出一致。复制进显卡后位图就能关掉。
      const key = `jpg:${photo.id}`;
      this.engine.addJpg(key, bitmap);
      bitmap.close();
      this.engine.show(key);
      this.luts?.start(key);
      this.push();
      await this.cropStage?.applyPreviewGeometry();
      if (!alive()) return;
      this.engine.frame();
      this.cropStage?.syncLoaded();
      return;
    }
    this.sharp?.close();
    this.sharp = bitmap;
    this.cropStage?.paintStill(bitmap);
    this.cropStage?.syncLoaded();
  }
}
