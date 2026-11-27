// 编辑一张：大照片舞台（纯黑底边到边）+ 固定 128pt 单带 + 4 工具底栏（配方 · 调整 · 裁剪 · 更多 + 存到相册）+ U1 顶栏与一键导出。
// 拖滑块和切换工具时不重排舞台；顶栏与一键「存到相册」/ 就绪条衔接 U1。

import type { Engine } from '../../../frontend/src/index';
import { NEUTRAL_BAKE_KEY } from '../lib/neutral';
import { AdjustView, toAdjust } from './adjust_view';
import { showCalibSheet } from './calib_ui';
import { CropStage, DEFAULT_GEOMETRY } from './crop_stage';
import { openCurvesSheet } from './curves_sheet';
import { mountCurves, previewCurve, type CurvesView } from './curves_view';
import { el } from './dom';
import { EditorBand, positionFloatHist } from './editor_band';
import { EditorBar, type BandTool, type BarTool } from './editor_bar';
import { createHistWidget, drawHistCanvas, editedFlagsOf, fitGpuCanvas, observeStageBox, syncEditorBarOrder, type HistWidget, type StageBoxCache } from './editor_hist';
import { createEditorIpadBridge, type EditorIpadBridge } from './editor_ipad_bridge';
import { EditorMedia, NO_GPU_DETAIL, NO_GPU_RAW_DETAIL, NO_GPU_TITLE, type MediaMode } from './editor_media';
import { openExportOptions, startQuickExport, syncExportDock } from './export_ui';
import { releaseFinishDrawResources } from './finish_draw';
import { photoExportSize } from './frame_state';
import { FrameView } from './frame_view';
import { NO_PREVIEW_TEXT, noPreviewPlaceholder } from './gpu_loss';
import { createHdrPreview } from './hdr_hookup';
import { isIpadLayout, loadIpadHist, saveIpadHist, type LayoutMode } from './layout';
import { lookCache } from './looks';
import { LutView } from './lut_view';
import { zeroKnobs, type AppApi, type Photo } from './model';
import { openMoreSheet } from './more_sheet';
import { pasteState } from './paste_state';
import { mountEditorNav, type EditorNavHandle } from './editor_nav';
import { RecipeView } from './recipe_view';
import { ScrubPill } from './scrub_pill';
import { cancelHostSheets } from './sheet_lifecycle';
import { StageDecorController } from './stage_finish';
import { StageZoom } from './stage_zoom';
import { StampBandView } from './stamp_band';
import { showToast } from './toast';
import './frame.css';
import './ipad.css';

const HIST_FAST_MISSES = 300;
const HIST_SLOW_MS = 250;

export class EditorView {
  private token = 0;
  private openGen = 0;
  private photo: Photo | null = null;
  private mode: MediaMode = 'still';
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
  private histOpen = false;
  private histWidget: HistWidget | null = null;
  private scrubPill: ScrubPill | null = null;
  private stageLabel: HTMLElement | null = null;
  private stage: HTMLElement | null = null;
  private pv: HTMLElement | null = null;
  private gpuCanvas: HTMLCanvasElement | null = null;
  private still: HTMLCanvasElement | null = null;
  private caption = '';
  private lookCaption = '';
  private sharp: ImageBitmap | null = null;
  private placeholder: HTMLElement | null = null;
  private screen: HTMLElement | null = null;
  private band: EditorBand | null = null;
  private bar: EditorBar | null = null;
  private stageZoom: StageZoom | null = null;
  private recipes: RecipeView | null = null;
  private luts: LutView | null = null;
  private adjust: AdjustView | null = null;
  private curves: CurvesView | null = null;
  private cropStage: CropStage | null = null;
  private frames: FrameView | null = null;
  private stampBand: StampBandView | null = null;
  private ipad: EditorIpadBridge | null = null;
  private layoutMode: LayoutMode = 'phone-portrait';
  private pvObserver: ResizeObserver | null = null;
  private readonly boxCache: StageBoxCache = { pv: null, stage: null };
  private decor: StageDecorController | null = null;
  /** 舞台上套的烘焙表（中性转换或某个配方）。 */
  private recipeKey = NEUTRAL_BAKE_KEY;
  /** 舞台上套的 LUT 渲染令牌和名称。 */
  private lutToken: string | null = null;
  private lutLabel: string | null = null;
  private activeTool: BandTool = 'recipe';
  private recipeSub: 'recipe' | 'lut' = 'recipe';
  /** 这张读完、清楚的图也换上之后才 resolve。只给截图和测试等。 */
  settled: Promise<void> = Promise.resolve();
  private offPaste: (() => void) | null = null;
  private navCtrl: EditorNavHandle | null = null;
  private readonly host: HTMLElement;
  private readonly shell: HTMLElement;
  private readonly api: AppApi;
  private readonly engine: Engine | null;
  private readonly media: EditorMedia;
  private readonly hdr = createHdrPreview({
    photo: () => this.photo,
    api: () => this.api,
    dragging: () => this.dragging,
    holding: () => this.holding,
    engine: () => this.engine,
    onShowing: () => this.renderStageLabel(),
    recipeKey: () => this.recipeKey,
    lutToken: () => this.lutToken,
    knobs: () => this.photo?.knobs ?? zeroKnobs(),
    mode: () => this.mode,
  });

