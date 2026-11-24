// 编辑器的照片载入管线：RAW / JPG 上传显卡、清图提锐、中性配方小图渲染与下一张预取。

import type { Engine } from '../../../frontend/src/index';
import { describeDng, type Described } from '../core/api';
import { NEUTRAL_BAKE_KEY } from '../lib/neutral';
import { toAdjust } from './adjust_view';
import type { CropStage } from './crop_stage';
import { isGone, zeroKnobs, type AppApi, type Photo } from './model';

export type MediaMode = 'raw' | 'jpg' | 'still';

export const NO_GPU_TITLE = '这个浏览器现在用不了显卡预览';
// 「配方和 LUT」不拆行：字间用 U+2060（词连接符），「和」与「LUT」之间用不换行空格，折行时整组移到下一行，「LUT」不会单独一行。
export const NO_GPU_DETAIL = '预览只显示相机 JPG，导出仍会套用配\u2060方\u2060和\u00a0LUT';

export interface EditorMediaCallbacks {
  api(): AppApi;
  engine(): Engine | null;
  host(): HTMLElement;
  shell(): HTMLElement;
  photo(): Photo | null;
  mode(): MediaMode;
  setMode(mode: MediaMode): void;
  nextToken(): number;
  same(token: number, photo: Photo, version: number, session: number, rev?: number): boolean;
  gpuCanvas(): HTMLCanvasElement | null;
  stillCanvas(): HTMLCanvasElement | null;
  placeholderEl(): HTMLElement | null;
  cropStage(): CropStage | null;
  setSharp(bitmap: ImageBitmap | null): void;
  setLookCaption(text: string): void;
  lookCaption(): string;
  caption(): string;
  isCropActive(): boolean;
  setLabel(text: string): void;
  push(): void;
  setAdjustDisabled(disabled: boolean): void;
  startRecipes(key: string): void;
  refreshRecipes(): void;
  setNeutralRecipe(bitmap: ImageBitmap | null): void;
  startLuts(key: string): void;
  onMediaReady(): void;
  syncFloatHistPos(): void;
}

export class EditorMedia {
  private wantNeighbor = '';
  private sharpening: Promise<void> = Promise.resolve();
  private loadingRaw: { id: string; version: number } | null = null;
  private offBook: (() => void) | null = null;
  private readonly cb: EditorMediaCallbacks;

  constructor(cb: EditorMediaCallbacks) {
    this.cb = cb;
  }

  reset(): void {
    this.wantNeighbor = '';
    this.loadingRaw = null;
    this.offBook?.();
    this.offBook = null;
  }

  private watchBook(): void {
    if (this.offBook) return;
    const api = this.cb.api();
    this.offBook = api.book().onChange(() => {
      const cur = this.cb.photo();
      if (!cur || !cur.dng || !cur.jpg || cur.diptych || cur.choice !== null) return;
      const hasDat = api.book().dat !== null;
      const mode = this.cb.mode();
      // 补选相机配方文件后从相机 JPG 切到 RAW；忘掉配方文件且未显式选配方时切回相机 JPG。
      if (hasDat && mode === 'jpg') {
        if (this.loadingRaw?.id === cur.id && this.loadingRaw.version === cur.version) return;
        void this.load(cur).catch(() => undefined);
      } else if (!hasDat && mode === 'raw') {
        void this.load(cur).catch(() => undefined);
      }
    });
  }

  sharpeningPromise(): Promise<void> {
    return this.sharpening;
  }

  private nextPhoto(dir: -1 | 1): Photo | null {
    const cur = this.cb.photo();
    if (!cur) return null;
    const photos = this.cb.api().photos();
    const index = photos.findIndex((item) => item.id === cur.id);
    return photos[index + dir] ?? null;
  }

  private async take(photo: Photo): Promise<ArrayBuffer> {
    const api = this.cb.api();
    const warm = api.takeWarm(photo.id, photo.version);
    if (warm) return warm;
    if (!photo.dng) throw new DOMException('没有 RAW', 'NotFoundError');
    const reply = await api.readFull(photo);
    if (reply.ok) return reply.bytes;
    if (reply.stale) throw new DOMException('重新选过', 'AbortError');
    throw new DOMException('读不了', reply.gone ? 'NotReadableError' : 'EncodingError');
  }

