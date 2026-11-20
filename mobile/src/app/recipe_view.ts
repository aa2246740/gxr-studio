// 编辑器 128pt 单带里的配方视图：配方卡横条（带首「配方 | LUT」切换）、点击已选卡进入微调子页、顶部筛选与状态说明。
// 状态照设计稿：06c 没校准（中性转换，可以选）、06d 没有相机配方文件（卡片置灰）、06e 只有 JPG。

import type { Engine, Geometry } from '../../../frontend/src/index';
import { crossed, unitOfLens, type Unit } from '../lib/calib';
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
  shelves,
} from '../lib/recipes';
import type { AdoptedSelection, CameraRecipeError, Card, Control, Shelf, Shelves } from '../lib/recipes';
import { recipeSwitch } from '../lib/recipe_memory';
import type { UnitState } from './book';
import { button, el, icon } from './dom';
import { exportPaused } from './export_pause';
import { lookCache } from './looks';
import { ensureCameraRecipe, needsCameraRecipe, onCameraRecipe, type AppApi, type Photo } from './model';
import {
  bitmapId,
  buildKnobRow,
  centerStripCard,
  makeRecipeFilterTile,
  makeRecipeLutSwitch,
  openAskDatSheet,
  openJpgInfoSheet,
  openRecipeFilterSheet,
  openRecipeStatusSheet,
  shelfCount,
  type AskSheetHandle,
} from './recipe_band';

