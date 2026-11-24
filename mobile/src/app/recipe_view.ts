// 编辑器 128pt 单带里的配方视图：配方卡横条（带首「配方 | LUT」切换）、点击已选卡进入微调子页、顶部筛选与状态说明。
// 状态照设计稿：06c 没校准（中性转换，可以选）、06d 没有相机配方文件（卡片置灰）、06e 只有 JPG。

import type { Engine, Geometry } from '../../../frontend/src/index';
import { unitOfLens, type Unit } from '../lib/calib';
import { mergeMeta } from '../lib/exif';
import { NEUTRAL_BAKE_KEY } from '../lib/neutral';
import {
  adoptedSelectionOf,
  cameraRecipeErrorOf,
  cardTag,
  DEFAULT_CARD,
  effective,
  KNOB_NAMES,
  namesOnly,
  recipeUnitOf,
  shelf,
} from '../lib/recipes';
import type { AdoptedSelection, CameraRecipeError, Card, Control, Shelf, Shelves } from '../lib/recipes';
import { recipeSwitch } from '../lib/recipe_memory';
import type { UnitState } from './book';
import { isCatalogMismatch, openMismatchDatSheet, shouldPromptMismatch } from './camfiles_ui';
import { button, el, icon } from './dom';
import { exportPaused } from './export_pause';
import { CardBakeController, lookCache, renderCardThumb } from './looks';
import { ensureCameraRecipe, needsCameraRecipe, onCameraRecipe, type AppApi, type Photo } from './model';
import {
  bitmapId,
  buildIpadRecipeMeta,
  buildKnobRow,
  centerStripCard,
  makeRecipeFilterTile,
  makeRecipeLutSwitch,
  makeRecipeStatusTile,
  openAskDatSheet,
  openJpgInfoSheet,
  openRecipeFilterSheet,
  openRecipeStatusSheet,
  recipeStatusSpec,
  shelfCount,
  type AskSheetHandle,
} from './recipe_band';

const CHECK = 'M5 12.5l4.5 4.5L19 7.5';
const GEOMETRY: Geometry = { straighten: 0, rotate90: 0, flip: false, vertical: 0, horizontal: 0, distortion: 0, box: null };
const ADJUST = {
  exposure_ev: 0,
  wb: { warm: 0, tint: 0 },
  contrast: 0,
  highlight: 0,
  shadow: 0,
  black_point: 0,
  saturation: 0,
  vibrance: 0,
  vignette: 0,
};
const MINE_EMPTY = '电脑上还没有我的配方。';
const NEUTRAL_LABEL = '中性转换';
const PREDICTED_LABEL = '电脑预测';

export interface StageHooks {
  /** 照片现在是显卡上的 RAW、显卡上的相机 JPG，还是静态图。 */
  mode(): 'raw' | 'jpg' | 'still';
  /** 换成这张烘焙表，并写舞台上的标签。 */
  look(key: string, label: string): void;
  dragging(): boolean;
  /** 「配方」页现在开着没有。 */
  recipeTab(): boolean;
  /** 几何与裁切框（给配方卡小图裁剪预览）。 */
  geometry(): Promise<{ geometry: Geometry; cropped: boolean }>;
  /** 点带首「LUT」切到 LUT 子视图。 */
  openLut?(): void;
  /** 当前照片是否选中了 LUT（控制「LUT」键蓝点）。 */
  lutActive?(): boolean;
  /** 配方微调拖动时的数值胶囊。 */
  onScrub?(text: string | null): void;
  /** 配方改动后刷新底栏圆点。 */
  onEditedChanged?(): void;
}

interface Slot {
  card: Card;
  node: HTMLButtonElement;
  thumb: HTMLElement;
  nameEl: HTMLElement;
  sub: HTMLElement;
}

interface Scene {
  raw: boolean;
  gpu: boolean;
  dat: boolean;
  photoUnit: Unit | null;
  unit: Unit;
  iso: number | null;
  state: UnitState;
  list: Shelves | null;
  generation: number | null;
  adopted: AdoptedSelection | null;
  camError: CameraRecipeError | null;
}

