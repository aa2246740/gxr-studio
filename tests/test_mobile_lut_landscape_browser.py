"""手机横屏 LUT 面板适配与 320 竖屏导入报错间距的 WebKit 回归验收（`browser_serial`）。

覆盖：
1. 手机横屏（844×390 与 956×440，含安全区与无安全区）在空 LUT 库和导入 3 个 LUT 两种状态下：
   - 「不用 LUT」卡、全部已导入 LUT 卡、导入按钮、色彩空间按钮和空库说明均完整落在视口与 `.panel.lut` 框内；
   - LUT 面板内任何元素不与「配方 | LUT」切换键或照片舞台重叠，且各交互热区 ≥ 44pt；
   - 合并菜单瓷砖（`.lut-menu-tile`）在横屏下保持隐藏，不出现重复的导入按钮。
2. 320×568 手机竖屏空库状态下导入损坏的 `.cube` 文件时，报错条（`.lut-err`）底沿与空库说明（`.lut-foot`）顶沿之间
   至少留出 4 px 间距，且空库说明不超出面板或被裁切。

仅使用合成 DNG+JPG 与合成 `.cube` 夹具，不接触真实照片；stdout 仅输出脱敏 JSON。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from conftest import REAL_HOME
from raw_support import bayer_scene, build_dng
from support import g262_cases as g262
from support.dng_writer import COLOR_MATRIX
from test_mobile_ui_browser import MODULES, PKG, RUNTIME, SAMPLES, _free_port, _need

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
FORBIDDEN_PORTS = {60887, 18772, 18801, 18802, 18803, 5290}

DRIVER_JS = r"""
import { existsSync, mkdirSync, readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [runtime, base, manifestPath] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const { pinnedRuntime, verifyPinnedRuntime } = await import(pathToFileURL(runtime).href);
verifyPinnedRuntime();

const { webkit } = await import(pathToFileURL(pinnedRuntime.modulePath).href);

function resolveWebKitExecutable() {
  const direct = webkit.executablePath();
  if (direct && existsSync(direct)) return direct;
  const baseName = direct ? path.basename(direct) : 'pw_run.sh';
  if (existsSync(pinnedRuntime.browsersPath)) {
    for (const entry of readdirSync(pinnedRuntime.browsersPath)) {
      if (!entry.startsWith('webkit-')) continue;
      const candidate = path.join(pinnedRuntime.browsersPath, entry, baseName);
      if (existsSync(candidate)) return candidate;
    }
  }
  return null;
}

const executablePath = resolveWebKitExecutable();
if (!executablePath) {
  process.stdout.write(`${JSON.stringify({ skipped: 'Playwright WebKit 未安装（需运行 playwright install webkit）' })}\n`);
  process.exit(0);
}

const IPHONE_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1';

const LANDSCAPE_VPS = [
  { label: '844x390-nosafe', width: 844, height: 390, safe: { top: 0, bottom: 0, left: 0, right: 0 } },
  { label: '844x390', width: 844, height: 390, safe: { top: 0, bottom: 21, left: 47, right: 47 } },
  { label: '956x440-nosafe', width: 956, height: 440, safe: { top: 0, bottom: 0, left: 0, right: 0 } },
  { label: '956x440', width: 956, height: 440, safe: { top: 0, bottom: 21, left: 59, right: 59 } },
];

const BASELINE_VPS = [
  { label: '320x568', width: 320, height: 568, layout: 'phone-portrait', safe: { top: 20, bottom: 0, left: 0, right: 0 } },
  { label: '375x667', width: 375, height: 667, layout: 'phone-portrait', safe: { top: 20, bottom: 0, left: 0, right: 0 } },
  { label: '390x844', width: 390, height: 844, layout: 'phone-portrait', safe: { top: 47, bottom: 34, left: 0, right: 0 } },
  { label: '440x956', width: 440, height: 956, layout: 'phone-portrait', safe: { top: 62, bottom: 34, left: 0, right: 0 } },
  { label: '1024x1366', width: 1024, height: 1366, layout: 'ipad-portrait', safe: { top: 24, bottom: 20, left: 0, right: 0 } },
  { label: '1366x1024', width: 1366, height: 1024, layout: 'ipad-landscape', safe: { top: 24, bottom: 20, left: 0, right: 0 } },
];

const failures = [];
const pageErrors = [];
const summary = { landscape: { empty: {}, three: {} }, err320: null, baseline: { empty: {}, three: {} }, shots: [] };

function redact(text) {
  return String(text)
    .replace(/\/(?:Volumes|Users|private|var\/folders|tmp)\/\S+/g, '[path]')
    .replaceAll(manifest.stem || '', 'photo#1')
    .slice(0, 320);
}

function check(ok, msg) {
  if (!ok) failures.push(redact(msg));
  return Boolean(ok);
}

async function frame(page, n = 3) {
  await page.evaluate(
    (count) =>
      new Promise((done) => {
        const step = (left) => (left <= 0 ? done() : requestAnimationFrame(() => step(left - 1)));
        step(count);
      }),
    n,
  );
}

async function setVp(page, vp, wantLayout) {
  await page.setViewportSize({ width: vp.width, height: vp.height });
  await page.evaluate((sa) => {
    for (const side of ['top', 'bottom', 'left', 'right']) {
      document.documentElement.style.setProperty(`--safe-${side}`, `${sa[side] ?? 0}px`);
    }
  }, vp.safe);
  await page.waitForFunction(
    (layout) => document.querySelector('.app')?.getAttribute('data-layout') === layout,
    wantLayout,
    { timeout: 10_000 },
  );
  await frame(page, 4);
}

async function saveCrop(page, name, mode) {
  if (!manifest.shotsDir) return;
  mkdirSync(manifest.shotsDir, { recursive: true });
  const clip = await page.evaluate((kind) => {
    if (kind === 'landscape') {
      const nav = document.querySelector('.editor > .nav');
      const band = document.querySelector('.editor > .band');
      const bar = document.querySelector('.editor > .toolbar');
      const els = [nav, bar, band].filter((e) => e && e.getClientRects().length);
      if (!els.length) return null;
      let x0 = Infinity;
      let y0 = Infinity;
      let x1 = 0;
      let y1 = 0;
      for (const e of els) {
        const b = e.getBoundingClientRect();
        x0 = Math.min(x0, b.left);
        y0 = Math.min(y0, b.top);
        x1 = Math.max(x1, b.right);
        y1 = Math.max(y1, b.bottom);
      }
      const x = Math.max(0, Math.floor(x0) - 8);
      const y = Math.max(0, Math.floor(y0));
      return {
        x,
        y,
        width: Math.min(window.innerWidth, Math.ceil(x1)) - x,
        height: Math.min(window.innerHeight, Math.ceil(y1)) - y,
      };
    }
    const band = document.querySelector('.editor .band');
    const bar = document.querySelector('.editor .toolbar');
    if (!band || !bar) return null;
    const br = band.getBoundingClientRect();
    const tr = bar.getBoundingClientRect();
    const y = Math.max(0, Math.floor(br.top) - 6);
    const bottom = Math.min(window.innerHeight, Math.ceil(tr.bottom));
    return { x: 0, y, width: window.innerWidth, height: Math.max(1, bottom - y) };
  }, mode);
  if (!clip || clip.width <= 0 || clip.height <= 0) return;
  const prefix = manifest.shotsPrefix || 'shot';
  const file = path.join(manifest.shotsDir, `${prefix}-${name}.png`);
  await page.screenshot({ path: file, clip, animations: 'disabled' });
  summary.shots.push(`${prefix}-${name}.png`);
}

async function measureLutPanel(page) {
  return page.evaluate(() => {
    const r1 = (n) => Math.round(n * 10) / 10;
    const vis = (el) => {
      if (!el || el.hidden) return false;
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      const b = el.getBoundingClientRect();
      return b.width > 0.5 && b.height > 0.5;
    };
    const rect = (el) => {
      if (!vis(el)) return null;
      const b = el.getBoundingClientRect();
      return { x: r1(b.left), y: r1(b.top), w: r1(b.width), h: r1(b.height), right: r1(b.right), bottom: r1(b.bottom) };
    };
    const overlap = (a, b) => {
      if (!a || !b) return 0;
      const ow = Math.max(0, Math.min(a.right, b.right) - Math.max(a.x, b.x));
      const oh = Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.y, b.y));
      return r1(ow * oh);
    };
    const inside = (child, parent) => {
      if (!child || !parent) return false;
      return (
        child.x >= parent.x - 0.5 &&
        child.y >= parent.y - 0.5 &&
        child.right <= parent.right + 0.5 &&
        child.bottom <= parent.bottom + 0.5
      );
    };
    const hitCenter = (el) => {
      if (!vis(el)) return false;
      const b = el.getBoundingClientRect();
      const cx = b.left + b.width / 2;
      const cy = b.top + b.height / 2;
      if (cx < 0 || cy < 0 || cx >= window.innerWidth || cy >= window.innerHeight) return false;
      const hit = document.elementFromPoint(cx, cy);
      return Boolean(hit && (hit === el || el.contains(hit)));
    };

    const vp = { x: 0, y: 0, w: window.innerWidth, h: window.innerHeight, right: window.innerWidth, bottom: window.innerHeight };
    const panelEl = document.querySelector('.editor .panel.lut');
    const stageEl = document.querySelector('.editor .stage');
    const swEl = panelEl?.querySelector(':scope > .mode-switch');
    const wrapEl = document.querySelector('.editor .lut-strip-wrap');
    const pinEl = wrapEl?.querySelector('.lut-pin');
    const stripEl = wrapEl?.querySelector('.strip');
    const importEl = pinEl?.querySelector('[data-act="lut-import"]');
    const spaceEl = pinEl?.querySelector('[data-act="lut-space"]');
    const menuEl = pinEl?.querySelector('[data-act="lut-menu"]');
    const footEl = panelEl?.querySelector(':scope > .lut-foot');
    const errEl = panelEl?.querySelector(':scope > .lut-err');
    const barEl = document.querySelector('.editor .toolbar');
    const bandEl = document.querySelector('.editor .band');
    const dockEl = document.querySelector('.editor .ipad-dock');
    const ipadMetaEl = panelEl?.querySelector(':scope > .ipad-lut-meta');

    const panel = rect(panelEl);
    const stage = rect(stageEl);
    const sw = rect(swEl);
    const wrap = rect(wrapEl);
    const pin = rect(pinEl);
    const strip = rect(stripEl);
    const importBtn = rect(importEl);
    const spaceBtn = rect(spaceEl);
    const menuTile = rect(menuEl);
    const foot = rect(footEl);
    const err = rect(errEl);
    const bar = rect(barEl);
    const band = rect(bandEl);
    const dock = rect(dockEl);
    const ipadMeta = rect(ipadMetaEl);

    const cards = [...(stripEl?.querySelectorAll('.rc') ?? [])].map((node) => {
      const box = rect(node);
      return {
        lut: node.dataset.lut ?? '',
        name: node.querySelector('.nm')?.textContent?.trim() ?? '',
        box,
        inPanel: inside(box, panel),
        inVp: inside(box, vp),
        hitOk: hitCenter(node),
        ovSw: overlap(box, sw),
        ovStage: overlap(box, stage),
        ovPin: overlap(box, pin),
      };
    });

    let maxCardPairOverlap = 0;
    for (let i = 0; i < cards.length; i += 1) {
      for (let j = i + 1; j < cards.length; j += 1) {
        maxCardPairOverlap = Math.max(maxCardPairOverlap, overlap(cards[i].box, cards[j].box));
      }
    }

    const footClipped = footEl && vis(footEl)
      ? footEl.scrollWidth - footEl.clientWidth > 1 || footEl.scrollHeight - footEl.clientHeight > 1
      : false;

    const panelItems = [wrap, pin, importBtn, spaceBtn, foot, ...cards.map((c) => c.box)].filter(Boolean);
    const maxStageOverlap = r1(panelItems.reduce((acc, b) => Math.max(acc, overlap(b, stage)), 0));
    const maxSwOverlap = r1(panelItems.reduce((acc, b) => Math.max(acc, overlap(b, sw)), 0));

    return {
      layout: document.querySelector('.app')?.getAttribute('data-layout') ?? '',
      empty: panelEl?.dataset.lutEmpty ?? null,
      vp,
      panel,
      stage,
      band,
      bar,
      dock,
      ipadMeta,
      sw,
      wrap,
      pin,
      strip,
      importBtn,
      importInPanel: inside(importBtn, panel),
      importInVp: inside(importBtn, vp),
      importHitOk: hitCenter(importEl),
      spaceBtn,
      spaceInPanel: inside(spaceBtn, panel),
      spaceInVp: inside(spaceBtn, vp),
      menuTile,
      foot,
      footText: footEl && vis(footEl) ? footEl.textContent.trim() : '',
      footInPanel: inside(foot, panel),
      footInVp: inside(foot, vp),
      footClipped,
      ovFootSw: overlap(foot, sw),
      ovFootPin: overlap(foot, pin),
      ovFootCards: r1(cards.reduce((acc, c) => acc + overlap(foot, c.box), 0)),
      ovFootBar: overlap(foot, bar),
      ovFootErr: overlap(foot, err),
      err,
      errText: errEl && vis(errEl) ? errEl.textContent.trim() : '',
      errInPanel: inside(err, panel),
      errInVp: inside(err, vp),
      errFootGapY: err && foot ? r1(foot.y - err.bottom) : null,
      cards,
      maxCardPairOverlap: r1(maxCardPairOverlap),
      maxStageOverlap,
      maxSwOverlap,
    };
  });
}

