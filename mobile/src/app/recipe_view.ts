// 编辑器里的配方：照片下面的配方卡（03a）和「配方」页。点卡换配方；卡片在后台一张张画出来。
// 状态照设计稿：06c 没校准（中性转换，可以选）、06d 没有相机配方文件（卡片置灰）、06e 只有 JPG。

import type { Engine, Geometry } from '../../../frontend/src/index';
import { crossed, uncalibratedReason, unitOfLens, type Unit } from '../lib/calib';
import { mergeMeta } from '../lib/exif';
import { NEUTRAL_BAKE_KEY } from '../lib/neutral';
import { cardTag, listing, namesOnly, shelf, type Card, type Listing, type Shelf } from '../lib/recipes';
import type { UnitState } from './book';
import { button, el, icon } from './dom';
import type { AppApi, Photo } from './model';

const CHECK = 'M5 12.5l4.5 4.5L19 7.5';
const WARN = 'M12 4l9 16H3zM12 10v4M12 17h.01';
const PLUS = 'M12 5v14M5 12h14';
const FILE = 'M7 3h7l4 4v14H7zM14 3v4h4';
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
/** 桌面 lut.js 的那句；手机上没有本机配方，只用前半句。 */
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
}

interface Slot {
  card: Card;
  node: HTMLButtonElement;
  thumb: HTMLElement;
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
  list: Listing | null;
}

export class RecipeView {
  private token = 0;
  private which: Shelf = 'all';
  private slots: Slot[] = [];
  private readonly strip = el('div', 'strip');
  private readonly panel = el('div', 'panel recipe');
  private rawKey: string | null = null;
  private neutralBitmap: ImageBitmap | null = null;
  /** 舞台上现在套的烘焙表；新的没好之前一直留着上一张，照片不会空。 */
  private shown = '';
  /** 最近一次要的那张表。旧的烘焙结果回来时不再换上去。 */
  private wanted = '';
  private asked = false;
  private readonly off: () => void;

  constructor(
    private readonly api: AppApi,
    private readonly engine: Engine | null,
    private readonly photo: Photo,
    private readonly hooks: StageHooks,
  ) {
    this.strip.dataset.recipes = '1';
    this.off = api.book().onChange(() => this.refresh());
    this.build();
  }

  stripEl(): HTMLElement {
    return this.strip;
  }

  panelEl(): HTMLElement {
    return this.panel;
  }

  destroy(): void {
    this.token += 1;
    this.off();
    this.closeAsk();
  }

  /** 这张 RAW 已经在显卡里：套上该用的烘焙表，开始画卡片。 */
  start(rawKey: string): void {
    this.rawKey = rawKey;
    this.refresh();
  }

