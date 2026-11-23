// 手机竖屏（390×844 / 440×956）无 WebGPU 带头回归检查（F1 防挤压 + N2 说明折两行不截断、单带高度保持 128 pt）。

const LONG_RECIPE_NAME = 'Kodachrome 64 Night Street Cool Shadows';

async function measureHeader(page) {
  return page.evaluate(() => {
    const box = (sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      const range = document.createRange();
      range.selectNodeContents(el);
      const lineRects = [...range.getClientRects()].filter((lr) => lr.width > 0.5 && lr.height > 0.5);
      const visualLineTops = [];
      for (const lr of lineRects) {
        if (!visualLineTops.some((y) => Math.abs(y - lr.top) <= 4)) visualLineTops.push(lr.top);
      }
      const lineTop = lineRects.length ? Math.min(...lineRects.map((lr) => lr.top)) : r.top;
      const lineBottom = lineRects.length ? Math.max(...lineRects.map((lr) => lr.bottom)) : r.bottom;
      return {
        text: (el.textContent || '').trim(),
        left: Math.round(r.left * 10) / 10,
        right: Math.round(r.right * 10) / 10,
        top: Math.round(r.top * 10) / 10,
        bottom: Math.round(r.bottom * 10) / 10,
        w: Math.round(r.width * 10) / 10,
        h: Math.round(r.height * 10) / 10,
        scrollW: Math.round(el.scrollWidth * 10) / 10,
        clientW: Math.round(el.clientWidth * 10) / 10,
        scrollH: Math.round(el.scrollHeight * 10) / 10,
        clientH: Math.round(el.clientHeight * 10) / 10,
        lineCount: lineRects.length,
        visualLines: visualLineTops.length,
        lineTop: Math.round(lineTop * 10) / 10,
        lineBottom: Math.round(lineBottom * 10) / 10,
      };
    };
    return {
      band: box('.editor .band'),
      hd: box('.editor .band-hd'),
      lbl: box('.editor .band-hd .lbl.nogpu'),
      coreTag: box('.editor .band-hd .lbl.nogpu .tag.neutral'),
      note: box('.editor .band-hd .lbl.nogpu .nogpu-note'),
      right: box('.editor .band-hd-right'),
      slot: box('.editor .band-head-slot'),
      rname: box('.editor .band-recipe-hd .rname'),
      pill: box('.editor .band-recipe-hd .tag.neutral-tag'),
    };
  });
}

function assertNoteAndBand(check, prefix, hdr, { requireWrap = false } = {}) {
  check(
    Boolean(hdr.band && Math.abs(hdr.band.h - 128) <= 1),
    `${prefix}: band height changed to ${hdr.band?.h} pt (want 128 pt)`,
  );
  check(
    Boolean(hdr.hd && Math.abs(hdr.hd.h - 44) <= 1),
    `${prefix}: band header height changed to ${hdr.hd?.h} pt (want 44 pt)`,
  );
  if (!hdr.hd || !hdr.coreTag || !hdr.note) return;
  check(
    hdr.coreTag.w >= 100 && hdr.coreTag.scrollW <= hdr.coreTag.clientW + 1 && hdr.coreTag.top >= hdr.hd.top - 1,
    `${prefix}: no-GPU core notice truncated or clipped (${JSON.stringify(hdr.coreTag)})`,
  );
  check(
    hdr.note.w >= 120 && hdr.note.h >= 10 && hdr.note.top >= hdr.hd.top - 1 && hdr.note.bottom <= hdr.hd.bottom + 1,
    `${prefix}: no-GPU detail line missing or clipped (${JSON.stringify(hdr.note)} vs hd ${hdr.hd.top}..${hdr.hd.bottom})`,
  );
  check(
    hdr.note.scrollW <= hdr.note.clientW + 1 && hdr.note.scrollH <= hdr.note.clientH + 1,
    `${prefix}: no-GPU detail line truncated/ellipsized (scrollW=${hdr.note.scrollW}, clientW=${hdr.note.clientW}, scrollH=${hdr.note.scrollH}, clientH=${hdr.note.clientH})`,
  );
  check(
    hdr.note.visualLines >= (requireWrap ? 2 : 1) && hdr.note.visualLines <= 2,
    `${prefix}: no-GPU detail line visualLines=${hdr.note.visualLines}, want ${requireWrap ? '2' : '1..2'}`,
  );
}