export class RecipeView {
  private token = 0;
  private which: Shelf = 'all';
  private slots: Slot[] = [];
  private stripKeys: string[] = [];
  private fineTune = false;
  private activeKnob = '';
  private lastScrolledCardId: string | null = null;
  private scrollRaf = 0;
  private readonly head = el('div', 'recipe-head band-recipe-hd');
  private readonly stripWrap = el('div', 'recipe-strip-wrap');
  private readonly filterPin = el('div', 'recipe-filter-pin');
  private readonly strip = el('div', 'strip');
  private readonly fineBox = el('div', 'recipe-fine');
  private readonly datErrBar = button('dat-err', '');
  private readonly panel = el('div', 'panel recipe');
  private ipadMode = false;
  private modeSwitchEl: HTMLElement | null = null;
  private rawKey: string | null = null;
  private neutralBitmap: ImageBitmap | null = null;
  private shown = '';
  private wanted = '';
  private readonly bakeCtrl = new CardBakeController<Card>();
  private asked = false;
  private askHandle: AskSheetHandle | null = null;
  private readonly api: AppApi;
  private readonly engine: Engine | null;
  private readonly photo: Photo;
  private readonly hooks: StageHooks;
  private readonly off: () => void;
  private readonly offCam: () => void;

  constructor(api: AppApi, engine: Engine | null, photo: Photo, hooks: StageHooks) {
    this.api = api;
    this.engine = engine;
    this.photo = photo;
    this.hooks = hooks;
    this.strip.dataset.recipes = '1';
    this.panel.dataset.panel = 'recipe';
    this.filterPin.hidden = true;
    this.stripWrap.append(this.filterPin, this.strip);
    this.panel.append(this.makeModeSwitch());
    this.fineBox.hidden = true;
    this.datErrBar.hidden = true;
    this.datErrBar.addEventListener('click', () => {
      const scene = this.scene();
      if (scene.dat) this.openStatusSheet(scene);
      else this.ask();
    });
    this.panel.append(this.stripWrap, this.fineBox, this.datErrBar);
    this.off = api.book().onChange(() => this.refresh());
    this.offCam = onCameraRecipe((id) => {
      if (id === this.photo.id) this.refresh();
    });
    void ensureCameraRecipe(this.photo, api.book());
    this.build();
  }

  headEl(): HTMLElement { return this.head; }
  stripEl(): HTMLElement { return this.strip; }
  stripWrapEl(): HTMLElement { return this.stripWrap; }
  panelEl(): HTMLElement { return this.panel; }
  isFineTuning(): boolean { return this.fineTune; }

  setIpadMode(on: boolean): void {
    if (this.ipadMode === on) return;
    this.ipadMode = on;
    if (on) this.fineTune = false;
    this.modeSwitchEl?.remove();
    this.modeSwitchEl = on ? null : this.makeModeSwitch();
    if (this.modeSwitchEl) this.panel.append(this.modeSwitchEl);
    this.stripKeys = [];
    this.build();
  }

  openFilter(): void {
    const scene = this.scene();
    const shell = this.shellEl();
    if (!shell || !scene.raw || !scene.dat || !scene.list) return;
    openRecipeFilterSheet(shell, scene.list, this.which, (which) => {
      this.which = which;
      this.build();
    });
  }

  exitFineTune(): void {
    if (!this.fineTune) return;
    this.fineTune = false;
    this.syncSubView(this.scene());
  }

  syncLutDot(): void {
    const lutBtn = this.modeSwitchEl?.querySelector('[data-mode="lut"]');
    if (lutBtn) lutBtn.classList.toggle('dot', Boolean(this.hooks.lutActive?.()));
  }

  destroy(): void {
    this.token += 1;
    this.bakeCtrl.destroy();
    if (this.scrollRaf) {
      cancelAnimationFrame(this.scrollRaf);
      this.scrollRaf = 0;
    }
    this.off();
    this.offCam();
    this.closeAsk();
  }