  constructor(host: HTMLElement, shell: HTMLElement, api: AppApi, engine: Engine | null) {
    this.host = host;
    this.shell = shell;
    this.api = api;
    this.engine = engine;
    this.media = new EditorMedia({
      api: () => this.api,
      engine: () => this.engine,
      host: () => this.host,
      shell: () => this.shell,
      photo: () => this.photo,
      mode: () => this.mode,
      setMode: (m) => {
        this.mode = m;
      },
      nextToken: () => ++this.token,
      same: (token, photo, version, session, rev) => this.same(token, photo, version, session, rev),
      gpuCanvas: () => this.gpuCanvas,
      stillCanvas: () => this.still,
      placeholderEl: () => this.placeholder,
      cropStage: () => this.cropStage,
      setSharp: (bitmap) => {
        this.sharp?.close();
        this.sharp = bitmap;
      },
      setLookCaption: (text) => {
        this.lookCaption = text;
      },
      lookCaption: () => this.lookCaption,
      caption: () => this.caption,
      isCropActive: () => this.activeTool === 'crop',
      setLabel: (text) => this.setLabel(text),
      push: () => this.push(),
      setAdjustDisabled: (disabled) => this.adjust?.setDisabled(disabled),
      startRecipes: (key) => this.recipes?.start(key),
      refreshRecipes: () => { this.recipes?.refresh(); this.navCtrl?.refresh(); },
      setNeutralRecipe: (bitmap) => this.recipes?.neutral(bitmap),
      startLuts: (key) => this.luts?.start(key),
      onMediaReady: () => {
        this.fitStageCanvas(true);
        this.decor?.onMediaReady(); this.ipad?.syncNoGpu(); // 说明条和舞台同时定下说什么
      },
      syncFloatHistPos: () => this.syncFloatHistPos(),
    });
  }

  open(photo: Photo): void {
    cancelHostSheets(this.shell);
    this.openGen += 1;
    const index = this.api.photos().findIndex((item) => item.id === photo.id);
    lookCache.setOpen(photo);
    pasteState.gridLeft();
    this.offPaste?.();
    this.offPaste = pasteState.onEdit((ids) => {
      if (this.photo && ids.has(this.photo.id) && this.api.photo(this.photo.id) === this.photo) this.api.open(this.photo.id);
    });
    this.build(photo, index);
    this.shell.dataset.settled = '0';
    this.settled = this.media
      .load(photo)
      .then(() => this.media.sharpeningPromise())
      .finally(() => {
        if (this.photo === photo) this.shell.dataset.settled = photo.stem;
      });
  }

  /** 模拟器没有显卡，滑块是停用的。截图时只把拖动中的版式摆出来。 */
  forceDrag(): void {
    this.enterDrag();
  }

