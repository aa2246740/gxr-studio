// 真浏览器（固定 Playwright 运行时）：换照片后编辑记录还在读时点「粘贴调整」「选择复制哪些内容」，点击不能丢。
// 编辑记录的 GET 由脚本扣住、再放行（或答 500），时序是确定的。只输出一行 JSON，断言在 tests/test_paste_record_race_browser.py。
import path from 'node:path';
import { writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const [runtime, port, token, idsJson, namesJson, outDir] = process.argv.slice(2);
const ids = JSON.parse(idsJson); // [photo1, photo2, photo3]
const names = JSON.parse(namesJson); // 合成假卡的文件名，只用来点胶片条
const { launchPinnedChromium } = await import(pathToFileURL(runtime).href);
const browser = await launchPinnedChromium({ args: ['--disable-gpu'] });
const out = { errors: [], scenarios: {} };
const WAIT_MS = 8000; // 放行之后，应有的粘贴请求 / 对话框 / 提示在这么久内出现

let page = null;
try {
  page = await browser.newPage({ viewport: { width: 1366, height: 860 } });
  page.on('pageerror', (e) => out.errors.push(String(e.message).split('\n')[0]));
  await page.addInitScript(() => {
    try { Object.defineProperty(navigator, 'gpu', { value: undefined, configurable: true }); } catch { /* ignore */ }
  });

  // 编辑记录 GET：hold 指定的那张时扣住，直到 release('ok' | 'fail')
  const edGets = [];
  let hold = null;
  const holdRecord = (id) => {
    let release;
    const gate = new Promise((r) => { release = r; });
    let arrived;
    const seen = new Promise((r) => { arrived = r; });
    hold = { id, gate, release, arrived, seen };
    return hold;
  };
  await page.route((u) => /^\/api\/photos\/[^/]+\/edit$/.test(u.pathname), async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    const id = decodeURIComponent(new URL(route.request().url()).pathname.split('/')[3]);
    edGets.push(id);
    const h = hold && hold.id === id ? hold : null;
    if (h) {
      h.arrived();
      if ((await h.gate) === 'fail') return route.fulfill({ status: 500, json: { error: 'test_unreadable' } });
    }
    return route.fallback();
  });
  const pastes = [];
  page.on('request', (rq) => {
    if (new URL(rq.url()).pathname === '/api/edits/paste') pastes.push(rq.postDataJSON()?.targets?.map((t) => t.id) || []);
  });

  const waitTrue = async (fn, ms = WAIT_MS) => {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      if (await fn()) return true;
      await page.waitForTimeout(50);
    }
    return false;
  };
  // page.waitForFunction 不等页面里的 Promise（返回 Promise 就当成立），要等异步条件用 evaluate 轮询
  const waitPage = async (fn, label, ms = 30000) => {
    if (!(await waitTrue(() => page.evaluate(fn), ms))) throw new Error(`timeout waiting for ${label}`);
  };
  const tb = (sel) => page.locator(`#tb-edit ${sel}`);
  const busyOf = () => page.evaluate(() => [...document.querySelectorAll('#tb-edit [data-paste="1"], #tb-edit button[title="选择复制哪些内容"]')]
    .map((b) => b.getAttribute('aria-busy')));
  const edLoaded = () => page.evaluate(() => import('/assets/viewer.js').then((v) => !!v.currentView()?.ed));
  const flush = (id) => page.evaluate(async (pid) => {
    const { flushPhotos } = await import('/assets/saves.js');
    await flushPhotos([pid]);
  }, id);
  const errToasts = () => page.locator('#toast-root .toast.err').count();
  // 像用户那样点胶片条换照片，等到屏幕上换成新照片的顶栏
  const switchTo = async (i) => {
    const old = await tb('[data-paste="1"]').elementHandle();
    await page.locator(`.fthumb[aria-label="${names[i]}"]`).click();
    await page.waitForFunction((o) => {
      const b = document.querySelector('#tb-edit [data-paste="1"]');
      return !!b && b !== o;
    }, old);
  };

  await page.goto(`http://127.0.0.1:${port}/?token=${token}#/photo/${encodeURIComponent(ids[0])}`);
  // 打开好了：记录和裁剪都读完（大图这一次的读取结束，顶栏去掉 aria-busy；旧版本没有 aria-busy，只看记录）
  await waitPage(() => import('/assets/viewer.js').then(async (v) => {
    const c = v.currentView();
    if (!c?.ed || !c.crop) return false;
    if (c.ready) await c.ready;
    return !!document.querySelector('#tb-edit [data-paste="1"]');
  }), 'photo 1 open');
  out.busyWhenLoaded = await busyOf();

  // 准备剪贴板：给第 1 张一个边框，用复制选项只复制边框
  await page.getByRole('tab', { name: '边框' }).first().click();
  await page.locator('.fcard[data-style="leica"]').click();
  await tb('button[title="选择复制哪些内容"]').click();
  const dlg = page.locator('#modal-root .dialog');
  await dlg.waitFor({ state: 'visible' });
  const box = dlg.locator('input[type="checkbox"][data-group="frame"]');
  if (!(await box.isChecked())) await box.check();
  await dlg.getByRole('button', { name: '复制', exact: true }).click();
  await tb('[data-paste="1"]:not([aria-disabled="true"])').waitFor();
  await flush(ids[0]);

  // A) 粘贴：记录还在读时点，放行后照样粘贴一次，且只读了一次记录
  {
    const h = holdRecord(ids[1]);
    const gets0 = edGets.length;
    const pastes0 = pastes.length;
    await switchTo(1);
    await h.seen;
    const busy = await busyOf();
    const loadedAtClick = await edLoaded();
    await tb('[data-paste="1"]').click();
    await page.waitForTimeout(300);
    const pastedWhileHeld = pastes.length - pastes0;
    h.release('ok');
    const sent = await waitTrue(async () => pastes.length > pastes0);
    await page.waitForTimeout(500);
    out.scenarios.paste = {
      loadedAtClick, busy, pastedWhileHeld, sent, pasteCount: pastes.length - pastes0,
      target: pastes.at(-1)?.[0] === ids[1], recordGets: edGets.slice(gets0).filter((x) => x === ids[1]).length,
      busyAfter: await busyOf(),
    };
    hold = null;
    await flush(ids[1]);
  }

  // B) 复制选项：记录还在读时点，放行后对话框打开
  {
    const h = holdRecord(ids[2]);
    await switchTo(2);
    await h.seen;
    const loadedAtClick = await edLoaded();
    await tb('button[title="选择复制哪些内容"]').click();
    await page.waitForTimeout(300);
    const openWhileHeld = await dlg.isVisible();
    h.release('ok');
    const opened = await waitTrue(() => dlg.isVisible());
    out.scenarios.options = { loadedAtClick, openWhileHeld, opened };
    if (opened) await dlg.getByRole('button', { name: '取消', exact: true }).click();
    hold = null;
  }

  // C) 等的时候换走：粘贴不做（不粘到原来那张，也不粘到新的这张）
  {
    const h = holdRecord(ids[1]);
    const pastes0 = pastes.length;
    const toasts0 = await page.locator('#toast-root > *').count();
    const err0 = await errToasts();
    await switchTo(1);
    await h.seen;
    await tb('[data-paste="1"]').click();
    await switchTo(2);
    await waitPage(() => import('/assets/viewer.js').then((v) => !!v.currentView()?.ed), 'photo 3 record');
    h.release('ok');
    await page.waitForTimeout(1500);
    out.scenarios.switchAway = {
      pasteCount: pastes.length - pastes0,
      newErrors: (await errToasts()) - err0,
      newToasts: (await page.locator('#toast-root > *').count()) - toasts0,
    };
    hold = null;
  }

  // D) 读取失败：点了之后说出读不出来，不粘贴
  {
    const h = holdRecord(ids[1]);
    const pastes0 = pastes.length;
    const err0 = await errToasts();
    await switchTo(1);
    await h.seen;
    await tb('[data-paste="1"]').click();
    h.release('fail');
    const shown = await waitTrue(async () => (await errToasts()) > err0);
    out.scenarios.failed = {
      shown,
      text: shown ? (await page.locator('#toast-root .toast.err .toast-msg').last().innerText()).slice(0, 80) : null,
      pasteCount: pastes.length - pastes0,
      busyAfter: await busyOf(),
    };
    hold = null;
  }
  console.log(JSON.stringify(out));
} catch (e) {
  // 超时时把顶栏按钮状态留在临时目录（不截图：桌面页面会显示照片名）
  try {
    const state = await page?.evaluate(() => ({
      hash: location.hash.slice(-12),
      buttons: [...document.querySelectorAll('#tb-edit button')].map((b) => ({
        title: (b.getAttribute('title') || '').slice(0, 12), busy: b.getAttribute('aria-busy'), disabled: b.getAttribute('aria-disabled'),
      })),
    }));
    writeFileSync(path.join(outDir, 'race-failure.json'), JSON.stringify({ error: String(e.message).split('\n')[0], state, out }));
  } catch { /* ignore */ }
  console.log(JSON.stringify({ ...out, error: String(e.message).split('\n')[0] }));
} finally {
  if (page) await page.unrouteAll({ behavior: 'ignoreErrors' });
  await browser.close();
}