  /** 这张 RAW 已经在显卡里：套上该用的烘焙表，开始画卡片。 */
  start(rawKey: string): void {
    this.rawKey = rawKey;
    this.refresh();
  }

  /** 中性小图好了（每个版本做一次）。 */
  neutral(bitmap: ImageBitmap | null): void {
    this.neutralBitmap = bitmap;
    this.paintThumbs();
  }

  /** 「配方」页打开时：没有相机配方文件就提示一次（06d）。 */
  tabOpened(): void {
    if (this.photo.dng && !this.api.book().dat && !this.asked) this.ask();
  }

  /** 舞台现在该用的那张表的键（长按看原图时由编辑器换成中性）。 */
  current(): string {
    return this.shown || NEUTRAL_BAKE_KEY;
  }

  private shellEl(): HTMLElement | null {
    const host = this.panel.closest('.app');
    return host instanceof HTMLElement ? host : null;
  }

  private scene(): Scene {
    const book = this.api.book();
    const rawDat = book.dat;
    const dat = book.usableDatFor(this.photo);
    const adopted = adoptedSelectionOf(this.photo, dat);
    const camError = cameraRecipeErrorOf(this.photo, rawDat);
    const meta = mergeMeta(this.photo.jpgMeta, this.photo.dngMeta);
    const photoUnit = unitOfLens(meta.lens);
    const unit = recipeUnitOf(this.photo.choice, adopted, photoUnit);
    return {
      raw: this.photo.dng !== null,
      gpu: this.engine !== null,
      dat: dat !== null,
      photoUnit,
      unit,
      iso: meta.iso,
      state: book.unitStateFor(this.photo, unit),
      list: book.shelvesFor(this.photo, unit),
      generation: dat?.generation ?? null,
      adopted,
      camError,
    };
  }

  private cardsOf(scene: Scene): Card[] {
    if (!scene.list) return namesOnly();
    return shelf(scene.list, this.which);
  }

  private levelsOf(card: Card, scene: Scene): Record<string, number> | undefined {
    const choice = this.photo.choice;
    if (choice?.id === card.id) return this.photo.levels.get(card.id);
    if (!choice && scene.adopted?.choice.id === card.id) return scene.adopted.levels;
    if (!choice && !scene.adopted && card.id === DEFAULT_CARD) return undefined;
    return this.photo.memory.get(card.id);
  }

  private chosen(scene: Scene): Card | null {
    const choice = this.photo.choice;
    if (!choice || !scene.list || choice.unit !== scene.unit) return null;
    return shelf(scene.list, 'all').find((card) => card.id === choice.id) ?? null;
  }

  private adoptedCard(scene: Scene): Card | null {
    const adopted = scene.adopted;
    if (!adopted || !scene.list || adopted.choice.unit !== scene.unit) return null;
    return shelf(scene.list, 'all').find((card) => card.id === adopted.choice.id) ?? null;
  }

  private activeCard(scene: Scene): Card | null {
    return this.chosen(scene) ?? this.adoptedCard(scene);
  }

  private target(scene: Scene): Card | null {
    const active = this.activeCard(scene);
    if (active) return active;
    return scene.list?.builtin.find((card) => card.id === DEFAULT_CARD) ?? null;
  }

  private orderedControls(card: Card): Control[] {
    const amount = card.controls.filter((c) => c.name === 'amount');
    const rest = card.controls.filter((c) => c.name !== 'amount');
    return [...amount, ...rest];
  }

  private build(): void {
    const scene = this.scene();
    this.buildStrip(scene);
    this.syncSubView(scene);
  }

  refresh(): void {
    if (!this.panel.isConnected && this.slots.length) return;
    const token = ++this.token;
    void ensureCameraRecipe(this.photo, this.api.book());
    const scene = this.scene();
    this.buildStrip(scene);
    this.syncSubView(scene);
    this.syncAsk();
    if (isCatalogMismatch(scene.camError, this.photo.choice) && shouldPromptMismatch(this.api.book().dat)) this.ask(true);
    this.applyLook(scene);
    void this.renderCards(scene, token);
  }