  destroy(): void {
    cancelHostSheets(this.shell);
    this.openGen += 1;
    this.token += 1;
    this.photo = null;
    lookCache.setOpen(null);
    this.offPaste?.();
    this.offPaste = null;
    this.navCtrl?.destroy();
    this.navCtrl = null;
    this.dragging = this.holding = this.histOpen = this.histAgain = false;
    this.stageZoom?.destroy();
    this.cropStage?.destroy();
    this.pvObserver?.disconnect();
    this.sharp?.close();
    this.recipes?.destroy();
    this.adjust?.destroy();
    this.curves?.destroy();
    this.frames?.destroy();
    this.ipad?.destroy();
    this.scrubPill?.destroy();
    this.decor?.destroy();
    this.luts?.destroy();
    this.stageZoom = this.cropStage = this.pvObserver = this.sharp = null;
    this.boxCache.pv = this.boxCache.stage = null;
    this.recipes = this.adjust = this.curves = this.frames = this.stampBand = null;
    this.ipad = this.band = this.bar = this.screen = this.stage = this.pv = null;
    this.scrubPill = this.decor = this.luts = null;
    this.media.reset();
    this.cancelHistRetry();
    this.shell.dataset.drag = '0';
    this.shell.dataset.settled = '0';
    releaseFinishDrawResources();
    this.hdr.close();
    for (const cv of [this.histWidget?.canvas ?? null, this.still, this.gpuCanvas]) {
      if (cv) cv.width = cv.height = 0;
    }
    this.histWidget = null;
    this.still = this.gpuCanvas = null;
    this.engine?.detach();
    syncExportDock(this.shell);
  }

