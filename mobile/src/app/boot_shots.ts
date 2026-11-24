// 开发服务器截图脚本入口（仅 import.meta.env.DEV 动态导入，不进入生产包）。

import type { Book } from './book';
import type { EditorView } from './editor';
import type { ExportFlow } from './export_ui';
import type { AppApi, Photo } from './model';

export interface DevShotHost {
  readonly api: AppApi & { take(files: File[]): void };
  readonly shell: HTMLElement;
  readonly view: HTMLElement;
  readonly pageErrors: string[];
  readonly recipeBook: Book;
  screenName(): 'start' | 'grid' | 'editor';
  hasEngine(): boolean;
  photoCount(): number;
  orderedPhotos(): Photo[];
  ingestFiles(files: File[]): void;
  settleReads(): Promise<void>;
  waitEditorReady(): Promise<EditorView | null>;
  ensureExporterFlow(): Promise<ExportFlow>;
  ensureGpuReady(): Promise<void>;
}

export async function runDevShot(shot: string, host: DevShotHost): Promise<void> {
  await host.ensureGpuReady();
  const { sampleFiles, reportShot, aliasFile } = await import('./shots');
  // UI-M2 的几屏：校准数据和相机配方文件也走样本端点（截图用的临时样本目录里起了 zz- 开头的别名）。
  const lutShotMode = shot === 'lut' || shot === 'lut-space';
  const recipeShot = ['recipe', 'nocal', 'nodat', 'jpg'].includes(shot);
  const cropShot = shot === 'crop' || shot === 'crop-editor';
  if (shot === 'calib' || shot === 'recipe' || cropShot || lutShotMode) {
    const pack = await aliasFile('zz-calib.dng', 'GXR 校准数据.zip');
    if (pack) await host.recipeBook.importPack(pack);
  } else if (recipeShot) await host.recipeBook.removePack();
  if (shot !== 'start' && shot !== 'calib') {
    let files = shot === 'jpg' ? [] : await sampleFiles();
    const extra =
      shot === 'jpg'
        ? await aliasFile('zz-jpg.dng', 'R9990002.JPG')
        : shot === 'recipe' || shot === 'nocal' || cropShot || lutShotMode
          ? await aliasFile('zz-dat.dng', 'GXRCE.DAT')
          : null;
    if (extra) files = [...files, extra];
    if (shot.endsWith('-hdr')) files = [...files, ...(await (await import('./export_shots')).hdrShotFiles(files))];
    if (files.length) {
      host.ingestFiles(files);
      await host.settleReads();
    }
    for (let i = 0; i < 100 && (host.recipeBook.datBusy || host.recipeBook.packBusy); i++) await new Promise((done) => setTimeout(done, 100));
  }
  if ((shot === 'editor' || shot === 'drag' || recipeShot || cropShot || lutShotMode) && host.photoCount()) {
    const first = host.orderedPhotos()[0];
    if (first) host.api.open(first.id);
    const editor = await host.waitEditorReady();
    if (shot === 'drag') editor?.forceDrag();
    if (recipeShot) {
      host.view.querySelector<HTMLButtonElement>('.toolbar [data-tool="recipe"]')?.click();
      await new Promise((done) => setTimeout(done, 1500));
      if (shot === 'recipe') host.view.querySelector<HTMLButtonElement>('.strip[data-recipes] .rc[data-card="builtin:7"]')?.click();
      await new Promise((done) => setTimeout(done, 800));
    }
    if (cropShot) {
      host.view.querySelector<HTMLButtonElement>('.strip[data-recipes] .rc[data-card="builtin:7"]')?.click();
      host.view.querySelector<HTMLButtonElement>('.toolbar [data-tool="crop"]')?.click();
      await new Promise((done) => setTimeout(done, 400));
      host.view.querySelector<HTMLButtonElement>('.crop-panel .chips button[data-preset="xpan-65-24"]')?.click();
      for (let i = 0; i < 60 && first?.crop?.preset !== 'xpan-65-24'; i++) await new Promise((done) => setTimeout(done, 100));
      await new Promise((done) => setTimeout(done, 800));
      if (shot === 'crop-editor') {
        host.view.querySelector<HTMLButtonElement>('.toolbar [data-tool="recipe"]')?.click();
        await new Promise((done) => setTimeout(done, 1200));
      }
    }
    if (lutShotMode) {
      const { lutShot } = await import('./lut_shots');
      await lutShot(shot, host.api, host.shell);
    }
  }
  // M3 复制 / 套用那几屏（04a–04e、02a 带剪贴板）。
  if (shot.startsWith('paste-') && host.photoCount()) await (await import('./paste_shots')).pasteShot(shot, host.api, host.shell);
  if (shot.startsWith('export-') && host.photoCount()) {
    const [{ exportShot }, flow] = await Promise.all([import('./export_shots'), host.ensureExporterFlow()]);
    await exportShot(shot, host.orderedPhotos().map((photo) => photo.id), flow, host.shell);
  }
  if (shot.startsWith('frame') && host.photoCount()) {
    const first = host.orderedPhotos()[0];
    if (first) host.api.open(first.id);
    await host.waitEditorReady();
    const { frameShot } = await import('./frame_shots');
    await frameShot(shot, host.shell, host.view);
  }
  await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  host.shell.dataset.shot = shot;
  await reportShot(shot, {
    screen: host.screenName(),
    gpu: host.hasEngine(),
    drag: host.shell.dataset.drag,
    photos: host.orderedPhotos().map((photo) => ({
      stem: photo.stem,
      status: photo.status,
      jpg: photo.jpg !== null,
      bitmap: photo.bitmap ? [photo.bitmap.width, photo.bitmap.height] : null,
      captureMs: photo.captureMs,
    })),
    errors: host.pageErrors.slice(0, 10),
  });
}
