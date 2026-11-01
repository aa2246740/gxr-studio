// 引导、配方页提示、HDR 开关。只用 Node 自带的能力，不碰系统界面。
import { pathToFileURL } from 'node:url';

const dir = process.argv[2];
const fileUrl = (name) => pathToFileURL(`${dir}/${name}`).href;

class TestNode extends EventTarget {
  constructor(tag = '', text = '') {
    super();
    this.tagName = String(tag).toUpperCase();
    this.text = text;
    this.children = [];
    this.parentNode = null;
    this.style = {};
    this.dataset = {};
    this.attrs = {};
    this.className = '';
    this.disabled = false;
    this.checked = false;
    this.value = '';
    this.type = '';
  }

  get textContent() {
    return this.text + this.children.map((child) => child.textContent).join('');
  }

  set textContent(value) {
    this.text = String(value);
    this.children = [];
  }

  get isConnected() {
    return this === document.body || !!(this.parentNode && this.parentNode.isConnected);
  }

  append(...children) {
    for (const child of children) {
      if (child == null || child === false) continue;
      const node = typeof child === 'string' ? document.createTextNode(child) : child;
      node.parentNode = this;
      this.children.push(node);
    }
  }

  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }

  setAttribute(name, value) {
    this.attrs[name] = String(value);
  }

  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
  }

  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
    this.parentNode = null;
  }

  focus() {
    document.activeElement = this;
  }

  click() {
    if (this.disabled) return;
    this.dispatchEvent(new Event('click'));
  }
}

const body = new TestNode('body');
const modalRoot = new TestNode('div');
const toastRoot = new TestNode('div');
const roots = { 'modal-root': modalRoot, 'toast-root': toastRoot };
globalThis.Node = TestNode;
globalThis.HTMLElement = TestNode;
globalThis.document = {
  body,
  activeElement: null,
  createElement: (tag) => new TestNode(tag),
  createTextNode: (text) => new TestNode('', text),
  getElementById: (id) => roots[id] || null,
};
body.append(modalRoot, toastRoot);

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

function click(text) {
  const found = walk(modalRoot).filter((node) => node.tagName === 'BUTTON' && node.textContent === text);
  if (found.length !== 1) throw new Error(`按钮「${text}」有 ${found.length} 个`);
  found[0].click();
}

function part(name) {
  const found = walk(modalRoot).filter((node) => node.dataset && node.dataset.guide === name);
  if (found.length !== 1) throw new Error(`引导字段 ${name} 有 ${found.length} 个`);
  return found[0].textContent;
}

function resetPrefs() {
  globalThis.uiStore.onboarding_dismissed = false;
  globalThis.uiStore.export_hdr = false;
  globalThis.uiState.prefs = { onboarding_dismissed: false, export_hdr: false };
  globalThis.uiCalls.length = 0;
}

const flush = async () => {
  await Promise.resolve();
  await Promise.resolve();
};

const { state } = await import(fileUrl('state.js'));
const { closeTopModal } = await import(fileUrl('dom.js'));
const onboarding = await import(fileUrl('onboarding.js'));
const lut = await import(fileUrl('lut.js'));
const exp = await import(fileUrl('export.js'));

const seen = [];
onboarding.maybeShowOnboarding();
for (let i = 0; i < 4; i += 1) {
  seen.push([part('title'), part('text'), part('count')]);
  if (i < 3) click('下一步');
}
click('上一步');
const back = part('title');
click('下一步');
const last = part('title');
click('完成');
await flush();
const afterFinishOpen = modalRoot.children.length;
onboarding.maybeShowOnboarding();
const hiddenAfterFinish = modalRoot.children.length;
const finished = globalThis.uiCalls.filter((call) => call.path === '/api/prefs' && call.method === 'PUT').map((call) => call.body);

resetPrefs();
onboarding.maybeShowOnboarding();
click('跳过');
await flush();
const afterSkipOpen = modalRoot.children.length;
onboarding.maybeShowOnboarding();
const hiddenAfterSkip = modalRoot.children.length;
const skipped = globalThis.uiCalls.filter((call) => call.path === '/api/prefs' && call.method === 'PUT').map((call) => call.body);

onboarding.showHelpMenu();
click('引导');
const helpTitle = part('title');
click('✕');
await onboarding.showSettings();
click('打开');
const settingsTitle = part('title');
click('✕');

const view = document.createElement('div');
body.append(view);
lut.renderLutPage(view);
await flush();
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
await flush();
const turnedOn = hdrBox(exp.exportForm({ ids: ['raw'], label: '这一张', photos }));
turnedOn.input.checked = false;
turnedOn.input.dispatchEvent(new Event('change'));
await flush();
const turnedOff = hdrBox(exp.exportForm({ ids: ['raw'], label: '这一张', photos }));
state.prefs = { ...state.prefs, export_hdr: true };
globalThis.uiStore.export_hdr = true;
const putsBefore = globalThis.uiCalls.filter((call) => call.path === '/api/prefs' && call.method === 'PUT').length;
const noRaw = hdrBox(exp.exportForm({ ids: ['jpg'], label: '这一张', photos }));
const putsAfterNoRaw = globalThis.uiCalls.filter((call) => call.path === '/api/prefs' && call.method === 'PUT').length;
const rawAgain = hdrBox(exp.exportForm({ ids: ['raw'], label: '这一张', photos }));

console.log(JSON.stringify({
  seen,
  back,
  last,
  afterFinishOpen,
  hiddenAfterFinish,
  finished,
  afterSkipOpen,
  hiddenAfterSkip,
  skipped,
  helpTitle,
  settingsTitle,
  hint: hints.map((node) => node.textContent),
  first: { checked: first.checked, disabled: first.disabled, note: first.note },
  turnedOn: { checked: turnedOn.checked, disabled: turnedOn.disabled },
  turnedOff: { checked: turnedOff.checked, disabled: turnedOff.disabled },
  noRaw: { checked: noRaw.checked, disabled: noRaw.disabled, note: noRaw.note },
  noRawPuts: putsAfterNoRaw - putsBefore,
  memory: state.prefs.export_hdr,
  rawAgain: { checked: rawAgain.checked, disabled: rawAgain.disabled, note: rawAgain.note },
}));
