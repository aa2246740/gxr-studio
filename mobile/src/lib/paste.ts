// 把一张照片的编辑套用到别的照片，以及把一批撤销回去。照桌面 `render/paste.py` 的 `paste_one`、`PasteUndo`、`restore`：
// - 配方组：镜头单元不同 → unit_mismatch；只有 JPG → needs_raw（近似模式关着，`recipe_without_raw` 是 false）；
//   这份相机配方文件里这个单元没有这个配方 → recipe_rejected；否则换成这个配方和它的档位；来源的 memory 不是空的就整份跟过来，
//   是空的就保留目标原来的（`render/edits.write`：请求里没有 memory 键时沿用 sidecar 里的）。
// - 调整组：全部换成复制来的调整，0 也算。
// - 复制时没带上的组 → not_copied。配方组要么整个套用，要么整个不套用。
// - 撤销只在照片还停在套用后的那一版（rev）时才还原；同一批里套到同一张两次，还原到最早的样子；只留最近 20 批。
// 纯函数，不碰界面和 WASM：镜头单元、配方列表由调用方给，Node 测试直接跑。

import type { Choice } from './recipes';
import type { Unit } from './calib';
import type { GroupId, Outcome, Skip, UndoCounts } from './paste_text';
import type { Knobs } from '../app/model';

export const MAX_UNDO_BATCHES = 20;

export type KnobMap = Knobs;
export type Levels = Map<string, Record<string, number>>;

/** 一张照片这次会话里的编辑状态（`Photo` 的这几项）。 */
export interface Editable {
  id: string;
  rev: number;
  /** 文件的版本（重新选同一张照片就加一）。没有就不核对。 */
  version?: number;
  choice: Choice | null;
  /** 选中的 LUT（cube_id），null 表示不用 LUT。 */
  lut?: string | null;
  /** 选中的配方的档位（桌面 recipe.levels），键是 choice.id。 */
  levels: Levels;
  /** 桌面的 memory：切走的配方各自记着的档位。 */
  memory: Levels;
  knobs: KnobMap;
}

/** 剪贴板：复制那一刻的深拷贝，之后来源再改也不影响。 */
export interface Clip {
  groups: GroupId[];
  from: { id: string; stem: string };
  /** levels 是选中那个配方的档位；memory 是空的时套用不动目标的 memory（桌面 recipeBody 只在非空时带 memory）。 */
  recipe: { choice: Choice; levels: Record<string, number>; memory: Levels } | null;
  lut?: string | null;
  knobs: KnobMap | null;
}

/** 目标照片能不能用的判断依据。 */
export interface Target {
  photo: Editable;
  /** 有没有 DNG（桌面 `"dng" in files`）。 */
  raw: boolean;
  /** 照片的镜头单元（`unitOfLens`），认不出是 null：桌面这时不算单元不同。 */
  unit: Unit | null;
  /** 点「套用」那一刻的文件版本和编辑版本（不给就取这一批开始时的）。提交前核对，变了就不写。 */
  version?: number;
  rev?: number;
}

/** 桌面 `render/paste.py` 的 `_failed` 和 `api/paste_routes.py`：这一张没处理，结果里记成 error。 */
function failed(id: string, code: 'rev_conflict' | 'photo_not_found'): Outcome {
  const message = code === 'rev_conflict' ? '这张照片已在别处修改，没有粘贴。' : '找不到这张照片。';
  return { id, status: 'error', applied: [], skipped: [], code, message };
}

export interface Env {
  /** 这份相机配方文件里，这个单元列出了这张卡没有。 */
  listed(unit: Unit, cardId: string): boolean;
  /** 只有 JPG 能不能用配方。近似模式关着（`finish/approx.py` ENABLED），所以是 false。 */
  recipeWithoutRaw: boolean;
  /** 这个 LUT 需要 RAW 吗（非显示空间）；图库里已经删掉了就返回 `'missing'`。 */
  lutNeedsRaw?: (cubeId: string) => boolean | 'missing';
}

/** 套用之前这张照片的样子（撤销用）。 */
export interface Before {
  choice: Choice | null;
  lut?: string | null;
  levels: Levels;
  memory: Levels;
  knobs: KnobMap;
}

export function cloneLevels(levels: Levels): Levels {
  const out: Levels = new Map();
  for (const [key, value] of levels) out.set(key, { ...value });
  return out;
}