  private handleStatusAction(scene: Scene, action: 'jpg-info' | 'ask-dat' | 'status-sheet'): void {
    if (action === 'jpg-info') {
      const shell = this.shellEl();
      if (shell) openJpgInfoSheet(shell, Boolean(this.photo.diptych));
    } else if (action === 'ask-dat') {
      this.ask();
    } else {
      this.openStatusSheet(scene);
    }
  }

  private syncSubView(scene: Scene): void {
    const target = this.target(scene);
    const canFine = Boolean(scene.raw && scene.dat && scene.state.state === 'ready' && target && target.controls.length);
    if (this.fineTune && !canFine) this.fineTune = false;
    if (!this.ipadMode) {
      this.stripWrap.hidden = this.fineTune;
      // 切换键现在在面板上，不再跟着横条一起藏。微调时藏起来，和 04e8b92 一样不挡住旋钮。
      if (this.modeSwitchEl) this.modeSwitchEl.hidden = this.fineTune;
    }
    this.fineBox.hidden = !this.ipadMode && !this.fineTune;
    const datError = this.api.book().datError;
    if (datError && scene.raw) {
      this.datErrBar.hidden = false;
      this.datErrBar.textContent = scene.dat && target ? `已保留「${target.name}」· ${datError}` : datError;
    } else {
      this.datErrBar.hidden = true;
      this.datErrBar.textContent = '';
    }
    this.syncFilterPin(scene);
    this.buildHead(scene);
    this.buildFineBox(scene);
  }

  private syncFilterPin(scene: Scene): void {
    this.filterPin.replaceChildren();
    if (this.ipadMode) {
      this.filterPin.hidden = true;
      return;
    }
    if (scene.raw && scene.dat && scene.list) {
      this.filterPin.append(makeRecipeFilterTile(this.which, shelfCount(scene.list, this.which), () => this.openFilter()));
    }
    const statusSpec = recipeStatusSpec(scene, Boolean(this.photo.diptych), Boolean(this.api.book().datError));
    if (statusSpec) {
      this.filterPin.append(makeRecipeStatusTile(statusSpec, () => this.handleStatusAction(scene, statusSpec.action)));
    }
    this.filterPin.hidden = this.filterPin.childElementCount === 0;
  }

  private makeModeSwitch(): HTMLElement {
    const sw = makeRecipeLutSwitch('recipe', Boolean(this.hooks.lutActive?.()), (mode) => {
      if (mode === 'lut') this.hooks.openLut?.();
    });
    this.modeSwitchEl = sw;
    return sw;
  }

