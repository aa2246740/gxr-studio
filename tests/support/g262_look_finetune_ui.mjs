// 验证 Look + 微调在桌面（info_panel.js / recipes.js）与手机（halfframe.ts / recipes.ts / export_request.ts）的显示和装配。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { pathToFileURL } from 'node:url';
import { installDom, tick } from './mini_dom.mjs';

registerHooks({
  resolve(spec, ctx, next) {
    if (spec.endsWith('.css')) return { shortCircuit: true, url: 'data:text/javascript,export default ""' };
    try {
      return next(spec, ctx);
    } catch (err) {
      if (spec.startsWith('.') && !/\.[a-z]+$/.test(spec)) return next(`${spec}.ts`, ctx);
      throw err;
    }
  },
});

const [webDir, repoRoot, fixturePath] = process.argv.slice(2);
const fx = JSON.parse(readFileSync(fixturePath, 'utf8'));

installDom();
const webUrl = (name) => pathToFileURL(`${webDir}/${name}`).href;
const { state } = await import(webUrl('state.js'));
const { infoPanel } = await import(webUrl('info_panel.js'));
const { recipePanel } = await import(webUrl('recipes.js'));

state.catalog = { units: fx.listing };
state.camdata = { state: 'ready', generation: 262, dataVersion: 'g262-test' };
state.luts = { luts: [] };
state.photos = [];
state.partners = new Map();

function readSliderValues(panel) {
  const out = {};
  for (const row of panel.querySelectorAll('.knob')) {
    const valEl = row.querySelector('.val') || row.querySelector('output') || row.querySelector('.sl-head .num');
    const val = valEl?.textContent?.trim() || '';
    const labEl = row.querySelector('.lab') || row.querySelector('label') || row.querySelector('.sl-head span');
    const rawLabel = labEl?.textContent?.trim() || '';
    const label = val && rawLabel.endsWith(val) ? rawLabel.slice(0, -val.length).trim() : rawLabel;
    if (label) out[label] = val;
  }
  return out;
}

const desktop = {};
for (const item of fx.desktopCases) {
  const view = {
    photo: item.photo,
    ed: {
      recipe: item.editRecipe,
      recipeFromCamera: item.recipeFromCamera,
      adj: {},
      memory: {},
      caps: { recipe: 'full', raw: true },
      calibrated: true,
    },
    redrawSide: () => {},
  };
  const info = infoPanel(view);
  document.body.append(info);
  await tick();
  info.sync();
  const sections = info.querySelectorAll('section');
  const shootingText = sections[0]?.querySelector('.section-body')?.textContent ?? '';
  const editText = sections[1]?.querySelector('.section-body')?.textContent ?? '';
  info.remove();

  const rp = recipePanel(view);
  const curLook = rp.querySelector('.cur-look')?.textContent?.trim() ?? '';
  const pressedCard = rp.querySelector('.lcard[aria-pressed="true"]')?.getAttribute('data-look') ?? null;
  const sliders = readSliderValues(rp);
  view.lookDeck?.cancel?.();

  desktop[item.id] = { shootingText, editText, curLook, pressedCard, sliders };
}

// 半格一对：两半带不同 Look + 非零微调
{
  const p1 = fx.halfframe.first;
  const p2 = fx.halfframe.second;
  state.photos = [p1, p2];
  state.partners = new Map([[p1.id, p1]]);
  const view = {
    photo: p2,
    ed: {
      recipe: null,
      recipeFromCamera: true,
      adj: {},
      memory: {},
      caps: { recipe: 'full', raw: true },
      calibrated: true,
    },
    redrawSide: () => {},
  };
  const info = infoPanel(view);
  document.body.append(info);
  await tick();
  info.sync();
  const shootingText = info.querySelectorAll('section')[0]?.querySelector('.section-body')?.textContent ?? '';
  info.remove();

  const rp = recipePanel(view);
  const curLook = rp.querySelector('.cur-look')?.textContent?.trim() ?? '';
  view.lookDeck?.cancel?.();
  desktop.halfframe = { shootingText, curLook };
}

// 手机端纯逻辑：formatShootingRecipeTag（有/无目录 amount_levels）、adoptedSelectionOf、photoWithAdopted、buildRequest
const mobileUrl = (rel) => pathToFileURL(`${repoRoot}/mobile/src/${rel}`).href;
const hfMod = await import(mobileUrl('lib/halfframe.ts'));
const recMod = await import(mobileUrl('lib/recipes.ts'));
const reqMod = await import(mobileUrl('lib/export_request.ts'));

const datObj = { generation: 262, listing: { units: fx.listing } };
const datKey = recMod.datKeyOf(datObj);
const mobileAdopted = {};
for (const item of fx.mobileAdoptCases) {
  const photo = {
    dng: { name: `${item.id}.DNG` },
    jpg: { name: `${item.id}.JPG` },
    diptych: false,
    choice: null,
    recipeOff: false,
    levels: new Map(),
    memory: new Map(),
    cameraRecipe: {
      datKey,
      copyright: item.tag,
      version: 1,
      result: { status: 'ok', recipe: item.recipe, levelsUnknown: false },
    },
  };
  const adopted = recMod.adoptedSelectionOf(photo, datObj);
  assert.ok(adopted, `adoptedSelectionOf failed for ${item.id}`);
  const withAdopted = recMod.photoWithAdopted(photo, datObj);
  const cards = recMod.shelf(recMod.shelves(datObj.listing, adopted.choice.unit), 'all');
  const card = cards.find((c) => c.id === adopted.choice.id);
  assert.ok(card, `card not found for ${item.id}`);
  const req = reqMod.buildRequest(
    {
      stem: item.id,
      dngName: `${item.id}.DNG`,
      jpgName: `${item.id}.JPG`,
      lens: item.lens,
      iso: 200,
      choice: withAdopted.choice,
      levels: withAdopted.levels,
      memory: new Map(),
      knobs: {
        exposure_ev: 0, contrast: 0, highlight: 0, shadow: 0, black_point: 0,
        warm: 0, tint: 0, saturation: 0, vibrance: 0, vignette: 0,
      },
      sourceModified: 1700000000000,
    },
    cards,
    { size: 'original', quality: 'high', hdr: false },
    'g262-test',
    'binding-test',
  );
  const knobTexts = {};
  for (const ctrl of card.controls) {
    const val = recMod.effective(ctrl, adopted.levels);
    knobTexts[ctrl.name] = recMod.levelText(ctrl, val);
  }
  mobileAdopted[item.id] = {
    cardName: card.name,
    cardId: card.id,
    adoptedLevels: adopted.levels,
    knobTexts,
    resolve: req.resolve ? JSON.parse(req.resolve) : null,
    sideRecipe: req.side.recipe ?? null,
  };
}

const tagAmounts = typeof recMod.tagAmountLevels === 'function' ? recMod.tagAmountLevels : null;
const mobileHalfframeTags = {};
for (const item of fx.mobileHalfframeTags) {
  const levels = tagAmounts ? tagAmounts(hfMod.parseRecipeTag(item.tag), datObj) : item.amountLevels;
  mobileHalfframeTags[item.id] = {
    withDat: hfMod.formatShootingRecipeTag(item.tag, levels),
    withoutDat: hfMod.formatShootingRecipeTag(item.tag),
  };
}

console.log(JSON.stringify({ desktop, mobileAdopted, mobileHalfframeTags }));