function assertLandscape(tag, m, wantEmpty) {
  check(m.layout === 'phone-landscape', `${tag}: layout=${m.layout}, want phone-landscape`);
  check(Boolean(m.panel), `${tag}: panel missing`);
  check(m.menuTile === null, `${tag}: duplicate phone-portrait menuTile visible in landscape (${JSON.stringify(m.menuTile)})`);
  const impDiag = {
    box: m.importBtn,
    panel: m.panel,
    inPanel: m.importInPanel,
    inVp: m.importInVp,
    hit: m.importHitOk,
  };
  check(
    Boolean(m.importBtn) && m.importBtn.w >= 43.5 && m.importBtn.h >= 43.5 && m.importInPanel && m.importInVp && m.importHitOk,
    `${tag}: import button invalid/outside (${JSON.stringify(impDiag)})`,
  );
  const spDiag = { box: m.spaceBtn, panel: m.panel, inPanel: m.spaceInPanel, inVp: m.spaceInVp };
  check(
    Boolean(m.spaceBtn) && m.spaceBtn.w >= 43.5 && m.spaceBtn.h >= 43.5 && m.spaceInPanel && m.spaceInVp,
    `${tag}: space button invalid/outside (${JSON.stringify(spDiag)})`,
  );
  check(m.maxSwOverlap <= 0.5, `${tag}: LUT panel content overlaps mode-switch (${m.maxSwOverlap}px2)`);
  check(m.maxStageOverlap <= 0.5, `${tag}: LUT panel content overlaps photo stage (${m.maxStageOverlap}px2)`);
  check(m.maxCardPairOverlap <= 0.5, `${tag}: LUT cards overlap each other (${m.maxCardPairOverlap}px2)`);

  const wantCount = wantEmpty ? 1 : 4;
  check(m.cards.length === wantCount, `${tag}: expected ${wantCount} cards, got ${m.cards.length}`);
  const noneCard = m.cards.find((c) => c.lut === 'none');
  check(Boolean(noneCard), `${tag}: missing 「不用 LUT」 tile`);
  for (const card of m.cards) {
    const cardOk =
      Boolean(card.box) &&
      card.box.w >= 43.5 &&
      card.box.h >= 43.5 &&
      card.inPanel &&
      card.inVp &&
      card.hitOk &&
      card.ovSw <= 0.5 &&
      card.ovStage <= 0.5 &&
      card.ovPin <= 0.5;
    const cardDiag = {
      box: card.box,
      panel: m.panel,
      inPanel: card.inPanel,
      inVp: card.inVp,
      hit: card.hitOk,
      ovSw: card.ovSw,
      ovStage: card.ovStage,
      ovPin: card.ovPin,
    };
    check(cardOk, `${tag}: tile ${card.lut || card.name} outside/overlapping (${JSON.stringify(cardDiag)})`);
  }
  if (wantEmpty) {
    const footDiag = {
      foot: m.foot,
      panel: m.panel,
      inPanel: m.footInPanel,
      inVp: m.footInVp,
      clipped: m.footClipped,
      ovCards: m.ovFootCards,
      ovPin: m.ovFootPin,
      ovSw: m.ovFootSw,
    };
    check(
      Boolean(m.foot) &&
        m.footInPanel &&
        m.footInVp &&
        !m.footClipped &&
        m.ovFootSw <= 0.5 &&
        m.ovFootPin <= 0.5 &&
        m.ovFootCards <= 0.5 &&
        m.ovFootBar <= 0.5 &&
        m.footText.includes('LUT') &&
        m.footText.includes('照片') &&
        m.footText.includes('不写入相机'),
      `${tag}: empty-library note invalid/outside (${JSON.stringify(footDiag)})`,
    );
  } else {
    check(m.foot === null, `${tag}: empty-library note should be hidden when 3 LUTs exist`);
  }
}