  private prefetch(next: Photo | null, token: number): void {
    if (!next?.dng) {
      this.wantNeighbor = '';
      return;
    }
    const api = this.cb.api();
    const id = next.id;
    const version = next.version;
    const session = api.session();
    this.wantNeighbor = id;
    void api.readFull(next).then((reply) => {
      if (!reply.ok || this.wantNeighbor !== id || api.session() !== session || api.photo(id) !== next || next.version !== version) return;
      if (!this.cb.same(token, this.cb.photo() ?? next, (this.cb.photo() ?? next).version, session)) return;
      api.keepWarm(id, version, reply.bytes);
    });
  }

  async load(photo: Photo): Promise<void> {
    this.watchBook();
    const token = this.cb.nextToken();
    const version = photo.version;
    const api = this.cb.api();
    const session = api.session();
    const engine = this.cb.engine();

    // 没有相机配方文件时，带相机 JPG 的照片默认直接显示相机 JPG（不弹补选表）；纯 RAW 仍显示中性转换。
    const useCameraJpg = !photo.dng || (!!photo.jpg && !photo.diptych && !api.book().dat && !photo.choice);
    if (useCameraJpg) {
      this.loadingRaw = null;
      this.showStillPhoto(photo, photo.jpg ? '相机 JPG' : '只有 RAW', token, !!photo.jpg && !!engine);
      if (engine && photo.bitmap) {
        const key = `jpg:${photo.id}`;
        engine.addJpg(key, photo.bitmap);
        engine.show(key);
        this.cb.setMode('jpg');
        this.cb.startLuts(key);
        const gpu = this.cb.gpuCanvas();
        const still = this.cb.stillCanvas();
        if (gpu) gpu.hidden = false;
        if (still) still.hidden = true;
        this.cb.push();
        await this.cb.cropStage()?.applyPreviewGeometry();
        if (!this.cb.same(token, photo, version, session)) return;
        const frame = engine.frame();
        if (frame) this.cb.shell().dataset.frame = '1';
        this.cb.setAdjustDisabled(false);
        this.cb.cropStage()?.syncLoaded();
        this.cb.onMediaReady();
        this.cb.syncFloatHistPos();
      }
      if (photo.dng) this.cb.refreshRecipes();
      return;
    }
    this.loadingRaw = { id: photo.id, version };

    const key = `raw:${photo.id}`;
    if (engine && photo.cached === version && engine.hasSource(key)) {
      await this.showRaw(key, token);
      return;
    }
    if (!engine && photo.checked === version) {
      this.showStillPhoto(photo, NO_GPU_TITLE, token);
      return;
    }
    let bytes: ArrayBuffer;
    try {
      bytes = await this.take(photo);
    } catch (err) {
      if (!this.cb.same(token, photo, version, session)) return;
      if (err instanceof DOMException && err.name === 'AbortError') return;
      if (isGone(err)) api.markGone(photo.id, version);
      else api.markBad(photo.id, version);
      return;
    }
    if (!this.cb.same(token, photo, version, session)) return;
    let described: Described;
    try {
      described = await describeDng(bytes);
    } catch {
      if (this.cb.same(token, photo, version, session)) api.markBad(photo.id, version);
      return;
    }
    if (!this.cb.same(token, photo, version, session)) return;
    if (!described.layout || described.reason) {
      api.markBad(photo.id, version);
      return;
    }
    photo.checked = version;
    photo.dngMeta =
      described.meta.copyright === undefined && photo.dngMeta?.copyright !== undefined
        ? { ...described.meta, copyright: photo.dngMeta.copyright }
        : described.meta;
    if (!engine) {
      this.showStillPhoto(photo, NO_GPU_TITLE, token);
      this.cb.setAdjustDisabled(true);
      this.cb.refreshRecipes();
      this.prefetch(this.nextPhoto(1), token);
      return;
    }
    try {
      const installed = await api.uploadRaw(photo, version, described.layout, bytes, described.scene);
      if (!installed || !this.cb.same(token, photo, version, session)) return;
      await this.showRaw(key, token);
      if (!this.cb.same(token, photo, version, session)) return;
      this.prefetch(this.nextPhoto(1), token);
    } catch {
      if (!this.cb.same(token, photo, version, session)) return;
      api.markBad(photo.id, version);
    }
  }