export function snapshot(photo: Editable): Before {
  return {
    choice: photo.choice ? { ...photo.choice } : null,
    lut: photo.lut ?? null,
    levels: cloneLevels(photo.levels),
    memory: cloneLevels(photo.memory),
    knobs: { ...photo.knobs },
  };
}

/** 配方组「设置过」：选了配方或 LUT。 */
export function recipeIsSet(photo: Editable): boolean {
  return photo.choice !== null || (photo.lut ?? null) !== null;
}

/** 调整组「设置过」：有一项调整不是 0（手机上还没有曲线）。 */
export function adjustIsSet(photo: Editable): boolean {
  return Object.values(photo.knobs).some((value) => Number(value) !== 0);
}

/** 这张照片里有设置、可以复制的组（`groupsPresent`）。几何、裁剪、边框、时间戳在 M4 之前设不了。 */
export function groupsPresent(photo: Editable): GroupId[] {
  const out: GroupId[] = [];
  if (recipeIsSet(photo)) out.push('recipe');
  if (adjustIsSet(photo)) out.push('adjust');
  return out;
}

/** `copyFromView`：勾选的、这张照片里确实有设置的组进剪贴板。没有可复制的就不给剪贴板。 */
export function copyFrom(photo: Editable, stem: string, groups: readonly GroupId[]): { clip: Clip | null; copied: GroupId[]; empty: GroupId[] } {
  const present = new Set(groupsPresent(photo));
  const copied = groups.filter((id) => present.has(id));
  const empty = groups.filter((id) => !present.has(id));
  if (!copied.length) return { clip: null, copied, empty };
  const hasRecipe = copied.includes('recipe');
  const clip: Clip = {
    groups: copied,
    from: { id: photo.id, stem },
    recipe:
      hasRecipe && photo.choice
        ? { choice: { ...photo.choice }, levels: { ...(photo.levels.get(photo.choice.id) ?? {}) }, memory: cloneLevels(photo.memory) }
        : null,
    lut: hasRecipe ? (photo.lut ?? null) : null,
    knobs: copied.includes('adjust') ? { ...photo.knobs } : null,
  };
  return { clip, copied, empty };
}

/** 剪贴板里有没有这一组的内容（桌面 `GROUP_KEYS` 里的键在不在来源里）。 */
function carries(clip: Clip, group: GroupId): boolean {
  if (group === 'recipe') return clip.recipe !== null || (clip.lut ?? null) !== null;
  if (group === 'adjust') return clip.knobs !== null;
  return false;
}

interface Changes {
  recipe?: { choice: Choice | null; levels: Levels; memory: Levels | null };
  lut?: string | null;
  knobs?: KnobMap;
}

function recipeChanges(target: Target, clip: Clip, env: Env, skips: Skip[]): Changes {
  const out: Changes = {};
  const recipe = clip.recipe;
  if (!recipe) {
    out.recipe = { choice: null, levels: new Map(), memory: null };
  } else if (target.unit !== null && recipe.choice.unit !== target.unit) {
    skips.push({ group: 'recipe', reason: 'unit_mismatch', message: '这张照片的镜头和复制来源不是同一个，没有套用配方。', part: 'recipe' });
  } else if (!target.raw && !env.recipeWithoutRaw) {
    skips.push({ group: 'recipe', reason: 'needs_raw', message: '这张照片只有 JPG，没有套用配方。', part: 'recipe' });
  } else if (!env.listed(recipe.choice.unit, recipe.choice.id)) {
    skips.push({ group: 'recipe', reason: 'recipe_rejected', message: '这张照片用不了这个配方，没有套用。', part: 'recipe' });
  } else {
    out.recipe = {
      choice: { ...recipe.choice },
      levels: new Map([[recipe.choice.id, { ...recipe.levels }]]),
      memory: recipe.memory.size ? cloneLevels(recipe.memory) : null,
    };
  }
  const lut = clip.lut ?? null;
  if (lut === null) {
    out.lut = null;
  } else {
    const needsRaw = env.lutNeedsRaw ? env.lutNeedsRaw(lut) : 'missing';
    if (needsRaw === 'missing') {
      skips.push({ group: 'recipe', reason: 'lut_missing', message: '图库里找不到这个 LUT，没有套用 LUT。', part: 'lut' });
    } else if (needsRaw && !target.raw) {
      skips.push({ group: 'recipe', reason: 'needs_raw', message: '这个 LUT 需要 RAW 文件，这张照片只有 JPG，没有套用 LUT。', part: 'lut' });
    } else {
      out.lut = lut;
    }
  }
  return out;
}