const CHECK = 'M5 12.5l4.5 4.5L19 7.5';
const GEOMETRY: Geometry = { straighten: 0, rotate90: 0, flip: false, vertical: 0, horizontal: 0, distortion: 0, box: null };
const ADJUST = {
  exposure_ev: 0, wb: { warm: 0, tint: 0 }, contrast: 0, highlight: 0, shadow: 0, black_point: 0, saturation: 0, vibrance: 0, vignette: 0,
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
  private modeSwitchEl: HTMLElement | null = null;
  private rawKey: string | null = null;
  private neutralBitmap: ImageBitmap | null = null;
  private shown = '';
  private wanted = '';
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

  headEl(): HTMLElement {
    return this.head;
  }

  stripEl(): HTMLElement {
    return this.strip;
  }

  panelEl(): HTMLElement {
    return this.panel;
  }

  isFineTuning(): boolean {
    return this.fineTune;
  }

  exitFineTune(): void {
    if (!this.fineTune) return;
    this.fineTune = false;
    const scene = this.scene();
    this.syncSubView(scene);
  }

  syncLutDot(): void {
    const lutBtn = this.modeSwitchEl?.querySelector('[data-mode="lut"]');
    if (lutBtn) lutBtn.classList.toggle('dot', Boolean(this.hooks.lutActive?.()));
  }

  destroy(): void {
    this.token += 1;
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
    const dat = book.dat;
    const adopted = adoptedSelectionOf(this.photo, dat);
    const camError = cameraRecipeErrorOf(this.photo, dat);
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
      state: book.unitState(unit),
      list: dat ? shelves(dat.listing, unit) : null,
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
    this.applyLook(scene);
    void this.renderCards(scene, token);
  }

  private syncSubView(scene: Scene): void {
    const target = this.target(scene);
    const canFine = Boolean(scene.raw && scene.dat && scene.state.state === 'ready' && target && target.controls.length);
    if (this.fineTune && !canFine) this.fineTune = false;
    this.stripWrap.hidden = this.fineTune;
    this.fineBox.hidden = !this.fineTune;
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
    if (!scene.raw || !scene.dat || !scene.list) {
      this.filterPin.hidden = true;
      return;
    }
    this.filterPin.hidden = false;
    const list = scene.list;
    const tile = makeRecipeFilterTile(this.which, shelfCount(list, this.which), () => {
      const shell = this.shellEl();
      if (!shell) return;
      openRecipeFilterSheet(shell, list, this.which, (which) => {
        this.which = which;
        const next = this.scene();
        this.buildStrip(next);
        this.syncSubView(next);
      });
    });
    this.filterPin.append(tile);
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
      this.strip.replaceChildren(this.makeModeSwitch());
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
      this.paintThumbs();
      this.scheduleScrollActive(picked?.id ?? null);
      return;
    }

    this.strip.replaceChildren(this.makeModeSwitch());
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
        bitmap = this.photo.neutral?.bitmap === this.neutralBitmap ? this.neutralBitmap : null;
        source = NEUTRAL_LABEL;
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
      const pill = button('band-act warn', this.photo.diptych ? '半格双联' : '配方需要 RAW');
      pill.dataset.act = 'recipe-status';
      pill.addEventListener('click', () => {
        const shell = this.shellEl();
        if (shell) openJpgInfoSheet(shell, Boolean(this.photo.diptych));
      });
      this.head.append(pill);
      return;
    }
    const book = this.api.book();
    if (!scene.dat) {
      const pill = button('band-act warn', '选配方文件');
      pill.dataset.act = 'recipe-status';
      pill.addEventListener('click', () => this.ask());
      this.head.append(pill);
      return;
    }
    const target = this.target(scene);
    const nameEl = el('b', 'rname', target?.name ?? '');
    this.head.append(nameEl);
    if (target) this.head.append(el('span', 'tag neutral-tag', cardTag(target)));

    if (this.fineTune) return;

    const calibrated = scene.state.state === 'ready';
    const off = calibrated && crossed(scene.photoUnit, scene.unit, scene.iso);

    if (scene.camError) {
      const errBtn = button('band-act warn', '拍摄配方');
      errBtn.dataset.cameraRecipeError = scene.camError.code;
      errBtn.dataset.act = 'recipe-status';
      errBtn.addEventListener('click', () => this.openStatusSheet(scene));
      this.head.append(errBtn);
    } else if (scene.adopted?.levelsUnknown) {
      const noteBtn = button('band-act dim', '档位未标');
      noteBtn.dataset.cameraRecipeNote = 'levels-unknown';
      noteBtn.dataset.act = 'recipe-status';
      noteBtn.addEventListener('click', () => this.openStatusSheet(scene));
      this.head.append(noteBtn);
    } else if (!calibrated) {
      const calBtn = button('band-act warn', '未校准');
      calBtn.dataset.state = 'no-calibration';
      calBtn.dataset.act = 'recipe-status';
      calBtn.addEventListener('click', () => this.openStatusSheet(scene));
      this.head.append(calBtn);
    } else if (off || book.datError) {
      const warnBtn = button('band-act warn', '未校准');
      warnBtn.dataset.state = 'uncalibrated';
      warnBtn.dataset.act = 'recipe-status';
      warnBtn.addEventListener('click', () => this.openStatusSheet(scene));
      this.head.append(warnBtn);
    }
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
    if (!scene.raw || !scene.dat || scene.state.state !== 'ready' || !target || !target.controls.length) return;
    const controls = this.orderedControls(target);
    if (!controls.some((c) => c.name === this.activeKnob)) {
      this.activeKnob = controls[0].name;
    }

    const topRow = el('div', 'fine-top');
    const back = button('chip fine-back', '‹ 配方');
    back.dataset.act = 'recipe-back';
    back.addEventListener('click', () => this.exitFineTune());

    const knobChips = el('div', 'chips knob-chips');
    const knobsBox = this.knobs(scene, target, controls);
    for (const control of controls) {
      const chip = button(control.name === this.activeKnob ? 'chip on' : 'chip', KNOB_NAMES[control.name] ?? control.label);
      chip.dataset.knobChip = control.name;
      chip.addEventListener('click', () => {
        this.activeKnob = control.name;
        for (const item of knobChips.querySelectorAll('.chip')) {
          item.classList.toggle('on', item === chip);
        }
        for (const row of knobsBox.querySelectorAll<HTMLElement>('.krow')) {
          row.hidden = row.dataset.knob !== this.activeKnob;
        }
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
      row.hidden = control.name !== this.activeKnob;
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
    this.applyLook(nextScene);
    this.hooks.onEditedChanged?.();
    void this.renderCards(nextScene, this.token);
  }

  /** 点一张卡：切配方；再点已选卡则进入 128pt 微调子页。 */
  private pick(card: Card): void {
    const scene = this.scene();
    if (!scene.raw) return;
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
      if (scene.state.state === 'ready' && card.controls.length > 0) {
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
    this.applyLook(nextScene);
    this.hooks.onEditedChanged?.();
  }

  private applyLook(scene: Scene): void {
    if (!scene.raw || !this.engine || this.hooks.mode() !== 'raw') return;
    const target = this.target(scene);
    const book = this.api.book();
    const ready = scene.dat && scene.state.state === 'ready' && target !== null && !needsCameraRecipe(this.photo, book.dat);
    if (!target || !ready) {
      this.wanted = NEUTRAL_BAKE_KEY;
      this.show(NEUTRAL_BAKE_KEY, NEUTRAL_LABEL);
      return;
    }
    const levels = this.levelsOf(target, scene);
    const key = book.bakeKey(target, scene.unit, scene.iso, levels);
    if (key && book.ready(key)) {
      this.wanted = key;
      this.show(key, PREDICTED_LABEL);
      return;
    }
    if (!this.shown) this.show(NEUTRAL_BAKE_KEY, NEUTRAL_LABEL);
    const want = `${target.id}|${JSON.stringify(levels ?? null)}|${scene.unit}|${scene.iso}`;
    this.wanted = want;
    book.bake(target, scene.unit, scene.iso, levels).then(
      (made) => {
        if (this.wanted !== want || !this.panel.isConnected) return;
        this.wanted = made;
        this.show(made, PREDICTED_LABEL);
      },
      () => undefined,
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
    const engine = this.engine;
    const source = this.rawKey;
    const book = this.api.book();
    if (!engine || !source || !scene.raw || !scene.dat || !scene.list || scene.state.state !== 'ready' || needsCameraRecipe(this.photo, book.dat)) return;
    const photo = this.photo;
    const version = photo.version;
    const session = this.api.session();
    const alive = () => token === this.token && this.api.photo(photo.id) === photo && photo.version === version && this.api.session() === session;
    const geomInfo = await this.hooks.geometry().catch(() => ({ geometry: GEOMETRY, cropped: false }));
    if (!alive()) return;
    const thumbGeometry = geomInfo.geometry;
    const order = [...this.cardsOf(scene), ...shelf(scene.list, 'all')];
    const seen = new Set<string>();
    const jobs: Array<{ card: Card; bake: Promise<string> }> = [];
    for (const card of order) {
      if (seen.has(card.id) || !card.controls.length) continue;
      seen.add(card.id);
      const known = book.bakeKey(card, scene.unit, scene.iso, this.levelsOf(card, scene));
      if (known && lookCache.get(photo, known)) continue;
      const bake = book.bake(card, scene.unit, scene.iso, this.levelsOf(card, scene));
      bake.catch(() => undefined);
      jobs.push({ card, bake });
    }
    for (const job of jobs) {
      let key: string;
      try {
        key = await job.bake;
      } catch (err) {
        if (!alive()) return;
        const slot = this.slots.find((s) => s.card.id === job.card.id);
        if (slot) {
          slot.sub.textContent = '无法生成电脑预测';
          slot.node.classList.add('lockd');
          slot.node.title = err instanceof Error ? err.message : String(err);
        }
        continue;
      }
      while (alive() && (this.hooks.dragging() || exportPaused())) await new Promise((done) => setTimeout(done, 120));
      if (!alive()) return;
      let gpu = book.gpuKey(key);
      if (!gpu) {
        try {
          await book.bake(job.card, scene.unit, scene.iso, this.levelsOf(job.card, scene));
        } catch {
          continue;
        }
        if (!alive()) return;
        gpu = book.gpuKey(key);
        if (!gpu) continue;
      }
      const out = await engine.thumb({ source, look: { recipe: gpu, lut: null, lutStrength: 1, curve: null, adjust: ADJUST }, geometry: thumbGeometry, aspect: null, maxEdge: 192 });
      if (!out || !alive()) return;
      const bitmap = await createImageBitmap(new ImageData(new Uint8ClampedArray(out.data), out.w, out.h));
      if (!alive()) {
        bitmap.close();
        return;
      }
      if (lookCache.get(photo, key)) {
        bitmap.close();
        continue;
      }
      lookCache.put(photo, key, version, bitmap, job.card.id);
      this.paintThumbs();
    }
  }

  private syncAsk(): void {
    this.askHandle?.sync(this.api.book());
  }

  private ask(): void {
    this.asked = true;
    const host = this.shellEl();
    if (!host) return;
    this.closeAsk();
    this.askHandle = openAskDatSheet(host, this.api.book(), () => this.api.pickDat());
  }

  private closeAsk(): void {
    this.askHandle?.close();
    this.askHandle = null;
  }
}