  private buildStrip(scene: Scene): void {
    const cards = this.cardsOf(scene);
    const locked = scene.raw && !scene.dat;
    const picked = this.activeCard(scene);
    this.strip.dataset.state = !scene.raw
      ? 'jpg-only'
      : locked
        ? 'no-recipe-file'
        : scene.state.state === 'ready'
          ? 'calibrated'
          : 'uncalibrated';
    const isEmpty = Boolean(scene.list && this.which === 'mine' && !cards.length);
    if (isEmpty) {
      this.bakeCtrl.clearObserver();
      this.strip.replaceChildren();
      this.slots = [];
      this.stripKeys = ['__empty__'];
      this.lastScrolledCardId = null;
      this.strip.append(el('p', 'empty faint', MINE_EMPTY));
      return;
    }

    const nextKeys = cards.map((card) => card.id);
    const sameStrip =
      nextKeys.length === this.stripKeys.length &&
      nextKeys.every((k, i) => k === this.stripKeys[i]) &&
      this.slots.length === cards.length;

    if (sameStrip) {
      this.syncLutDot();
      for (let i = 0; i < cards.length; i += 1) {
        const card = cards[i];
        const slot = this.slots[i];
        slot.card = card;
        slot.nameEl.textContent = card.name;
        const usable = scene.raw && scene.dat && card.controls.length > 0;
        slot.node.classList.toggle('lockd', !usable);
        slot.node.disabled = !scene.raw;
        this.syncMark(slot.node, slot.thumb, Boolean(picked && picked.id === card.id));
      }
      if (this.strip.isConnected && !this.bakeCtrl.hasVisible()) this.bakeCtrl.syncVisible(this.slots.map((s) => s.node), this.strip);
      this.paintThumbs();
      this.scheduleScrollActive(picked?.id ?? null);
      return;
    }

    this.strip.replaceChildren();
    this.slots = [];
    this.stripKeys = nextKeys;
    this.lastScrolledCardId = null;
    for (let i = 0; i < cards.length; i += 1) {
      const card = cards[i];
      const node = button('rc', '');
      node.dataset.card = card.id;
      const thumb = el('div', 'th');
      const nameEl = el('span', 'nm', card.name);
      const sub = el('span', 'sub');
      node.append(thumb, nameEl, sub);
      const usable = scene.raw && scene.dat && card.controls.length > 0;
      if (!usable) node.classList.add('lockd');
      if (!scene.raw) node.disabled = true;
      this.syncMark(node, thumb, Boolean(picked && picked.id === card.id));
      const idx = i;
      node.addEventListener('click', () => {
        const current = this.slots[idx]?.card;
        if (current) this.pick(current);
      });
      this.strip.append(node);
      this.slots.push({ card, node, thumb, nameEl, sub });
    }
    this.bakeCtrl.observe(this.slots.map((s) => s.node), this.strip);
    this.bakeCtrl.attachInput(this.panel.closest('.screen.editor') ?? this.panel, this.strip);
    this.paintThumbs();
    this.scheduleScrollActive(picked?.id ?? null);
  }

  private scheduleScrollActive(cardId: string | null): void {
    if (!cardId || this.fineTune || this.lastScrolledCardId === cardId) return;
    if (this.scrollRaf) cancelAnimationFrame(this.scrollRaf);
    const token = this.token;
    this.scrollRaf = requestAnimationFrame(() => {
      this.scrollRaf = 0;
      if (token === this.token && !this.fineTune && centerStripCard(this.strip, cardId)) {
        this.lastScrolledCardId = cardId;
      }
    });
  }

  private syncMark(node: HTMLElement, thumb: HTMLElement, on: boolean): void {
    node.classList.toggle('on', on);
    const existing = thumb.querySelector('.ckk');
    if (on && !existing) {
      const tick = el('span', 'ckk');
      tick.append(icon(CHECK));
      thumb.append(tick);
    } else if (!on && existing) {
      existing.remove();
    }
  }

  private paintThumbs(): void {
    const scene = this.scene();
    const cropped = this.photo.crop !== null || this.photo.geometry.straighten !== 0;
    for (const slot of this.slots) {
      const { card, thumb, sub } = slot;
      let bitmap: ImageBitmap | null = null;
      let text = '';
      let source = '';
      if (!scene.raw) {
        bitmap = this.photo.bitmap;
        source = '相机 JPG';
      } else if (!scene.gpu) {
        bitmap = this.photo.bitmap;
        source = text = this.photo.jpg ? '相机 JPG' : this.photo.bitmap ? '内嵌预览' : '只有 RAW';
      } else if (!scene.dat) {
        const useJpg = this.hooks.mode() === 'jpg' && this.photo.bitmap !== null;
        bitmap = useJpg ? this.photo.bitmap : this.photo.neutral?.bitmap === this.neutralBitmap ? this.neutralBitmap : null;
        source = useJpg ? '相机 JPG' : NEUTRAL_LABEL;
      } else if (scene.state.state !== 'ready') {
        bitmap = this.photo.neutral?.bitmap === this.neutralBitmap ? this.neutralBitmap : null;
        source = text = NEUTRAL_LABEL;
      } else {
        const key = this.api.book().bakeKey(card, scene.unit, scene.iso, this.levelsOf(card, scene));
        const look = key ? lookCache.get(this.photo, key) : null;
        if (look) {
          bitmap = look;
          source = PREDICTED_LABEL;
        }
        text = card.desc ?? '';
      }
      sub.textContent = text;
      thumb.dataset.cropped = cropped && scene.raw && scene.gpu ? '1' : '0';
      const want = bitmap ? `${source}:${bitmapId(bitmap)}` : '';
      if (thumb.dataset.drawn === want) continue;
      thumb.querySelector('canvas')?.remove();
      thumb.dataset.drawn = want;
      thumb.dataset.source = bitmap ? source : '';
      if (!bitmap) continue;
      const mini = document.createElement('canvas');
      mini.width = bitmap.width;
      mini.height = bitmap.height;
      mini.getContext('2d')?.drawImage(bitmap, 0, 0);
      thumb.prepend(mini);
    }
  }

