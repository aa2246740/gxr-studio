// 编辑器上的 HDR 预览接线。拟合走专用 Worker，不进四份核心池。

import type { Engine, Look } from '../../../frontend/src/index';
import { toAdjust } from './adjust_view';
import { previewCurve } from './curves_view';
import { HdrFitHost } from '../core/hdr_fit_host';
import { HdrPreviewController } from '../core/hdr_preview';
import { PREVIEW_LONG_EDGE } from '../core/hdr_grid';
import { NEUTRAL_BAKE_KEY } from '../lib/neutral';
import { loadExportHdr, watchExportHdr } from '../lib/prefs';
import { hdrDisplayCapable } from '../core/hdr_display';
import { zeroKnobs, type AppApi, type Photo } from './model';

const IDENTITY = { straighten: 0, rotate90: 0, flip: false, vertical: 0, horizontal: 0, distortion: 0, box: null };

function needsSeparateBase(photo: Photo): boolean {
  if (Math.abs(photo.geometry?.straighten ?? 0) > 1e-9) return true;
  const rect = photo.crop?.rect;
  if (!rect) return false;
  return !(rect[0] === 0 && rect[1] === 0 && rect[2] === 1 && rect[3] === 1);
}

function geometryJson(photo: Photo | null): string | null {
  if (!photo) return null;
  return JSON.stringify({
    straighten: photo.geometry?.straighten ?? 0,
    vertical: 0,
    horizontal: 0,
    distortion: 0,
    rotate90: 0,
    flip: false,
  });
}

function cropJson(photo: Photo | null): string | null {
  if (!photo?.crop) return null;
  return JSON.stringify({ rect: photo.crop.rect, preset: photo.crop.preset });
}

/** blur_fill 交给 hdrtrack_frame，核心返回 frame_layout。其它边框盖在照片外面，增益仍按照片本身拟合。 */
function frameLayoutJson(photo: Photo | null): string | null {
  if (photo?.border?.style !== 'blur_fill') return null;
  return JSON.stringify({ width: 2, height: 2, items: [], bgfx: { kind: 'blur' } });
}

export function createHdrPreview(host: {
  photo: () => Photo | null;
  api: () => AppApi;
  dragging: () => boolean;
  holding: () => boolean;
  engine: () => Engine | null;
  onShowing?: () => void;
  recipeKey: () => string;
  lutToken: () => string | null;
  knobs: () => Photo['knobs'];
  mode: () => 'raw' | 'jpg' | 'still';
}): HdrPreviewController {
  const fits = new HdrFitHost();
  const look = (): Look => ({
    recipe: host.mode() === 'raw' ? (host.holding() ? NEUTRAL_BAKE_KEY : host.recipeKey()) : null,
    lut: host.holding() ? null : host.lutToken(),
    lutStrength: 1,
    curve: previewCurve(host.photo()?.curve, host.holding()),
    adjust: toAdjust(host.holding() ? zeroKnobs() : host.knobs()),
  });
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
    fit: (req) => fits.request(req),
    stopFit: () => fits.stop(),
    watchExport: (fn) => watchExportHdr(fn),
    onShowing: host.onShowing,
    frameLayout: () => frameLayoutJson(host.photo()),
    geometryJson: () => geometryJson(host.photo()),
    cropJson: () => cropJson(host.photo()),
    readBase: async () => {
      const photo = host.photo();
      const engine = host.engine();
      if (!photo || !engine || !needsSeparateBase(photo)) return null;
      const key = engine.currentKey();
      if (!key) return null;
      const out = await engine.thumb({ source: key, look: look(), geometry: IDENTITY, aspect: null, maxEdge: PREVIEW_LONG_EDGE });
      if (!out) return null;
      return { rgba: out.data, width: out.w, height: out.h };
    },
  });
}