const browser = await webkit.launch({ headless: true, executablePath });
const ctx = await browser.newContext({
  viewport: { width: 390, height: 844 },
  deviceScaleFactor: 2,
  isMobile: true,
  hasTouch: true,
  userAgent: IPHONE_UA,
  locale: 'zh-CN',
});
const page = await ctx.newPage();
page.on('pageerror', (err) => pageErrors.push(redact(err.message)));

try {
  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.app[data-core="1"]', { timeout: 60_000 });
  await page.locator('input.filepick:not(.calibpick)').first().setInputFiles(manifest.files);
  await page.waitForFunction(
    () => !document.querySelector('.nav small')?.textContent?.includes('正在读取') && document.querySelectorAll('.cell').length >= 1,
    null,
    { timeout: 60_000 },
  );
  await page.locator('.cell').first().tap();
  await page.waitForSelector('.editor', { timeout: 30_000 });
  const askLater = page.locator('.ask-sheet button', { hasText: '先不用' });
  if (await askLater.isVisible({ timeout: 2_000 }).catch(() => false)) {
    await askLater.tap();
    await page.waitForFunction(() => !document.querySelector('.ask-sheet'), null, { timeout: 5_000 });
  }
  await page.locator('.editor .panel.recipe > .mode-switch [data-mode="lut"]').tap();
  await page.waitForFunction(() => !document.querySelector('.editor .panel.lut')?.hidden, null, { timeout: 10_000 });
  await frame(page, 3);

  // 1) Empty library baseline boxes (portrait 320/375/390/440 + iPad 1024x1366 / 1366x1024)
  for (const vp of BASELINE_VPS) {
    await setVp(page, vp, vp.layout);
    summary.baseline.empty[vp.label] = await measureLutPanel(page);
  }

  // 2) Fix 1 (empty library) on phone landscape
  for (const vp of LANDSCAPE_VPS) {
    await setVp(page, vp, 'phone-landscape');
    const m = await measureLutPanel(page);
    summary.landscape.empty[vp.label] = m;
    assertLandscape(`fix1 empty ${vp.label}`, m, true);
    if (vp.label === '844x390') await saveCrop(page, '844x390-empty', 'landscape');
  }

  // 3) Fix 2: 320 portrait with broken .cube import error
  await setVp(page, BASELINE_VPS[0], 'phone-portrait');
  await page.locator('input.lutpick').setInputFiles(manifest.brokenCube);
  await page.waitForFunction(
    () => {
      const el = document.querySelector('.editor .panel.lut > .lut-err');
      return Boolean(el && !el.hidden && el.textContent && el.textContent.trim().length > 0);
    },
    null,
    { timeout: 15_000 },
  );
  await frame(page, 4);
  const err320 = await measureLutPanel(page);
  summary.err320 = err320;
  await saveCrop(page, '320-err', 'portrait');
  check(Boolean(err320.err) && err320.errInPanel && err320.errInVp, `fix2 320: missing/outside error toast (${JSON.stringify(err320.err)})`);
  check(
    Boolean(err320.foot) && err320.footInPanel && err320.footInVp && !err320.footClipped,
    `fix2 320: empty-library note clipped/outside (${JSON.stringify({ foot: err320.foot, panel: err320.panel, clipped: err320.footClipped })})`,
  );
  check(
    err320.errFootGapY !== null && err320.errFootGapY >= 4.0 && err320.ovFootErr <= 0.5,
    `fix2 320: error toast and empty-LUT note gap=${err320.errFootGapY}px, overlap=${err320.ovFootErr}px2 (want gap >= 4px)`,
  );
  const ovDiag = { sw: err320.ovFootSw, pin: err320.ovFootPin, cards: err320.ovFootCards, bar: err320.ovFootBar };
  check(
    err320.ovFootSw <= 0.5 && err320.ovFootPin <= 0.5 && err320.ovFootCards <= 0.5 && err320.ovFootBar <= 0.5,
    `fix2 320: empty-LUT note overlaps sibling controls (${JSON.stringify(ovDiag)})`,
  );

  // 4) Import 3 LUTs and clear transient import toast by selecting first LUT then returning to 「不用 LUT」
  await page.locator('input.lutpick').setInputFiles(manifest.cubes);
  await page.waitForFunction(() => (window.__gxrLutBook?.list?.()?.length ?? 0) === 3, null, { timeout: 30_000 });
  await page.locator('.editor .panel.lut .strip .rc:not([data-lut="none"])').first().tap();
  await page.waitForSelector('.editor .panel.lut .strip .rc.on:not([data-lut="none"])', { timeout: 10_000 });
  await page.locator('.editor .panel.lut .strip .rc[data-lut="none"]').tap();
  await page.waitForSelector('.editor .panel.lut .strip .rc[data-lut="none"].on', { timeout: 10_000 });
  await frame(page, 4);

  // Portrait & iPad baseline boxes with 3 LUTs
  for (const vp of BASELINE_VPS) {
    await setVp(page, vp, vp.layout);
    summary.baseline.three[vp.label] = await measureLutPanel(page);
  }

  // Fix 1 (3 imported LUTs) on phone landscape
  for (const vp of LANDSCAPE_VPS) {
    await setVp(page, vp, 'phone-landscape');
    const m = await measureLutPanel(page);
    summary.landscape.three[vp.label] = m;
    assertLandscape(`fix1 three ${vp.label}`, m, false);
    if (vp.label === '844x390') await saveCrop(page, '844x390-three', 'landscape');
  }
} catch (err) {
  failures.push(`threw: ${redact(err?.stack || err?.message || err)}`);
} finally {
  await ctx.close();
  await browser.close();
}