  async neutralCards(photo: Photo, key: string, token: number): Promise<void> {
    const engine = this.cb.engine();
    const cropStage = this.cb.cropStage();
    if (!engine || !cropStage) return;
    const version = photo.version;
    const rev = photo.rev;
    const session = this.cb.api().session();
    const alive = () => this.cb.same(token, photo, version, session, rev);
    if (photo.neutral && photo.neutral.version === version) {
      if (alive()) this.cb.setNeutralRecipe(photo.neutral.bitmap);
      return;
    }
    const look = { recipe: NEUTRAL_BAKE_KEY, lut: null, lutStrength: 1, curve: null, adjust: toAdjust(zeroKnobs()) };
    const { geometry } = await cropStage.resolveBoxGeometry(photo);
    if (!alive()) return;
    const out = await engine.thumb({ source: key, look, geometry, aspect: null, maxEdge: 192 });
    if (!out || !alive()) return;
    const bitmap = await createImageBitmap(new ImageData(new Uint8ClampedArray(out.data), out.w, out.h));
    if (!alive()) {
      bitmap.close();
      return;
    }
    this.cb.setNeutralRecipe(null);
    photo.neutral?.bitmap.close();
    photo.neutral = { version, bitmap };
    this.cb.setNeutralRecipe(bitmap);
  }

  private async showRaw(key: string, token: number): Promise<void> {
    const engine = this.cb.engine();
    const photo = this.cb.photo();
    if (!engine || !photo) return;
    const version = photo.version;
    const session = this.cb.api().session();
    engine.show(key);
    this.cb.setMode('raw');
    this.cb.push();
    await this.cb.cropStage()?.applyPreviewGeometry();
    if (!this.cb.same(token, photo, version, session)) return;
    const frame = engine.frame();
    if (frame) this.cb.shell().dataset.frame = '1';
    this.loadingRaw = null;
    if (!this.cb.lookCaption() || this.cb.lookCaption() === '相机 JPG') this.cb.setLookCaption('中性转换');
    if (!this.cb.isCropActive() && (!this.cb.caption() || this.cb.caption() === '相机 JPG')) this.cb.setLabel(this.cb.lookCaption());
    this.cb.cropStage()?.syncLoaded();
    this.cb.startRecipes(key);
    this.cb.startLuts(key);
    const gpu = this.cb.gpuCanvas();
    const still = this.cb.stillCanvas();
    if (gpu) gpu.hidden = false;
    if (still) still.hidden = true;
    this.cb.setAdjustDisabled(false);
    void this.neutralCards(photo, key, token);
    this.cb.onMediaReady();
    this.cb.syncFloatHistPos();
  }

  private showStillPhoto(photo: Photo, label: string, token: number, fullJpg = false): void {
    this.cb.setMode('still');
    this.cb.setLookCaption(label);
    if (!this.cb.isCropActive()) this.cb.setLabel(label);
    this.cb.cropStage()?.paintStill(photo.bitmap);
    const gpu = this.cb.gpuCanvas();
    const ph = this.cb.placeholderEl();
    if (gpu) gpu.hidden = true;
    if (!this.cb.engine()) this.cb.setAdjustDisabled(true);
    if (ph) ph.hidden = !!photo.bitmap || !!photo.jpg;
    this.cb.onMediaReady();
    this.cb.syncFloatHistPos();
    this.sharpening = this.sharpen(photo, token, fullJpg);
  }

  private async sharpen(photo: Photo, token: number, fullJpg: boolean): Promise<void> {
    const api = this.cb.api();
    const engine = this.cb.engine();
    const version = photo.version;
    const session = api.session();
    const alive = () => this.cb.same(token, photo, version, session);
    const width = fullJpg
      ? Number.POSITIVE_INFINITY
      : Math.min(2048, Math.round((this.cb.host().clientWidth || 390) * (window.devicePixelRatio || 1)));
    const bitmap = await api.still(photo, width);
    if (!bitmap) return;
    if (!alive() || this.cb.mode() === 'raw') {
      bitmap.close();
      return;
    }
    if (this.cb.mode() === 'jpg' && engine) {
      const key = `jpg:${photo.id}`;
      engine.addJpg(key, bitmap);
      bitmap.close();
      engine.show(key);
      this.cb.startLuts(key);
      this.cb.push();
      await this.cb.cropStage()?.applyPreviewGeometry();
      if (!alive()) return;
      engine.frame();
      this.cb.cropStage()?.syncLoaded();
      this.cb.onMediaReady();
      this.cb.syncFloatHistPos();
      return;
    }
    this.cb.setSharp(bitmap);
    this.cb.cropStage()?.paintStill(bitmap);
    this.cb.cropStage()?.syncLoaded();
    this.cb.onMediaReady();
    this.cb.syncFloatHistPos();
  }
}