  private buildHead(scene: Scene): void {
    this.head.replaceChildren();
    if (!scene.raw) {
      this.head.append(el('span', 'rname dim', this.photo.diptych ? '半格双联' : '相机 JPG'));
      return;
    }
    if (!scene.dat) {
      this.head.append(el('span', 'rname dim', this.hooks.mode() === 'jpg' ? '相机 JPG' : NEUTRAL_LABEL));
      return;
    }
    const target = this.target(scene);
    this.head.append(el('b', 'rname', target?.name ?? ''));
    if (target) this.head.append(el('span', 'tag neutral-tag', cardTag(target)));
  }

  private openStatusSheet(scene: Scene): void {
    const shell = this.shellEl();
    if (!shell) return;
    openRecipeStatusSheet(
      shell,
      this.api.book(),
      scene,
      () => this.api.pickDat(),
      () => this.api.pickCalib(),
    );
  }

  private buildFineBox(scene: Scene): void {
    this.fineBox.replaceChildren();
    const target = this.target(scene);
    if (this.ipadMode) {
      this.fineBox.append(buildIpadRecipeMeta(scene, target?.name ?? '', target ? cardTag(target) : '', Boolean(this.photo.diptych), this.api.book().datError, (a) => this.handleStatusAction(scene, a), this.hooks.mode() === 'jpg' ? '相机 JPG' : NEUTRAL_LABEL));
    }
    if (!scene.raw || !scene.dat || scene.state.state !== 'ready' || !target || !target.controls.length) return;
    const controls = this.orderedControls(target);
    if (!controls.some((c) => c.name === this.activeKnob)) this.activeKnob = controls[0].name;
    const knobsBox = this.knobs(scene, target, controls);
    if (this.ipadMode) {
      this.fineBox.append(knobsBox);
      return;
    }
    const topRow = el('div', 'fine-top');
    const back = button('chip fine-back', '‹ 配方');
    back.dataset.act = 'recipe-back';
    back.addEventListener('click', () => this.exitFineTune());
    const knobChips = el('div', 'chips knob-chips');
    for (const control of controls) {
      const chip = button(control.name === this.activeKnob ? 'chip on' : 'chip', KNOB_NAMES[control.name] ?? control.label);
      chip.dataset.knobChip = control.name;
      chip.addEventListener('click', () => {
        this.activeKnob = control.name;
        for (const item of knobChips.querySelectorAll('.chip')) item.classList.toggle('on', item === chip);
        for (const row of knobsBox.querySelectorAll<HTMLElement>('.krow')) row.hidden = row.dataset.knob !== this.activeKnob;
      });
      knobChips.append(chip);
    }
    topRow.append(back, knobChips);
    this.fineBox.append(topRow, knobsBox);
  }

  private knobs(scene: Scene, card: Card, controls: Control[]): HTMLElement {
    const box = el('div', 'knobs');
    box.dataset.card = card.id;
    const saved = this.levelsOf(card, scene);
    for (const control of controls) {
      const row = buildKnobRow(
        control,
        scene.generation,
        saved,
        (level) => this.setLevel(card, control.name, level),
        (text) => this.hooks.onScrub?.(text),
      );
      row.hidden = !this.ipadMode && control.name !== this.activeKnob;
      box.append(row);
    }
    return box;
  }

