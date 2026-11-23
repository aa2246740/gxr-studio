// 手机横屏「存到相册」检查（WebKit 与 Chromium 驱动共用）。
// 真机 iPhone Safari 横屏时工具栏占掉高度（round-6 r4b），右边一列的「存到相册」露一半。要求：
// 844×390、932×430 和带工具栏时更矮的 844×300、932×330 下，「存到相册」不滚动就完整可见、没被盖住，
// 四个工具也完整可见，照片不被右边一列盖住；配方行可以在面板里滚动。
// Safari 26 起页面照常排到底部浮动工具栏下面（100% 高度、dvh 和 visualViewport 都不扣掉它），真机上是
// 「按整屏排版、底下一截被工具栏盖住」。所以除了把视口直接设成 300 / 330 高，还按整屏 390 / 430 排版、
// 只把顶上 300 / 330 算看得见（visibleH）再查一遍。
// 只量版式矩形，不读照片内容；输出里没有文件名。

const PHONE_SAFE = { top: 0, bottom: 21, left: 47, right: 47 };
const PHONE_MAX_SAFE = { top: 0, bottom: 21, left: 59, right: 59 };

export const PHONE_LANDSCAPE_SAVE_VIEWPORTS = [
  { label: '844x390', width: 844, height: 390, safe: PHONE_SAFE },
  { label: '932x430', width: 932, height: 430, safe: PHONE_MAX_SAFE },
  { label: '844x300', width: 844, height: 300, safe: PHONE_SAFE },
  { label: '932x330', width: 932, height: 330, safe: PHONE_MAX_SAFE },
  { label: '844x390-bar300', width: 844, height: 390, visibleH: 300, safe: PHONE_SAFE },
  { label: '932x430-bar330', width: 932, height: 430, visibleH: 330, safe: PHONE_MAX_SAFE },
];

async function measureLandscapePanel(page, visibleH) {
  return page.evaluate((visH) => {
    const r1 = (v) => Math.round(v * 10) / 10;
    const rect = (el) => {
      if (!el) return null;
      const b = el.getBoundingClientRect();
      return { x: r1(b.left), y: r1(b.top), w: r1(b.width), h: r1(b.height), right: r1(b.right), bottom: r1(b.bottom) };
    };
    const vw = window.innerWidth;
    const vh = Math.min(window.innerHeight, visH ?? window.innerHeight);
    // 元素在看得见的区域里、且中心和四条边的中点都打到它自己（没被别的东西盖住；圆角按钮不取四角）。
    const hitOk = (el) => {
      if (!el) return false;
      const b = el.getBoundingClientRect();
      if (b.width < 1 || b.height < 1) return false;
      const cx = (b.left + b.right) / 2;
      const cy = (b.top + b.bottom) / 2;
      const pts = [
        [cx, cy],
        [cx, b.top + 2],
        [cx, b.bottom - 2],
        [b.left + 2, cy],
        [b.right - 2, cy],
      ];
      return pts.every(([x, y]) => {
        if (x < 0 || y < 0 || x >= vw || y >= vh) return false;
        const hit = document.elementFromPoint(x, y);
        return Boolean(hit && (hit === el || el.contains(hit)));
      });
    };
    const inView = (r) => Boolean(r && r.x >= -0.5 && r.y >= -0.5 && r.right <= vw + 0.5 && r.bottom <= vh + 0.5);
    const save = document.querySelector('.editor .toolbar [data-act="export"]');
    const tools = [...document.querySelectorAll('.editor .toolbar [data-tool]')];
    const media = document.querySelector(
      '.editor .stage .pv canvas.gpu:not([hidden]), .editor .stage .pv canvas.still:not([hidden]), .editor .stage .pv img:not([hidden])',
    );
    let photo = null;
    if (media) {
      const b = media.getBoundingClientRect();
      const nw = media instanceof HTMLCanvasElement ? media.width : media.naturalWidth || 0;
      const nh = media instanceof HTMLCanvasElement ? media.height : media.naturalHeight || 0;
      let { left: x, top: y, width: w, height: h } = b;
      if (nw > 0 && nh > 0 && w > 0 && h > 0) {
        const s = Math.min(w / nw, h / nh);
        x += (w - nw * s) / 2;
        y += (h - nh * s) / 2;
        w = nw * s;
        h = nh * s;
      }
      photo = { x: r1(x), y: r1(y), w: r1(w), h: r1(h), right: r1(x + w), bottom: r1(y + h) };
    }
    const column = ['.editor > .nav', '.editor > .band', '.editor > .toolbar']
      .map((sel) => document.querySelector(`.screen${sel}`))
      .filter((el) => el && el.getBoundingClientRect().width > 0)
      .map((el) => el.getBoundingClientRect().left);
    const photoCenterHit = (() => {
      if (!photo) return null;
      const hit = document.elementFromPoint(photo.x + photo.w / 2, photo.y + photo.h / 2);
      return Boolean(hit && hit.closest('.editor .stage'));
    })();
    const bandBody = document.querySelector('.editor .band-body');
    const se = document.scrollingElement || document.documentElement;
    const editor = document.querySelector('.screen.editor');
    return {
      layout: document.querySelector('.app')?.getAttribute('data-layout') ?? null,
      collapsed: editor?.getAttribute('data-collapsed') ?? null,
      vw,
      vh,
      layoutH: window.innerHeight,
      save: rect(save),
      saveInView: inView(rect(save)),
      saveHit: hitOk(save),
      toolsInView: tools.length === 4 && tools.every((t) => inView(rect(t))),
      toolsHit: tools.length === 4 && tools.every((t) => hitOk(t)),
      photo,
      // 照片只要求在排版视口里：Safari 盖住的那一截谁也躲不开，这里查的是自己的面板和条不盖照片。
      photoInView: Boolean(photo && photo.x >= -0.5 && photo.y >= -0.5 && photo.right <= vw + 0.5 && photo.bottom <= window.innerHeight + 0.5),
      photoCenterHit,
      columnLeft: column.length ? r1(Math.min(...column)) : null,
      band: rect(document.querySelector('.screen.editor > .band')),
      bandBody: bandBody ? { h: r1(bandBody.clientHeight), scrollH: r1(bandBody.scrollHeight), overflowY: getComputedStyle(bandBody).overflowY } : null,
      pageScrollY: r1(window.scrollY),
      pageScrollH: r1(se.scrollHeight),
      editorScrollTop: editor ? r1(editor.scrollTop) : null,
      editorOverflow: editor ? r1(editor.scrollHeight - editor.clientHeight) : null,
    };
  }, visibleH ?? null);
}

