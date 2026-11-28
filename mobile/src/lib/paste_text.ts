// 复制 / 套用调整的字：六组的名字和说明、记住勾选、复制和套用的结果说成白话。
// 全部照桌面 `web/assets/paste_core.js`、`paste_batch.js`、`paste_edits.js` 的原话；测试读桌面的源码逐句核对，
// 桌面改了字这里的测试就会失败。纯函数，不碰界面，Node 测试直接跑。

export type GroupId = 'recipe' | 'adjust' | 'geometry' | 'crop' | 'frame' | 'stamp';

/** `PASTE_GROUPS`：[id, 名称, 说明]。前两组默认勾上。 */
export const PASTE_GROUPS: ReadonlyArray<readonly [GroupId, string, string]> = [
  ['recipe', '配方 / 风格', '配方、LUT'],
  ['adjust', '调整', '曝光、色温、对比度、主曲线和红绿蓝曲线等'],
  ['geometry', '几何', '拉直、透视、畸变（不含旋转和翻转）'],
  ['crop', '裁剪', '比例和裁剪框'],
  ['frame', '边框', '边框样式、成品比例'],
  ['stamp', '时间戳', '日期或文字戳'],
];
export const DEFAULT_GROUPS: GroupId[] = ['recipe', 'adjust'];
/** 和桌面同一个键：勾选记在这台设备的浏览器里。 */
export const GROUPS_KEY = 'gxr-studio.paste-groups';

export const groupName = (id: string): string => (PASTE_GROUPS.find((g) => g[0] === id) ?? [id, id])[1];

/** `paste_edits.js`：这张照片里没有设置的组，说明后面加这一句。 */
export const NOT_SET = '（这张没有设置）';

interface Store {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

function store(): Store | null {
  try {
    return (globalThis as { localStorage?: Store }).localStorage ?? null;
  } catch {
    return null;
  }
}

/** `chosenGroups`：存过的勾选（按六组的顺序），没存过或读不出来用默认。 */
export function chosenGroups(from: Store | null = store()): GroupId[] {
  try {
    const saved: unknown = JSON.parse(from?.getItem(GROUPS_KEY) ?? 'null');
    if (Array.isArray(saved)) return PASTE_GROUPS.map((g) => g[0]).filter((id) => saved.includes(id));
  } catch {
    /* 没存过或读不出来：用默认 */
  }
  return [...DEFAULT_GROUPS];
}

/** `rememberGroups`。隐私模式写不进去就算了。 */
export function rememberGroups(ids: readonly GroupId[], to: Store | null = store()): void {
  try {
    to?.setItem(GROUPS_KEY, JSON.stringify(ids));
  } catch {
    /* 写不进去就算了 */
  }
}

export interface CopyResult {
  copied: GroupId[];
  empty: GroupId[];
}

/** `copyMessage`。 */
export function copyMessage({ copied, empty }: CopyResult): string {
  if (!copied.length) return '这张照片在勾选的项目里没有修改，没有复制。';
  const names = copied.map(groupName).join('、');
  return empty.length ? `已复制：${names}。${empty.map(groupName).join('、')}这张没有设置，没有复制。` : `已复制：${names}`;
}

export interface Skip {
  group: GroupId;
  reason: string;
  message: string;
  part?: 'recipe' | 'lut';
}

export interface Outcome {
  id: string;
  status: 'applied' | 'partial' | 'skipped' | 'error';
  applied: GroupId[];
  skipped: Skip[];
  rev?: number;
  code?: string;
  message?: string;
}

type Line = (who: string, info: Skip & Partial<Outcome>) => string;

/** 结果句里半格双联的 needs_raw 换成这个原因，说「按相机拼好的双联图调整」。 */
export const DIPTYCH_NEEDS_RAW = 'diptych_needs_raw';

/** `SKIP_LINES`：跳过的原因 → 一句话。who 是「2 张」或（只有一张时）「这张」。 */
const SKIP_LINES: Record<string, Line> = {
  unit_mismatch: (who) => `${who}镜头单元不同，没有套用配方`,
  needs_raw: (who, s) => `${who}只有 JPG，没有套用${s.part === 'lut' ? ' LUT' : '配方'}`,
  /** 半格双联没有 RAW（Import P3-3）：不说「只有 JPG」。只在手机的结果句里用，桌面 `Outcome` 仍是 `needs_raw`。 */
  [DIPTYCH_NEEDS_RAW]: (who, s) => `${who}按相机拼好的双联图调整，没有套用${s.part === 'lut' ? ' LUT' : '配方'}`,
  lut_missing: (who) => `${who}找不到这个 LUT，没有套用 LUT`,
  recipe_rejected: (who) => `${who}用不了这个配方，没有套用配方`,
  not_copied: (who, s) => `${who}没有套用${groupName(s.group)}（复制时没带上）`,
};
const ERROR_LINES: Record<string, (who: string) => string> = {
  rev_conflict: (who) => `${who}在别处改过，没有粘贴`,
  photo_not_found: (who) => `${who}找不到，没有粘贴`,
  sidecar_too_new: (who) => `${who}的编辑来自更新的版本，没有粘贴`,
  sidecar_unreadable: (who) => `${who}的编辑记录读不了，没有粘贴`,
};

/** `pasteMessage`：已套用到几张，再逐类说明没套用的原因。单张照片时说得更直接。 */
export function pasteMessage(results: readonly Outcome[]): string {
  const used = results.filter((r) => r.applied.length);
  const single = results.length === 1;
  const reasons = new Map<string, { line: Line; info: Skip & Partial<Outcome>; n: number }>();
  const note = (key: string, line: Line, info: Skip & Partial<Outcome>): void => {
    const hit = reasons.get(key) ?? { line, info, n: 0 };
    hit.n += 1;
    reasons.set(key, hit);
  };
  for (const r of results) {
    for (const s of r.skipped) {
      note(`${s.reason}|${s.part ?? ''}|${s.group}`, SKIP_LINES[s.reason] ?? ((who) => `${who}没有套用${groupName(s.group)}：${s.message}`), s);
    }
    if (r.status === 'error') {
      const code = r.code ?? '';
      const line = ERROR_LINES[code] ?? ((who: string) => `${who}没有粘贴`);
      note(`error|${code}`, (who) => line(who), { group: 'recipe', reason: 'error', message: r.message ?? '' });
    }
  }
  const head = single ? (used.length ? `已套用：${used[0].applied.map(groupName).join('、')}` : '没有套用') : used.length ? `已套用到 ${used.length} 张` : '没有套用到任何一张';
  const tail = [...reasons.values()].map(({ line, info, n }) => line(single ? '这张' : `${n} 张`, info));
  return [head, ...tail].join('；');
}

export interface UndoCounts {
  restored?: number;
  changed?: number;
  error?: number;
}

/** `undoMessage`。 */
export function undoMessage(res: { counts?: UndoCounts }): string {
  const { restored = 0, changed = 0, error = 0 } = res.counts ?? {};
  const parts = [restored ? `已撤销 ${restored} 张` : '没有撤销任何一张'];
  if (changed) parts.push(`${changed} 张粘贴之后又改过，没有撤销`);
  if (error) parts.push(`${error} 张没有撤销成功`);
  return parts.join('；');
}

/** `paste_batch.js`：选中的只有复制来源这一张。 */
export const ONLY_SOURCE = '选中的只有复制来源这一张。请再选几张要粘贴的照片。';
/** 还没复制就点「套用到这一组」或「套用调整」。桌面 `pasteToSelection` 那句去掉了快捷键（经理审定）。 */
export const NOTHING_COPIED = '先打开一张照片，点「复制调整」。';
