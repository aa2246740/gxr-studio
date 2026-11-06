// 快捷键表、设置、配方页提示、HDR 开关。只用 Node 自带的能力，不碰系统界面。
// 原来的四步引导对话框删掉了（U9）：这里核对它不会再弹出来，帮助按钮直接打开快捷键表。
import { pathToFileURL } from 'node:url';
import { installDom, tick } from './mini_dom.mjs';

const dir = process.argv[2];
const fileUrl = (name) => pathToFileURL(`${dir}/${name}`).href;

installDom();
const modalRoot = document.getElementById('modal-root');

globalThis.uiStore = { onboarding_dismissed: false, export_hdr: false };
globalThis.uiCalls = [];
globalThis.uiState = {
  settings: { export_dir: '/exp' },
  prefs: { onboarding_dismissed: false, export_hdr: false },
  photos: [],
};

function walk(node, acc = []) {
  acc.push(node);
  for (const child of node.children || []) walk(child, acc);
  return acc;
}

function resetPrefs() {
  globalThis.uiStore.export_hdr = false;
  globalThis.uiState.prefs = { onboarding_dismissed: false, export_hdr: false };
  globalThis.uiCalls.length = 0;
}

const { state } = await import(fileUrl('state.js'));
const { closeTopModal } = await import(fileUrl('dom.js'));
const onboarding = await import(fileUrl('onboarding.js'));
const lut = await import(fileUrl('lut.js'));
const exp = await import(fileUrl('export.js'));

const guideExports = ['maybeShowOnboarding', 'showGuide', 'GUIDE_STEPS'].filter((name) => name in onboarding);

onboarding.showHelpMenu();
const helpTitle = (walk(modalRoot).find((n) => n.tagName === 'H2') || { textContent: '' }).textContent;
const groups = walk(modalRoot).filter((n) => n.tagName === 'H3').map((n) => n.textContent);
const keys = walk(modalRoot).filter((n) => n.tagName === 'KBD').map((n) => n.textContent);
closeTopModal();
await tick();
const afterHelpClose = modalRoot.children.length;

const winGroups = onboarding.shortcutGroups('win');
const winKeys = winGroups.flatMap(([, rows]) => rows.map(([k]) => k));

const settingsOpen = onboarding.showSettings();
await tick(10);
await settingsOpen;
const settingsTitle = (walk(modalRoot).find((n) => n.tagName === 'H2') || { textContent: '' }).textContent;
const settingsText = modalRoot.textContent;
closeTopModal();
await tick();

const view = document.createElement('div');
document.body.append(view);
lut.renderLutPage(view);
await tick();
const hints = walk(view).filter((node) => node.dataset && node.dataset.role === 'recipe-hint');

function hdrBox(form) {
  const input = walk(form).find((node) => node.getAttribute && node.getAttribute('aria-label') === 'HDR');
  if (!input) throw new Error('没有 HDR 开关');
  const note = walk(form).some((node) => node.textContent === exp.HDR_NO_RAW);
  return { checked: input.checked === true, disabled: input.disabled === true, note, input };
}

const photos = [{ id: 'raw', has_raw: true }, { id: 'jpg', has_raw: false }];
resetPrefs();
state.settings = { export_dir: '/exp' };
const first = hdrBox(exp.exportForm({ ids: ['raw'], label: '这一张', photos }));
first.input.checked = true;
first.input.dispatchEvent(new Event('change'));
await tick();
const turnedOn = hdrBox(exp.exportForm({ ids: ['raw'], label: '这一张', photos }));
turnedOn.input.checked = false;
turnedOn.input.dispatchEvent(new Event('change'));
await tick();
const turnedOff = hdrBox(exp.exportForm({ ids: ['raw'], label: '这一张', photos }));
state.prefs = { ...state.prefs, export_hdr: true };
globalThis.uiStore.export_hdr = true;
const putsBefore = globalThis.uiCalls.filter((call) => call.path === '/api/prefs' && call.method === 'PUT').length;
const noRaw = hdrBox(exp.exportForm({ ids: ['jpg'], label: '这一张', photos }));
const putsAfterNoRaw = globalThis.uiCalls.filter((call) => call.path === '/api/prefs' && call.method === 'PUT').length;
const rawAgain = hdrBox(exp.exportForm({ ids: ['raw'], label: '这一张', photos }));

console.log(JSON.stringify({
  guideExports,
  helpTitle,
  groups,
  keys,
  afterHelpClose,
  winKeys,
  settingsTitle,
  settingsText,
  prefsWrites: globalThis.uiCalls.filter((c) => c.path === '/api/prefs' && c.method === 'PUT' && c.body && 'onboarding_dismissed' in c.body).length,
  hint: hints.map((node) => node.textContent),
  first: { checked: first.checked, disabled: first.disabled, note: first.note },
  turnedOn: { checked: turnedOn.checked, disabled: turnedOn.disabled },
  turnedOff: { checked: turnedOff.checked, disabled: turnedOff.disabled },
  noRaw: { checked: noRaw.checked, disabled: noRaw.disabled, note: noRaw.note },
  noRawPuts: putsAfterNoRaw - putsBefore,
  memory: state.prefs.export_hdr,
  rawAgain: { checked: rawAgain.checked, disabled: rawAgain.disabled, note: rawAgain.note },
}));