  /** 中性小图好了（每个版本做一次）。没校准、没有相机配方文件时卡片都用它。 */
  neutral(bitmap: ImageBitmap): void {
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

  private scene(): Scene {
    const book = this.api.book();
    const meta = mergeMeta(this.photo.jpgMeta, this.photo.dngMeta);
    const photoUnit = unitOfLens(meta.lens);
    const unit = photoUnit ?? 'jlaunch';
    const dat = book.dat;
    return {
      raw: this.photo.dng !== null,
      gpu: this.engine !== null,
      dat: dat !== null,
      photoUnit,
      unit,
      iso: meta.iso,
      state: book.unitState(unit),
      list: dat ? listing(dat.summary, dat.menu, unit) : null,
    };
  }

  private cardsOf(scene: Scene): Card[] {
    if (!scene.list) return namesOnly();
    return shelf(scene.list, this.which);
  }

  private chosen(scene: Scene): Card | null {
    const choice = this.photo.choice;
    if (!choice || !scene.list || choice.unit !== scene.unit) return null;
    return shelf(scene.list, 'all').find((card) => card.id === choice.id) ?? null;
  }

  /** 没选配方时桌面用这个单元的原厂 04（电脑模拟）。 */
  private target(scene: Scene): Card | null {
    const picked = this.chosen(scene);
    if (picked) return picked;
    return scene.list?.builtin.find((card) => card.slot === 4) ?? null;
  }

  private build(): void {
    const scene = this.scene();
    this.buildStrip(scene);
    this.buildPanel(scene);
  }

  refresh(): void {
    if (!this.strip.isConnected && this.slots.length) return;
    const token = ++this.token;
    const scene = this.scene();
    this.buildStrip(scene);
    this.buildPanel(scene);
    this.applyLook(scene);
    void this.renderCards(scene, token);
  }

  private buildStrip(scene: Scene): void {
    this.strip.replaceChildren();
    this.slots = [];
    const cards = this.cardsOf(scene);
    const locked = scene.raw && !scene.dat;
    const picked = this.chosen(scene);
    this.strip.dataset.state = !scene.raw ? 'jpg-only' : locked ? 'no-recipe-file' : scene.state.state === 'ready' ? 'calibrated' : 'uncalibrated';
    if (scene.list && this.which === 'mine' && !cards.length) {
      const empty = el('p', 'empty faint', MINE_EMPTY);
      this.strip.append(empty);
      return;
    }
    for (const card of cards) {
      const node = button('rc', '');
      node.dataset.card = card.id;
      const thumb = el('div', 'th');
      const sub = el('span', 'sub');
      node.append(thumb, el('span', 'nm', card.name), sub);
      const usable = scene.raw && scene.dat && !card.pending && card.levels !== null;
      if (!usable) node.classList.add('lockd');
      if (!scene.raw) node.disabled = true;
      if (picked && picked.id === card.id) this.mark(node, thumb);
      node.addEventListener('click', () => this.pick(card));
      this.strip.append(node);
      this.slots.push({ card, node, thumb, sub });
    }
    this.paintThumbs();
  }

  private mark(node: HTMLElement, thumb: HTMLElement): void {
    node.classList.add('on');
    const tick = el('span', 'ckk');
    tick.append(icon(CHECK));
    thumb.append(tick);
  }

  /** 每张卡画什么、底下写什么。 */
  private paintThumbs(): void {
    const scene = this.scene();
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
        bitmap = this.neutralBitmap;
        source = NEUTRAL_LABEL;
      } else if (card.pending) {
        bitmap = this.neutralBitmap;
        source = NEUTRAL_LABEL;
        text = '这项下一步做';
      } else if (card.error) {
        bitmap = this.neutralBitmap;
        source = NEUTRAL_LABEL;
        text = card.error;
      } else if (scene.state.state !== 'ready') {
        bitmap = this.neutralBitmap;
        source = text = NEUTRAL_LABEL;
      } else {
        const key = this.api.book().bakeKey(card, scene.unit, scene.iso);
        const look = key ? this.photo.looks.get(key) : undefined;
        if (look && look.version === this.photo.version) {
          bitmap = look.bitmap;
          source = PREDICTED_LABEL;
        }
        text = card.desc ?? '';
      }
      sub.textContent = text;
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

  private buildPanel(scene: Scene): void {
    this.panel.replaceChildren();
    if (!scene.raw) {
      // 06e：桌面的两句，下面写还能用的几页。
      const note = el('div', 'bn grey');
      const text = el('div', 'tx');
      text.append(el('b', '', '配方需要 RAW 文件'), el('div', 'dim', '拍摄时将画质设为 RAW+JPEG'));
      note.append(text);
      this.panel.append(
        note,
        el('p', 'pad dim', '这张只有相机 JPG。下面这些照常能用，导出的是相机 JPG 加上这些改动：调整、裁剪、边框、曲线。'),
        el('p', 'pad faint', 'HDR 需要 RAW 文件。这张会存普通版本。'),
      );
      return;
    }
    const book = this.api.book();
    if (!scene.dat) {
      const box = el('div', 'pad');
      box.append(el('p', 'dim', book.datError ?? '配方要用存储卡上的相机配方文件，这次没有和照片一起选上。每张卡选一次就行。没选之前，照片只显示中性转换。'));
      const pick = button('btn primary block', '在「文件」里选 GXRCE.DAT');
      pick.prepend(icon(FILE));
      pick.addEventListener('click', () => this.api.pickDat());
      box.append(pick);
      this.panel.append(box);
      return;
    }
    const target = this.target(scene);
    const head = el('div', 'pad recipe-head');
    const row = el('div', 'row');
    row.append(el('b', 'rname', target?.name ?? ''));
    if (target) row.append(el('span', 'tag neutral-tag', cardTag(target)));
    const calibrated = scene.state.state === 'ready';
    const off = calibrated && crossed(scene.photoUnit, scene.unit, scene.iso);
    const right = el('span', calibrated && !off ? 'tag ok' : 'tag warn', calibrated && !off ? PREDICTED_LABEL : '未校准');
    right.dataset.state = calibrated && !off ? 'calibrated' : 'uncalibrated';
    right.style.marginLeft = 'auto';
    row.append(right);
    head.append(row);
    this.panel.append(head);
    this.panel.append(this.statusBlock(scene, off));
    if (book.datNote) this.panel.append(el('p', 'pad faint small', book.datNote));
    this.panel.append(this.chips(scene));
  }

  private statusBlock(scene: Scene, off: boolean): HTMLElement {
    const state = scene.state;
    if (state.state === 'ready') {
      const line = el('div', 'pad hdr-note');
      line.dataset.state = off ? 'uncalibrated' : 'calibrated';
      if (off) {
        line.append(el('span', 'warn', '未校准：'), el('span', 'dim', uncalibratedReason(scene.photoUnit, scene.unit, scene.iso)));
      } else {
        const ok = el('span', 'ok');
        ok.append(icon(CHECK));
        line.append(ok, el('span', '', '已校准：颜色按校准数据预测。'));
      }
      return line;
    }
    if (state.state === 'checking') return el('p', 'pad dim', '正在核对校准数据…');
    const box = el('div');
    const banner = el('div', 'bn');
    banner.dataset.state = 'no-calibration';
    const mark = el('span', 'warn');
    mark.append(icon(WARN));
    const text = el('div', 'tx');
    if (state.state === 'none') {
      text.append(el('b', '', '还没导入校准数据。'), document.createTextNode('现在所有配方都只显示中性转换，和相机里的颜色不同。导出也一样。'));
    } else {
      text.append(el('b', '', state.message), document.createTextNode(' 这张照片的配方只显示中性转换，和相机里的颜色不同。'));
    }
    banner.append(mark, text);
    const bring = button('btn primary block', '导入校准数据');
    bring.prepend(icon(PLUS));
    bring.addEventListener('click', () => this.api.pickCalib());
    const wrap = el('div', 'pad');
    wrap.append(bring);
    box.append(banner, wrap, el('p', 'faint pad small', '在 Mac 的 GXR Studio 里导出校准数据，用隔空投送发到这台 iPhone，再点上面的按钮。只需一次。'));
    return box;
  }

  private chips(scene: Scene): HTMLElement {
    const list = scene.list;
    const row = el('div', 'chips');
    if (!list) return row;
    const all = list.builtin.length + list.looks.length + list.users.length;
    const items: Array<[Shelf, string]> = [
      ['all', `全部 ${all}`],
      ['builtin', `内置 ${list.builtin.length}`],
    ];
    if (list.looks.length) items.push(['looks', `Looks ${list.looks.length}`]);
    items.push(['mine', `我的配方 ${list.users.length}`]);
    for (const [which, label] of items) {
      const chip = button('chip', label);
      chip.dataset.shelf = which;
      if (which === this.which) chip.classList.add('on');
      chip.addEventListener('click', () => {
        this.which = which;
        for (const other of row.querySelectorAll('.chip')) other.classList.toggle('on', other === chip);
        this.buildStrip(this.scene());
      });
      row.append(chip);
    }
    return row;
  }

  /** 点一张卡：记进这张照片的编辑状态，照片换成它（烘焙表没好之前留着上一张）。 */
  private pick(card: Card): void {
    const scene = this.scene();
    if (!scene.raw) return;
    if (!scene.dat) {
      this.ask();
      return;
    }
    if (card.pending || !card.levels) return;
    this.photo.choice = { id: card.id, unit: scene.unit };
    for (const slot of this.slots) {
      slot.node.classList.remove('on');
      slot.thumb.querySelector('.ckk')?.remove();
      if (slot.card.id === card.id) this.mark(slot.node, slot.thumb);
    }
    this.buildPanel(scene);
    this.applyLook(scene);
  }

  /** 舞台上的配方。校准好了用这张卡的烘焙表；否则中性转换。 */
  private applyLook(scene: Scene): void {
    if (!scene.raw || !this.engine || this.hooks.mode() !== 'raw') return;
    const target = this.target(scene);
    const book = this.api.book();
    const key = scene.dat && scene.state.state === 'ready' && target ? book.bakeKey(target, scene.unit, scene.iso) : null;
    if (!target || !key) {
      this.wanted = NEUTRAL_BAKE_KEY;
      this.show(NEUTRAL_BAKE_KEY, NEUTRAL_LABEL);
      return;
    }
    if (book.ready(key)) {
      this.wanted = key;
      this.show(key, PREDICTED_LABEL);
      return;
    }
    // 新的表还在做：照片先留着上一张（第一次打开时是中性转换），做好了而且还是要它才换。
    if (!this.shown) this.show(NEUTRAL_BAKE_KEY, NEUTRAL_LABEL);
    this.wanted = key;
    book.bake(target, scene.unit, scene.iso).then(
      (made) => {
        if (this.wanted === made && this.strip.isConnected) this.show(made, PREDICTED_LABEL);
      },
      () => undefined,
    );
  }

  private show(key: string, label: string): void {
    this.shown = key;
    this.strip.closest('.app')?.setAttribute('data-recipe-key', key);
    this.hooks.look(key, label);
  }

  /** 校准好了才画配方卡：按列表顺序一张张做烘焙表、请显卡出小图。拖滑块时先停。 */
  private async renderCards(scene: Scene, token: number): Promise<void> {
    const engine = this.engine;
    const source = this.rawKey;
    if (!engine || !source || !scene.raw || !scene.dat || !scene.list || scene.state.state !== 'ready') return;
    const book = this.api.book();
    const photo = this.photo;
    const version = photo.version;
    const session = this.api.session();
    const alive = () => token === this.token && this.api.photo(photo.id) === photo && photo.version === version && this.api.session() === session;
    const order = [...this.cardsOf(scene), ...shelf(scene.list, 'all')];
    const seen = new Set<string>();
    const jobs: Array<{ card: Card; key: string; bake: Promise<string> }> = [];
    for (const card of order) {
      const key = book.bakeKey(card, scene.unit, scene.iso);
      if (!key || seen.has(key)) continue;
      seen.add(key);
      const cached = photo.looks.get(key);
      if (cached && cached.version === version) continue;
      const bake = book.bake(card, scene.unit, scene.iso);
      bake.catch(() => undefined);
      jobs.push({ card, key, bake });
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
      while (alive() && this.hooks.dragging()) await new Promise((done) => setTimeout(done, 120));
      if (!alive()) return;
      book.ready(key);
      const out = await engine.thumb({ source, look: { recipe: key, lut: null, lutStrength: 1, curve: null, adjust: ADJUST }, geometry: GEOMETRY, aspect: null, maxEdge: 192 });
      if (!out || !alive()) return;
      const bitmap = await createImageBitmap(new ImageData(new Uint8ClampedArray(out.data), out.w, out.h));
      if (!alive()) {
        bitmap.close();
        return;
      }
      photo.looks.get(key)?.bitmap.close();
      photo.looks.set(key, { version, bitmap });
      this.paintThumbs();
    }
  }

  /** 06d：还差相机配方文件。 */
  private ask(): void {
    this.asked = true;
    const host = this.strip.closest('.app');
    if (!(host instanceof HTMLElement)) return;
    this.closeAsk();
    const scrim = el('div', 'scrim ask-scrim');
    const sheet = el('div', 'ms ask-sheet');
    sheet.dataset.state = 'no-recipe-file';
    const head = el('div', 'hd');
    head.append(el('h2', '', '还差相机配方文件'));
    const path = el('div', 'path');
    const fic = el('span', 'fic');
    fic.append(icon(FILE));
    path.append(fic, el('span', '', '存储卡'), el('span', 'faint', '›'), el('b', 'num', 'GXRCE.DAT'), el('span', 'faint small', '（在最外层，和 DCIM 并排）'));
    const pick = button('btn primary lg block', '在「文件」里选 GXRCE.DAT');
    pick.prepend(icon(FILE));
    pick.addEventListener('click', () => {
      this.closeAsk();
      this.api.pickDat();
    });
    const later = button('btn lg ghost block', '先不用，只看中性转换');
    later.addEventListener('click', () => this.closeAsk());
    scrim.addEventListener('click', () => this.closeAsk());
    sheet.append(
      el('div', 'grab'),
      head,
      el('p', 'lead', '配方要用存储卡上的相机配方文件，这次没有和照片一起选上。'),
      el('p', 'lead', '每张卡选一次就行。没选之前，照片只显示中性转换。'),
      path,
      pick,
      later,
    );
    host.append(scrim, sheet);
  }

  private closeAsk(): void {
    const host = this.strip.closest('.app') ?? document;
    host.querySelector('.ask-scrim')?.remove();
    host.querySelector('.ask-sheet')?.remove();
  }
}

const ids = new WeakMap<ImageBitmap, number>();
let nextId = 1;

/** 位图的身份（同一张位图不重画）。 */
function bitmapId(bitmap: ImageBitmap): number {
  let id = ids.get(bitmap);
  if (!id) {
    id = nextId++;
    ids.set(bitmap, id);
  }
  return id;
}