function assertPill(check, prefix, hdr) {
  if (!hdr.hd || !hdr.pill) return;
  check(
    hdr.pill.h <= 22 && hdr.pill.scrollH <= 22 && hdr.pill.lineCount === 1,
    `${prefix}: recipe source pill 「${hdr.pill.text}」 wrapped (h=${hdr.pill.h}, scrollH=${hdr.pill.scrollH}, lines=${hdr.pill.lineCount})`,
  );
  check(
    hdr.pill.scrollW <= hdr.pill.clientW + 1,
    `${prefix}: recipe source pill truncated (scrollW=${hdr.pill.scrollW} > clientW=${hdr.pill.clientW})`,
  );
  check(
    hdr.pill.top >= hdr.hd.top - 1 && hdr.pill.bottom <= hdr.hd.bottom + 1 && hdr.pill.lineTop >= hdr.hd.top - 1 && hdr.pill.lineBottom <= hdr.hd.bottom + 1,
    `${prefix}: recipe source pill overflows 44pt header (pill ${hdr.pill.lineTop}..${hdr.pill.lineBottom} vs hd ${hdr.hd.top}..${hdr.hd.bottom})`,
  );
}

export async function auditPhonePortraitNoGpuHeader(page, vp, { check, frame, summary }) {
  const tealCard = page.locator('[data-recipes="1"] .rc[data-card]').filter({ hasText: 'Teal Orange' }).first();
  if (await tealCard.isVisible().catch(() => false)) {
    await tealCard.tap();
    await frame(page, 3);
  }
  const hdr = await measureHeader(page);
  summary.f1[vp.label] = hdr;
  check(Boolean(hdr.hd && hdr.lbl && hdr.coreTag && hdr.note && hdr.rname && hdr.pill && hdr.slot), `F1 ${vp.label}: phone portrait header elements missing`);
  if (hdr.hd && hdr.coreTag && hdr.note && hdr.rname && hdr.pill && hdr.slot) {
    check(
      hdr.rname.scrollW <= hdr.rname.clientW + 1,
      `F1 ${vp.label}: recipe name 「${hdr.rname.text}」 truncated (scrollW=${hdr.rname.scrollW} > clientW=${hdr.rname.clientW})`,
    );
    assertPill(check, `F1 ${vp.label}`, hdr);
    check(
      hdr.slot.w >= 130,
      `F1 ${vp.label}: .band-head-slot squeezed to ${hdr.slot.w} pt (< 130 pt)`,
    );
    assertNoteAndBand(check, `F1 ${vp.label}`, hdr);
  }

  // N2：真实较宽配方「GR 高对比黑白」下，无 GPU 说明行折两行不截断，且不撑高 128 pt 单带。
  const hcbwCard = page.locator('[data-recipes="1"] .rc[data-card]').filter({ hasText: 'GR 高对比黑白' }).first();
  if (await hcbwCard.isVisible().catch(() => false)) {
    await hcbwCard.tap();
    await frame(page, 3);
    const hcbwHdr = await measureHeader(page);
    summary.f1[`${vp.label}-hcbw`] = hcbwHdr;
    if (hcbwHdr.hd && hcbwHdr.coreTag && hcbwHdr.note && hcbwHdr.rname && hcbwHdr.pill) {
      check(
        hcbwHdr.rname.scrollW <= hcbwHdr.rname.clientW + 1,
        `N2 ${vp.label} hcbw: recipe name 「${hcbwHdr.rname.text}」 truncated (scrollW=${hcbwHdr.rname.scrollW} > clientW=${hcbwHdr.rname.clientW})`,
      );
      assertPill(check, `N2 ${vp.label} hcbw`, hcbwHdr);
      assertNoteAndBand(check, `N2 ${vp.label} hcbw`, hcbwHdr);
    }
  }

  // N2：超长配方名下，说明行折两行完整可读（不省略），来源胶囊仍单行，单带高度保持 128 pt。
  const prevName = await page.evaluate((longName) => {
    const el = document.querySelector('.editor .band-recipe-hd .rname');
    if (!el) return null;
    const saved = el.textContent ?? '';
    el.textContent = longName;
    return saved;
  }, LONG_RECIPE_NAME);
  try {
    await frame(page, 3);
    const longHdr = await measureHeader(page);
    summary.f1[`${vp.label}-long`] = longHdr;
    check(Boolean(longHdr.hd && longHdr.coreTag && longHdr.note && longHdr.rname && longHdr.pill), `N2 ${vp.label} long: header elements missing`);
    if (longHdr.hd && longHdr.coreTag && longHdr.note && longHdr.pill) {
      assertPill(check, `N2 ${vp.label} long`, longHdr);
      assertNoteAndBand(check, `N2 ${vp.label} long`, longHdr, { requireWrap: vp.width <= 440 });
    }
  } finally {
    if (prevName !== null) {
      await page.evaluate((saved) => {
        const el = document.querySelector('.editor .band-recipe-hd .rname');
        if (el) el.textContent = saved;
      }, prevName);
      await frame(page, 2);
    }
  }
}
