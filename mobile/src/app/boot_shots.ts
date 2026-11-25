// 开发服务器的截图脚本（?shot=…）。只在 import.meta.env.DEV 里动态导入，生产包里没有。
// 从 boot.ts 原样移出；开发服务器上两个分包在开始截图前都已经加载好，所以这里的切屏都是同步的。

import type { Book } from './book';
import type { EditorView } from './editor';
import type { ExportFlow } from './export_ui';
import type { AppApi } from './model';

export interface ShotHost {
  api: AppApi & { take(files: File[]): void };
  shell: HTMLElement;
  view: HTMLElement;
  pageErrors: string[];
  book: Book;
  screen(): 'start' | 'grid' | 'editor';
  editor(): EditorView | null;
  exporter(): ExportFlow | null;
  ingest(files: File[]): void;
}

function settle(api: AppApi): Promise<void> {
  return new Promise((resolve) => {
    const deadline = performance.now() + 20_000;
    const tick = () => {
      const { done, total } = api.loading();
      if ((total > 0 && done >= total) || performance.now() > deadline) resolve();
      else window.setTimeout(tick, 200);
    };
    tick();
  });
}

export async function runShot(shot: string, host: ShotHost): Promise<void> {
  const { api, shell, view } = host;
  const { sampleFiles, reportShot, aliasFile } = await import('./shots');
  // UI-M2 的几屏：校准数据和相机配方文件也走样本端点（截图用的临时样本目录里起了 zz- 开头的别名）。
  const lutShotMode = shot === 'lut' || shot === 'lut-space';
  const recipeShot = ['recipe', 'nocal', 'nodat', 'jpg'].includes(shot);
  const cropShot = shot === 'crop' || shot === 'crop-editor';
  if (shot === 'calib' || shot === 'recipe' || cropShot || lutShotMode) {
    const pack = await aliasFile('zz-calib.dng', 'GXR 校准数据.zip');
    if (pack) await host.book.importPack(pack);
  } else if (recipeShot) await host.book.removePack();
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
      host.ingest(files);
      await settle(api);
    }
    for (let i = 0; i < 100 && (host.book.datBusy || host.book.packBusy); i++) await new Promise((done) => setTimeout(done, 100));
  }
  if ((shot === 'editor' || shot === 'drag' || recipeShot || cropShot || lutShotMode) && api.photos().length) {
    const first = api.photos()[0];
    if (first) api.open(first.id);
    await host.editor()?.settled;
    if (shot === 'drag') host.editor()?.forceDrag();
    if (recipeShot) {
      view.querySelector<HTMLButtonElement>('.toolbar [data-tool="recipe"]')?.click();
      await new Promise((done) => setTimeout(done, 1500));
      if (shot === 'recipe') view.querySelector<HTMLButtonElement>('.strip[data-recipes] .rc[data-card="builtin:7"]')?.click();
      await new Promise((done) => setTimeout(done, 800));
    }
    if (cropShot) {
      view.querySelector<HTMLButtonElement>('.strip[data-recipes] .rc[data-card="builtin:7"]')?.click();
      view.querySelector<HTMLButtonElement>('.toolbar [data-tool="crop"]')?.click();
      await new Promise((done) => setTimeout(done, 400));
      view.querySelector<HTMLButtonElement>('.crop-panel .chips button[data-preset="xpan-65-24"]')?.click();
      for (let i = 0; i < 60 && first?.crop?.preset !== 'xpan-65-24'; i++) await new Promise((done) => setTimeout(done, 100));
      await new Promise((done) => setTimeout(done, 800));
      if (shot === 'crop-editor') {
        view.querySelector<HTMLButtonElement>('.toolbar [data-tool="recipe"]')?.click();
        await new Promise((done) => setTimeout(done, 1200));
      }
    }
    if (lutShotMode) {
      const { lutShot } = await import('./lut_shots');
      await lutShot(shot, api, shell);
    }
  }
  // M3 复制 / 套用那几屏（04a–04e、02a 带剪贴板）。
  if (shot.startsWith('paste-') && api.photos().length) await (await import('./paste_shots')).pasteShot(shot, api, shell);
  const flow = host.exporter();
  if (shot.startsWith('export-') && api.photos().length && flow) {
    const { exportShot } = await import('./export_shots');
    await exportShot(shot, api.photos().map((photo) => photo.id), flow, shell);
  }
  if (shot.startsWith('frame') && api.photos().length) {
    const first = api.photos()[0];
    if (first) api.open(first.id);
    await host.editor()?.settled;
    const { frameShot } = await import('./frame_shots');
    await frameShot(shot, shell, view);
  }
  await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  shell.dataset.shot = shot;
  await reportShot(shot, {
    screen: host.screen(),
    gpu: api.gpu(),
    drag: shell.dataset.drag,
    photos: api.photos().map((photo) => ({
      stem: photo.stem,
      status: photo.status,
      jpg: photo.jpg !== null,
      bitmap: photo.bitmap ? [photo.bitmap.width, photo.bitmap.height] : null,
      captureMs: photo.captureMs,
    })),
    errors: host.pageErrors.slice(0, 10),
  });
}
