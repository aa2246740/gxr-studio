// 编辑器上的 HDR 预览接线。控制器的生命周期留在编辑器里，这里只负责把它接上。

import type { Engine } from '../../../frontend/src/index';
import { hdrPreviewGrid } from '../core/api';
import { hdrDisplayCapable } from '../core/hdr_display';
import { HdrPreviewController } from '../core/hdr_preview';
import { loadExportHdr, watchExportHdr } from '../lib/prefs';
import type { AppApi, Photo } from './model';

export function createHdrPreview(host: {
  photo: () => Photo | null;
  api: () => AppApi;
  dragging: () => boolean;
  holding: () => boolean;
  engine: () => Engine | null;
}): HdrPreviewController {
  return new HdrPreviewController({
    photo: () => host.photo(),
    session: () => host.api().session(),
    dragging: host.dragging,
    holding: host.holding,
    exportOn: () => loadExportHdr(),
    display: () => hdrDisplayCapable(host.engine()?.device ?? null),
    engine: host.engine,
    readDng: async (photo) => {
      const full = await host.api().readFull(photo as Photo);
      return full.ok ? full.bytes : null;
    },
    fit: (req) => hdrPreviewGrid(req.rgba, req.width, req.height, req.dng, req.jpg),
    watchExport: (fn) => watchExportHdr(fn),
  });
}