check(pageErrors.length === 0, `pageErrors: ${pageErrors.join(' | ')}`);
process.stdout.write(`${JSON.stringify({ ok: failures.length === 0, failures, pageErrors, summary })}\n`);
if (failures.length > 0) process.exit(1);
"""


def _port() -> int:
    for _ in range(20):
        port = _free_port()
        if port not in FORBIDDEN_PORTS:
            return port
    pytest.fail("no free port")


def _stop(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


def _redacted_tail(text: str) -> str:
    cleaned = re.sub(r"/(?:Volumes|Users|private|var/folders|tmp)/\S+", "[path]", text)
    return cleaned[-500:].strip()


def _cube(title: str, shift: float) -> str:
    lines = [f'TITLE "{title}"', "# Input: sRGB", "LUT_3D_SIZE 2"]
    for b in (0, 1):
        for g in (0, 1):
            for r in (0, 1):
                rr = min(1.0, max(0.0, r * (1.0 - shift) + shift))
                lines.append(f"{rr:.3f} {float(g):.3f} {float(b):.3f}")
    return "\n".join(lines) + "\n"


def _synth_pair(out_dir: Path, stem: str) -> tuple[Path, Path]:
    height, width = 128, 192
    yy, xx = np.mgrid[0:height, 0:width]
    scene = np.stack(
        [
            0.14 + 0.56 * (xx / (width - 1)),
            0.18 + 0.44 * (yy / (height - 1)),
            0.24 + 0.34 * (1.0 - xx / (width - 1)),
        ],
        axis=-1,
    )
    neutral = [0.65, 1.0, 0.58]
    when = "2026:11:26 08:00:00"
    dng_bytes = build_dng(
        bayer_scene(height, width, scene * np.array(neutral), white=4095),
        bps=16,
        white=4095,
        neutral=neutral,
        orientation=1,
        iso=200,
        when=when,
        lens=f"RICOH {g262.LENS_50}",
        exif_entries=[(42036, "ASCII", g262.LENS_50)],
        root_extra=[
            (50721, "SRATIONAL", COLOR_MATRIX),
            (50727, "RATIONAL", [1.0, 1.0, 1.0]),
            (50778, "SHORT", [21]),
        ],
    )
    dng_path, jpg_path = out_dir / f"{stem}.DNG", out_dir / f"{stem}.JPG"
    dng_path.write_bytes(dng_bytes)
    exif = Image.Exif()
    exif[271], exif[272], exif[274], exif[306] = "RICOH", "GXR A12", 1, when
    sub = exif.get_ifd(0x8769)
    sub[34855], sub[36867], sub[36868], sub[42036] = 200, when, when, g262.LENS_50
    rgb8 = np.clip(np.round(np.power(scene, 1.0 / 2.2) * 255.0), 0, 255).astype(np.uint8)
    Image.fromarray(rgb8, "RGB").save(jpg_path, "JPEG", quality=92, exif=exif)
    return dng_path, jpg_path


def test_mobile_lut_landscape_and_320_error_spacing_webkit(tmp_path: Path) -> None:
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")

    dng_path, jpg_path = _synth_pair(tmp_path, "R9980201")
    cubes = []
    for idx, (fname, title, shift) in enumerate(
        (
            ("warm_a.cube", "Warm A", 0.04),
            ("cool_b.cube", "Cool B", 0.08),
            ("film_c.cube", "Film C", 0.12),
        )
    ):
        cube_path = tmp_path / fname
        cube_path.write_text(_cube(title, shift + idx * 0.01), encoding="utf-8")
        cubes.append(str(cube_path))
    broken_cube = tmp_path / "broken.cube"
    broken_cube.write_text('TITLE "Broken"\nLUT_3D_SIZE 2\nnot a number\n', encoding="utf-8")

    driver_path = tmp_path / "mobile_lut_landscape_webkit.mjs"
    driver_path.write_text(DRIVER_JS, encoding="utf-8")
    manifest: dict[str, Any] = {
        "files": [str(dng_path), str(jpg_path)],
        "stem": dng_path.stem,
        "cubes": cubes,
        "brokenCube": str(broken_cube),
        "shotsDir": os.environ.get("GXR_LUT_LANDSCAPE_SHOTS_DIR", ""),
        "shotsPrefix": os.environ.get("GXR_LUT_LANDSCAPE_SHOTS_PREFIX", "after"),
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    port = _port()
    env = os.environ.copy()
    env["GXR_MOBILE_SAMPLES"] = str(SAMPLES)
    env["HOME"] = str(REAL_HOME)
    log = tmp_path / "vite.log"
    vite: subprocess.Popen[bytes] | None = None
    try:
        with log.open("w", encoding="utf-8") as stream:
            vite = subprocess.Popen(
                ["node", str(MODULES / "vite" / "bin" / "vite.js"), "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
                cwd=REPO / "mobile",
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if vite.poll() is not None:
                pytest.fail(f"vite exited {vite.returncode}")
            probe = subprocess.run(
                [
                    "node",
                    "-e",
                    "fetch(process.argv[1]).then((r)=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))",
                    f"http://127.0.0.1:{port}/",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.4)
        else:
            pytest.fail("vite did not answer")

        result = subprocess.run(
            ["node", str(driver_path), str(RUNTIME), f"http://127.0.0.1:{port}", str(manifest_path)],
            cwd=REPO,
            env={**os.environ, "HOME": str(REAL_HOME)},
            check=False,
            capture_output=True,
            text=True,
            timeout=240,
        )
    finally:
        _stop(vite)

    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert lines, _redacted_tail(result.stdout + "\n" + result.stderr)
    report: dict[str, Any] = json.loads(lines[-1])
    if report.get("skipped"):
        pytest.skip(str(report["skipped"]))

    out_report = os.environ.get("GXR_LUT_LANDSCAPE_REPORT", "")
    if out_report:
        Path(out_report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    assert result.returncode == 0, report.get("failures")
    assert report["ok"], report.get("failures")
    assert report["pageErrors"] == [], report["pageErrors"]
