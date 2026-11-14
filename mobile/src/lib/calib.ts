// 校准包的核对，照桌面 `camdata/calib.py` 的 `model_for` / `unit_status` 做：版本、单元、文件清单、SHA-256。
// 原厂 04 的指纹和建模型要等有了相机配方文件才查（见 recipes 的 unit 检查）。文案用桌面的原话。
// 纯函数，Worker 和 Node 测试都能跑；不抛，坏数据给原因。

export const SCHEMA = 'gxr-studio/calib/2';
export const UNITS = ['jlaunch', 'qlaunch', 'l08firm'] as const;
export type Unit = (typeof UNITS)[number];
export const UNIT_ISOS: Record<Unit, number[]> = { jlaunch: [200, 1600], qlaunch: [200], l08firm: [200] };
export const MODEL_ISOS = [200, 1600] as const;
/** 200 和 1600 的几何中点：低于它用 ISO 200 的模型（render/service.py 的 ISO_SPLIT）。 */
export const ISO_SPLIT = 566;
export const UNIT_LABEL: Record<Unit, string> = { jlaunch: '50mm', qlaunch: '28mm', l08firm: 'MOUNT' };

export const TEXT = {
  missing: '缺少校准数据',
  unitMissing: '缺少这个单元的校准数据',
  filesMissing: '校准数据文件缺失，请重新导入',
  changed: '校准数据被改动，请重新导入',
  badList: '校准数据的文件清单不正确',
  outdated: '校准数据版本已更新，请重新导入',
  unreadable: '校准数据读不出来，请重新导入',
  stock: '校准数据与这个单元的原厂 04 不匹配，请重新导入',
  units: '校准数据的单元不正确',
} as const;

export interface UnitPack {
  unit: Unit;
  stockSha256: string;
  /** ISO 分支（"200"、"1600"）→ { ctx, ring } 文件名。 */
  models: Record<string, { ctx: string; ring: string }>;
}

export type PackCheck =
  | { ok: true; units: UnitPack[]; manifestSha256: string }
  | { ok: false; message: string; detail: string };

function isUnit(name: string): name is Unit {
  return (UNITS as readonly string[]).includes(name);
}

function hex(buffer: ArrayBuffer): string {
  return [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

export async function sha256(bytes: Uint8Array): Promise<string> {
  const copy = new Uint8Array(bytes.length);
  copy.set(bytes);
  return hex(await crypto.subtle.digest('SHA-256', copy.buffer));
}

function plain(name: unknown): name is string {
  return typeof name === 'string' && !!name && !name.includes('/') && !name.includes('\\') && name !== '.' && name !== '..' && name !== 'manifest.json';
}

function bad(message: string, detail = ''): PackCheck {
  return { ok: false, message, detail };
}

/**
 * `files` 是 zip 里的全部条目。核对 manifest 和它列出的每个文件（大小、SHA-256），以及每个单元两个 ISO 分支的文件名。
 * 多出来的、清单外的文件不收。
 */
export async function checkPack(files: Map<string, Uint8Array>): Promise<PackCheck> {
  try {
    const raw = files.get('manifest.json');
    if (!raw) return bad(TEXT.missing, 'manifest.json');
    let manifest: unknown;
    try {
      manifest = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw));
    } catch {
      return bad(TEXT.unreadable, 'manifest.json');
    }
    if (!manifest || typeof manifest !== 'object' || (manifest as { schema?: unknown }).schema !== SCHEMA) return bad(TEXT.outdated);
    const m = manifest as { units?: unknown; files?: unknown };
    if (!m.files || typeof m.files !== 'object' || Array.isArray(m.files)) return bad(TEXT.badList, 'files');
    if (!m.units || typeof m.units !== 'object' || Array.isArray(m.units)) return bad(TEXT.badList, 'units');
    const listed = m.files as Record<string, unknown>;
    const unitRows = m.units as Record<string, unknown>;
    const names = Object.keys(unitRows);
    if (!names.length || !names.every(isUnit)) return bad(TEXT.units);
    for (const [name, meta] of Object.entries(listed)) {
      if (!plain(name) || !meta || typeof meta !== 'object') return bad(TEXT.badList, name);
      const blob = files.get(name);
      if (!blob) return bad(TEXT.filesMissing, name);
      const want = meta as { size?: unknown; sha256?: unknown };
      if (want.size !== blob.length || typeof want.sha256 !== 'string' || (await sha256(blob)) !== want.sha256) return bad(TEXT.changed, name);
    }
    const units: UnitPack[] = [];
    for (const unit of UNITS) {
      const row = unitRows[unit];
      if (row === undefined) continue;
      const meta = row as { camera_stock_sha256?: unknown; models?: unknown } | null;
      if (!meta || typeof meta !== 'object' || typeof meta.camera_stock_sha256 !== 'string') return bad(TEXT.badList, unit);
      const models = meta.models as Record<string, unknown> | undefined;
      if (!models || typeof models !== 'object') return bad(TEXT.badList, unit);
      const out: Record<string, { ctx: string; ring: string }> = {};
      for (const branch of MODEL_ISOS) {
        const pair = models[String(branch)] as { ctx?: unknown; ring?: unknown } | undefined;
        if (!pair || typeof pair !== 'object' || !plain(pair.ctx) || !plain(pair.ring)) return bad(TEXT.badList, `${unit}/${branch}`);
        if (!(pair.ctx in listed) || !(pair.ring in listed)) return bad(TEXT.badList, `${unit}/${branch}`);
        out[String(branch)] = { ctx: pair.ctx, ring: pair.ring };
      }
      units.push({ unit, stockSha256: meta.camera_stock_sha256, models: out });
    }
    return { ok: true, units, manifestSha256: await sha256(raw) };
  } catch {
    return bad(TEXT.unreadable);
  }
}