  private build(photo: Photo, index: number): void {
    this.photo = photo;
    const gen = this.openGen;
    const version = photo.version;
    const session = this.api.session();
    const viewAlive = (): boolean =>
      gen === this.openGen &&
      this.photo === photo &&
      this.api.photo(photo.id) === photo &&
      photo.version === version &&
      this.api.session() === session;

    // 换下一张时总是回到「配方」工具；单带收起状态由 EditorBand 从本地存储记住。
    this.activeTool = 'recipe';
    this.recipeSub = 'recipe';
    this.histOpen = false;
    this.lookCaption = '';
    this.pvObserver?.disconnect();
    this.pvObserver = null;
    this.boxCache.pv = this.boxCache.stage = null;
    this.stageZoom?.destroy();
    this.stageZoom = null;
    this.cropStage?.destroy();
    this.scrubPill?.destroy();
    this.scrubPill = new ScrubPill(this.shell);
    const onScrub = (text: string | null) => {
      if (text) this.scrubPill?.show(text);
      else this.scrubPill?.release();
    };

    this.cropStage = new CropStage(this.shell, this.api, this.engine, {
      photo: () => this.photo,
      mode: () => this.mode,
      active: () => this.activeTool === 'crop',
      sharp: () => this.sharp,
      stillCanvas: () => this.still,
      placeholderEl: () => this.placeholder,
      pvEl: () => this.pv,
      caption: () => this.caption,
      holding: () => this.holding,
      setHolding: (on) => this.setHolding(on),
      setLabel: (text) => this.setLabel(text),
      enterDrag: () => this.enterDrag(),
      leaveDrag: () => this.leaveDrag(),
      dragging: () => this.dragging,
      requestHist: () => this.requestHist(),
      swipe: (id, dir) => this.api.swipe(id, dir),
      resetZoom: () => this.stageZoom?.reset(),
      onScrub,
      onCommitted: (target, changed) => {
        if (changed && this.photo === target) {
          if (this.mode === 'raw') {
            this.recipes?.neutral(null);
            void this.media.neutralCards(target, `raw:${target.id}`, this.token);
          }
          this.recipes?.refresh();
          this.decor?.onCropChanged();
          this.frames?.refresh();
          this.updateEditedDots();
          this.syncFloatHistPos();
        }
      },
      restLabelText: () => this.restLabelText(),
    });

    this.host.replaceChildren();
    const photos = this.api.photos();
    const screen = el('section', 'screen editor');
    screen.dataset.photo = photo.stem;
    this.screen = screen;
    this.navCtrl?.destroy();
    this.navCtrl = mountEditorNav(this.shell, this.api, photo, index, photos.length);
    const nav = this.navCtrl.el;

    const stage = el('div', 'stage');
    this.stage = stage;
    const pv = el('div', 'pv');
    this.pv = pv;
    this.gpuCanvas = document.createElement('canvas');
    this.gpuCanvas.className = 'gpu';
    this.gpuCanvas.hidden = !this.engine;
    this.still = document.createElement('canvas');
    this.still.className = 'still';
    this.still.hidden = true;
    this.placeholder = noPreviewPlaceholder();
    this.placeholder.hidden = true;

    pv.append(this.gpuCanvas, this.still, this.placeholder);
    this.cropStage.mountOverlay(pv);

    const histWidget = createHistWidget();
    histWidget.root.hidden = true;
    this.histWidget = histWidget;
    stage.append(pv, histWidget.root, this.scrubPill.el());

    this.pvObserver = observeStageBox(stage, pv, this.boxCache, () => {
      this.fitStageCanvas();
      this.engine?.redraw();
      if (this.activeTool === 'crop') this.cropStage?.renderOverlay();
      this.syncFloatHistPos();
    });

    this.stageZoom = new StageZoom(stage, pv, {
      isCropActive: () => this.activeTool === 'crop',
      onTap: () => this.toggleBandCollapsed(),
      onSwipe: (dir) => { if (this.photo) this.api.swipe(this.photo.id, dir); },
      onHold: (on) => this.setHolding(on),
      imageSize: () => (this.photo ? photoExportSize(this.photo, this.engine) : null),
      // 电脑上：Ctrl/⌘ + 滚轮、捏合、双击和 + / - / 0 缩放，F 收起面板，按住 \ 看原图（stage_zoom.ts）。
    });
    this.cropStage.bindGesture(stage);

    this.recipeKey = NEUTRAL_BAKE_KEY;
    this.lutToken = null;
    this.lutLabel = null;
    const cropGeo = () =>
      this.cropStage?.resolveBoxGeometry(photo) ?? Promise.resolve({ geometry: { ...DEFAULT_GEOMETRY }, cropped: false });

    this.recipes = new RecipeView(this.api, this.engine, photo, {
      mode: () => this.mode,
      look: (key, label) => this.useLook(key, label),
      dragging: () => this.dragging,
      recipeTab: () => (this.activeTool === 'recipe' || isIpadLayout(this.layoutMode)) && this.recipeSub === 'recipe',
      geometry: cropGeo,
      openLut: () => this.setRecipeSub('lut'),
      lutActive: () => this.photo?.lut !== null,
      onScrub,
      onEditedChanged: () => this.updateEditedDots(),
    });
    this.luts = new LutView(this.shell, this.api, this.engine, photo, {
      mode: () => this.mode,
      recipeGpuKey: () => this.recipeKey,
      stageLut: (token, label) => this.useLut(token, label),
      dragging: () => this.dragging,
      lutTab: () => (this.activeTool === 'recipe' || isIpadLayout(this.layoutMode)) && this.recipeSub === 'lut',
      lutChanged: () => {
        this.recipes?.syncLutDot();
        this.ipad?.syncSub(this.recipeSub, this.photo?.lut !== null);
        this.updateEditedDots();
      },
      openRecipe: () => this.setRecipeSub('recipe'),
    });
    this.adjust?.destroy();
    this.adjust = new AdjustView({
      photo: () => this.photo,
      push: () => this.push(),
      enterDrag: () => this.enterDrag(),
      leaveDrag: () => this.leaveDrag(),
      dragging: () => this.dragging,
      refreshCurves: () => this.curves?.refresh(),
      openCurves: () => this.openCurves(),
      toggleHist: () => this.toggleHist(),
      histVisible: () => this.histOpen,
      onScrub,
      onEditedChanged: () => this.updateEditedDots(),
    });
    if (!this.engine) this.adjust.setDisabled(true);
    const cropPanelEl = this.cropStage.createPanel();
    const cropHeadEl = this.cropStage.panelHeadEl();

    this.decor?.destroy();
    this.decor = new StageDecorController({
      pv, stage, photo, engine: this.engine, gpuCanvas: this.gpuCanvas, still: this.still,
      mode: () => this.mode,
      frameTab: () => this.activeTool === 'frame' || this.activeTool === 'stamp',
      cropTab: () => this.activeTool === 'crop' || this.holding,
      geometry: cropGeo,
      isAlive: viewAlive,
      onMeta: () => { this.frames?.refresh(); this.stampBand?.refresh(); },
      onBackdrop: () => this.frames?.backdropUpdated(),
      onDecorClear: () => this.fitStageCanvas(true),
      onToast: (msg) => showToast(this.shell, msg),
    });

    this.frames?.destroy();
    this.frames = new FrameView(this.shell, photo, {
      ew: () => photoExportSize(photo, this.engine),
      backdrop: () => this.decor?.backdropCanvas() ?? null,
      backdropKey: () => this.decor?.backdrop()?.key ?? '',
      isAlive: viewAlive,
      onBack: () => this.selectBandTool('more', true),
      onChange: () => { void this.decor?.sync(); this.hdr.invalidate(); this.stampBand?.refresh(); this.updateEditedDots(); },
    });

    this.stampBand = new StampBandView(this.shell, photo, {
      isAlive: viewAlive,
      onBack: () => this.selectBandTool('more', true),
      onChange: () => { void this.decor?.sync(); this.hdr.invalidate(); this.frames?.refresh(); this.updateEditedDots(); },
    });

    this.curves = mountCurves(this.curves, {
      photo: () => this.photo,
      push: () => { this.push(); this.updateEditedDots(); },
      enterDrag: () => this.enterDrag(false),
      leaveDrag: () => this.leaveDrag(),
      dragging: () => this.dragging,
    });
    this.curves.panelEl().hidden = true;

    const band = new EditorBand();
    this.band = band;
    this.stageLabel = band.labelEl();
    this.ipad?.destroy();
    this.ipad = createEditorIpadBridge({
      shell: this.shell, api: this.api, photo, hasGpu: this.engine !== null, moreHooks: this.moreHooksOf(photo),
      onLayoutChange: (mode) => this.applyLayout(mode),
      onToggleHist: () => { this.toggleHist(); },
      onSubChange: (sub) => this.setRecipeSub(sub),
      onRecipeFilter: () => this.recipes?.openFilter(),
    });

    const recipeSlot = el('div', 'band-slot');
    this.luts.panelEl().hidden = true;
    recipeSlot.append(this.recipes.panelEl(), this.luts.panelEl());
    const recipeHeadSlot = el('div', 'band-head-slot');
    this.luts.headEl().hidden = true;
    recipeHeadSlot.append(this.recipes.headEl(), this.luts.headEl());

    band.register('recipe', recipeSlot, recipeHeadSlot);
    band.register('adjust', this.adjust.panelEl(), this.adjust.headEl());
    band.register('crop', cropPanelEl, cropHeadEl ?? undefined);
    band.register('frame', this.frames.panelEl(), this.frames.headEl());
    band.register('stamp', this.stampBand.panelEl(), el('b', 'stamp-hd-title', '时间戳'));
    band.register('more', this.ipad.morePanelEl(), el('b', 'more-hd-title', '更多'));

    const bar = new EditorBar({
      onTool: (tool) => this.onBarTool(tool),
      onExport: () => { if (!startQuickExport(this.shell, [photo.id])) this.api.exportPhotos([photo.id]); },
    });
    this.bar = bar;
    const cropBtn = bar.toolButton('crop');
    if (cropBtn) this.cropStage.attachTabButton(cropBtn);

    band.setTool('recipe');
    bar.setActive('recipe', band.isCollapsed());
    screen.dataset.collapsed = band.isCollapsed() ? '1' : '0';
    this.updateEditedDots();

    screen.append(nav, stage, band.el(), bar.el());
    this.host.append(screen);
    this.applyLayout(this.ipad.mode(), true);
    syncExportDock(this.shell);
    if (this.engine && this.gpuCanvas) this.engine.attach(this.gpuCanvas);
    this.cropStage.paintTabDot();
    this.cropStage.paintStill(photo.bitmap);
    void this.decor.sync();
  }