/** `paste_one` 的判断：返回结果和要改的内容，不改照片。 */
export function planOne(target: Target, clip: Clip, groups: readonly GroupId[], env: Env): { outcome: Outcome; changes: Changes } {
  const skips: Skip[] = [];
  for (const group of groups) {
    if (!carries(clip, group)) skips.push({ group, reason: 'not_copied', message: '复制时没有带上这一项。' });
  }
  const changes: Changes = {};
  if (groups.includes('recipe') && carries(clip, 'recipe')) {
    const before = skips.length;
    const part = recipeChanges(target, clip, env, skips);
    // 配方（+ LUT）是一个整体的「风格」：有一半用不了，这一组整个不套用。
    if (skips.length === before) Object.assign(changes, part);
  }
  if (groups.includes('adjust') && clip.knobs) changes.knobs = { ...clip.knobs };
  const done = new Set<GroupId>();
  if (changes.recipe !== undefined || changes.lut !== undefined) done.add('recipe');
  if (changes.knobs) done.add('adjust');
  const applied = groups.filter((g) => done.has(g));
  const whole = applied.length === groups.length && !skips.length;
  const status = whole ? 'applied' : applied.length ? 'partial' : 'skipped';
  return { outcome: { id: target.photo.id, status, applied, skipped: skips }, changes };
}

/** 套用到一张：改了东西就加 rev，返回结果和套用之前的样子（没改就是 null）。 */
export function pasteOne(target: Target, clip: Clip, groups: readonly GroupId[], env: Env): { outcome: Outcome; before: Before | null } {
  const { outcome, changes } = planOne(target, clip, groups, env);
  if (changes.recipe === undefined && changes.lut === undefined && !changes.knobs) return { outcome, before: null };
  const photo = target.photo;
  const before = snapshot(photo);
  if (changes.recipe) {
    photo.choice = changes.recipe.choice;
    photo.levels = changes.recipe.levels;
    // 桌面 memory：来源记着的整份换过来；来源是空的就留着目标自己的。
    if (changes.recipe.memory) photo.memory = changes.recipe.memory;
  }
  if (changes.lut !== undefined && ('lut' in photo || changes.lut !== null)) {
    photo.lut = changes.lut;
  }
  if (changes.knobs) Object.assign(photo.knobs, changes.knobs);
  photo.rev += 1;
  outcome.rev = photo.rev;
  return { outcome, before };
}

export interface UndoEntry {
  /** 套用之后的版本：撤销只在照片还是这一版时才还原。 */
  rev: number;
  before: Before;
}

/** 最近几批套用的「套用之前」。只在内存里：重新打开页面后不能再撤销（桌面重启后也不能）。 */
export class PasteUndo {
  private readonly batches = new Map<string, Map<string, UndoEntry>>();
  private readonly limit: number;

  constructor(limit = MAX_UNDO_BATCHES) {
    this.limit = limit;
  }

  add(batchId: string, photoId: string, rev: number, before: Before): void {
    let batch = this.batches.get(batchId);
    if (batch) this.batches.delete(batchId);
    else batch = new Map();
    this.batches.set(batchId, batch);
    const kept = batch.get(photoId);
    // 同一批里再次套用到同一张：还原到最早的样子。
    batch.set(photoId, { rev, before: kept ? kept.before : before });
    while (this.batches.size > this.limit) {
      const oldest = this.batches.keys().next().value;
      if (oldest === undefined) break;
      this.batches.delete(oldest);
    }
  }

  take(batchId: string): Map<string, UndoEntry> | null {
    const batch = this.batches.get(batchId) ?? null;
    this.batches.delete(batchId);
    return batch;
  }

  has(batchId: string): boolean {
    return this.batches.has(batchId);
  }

  size(): number {
    return this.batches.size;
  }

  clear(): void {
    this.batches.clear();
  }
}

export type RestoreStatus = 'restored' | 'changed' | 'error';

