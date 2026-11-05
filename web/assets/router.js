// 路由：#/photos、#/photo/<id>、#/recipes、#/camera。
// 每次进路由加一代（R11）。页面的 enter() 拿到 isCurrent()，每次 await 回来、改页面之前先核对；
// enter() 返回的快捷键也只在仍是当前路由时才装上。过时的慢响应不能把页面换回去。

export function parseHash(hash) {
  const h = hash || '#/photos';
  const m = /^#\/photo\/([0-9a-f]+)$/.exec(h);
  if (m) return { name: 'viewer', id: m[1] };
  if (h === '#/recipes') return { name: 'recipes' };
  if (h === '#/camera') return { name: 'camera' };
  return { name: 'photos' };
}

// pages：{ name: { enter(route, isCurrent) → 快捷键对象或 null, leave() } }
// before(route)：每次进路由先做的事（清提示、改导航高亮）
export function createRouter({ pages, before = () => {}, getHash = () => location.hash }) {
  let gen = 0;
  let current = null;
  let keys = null;

  async function route() {
    const g = ++gen;
    const isCurrent = () => g === gen;
    const r = parseHash(getHash());
    // 换到别的页面才清掉快捷键。大图里换照片时留着上一张的：新的一张还在加载时连按方向键不会被吃掉（B1）。
    // 页面自己决定加载中哪些键还能用（大图只放行方向键）
    if (current !== r.name) keys = null;
    if (current && current !== r.name) pages[current]?.leave?.();
    current = r.name;
    before(r);
    const k = await pages[r.name].enter(r, isCurrent);
    if (isCurrent()) keys = k || null;
    return isCurrent();
  }

  return {
    route,
    keys: () => keys,
    current: () => current,
    generation: () => gen,
  };
}
