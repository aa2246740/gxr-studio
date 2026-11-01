// 共享的界面状态与事件。视图之间只通过这里的事件通信，不互相直接调用。

export const state = {
  settings: null,
  prefs: null, // /api/prefs：onboarding_dismissed、export_hdr
  offline: false,
  cards: [], // /api/cards 的结果
  summaries: new Map(), // 卡 id -> 摘要（只读统计）
  card: null, // 当前卡（列表第一张）
  cardSummary: null, // /api/cards/{id}/summary：这张卡上有几张、几张是新的
  cardAccess: null, // 系统不让读外接卡时的说明（null = 没问题）
  cardMsg: null, // 横幅里的一次性提示（例如"都导入过了"）
  offline: false, // 连不上本机服务
  job: null, // 最近一次导入任务（/api/jobs 的一项）
  dismissedJobId: null, // 用户点过「知道了」的导入任务
  viewerTab: 'crop', // 大图右侧记住的标签
  dates: [], // /api/dates
  galleryDate: null, // 图库当前筛选的日期，null = 全部
  photos: [], // 当前筛选下的照片（大图左右切换用）
  selection: new Set(), // 选中的照片 id
  camdata: null, // 相机数据状态：{ ready, state, message, generation }（见 camdata.js）
  catalog: null, // /api/recipes：3 个单元 × 8 个底子、每个底子的旋钮和档位
};

export const bus = new EventTarget();

export function emit(type, detail) {
  bus.dispatchEvent(new CustomEvent(type, { detail }));
}

export function on(type, fn) {
  bus.addEventListener(type, fn);
}