// 换尺寸后舞台会按新尺寸重画：等照片矩形连续两次不变（最多 5 秒）再量。
async function measureSettled(page, frame, visibleH) {
  let prev = null;
  let cur = null;
  for (let i = 0; i < 20; i += 1) {
    cur = await measureLandscapePanel(page, visibleH);
    if (prev && JSON.stringify(prev.photo) === JSON.stringify(cur.photo) && JSON.stringify(prev.save) === JSON.stringify(cur.save)) return cur;
    prev = cur;
    await page.waitForTimeout(250);
    await frame(page, 2);
  }
  return cur;
}

/**
 * 在已打开的编辑器里，逐个横屏尺寸量「存到相册」、四个工具和照片。
 * ctx: { check, frame, setSafeArea, summary, prefix, shot }。summary 里按尺寸记下度量；shot(label, clip) 可选，
 * 给了就在量完后截一张（被 Safari 工具栏盖住的情形只截看得见的上面一截）。
 */
export async function auditPhoneLandscapeSave(page, { check, frame, setSafeArea, summary, prefix = 'L5', shot = null }) {
  for (const vp of PHONE_LANDSCAPE_SAVE_VIEWPORTS) {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    await setSafeArea(page, vp.safe);
    await page.waitForFunction(() => document.querySelector('.app')?.getAttribute('data-layout') === 'phone-landscape', null, { timeout: 10_000 });
    await frame(page, 4);
    if ((await page.locator('.screen.editor').getAttribute('data-collapsed')) === '1') {
      await page.locator('.editor .toolbar [data-tool="recipe"]').click();
      await frame(page, 3);
    }
    const m = await measureSettled(page, frame, vp.visibleH);
    summary[vp.label] = m;
    if (shot) await shot(vp.label, vp.visibleH ? { x: 0, y: 0, width: vp.width, height: vp.visibleH } : undefined);
    const tag = `${prefix} ${vp.label}`;
    check(m.layout === 'phone-landscape', `${tag}: layout=${m.layout}, want phone-landscape`);
    check(m.collapsed !== '1', `${tag}: panel is collapsed`);
    check(
      m.saveInView && m.saveHit,
      `${tag}: 存到相册 not fully visible (rect ${JSON.stringify(m.save)} in visible ${m.vw}x${m.vh} of ${m.vw}x${m.layoutH}, hit=${m.saveHit})`,
    );
    check(m.toolsInView && m.toolsHit, `${tag}: toolbar tools not fully visible (inView=${m.toolsInView}, hit=${m.toolsHit})`);
    check(
      m.pageScrollY === 0 && m.pageScrollH <= m.layoutH + 1 && m.editorScrollTop === 0 && m.editorOverflow <= 1,
      `${tag}: page or editor scrolls (scrollY=${m.pageScrollY}, scrollH=${m.pageScrollH}, editor overflow=${m.editorOverflow})`,
    );
    check(
      Boolean(m.photo && m.photoInView && m.photoCenterHit && m.columnLeft !== null && m.photo.right <= m.columnLeft + 0.5),
      `${tag}: photo covered or off-screen (photo ${JSON.stringify(m.photo)}, column x=${m.columnLeft}, centerHit=${m.photoCenterHit})`,
    );
    check(
      Boolean(m.bandBody && m.bandBody.h >= 1 && /auto|scroll/.test(m.bandBody.overflowY)),
      `${tag}: recipe panel has no scrollable room (${JSON.stringify(m.bandBody)})`,
    );
  }
}