/** `restore`：照片还停在套用后的那一版才还原（并加 rev），否则什么都不改。 */
export function restore(photo: Editable | undefined, entry: UndoEntry): RestoreStatus {
  if (!photo) return 'error';
  if (photo.rev !== entry.rev) return 'changed';
  photo.choice = entry.before.choice ? { ...entry.before.choice } : null;
  if ('lut' in photo || entry.before.lut !== undefined) photo.lut = entry.before.lut ?? null;
  photo.levels = cloneLevels(entry.before.levels);
  photo.memory = cloneLevels(entry.before.memory);
  for (const key of Object.keys(photo.knobs) as Array<keyof Knobs>) photo.knobs[key] = entry.before.knobs[key] ?? 0;
  photo.rev += 1;
  return 'restored';
}

/** `undo_batch`：整批还原，数出还原了几张、改过的几张、找不到的几张。 */
export function undoBatch(entries: Map<string, UndoEntry>, find: (id: string) => Editable | undefined): { counts: Required<UndoCounts>; results: Array<{ id: string; status: RestoreStatus }> } {
  const counts = { restored: 0, changed: 0, error: 0 };
  const results: Array<{ id: string; status: RestoreStatus }> = [];
  for (const [id, entry] of entries) {
    const status = restore(find(id), entry);
    counts[status] += 1;
    results.push({ id, status });
  }
  return { counts, results };
}

/**
 * 一批里逐张套用。按时间片做：每片最多 `budgetMs`，片与片之间让出主线程（`yieldFn`），500 张也不会一帧超过 8 ms。
 * `alive()` 是假的时候（清空了照片）就停下，已经套用的照样记进撤销。
 */
export async function pasteBatch(
  targets: readonly Target[],
  clip: Clip,
  groups: readonly GroupId[],
  env: Env,
  undo: PasteUndo,
  batchId: string,
  options: {
    budgetMs?: number;
    now?: () => number;
    yieldFn?: () => Promise<void>;
    alive?: () => boolean;
    onSlice?: (ms: number) => void;
    /** 这张照片现在是哪个对象（清空、去掉、换了对象就是 undefined 或别的对象）。不给就认为一直是同一个。 */
    current?: (id: string) => Editable | undefined;
  } = {},
): Promise<Outcome[]> {
  const budget = options.budgetMs ?? 4;
  const now = options.now ?? (() => performance.now());
  const pause = options.yieldFn ?? (() => new Promise<void>((done) => setTimeout(done, 0)));
  const alive = options.alive ?? (() => true);
  const outcomes: Outcome[] = [];
  // 每张照片这一批预期的版本：开始时的（或点套用那一刻的）；这一批自己写过一次就换成写完的 rev。
  const expected = new Map<string, { version: number | null; rev: number }>();
  for (const target of targets) {
    if (!expected.has(target.photo.id)) expected.set(target.photo.id, { version: target.version ?? target.photo.version ?? null, rev: target.rev ?? target.photo.rev });
  }
  const stale = (target: Target): Outcome | null => {
    const now = options.current ? options.current(target.photo.id) : target.photo;
    if (!now || now !== target.photo) return failed(target.photo.id, 'photo_not_found');
    const want = expected.get(target.photo.id);
    if (!want || (now.version ?? null) !== want.version || now.rev !== want.rev) return failed(target.photo.id, 'rev_conflict');
    return null;
  };
  let i = 0;
  while (i < targets.length) {
    if (!alive()) break;
    const start = now();
    do {
      const target = targets[i];
      i += 1;
      if (target.photo.id === clip.from.id) continue; // 来源这张从来不是目标
      // 片与片之间这张被改过、重新选过、去掉或换了对象：不写，不记撤销，按桌面的 rev_conflict / photo_not_found 计数。
      const gone = stale(target);
      if (gone) {
        outcomes.push(gone);
        continue;
      }
      const { outcome, before } = pasteOne(target, clip, groups, env);
      if (before && outcome.rev !== undefined) {
        undo.add(batchId, target.photo.id, outcome.rev, before);
        const want = expected.get(target.photo.id);
        if (want) want.rev = outcome.rev;
      }
      outcomes.push(outcome);
    } while (i < targets.length && now() - start < budget);
    options.onSlice?.(now() - start);
    if (i < targets.length) await pause();
  }
  return outcomes;
}