  private moreHooksOf(photo: Photo) {
    return {
      onFrame: () => this.selectBandTool('frame', true),
      onStamp: () => this.selectBandTool('stamp', true),
      onCurves: () => this.openCurves(),
      onExport: () => { if (!openExportOptions(this.shell, [photo.id])) this.api.exportPhotos([photo.id]); },
      onCalib: () => showCalibSheet(this.shell, this.api),
    };
  }

  private applyLayout(mode: LayoutMode, initial = false): void {
    const wasIpad = isIpadLayout(this.layoutMode);
    const nowIpad = isIpadLayout(mode);
    this.layoutMode = mode;
    syncEditorBarOrder(this.screen, this.band?.el() ?? null, this.bar?.el() ?? null, mode === 'phone-landscape');
    if (initial || wasIpad !== nowIpad) {
      this.histOpen = this.engine !== null && nowIpad ? loadIpadHist() : false;
      if (!nowIpad && this.activeTool === 'more') this.selectBandTool('recipe', false);
    }
    this.syncIpad(mode, this.recipeSub);
    this.adjust?.syncHistBtn();
    if (this.histOpen) this.requestHist();
    else { this.histAgain = false; this.cancelHistRetry(); }
    if (initial) return; // 打开照片时版式已经在挂进 DOM 前定好，舞台照常由 mount 和 ResizeObserver 摆
    const body = this.band?.el().querySelector('.band-body');
    if (body) body.scrollTop = 0;
    this.refitStage();
  }