  private setLevel(card: Card, name: string, level: number): void {
    const scene = this.scene();
    const isAdopted = !this.photo.choice && scene.adopted?.choice.id === card.id;
    const base =
      this.photo.choice?.id === card.id
        ? (this.photo.levels.get(card.id) ?? {})
        : isAdopted && scene.adopted
          ? scene.adopted.levels
          : {};
    const next: Record<string, number> = { ...base };
    const control = card.controls.find((c) => c.name === name);
    if ((this.photo.choice?.id === card.id || isAdopted) && control && effective(control, next) === level) return;
    next[name] = level;
    this.photo.levels = new Map([[card.id, next]]);
    this.photo.rev += 1;
    if (this.photo.choice?.id !== card.id) {
      this.photo.choice = { id: card.id, unit: scene.unit };
      this.photo.recipeOff = false;
      for (const slot of this.slots) {
        this.syncMark(slot.node, slot.thumb, slot.card.id === card.id);
      }
    }
    const nextScene = this.scene();
    this.buildHead(nextScene);
    this.applyLook(nextScene, true);
    this.hooks.onEditedChanged?.();
    void this.renderCards(nextScene, this.token);
  }

  /** 点一张卡：切配方；再点已选卡则进入 128pt 微调子页。 */
  private pick(card: Card): void {
    const scene = this.scene();
    if (!scene.raw) return;
    if (isCatalogMismatch(scene.camError, this.photo.choice)) {
      this.ask(true);
      return;
    }
    if (!scene.dat) {
      this.ask();
      return;
    }
    if (!card.controls.length) return;
    const choice = this.photo.choice;
    const adopted = scene.adopted;
    const currentId = choice?.id ?? adopted?.choice.id ?? DEFAULT_CARD;
    const currentCard = scene.list ? (shelf(scene.list, 'all').find((item) => item.id === currentId) ?? null) : null;
    const currentLevels = choice ? this.photo.levels.get(currentId) : adopted ? adopted.levels : undefined;
    const switched = recipeSwitch(
      this.photo.memory,
      { id: currentId, card: currentCard, levels: currentLevels, chosen: choice !== null || adopted !== null },
      card,
    );
    if (switched.same) {
      if (!this.ipadMode && scene.state.state === 'ready' && card.controls.length > 0) {
        this.fineTune = true;
        this.activeKnob = this.orderedControls(card)[0]?.name ?? '';
        this.syncSubView(scene);
      }
      return;
    }
    this.photo.memory = switched.memory;
    this.photo.levels = new Map([[card.id, switched.levels]]);
    this.photo.choice = { id: card.id, unit: scene.unit };
    this.photo.recipeOff = false;
    this.photo.rev += 1;
    for (const slot of this.slots) {
      this.syncMark(slot.node, slot.thumb, slot.card.id === card.id);
    }
    const nextScene = this.scene();
    this.syncSubView(nextScene);
    this.applyLook(nextScene, true);
    this.hooks.onEditedChanged?.();
  }

  private applyLook(scene: Scene, userTap = false): void {
    if (!scene.raw || !this.engine || this.hooks.mode() !== 'raw') return;
    const target = this.target(scene);
    const book = this.api.book();
    const mismatch = isCatalogMismatch(scene.camError, this.photo.choice);
    const ready = scene.dat && scene.state.state === 'ready' && target !== null && !needsCameraRecipe(this.photo, book.dat) && !mismatch;
    if (!target || !ready) {
      this.wanted = NEUTRAL_BAKE_KEY;
      this.bakeCtrl.clearStage();
      this.show(NEUTRAL_BAKE_KEY, NEUTRAL_LABEL);
      return;
    }
    const levels = this.levelsOf(target, scene);
    const key = book.bakeKey(target, scene.unit, scene.iso, levels);
    if (key && book.ready(key)) {
      this.wanted = key;
      this.bakeCtrl.clearStage();
      this.show(key, PREDICTED_LABEL);
      return;
    }
    if (!this.shown) this.show(NEUTRAL_BAKE_KEY, NEUTRAL_LABEL);
    const want = `${target.id}|${JSON.stringify(levels ?? null)}|${scene.unit}|${scene.iso}`;
    this.wanted = want;
    this.bakeCtrl.beginStage(want, target.id, userTap);
    book.bake(target, scene.unit, scene.iso, levels).then(
      (made) => {
        if (this.wanted === want && this.panel.isConnected) {
          this.wanted = made;
          this.show(made, PREDICTED_LABEL);
        }
        this.bakeCtrl.endStage(want, target.id);
      },
      () => this.bakeCtrl.endStage(want, target.id),
    );
  }