/** 照 `calib.model_for`：ISO ≥ 566 用 1600 的模型，否则（含读不到 ISO）用 200。 */
export function modelIso(iso: number | null): 200 | 1600 {
  return iso !== null && iso >= ISO_SPLIT ? 1600 : 200;
}

/** 照 `render/recipes.py` 的 `unit_of_lens`：先认 MOUNT，再认 50mm、28mm；认不出是 null。 */
export function unitOfLens(lens: string | null): Unit | null {
  const text = `${lens ?? ''} `.toLowerCase();
  if (text.includes('mount')) return 'l08firm';
  if (text.includes('50mm')) return 'jlaunch';
  if (text.includes('28mm')) return 'qlaunch';
  return null;
}

/**
 * 照 `RenderService.plan`：配方单元不是这张照片的单元、认不出单元、或 ISO 不在实拍校准范围里，都是「未校准」
 * （颜色照样用校准模型预测，只是标出来）。
 */
export function crossed(photoUnit: Unit | null, recipeUnit: Unit, iso: number | null): boolean {
  return photoUnit === null || iso === null || !UNIT_ISOS[recipeUnit].includes(iso) || recipeUnit !== photoUnit;
}

const TAIL = '颜色可能有些偏差。';

/** 桌面 `calibration_note.js` 的 `uncalibratedReason`（手机上没有 LUT 这一条）。 */
export function uncalibratedReason(photoUnit: Unit | null, recipeUnit: Unit, iso: number | null): string {
  if (!photoUnit) return `认不出这张照片用的镜头单元，按 A12 50mm 的颜色模型预测，${TAIL}`;
  const calibrated = UNIT_ISOS[photoUnit];
  if (iso === null || !calibrated.includes(iso)) {
    const known = calibrated.join(' 和 ISO ');
    return iso !== null && iso > 0
      ? `这张照片是 ISO ${iso}，颜色校准只做了 ISO ${known}，借用相近的一档，${TAIL}`
      : `读不到这张照片的 ISO，颜色校准只做了 ISO ${known}，借用相近的一档，${TAIL}`;
  }
  if (recipeUnit !== photoUnit) return `选的配方来自 ${UNIT_LABEL[recipeUnit]} 单元，不是这张照片的镜头，${TAIL}`;
  return `这张照片没有对应的颜色校准，${TAIL}`;
}

/** 起始页和「校准数据」里的一行：哪个镜头、哪几个 ISO。 */
export function unitLine(unit: Unit): string {
  return `${UNIT_LABEL[unit]} · ISO ${UNIT_ISOS[unit].join('、')}`;
}