  private onBarTool(tool: BarTool): void {
    const photo = this.photo;
    if (!photo) return;
    if (tool === 'more') {
      if (this.ipad?.isIpad()) {
        this.ipad.refreshMore(photo);
        this.selectBandTool('more', true);
      } else {
        openMoreSheet(this.shell, photo, this.moreHooksOf(photo));
      }
      return;
    }
    if (this.band?.isCollapsed()) {
      this.expandBand();
    } else if (tool === 'recipe' && this.activeTool === 'recipe') {
      if (this.recipeSub === 'lut') { this.setRecipeSub('recipe'); return; }
      if (this.recipes?.isFineTuning()) { this.recipes.exitFineTune(); return; }
    }
    this.selectBandTool(tool, false);
  }

  private expandBand(): void {
    if (!this.band?.isCollapsed()) return;
    this.band.setCollapsed(false);
    if (this.screen) this.screen.dataset.collapsed = '0';
    syncExportDock(this.shell);
    this.refitStage();
  }

  private selectBandTool(tool: BandTool, expand: boolean): void {
    const prev = this.activeTool;
    if (expand) this.expandBand();
    this.activeTool = tool;
    if (tool === 'crop') { this.stageZoom?.reset(); this.stageZoom?.cancel(); }
    if (tool === 'recipe' && this.recipeSub === 'recipe') this.recipes?.tabOpened();
    else if (tool === 'recipe' && this.recipeSub === 'lut') this.luts?.tabOpened();
    if (tool === 'frame') { this.frames?.tabOpened(); this.decor?.onFrameTabOpened(); }
    else if (prev === 'frame') this.frames?.tabClosed();
    if (tool === 'stamp') { this.stampBand?.refresh(); this.decor?.onFrameTabOpened(); }
    if (tool === 'adjust') this.adjust?.refresh();
    if (tool === 'more' && this.photo) this.ipad?.refreshMore(this.photo);
    this.band?.setTool(tool);
    this.bar?.setActive(tool, Boolean(this.band?.isCollapsed()));
    if (prev === 'crop' || tool === 'crop') { void this.decor?.sync(); this.cropStage?.syncTabMode(); }
    this.syncFloatHistPos();
  }

