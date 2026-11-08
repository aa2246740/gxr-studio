// 配方卡片的数据（纯函数，不碰界面和网络；tests/support/look_cards.mjs 在 Node 里测）：
// 说明表、哪些配方这张照片不能用（以及为什么）、卡片图的缓存键。

// 配方为哪个镜头单元做的，和这张照片是哪个单元拍的（短名，用在原因里）
const MADE_FOR = { jlaunch: 'A12 50mm', qlaunch: 'A12 28mm', l08firm: 'A12 MOUNT' };
const SHOT_WITH = { jlaunch: '50mm', qlaunch: '28mm', l08firm: 'MOUNT' };

export const LOOK_TEXT_MAX = 14; // 一行说明最多多少个字

// ---- 说明表：一个配方一行，只写画面上量得出来的 ----------------------------------------------------
// 数据来源：同一批 4 张真照片（50mm 和 MOUNT 的 RAW）套上每个配方，和「04 原厂标准」比，取 4 张的平均。
// 用词只有下面这几个，每个写明白什么时候用（比值 = 这个配方 ÷ 04）：
//   饱和度（平均 max-min 与 max 的比）：比值 ≥ 1.15 → 饱和度高；0.75–0.9 → 饱和度略低；≤ 0.75 → 饱和度低；平均饱和度 = 0 → 黑白。
//   对比（亮度标准差）：比值 ≥ 1.3 → 对比高（量到的只有槽位 3：1.39）；≤ 0.8 → 对比低。
//   明暗（0–255 的平均亮度差）：≥ +15 → 偏亮；≤ −15 → 偏暗。
//   冷暖（红减蓝的平均差）：≥ +5 → 偏暖。
// 量不出明显差别的（HNCS LowSat、Kodachrome25、instax mini）不写，也不按名字猜；没有一项达到阈值的不写那一项。
// 内置配方按位置（1–4 是 GR 的四个底子；5–8 是相机里留给用户改的空位，不写说明）。
export const BUILTIN_TEXT = {
  1: '饱和度高', // 饱和度 ×1.28；亮度 −11.7，不够「偏暗」
  2: '饱和度略低', // ×0.83；亮度 +8.7，不够「偏亮」
  3: '黑白，对比高', // 平均饱和度 0；对比 ×1.39
  4: '原厂标准色彩', // 比较的基准
};

// 相机配方文件里的 Looks 按名字。表里没有的名字不写说明（不猜）。
export const LOOK_TEXT = {
  'M9 STD': '饱和度高', // ×1.20；亮度 −11.2，不够「偏暗」
  'HNCS HiCon': '饱和度高', // ×1.18
  'Tri-X 400': '饱和度低', // ×0.60（不是黑白：平均饱和度 0.16）
  Acros: '黑白', // 平均饱和度 0
  'Portra 400': '饱和度略低', // ×0.87；对比 ×0.89，不够「对比低」
  'Velvia 50': '饱和度高', // ×1.17
  'Vision3 500T': '饱和度高，偏暗', // ×1.18；亮度 −22.6
  'Eterna BB': '饱和度低，偏暗', // ×0.52；−23.3
  'ARRI 709': '对比低，偏暗', // 对比 ×0.74；亮度 −34.2
  'RED IPP2': '对比低，偏暗', // ×0.79；−26.5
  'Agfa LeBox': '饱和度低，偏亮', // ×0.66；+19.5
  Metropolis: '饱和度低，对比低', // ×0.27（平均饱和度 0.07，不到黑白）；对比 ×0.71
  'Y2K Plady': '饱和度低，偏亮', // ×0.67；+23.3
  'QianXi A': '偏暖，饱和度低', // 红减蓝 +9.1；×0.68
  'QianXi B': '偏暖，饱和度低', // +6.1；×0.64
};

// 一张卡片下面的小字。item：{ kind: 'builtin' | 'look' | 'camera_user' | 'computer', slot, name, source }
export function describeLook(item) {
  if (item.kind === 'builtin') return BUILTIN_TEXT[item.slot] || null;
  if (item.kind === 'look') return LOOK_TEXT[item.name] || null;
  if (item.kind === 'camera_user') return '相机里的配方';
  if (item.kind === 'computer') {
    const title = item.source && item.source.title;
    return title ? `来自 ${title}` : '用 LUT 生成';
  }
  return null;
}

// 为别的镜头单元做的配方，为什么这张照片用不了
export function otherUnitReason(recipeUnit, photoUnit) {
  const made = MADE_FOR[recipeUnit] || '别的镜头';
  if (!photoUnit) return `为 ${made} 做的配方；认不出这张照片的镜头`;
  return `为 ${made} 做的配方；这张是 ${SHOT_WITH[photoUnit] || photoUnit} 拍的`;
}

// 电脑上的配方分组：这个单元的，和别的单元的（灰着列出，写清原因）
export function groupComputer(mine, unit, photoUnit) {
  const usable = [];
  const other = [];
  for (const r of mine || []) {
    if (r.recipe.unit === unit) usable.push(r);
    else other.push({ recipe: r, unit: r.recipe.unit, reason: otherUnitReason(r.recipe.unit, photoUnit) });
  }
  return { usable, other };
}

// ---- 缓存键 ------------------------------------------------------------------------------
// 一张卡片的图由这些决定：照片、相机数据版本、画法（显卡 / 服务器）、成片显示的宽高比、小图长边、
// 这张卡片的配方（底子和档位）、LUT、调整、曲线、几何、裁切。
// 不在里面的（换标签、选中哪张卡、边框、时间戳、别的卡片的档位、撤销记录）不会让卡片重画。
export function sharedKey(ctx) {
  return JSON.stringify([ctx.photoId, ctx.dataVersion ?? '', ctx.way, ctx.aspect ?? null, ctx.edge, ctx.lut || null, ctx.adjust, ctx.curve || null, ctx.geometry || null, ctx.crop || null]);
}

export function cardKey(shared, recipe) {
  return `${shared}|${JSON.stringify(recipe)}`;
}

// 一小块最近用过的缓存（Map 的插入顺序当作使用顺序）
export class Lru {
  constructor(max) {
    this.max = max;
    this.map = new Map();
  }

  get(key) {
    if (!this.map.has(key)) return undefined;
    const v = this.map.get(key);
    this.map.delete(key);
    this.map.set(key, v);
    return v;
  }

  set(key, value) {
    this.map.delete(key);
    this.map.set(key, value);
    while (this.map.size > this.max) this.map.delete(this.map.keys().next().value);
  }

  clear() {
    this.map.clear();
  }

  get size() {
    return this.map.size;
  }
}