  private show(key: string, label: string): void {
    const book = this.api.book();
    const gpu = key === NEUTRAL_BAKE_KEY ? key : book.gpuKey(key);
    if (!gpu) return;
    this.shown = key;
    book.pin(key);
    this.panel.closest('.app')?.setAttribute('data-recipe-key', key);
    this.hooks.look(gpu, label);
  }

  private async renderCards(scene: Scene, token: number): Promise<void> {
    this.bakeCtrl.cancel();
    const engine = this.engine;
    const source = this.rawKey;
    const book = this.api.book();
    if (!engine || !source || !scene.raw || !scene.dat || !scene.list || scene.state.state !== 'ready' || needsCameraRecipe(this.photo, book.dat)) return;
    const photo = this.photo;
    const version = photo.version;
    const session = this.api.session();
    const alive = () => token === this.token && this.api.photo(photo.id) === photo && photo.version === version && this.api.session() === session;
    const geomPromise = this.hooks.geometry().then((info) => info.geometry, () => GEOMETRY);
    const order = [...this.cardsOf(scene), ...shelf(scene.list, 'all')];
    const seen = new Set<string>();
    const jobs: Array<{ id: string; item: Card }> = [];
    for (const card of order) {
      if (seen.has(card.id) || !card.controls.length) continue;
      seen.add(card.id);
      const known = book.bakeKey(card, scene.unit, scene.iso, this.levelsOf(card, scene));
      if (known && lookCache.get(photo, known)) continue;
      jobs.push({ id: card.id, item: card });
    }
    if (!jobs.length) return;
    this.bakeCtrl.attachInput(this.panel.closest('.screen.editor') ?? this.panel, this.strip);
    if (this.strip.isConnected && !this.bakeCtrl.hasVisible()) this.bakeCtrl.syncVisible(this.slots.map((s) => s.node), this.strip);
    const isBusy = () => this.hooks.dragging() || exportPaused();
    this.bakeCtrl.start(jobs, {
      isAlive: alive,
      isBusy,
      isDeferred: (id) => this.bakeCtrl.isStageBaking(id),
      isPriority: (id) => id === this.target(scene)?.id,
      run: (card) => {
        const levels = this.levelsOf(card, scene);
        return renderCardThumb({
          engine, source, photo, version, cardId: card.id, geometry: () => geomPromise, adjust: ADJUST,
          alive, isBusy, bakeCtrl: this.bakeCtrl,
          bakeKey: () => book.bakeKey(card, scene.unit, scene.iso, levels),
          gpuKey: (k) => book.gpuKey(k),
          bake: () => book.bake(card, scene.unit, scene.iso, levels),
          onError: (err) => {
            const slot = this.slots.find((s) => s.card.id === card.id);
            if (!slot) return;
            slot.sub.textContent = '无法生成电脑预测';
            slot.node.classList.add('lockd');
            slot.node.title = err instanceof Error ? err.message : String(err);
          },
          onPainted: () => this.paintThumbs(),
        });
      },
    });
  }

  private syncAsk(): void {
    this.askHandle?.sync(this.api.book());
  }

  private ask(mismatch = false): void {
    if (!mismatch) this.asked = true;
    const host = this.shellEl();
    if (!host) return;
    this.closeAsk();
    this.askHandle = mismatch
      ? openMismatchDatSheet(host, this.api.book(), Boolean(this.photo.jpg), () => this.api.pickDat())
      : openAskDatSheet(host, this.api.book(), () => this.api.pickDat(), Boolean(this.photo.jpg));
  }

  private closeAsk(): void {
    this.askHandle?.close();
    this.askHandle = null;
  }
}