  private setRecipeSub(sub: 'recipe' | 'lut'): void {
    this.recipeSub = sub;
    if (this.recipes) {
      if (sub === 'lut') this.recipes.exitFineTune();
      this.recipes.panelEl().hidden = sub !== 'recipe';
      this.recipes.headEl().hidden = sub !== 'recipe';
    }
    if (this.luts) {
      this.luts.panelEl().hidden = sub !== 'lut';
      this.luts.headEl().hidden = sub !== 'lut';
      if (sub === 'lut') this.luts.tabOpened();
    }
    this.syncIpad(this.layoutMode, sub);
  }

  private syncIpad(mode: LayoutMode, recipeSub: 'recipe' | 'lut'): void {
    this.ipad?.apply({
      mode, histOpen: this.histOpen, recipeSub, lutActive: this.photo?.lut !== null, band: this.band?.el() ?? null,
      stage: this.stage, histWidget: this.histWidget, recipes: this.recipes, luts: this.luts,
    });
  }

  private toggleBandCollapsed(): void {
    if (!this.band) return;
    const collapsed = this.band.toggleCollapsed();
    if (this.screen) this.screen.dataset.collapsed = collapsed ? '1' : '0';
    this.bar?.setActive(this.activeTool, collapsed);
    syncExportDock(this.shell);
    this.refitStage();
  }

  private refitStage(): void {
    this.fitStageCanvas();
    this.syncFloatHistPos();
    const token = this.token;
    requestAnimationFrame(() => {
      if (token !== this.token) return;
      this.fitStageCanvas();
      this.engine?.redraw();
      void this.decor?.sync();
      this.syncFloatHistPos();
    });
  }

  private fitStageCanvas(redraw = false): void {
    if (this.mode === 'still') return;
    const engineSize = typeof this.engine?.finalSize === 'function' ? this.engine.finalSize() : null;
    const size = engineSize ?? (this.photo?.bitmap ? { w: this.photo.bitmap.width, h: this.photo.bitmap.height } : null);
    if (fitGpuCanvas(this.stage, this.pv, this.gpuCanvas, size, this.boxCache.pv ?? this.boxCache.stage) && redraw) {
      this.engine?.redraw();
    }
  }

  private openCurves(): void {
    if (!this.curves || !this.screen) return;
    openCurvesSheet(this.shell, this.screen, this.curves, () => this.updateEditedDots());
  }

  private toggleHist(): boolean {
    if (!this.engine) return false;
    this.histOpen = !this.histOpen;
    if (isIpadLayout(this.layoutMode)) {
      saveIpadHist(this.histOpen);
      this.ipad?.syncHistUi(this.histOpen);
    }
    if (this.histWidget) this.histWidget.root.hidden = !this.histOpen;
    this.adjust?.syncHistBtn();
    if (this.histOpen) {
      this.syncFloatHistPos();
      this.requestHist();
    } else {
      this.histAgain = false;
      this.cancelHistRetry();
    }
    return this.histOpen;
  }

  private syncFloatHistPos(): void {
    if (isIpadLayout(this.layoutMode) || !this.histOpen || !this.stage || !this.pv || !this.histWidget) return;
    const photo = this.photo;
    const dims = photo ? photoExportSize(photo, this.engine) : null;
    positionFloatHist(this.stage, this.pv, this.histWidget.root, dims ? { w: dims[0], h: dims[1] } : null);
  }

  private setHolding(on: boolean): void {
    this.holding = on;
    this.navCtrl?.setHolding(on);
    this.renderStageLabel();
    this.push();
    void this.decor?.sync();
  }

  private updateEditedDots(): void {
    if (this.photo && this.bar) this.bar.setEdited(editedFlagsOf(this.photo));
    if (this.photo) this.ipad?.refreshMore(this.photo);
    this.navCtrl?.refresh();
  }

  /** 配方那边定下了舞台该用哪张烘焙表。 */
  private useLook(key: string, label: string): void {
    this.recipeKey = key;
    this.lookCaption = label;
    if (this.activeTool !== 'crop') this.setLabel(label);
    this.navCtrl?.refresh();
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

  private push(): void {
    if (!this.engine || !this.photo || this.mode === 'still') return;
    if (!this.dragging) this.fitStageCanvas();
    const knobs = this.holding ? zeroKnobs() : this.photo.knobs;
    const lut = this.holding ? null : this.lutToken;
    if (lut && !this.engine.hasLut(lut)) this.luts?.ensureActive();
    this.engine.setLook({
      // 按住看原图：中性转换、不用 LUT、调整归零。
      recipe: this.mode === 'raw' ? (this.holding ? NEUTRAL_BAKE_KEY : this.recipeKey) : null,
      lut,
      lutStrength: 1,
      curve: previewCurve(this.photo.curve, this.holding),
      adjust: toAdjust(knobs),
    });
    this.requestHist(false, false);
    this.decor?.onLookChanged(this.dragging);
    this.hdr.sync();
  }

  private setLabel(text: string): void {
    this.caption = text;
    this.renderStageLabel();
  }

  private renderStageLabel(): void {
    if (!this.stageLabel) return;
    this.stageLabel.replaceChildren();
    const noGpu = !this.engine && !this.holding && this.caption === NO_GPU_TITLE;
    const rawOnly = noGpu && !this.photo?.jpg && !this.photo?.bitmap; // 舞台显示「预览不了」时，这里说同一句话
    this.stageLabel.classList.toggle('nogpu', noGpu);
    const text = this.holding ? '原图' : rawOnly ? NO_PREVIEW_TEXT : noGpu ? this.caption : this.lutLabel ? `LUT · ${this.lutLabel}${this.caption ? ` · ${this.caption}` : ''}` : this.caption;
    if (text) this.stageLabel.append(el('span', 'tag neutral', text));
    if (noGpu) this.stageLabel.append(el('span', 'nogpu-note', rawOnly ? NO_GPU_RAW_DETAIL : NO_GPU_DETAIL));
    this.hdr.paintLabel(this.stageLabel, this.stage);
  }

  private enterDrag(layout = true): void {
    if (this.dragging) return;
    this.dragging = true;
    if (layout) this.shell.dataset.drag = '1';
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
      if (token !== this.token) return;
      this.fitStageCanvas();
      this.engine?.redraw();
      this.decor?.onLookChanged(false);
      this.hdr.sync();
    });
  }

  private requestHist(retry = false, fit = !retry): void {
    if (!this.engine || this.mode === 'still') return;
    if (!retry) {
      if (fit && !this.dragging) this.fitStageCanvas(true);
      this.histMisses = 0;
      this.cancelHistRetry();
    }
    if (!this.histOpen) {
      this.histAgain = false;
      this.cancelHistRetry();
      return;
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
        if (token !== this.token || !this.histOpen) return;
        this.histMisses = drawn ? 0 : this.histMisses + 1;
        const engine = this.engine;
        if (drawn && !this.histAgain && !(engine && engine.contentVersion !== this.histDrawn)) return;
        this.cancelHistRetry();
        const again = (): void => {
          this.histFrame = 0;
          this.histTimer = 0;
          if (token === this.token && this.histOpen) this.requestHist(true);
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

  private async countHist(token: number): Promise<boolean> {
    const engine = this.engine;
    if (!engine || token !== this.token || !this.histOpen || engine.pending) return false;
    const data = await engine.histogram();
    if (!data || token !== this.token || !this.histOpen) return false;
    this.histDrawn = data.version;
    drawHistCanvas(this.histWidget, data.counts, data.samples, data.version);
    return true;
  }

  private restLabelText(): string {
    return this.mode === 'raw' ? this.lookCaption || '中性转换' : this.mode === 'jpg' ? '相机 JPG' : this.lookCaption || (this.photo?.jpg ? '相机 JPG' : '只有 RAW');
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
}
