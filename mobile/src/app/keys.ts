// 键盘和鼠标（电脑浏览器）：一次 Esc 只关最上面的一层，面板打开时焦点进面板、Tab 在面板里转、关上回到打开它的那个按钮。
// 开机时装一次（boot.ts）。所有面板都是「.scrim + 面板」一起挂上去的，这里看着 DOM 记下打开的先后；
// 在 sheet_lifecycle.ts 登记过的编辑器面板，Esc 就是「取消」，调它登记的 onCancel；其余的等于点一下遮罩。
// 自己处理 Esc 的面板（导出、关机图、下载兜底）先拿到按键并 preventDefault，这里就不再动别的。

export interface KeyHooks {
  screen(): string;
  back(): void;
}

/** 网格的选择模式（「选择」和「选要套用的照片」）。 */
export interface GridMode {
  active(): boolean;
  leave(): void;
  selectAll(): void;
}

interface Layer {
  scrim: HTMLElement;
  sheet: HTMLElement | null;
  cancel: (() => void) | null;
  cancelLive: () => boolean;
  opener: HTMLElement | null;
  /** 面板自己管焦点（打开时焦点已经在里面）：不抢焦点、不圈 Tab、不还焦点。 */
  own: boolean;
}

const FOCUSABLE = 'button:not([disabled]), a[href], input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
const NOT_TEXT = new Set(['range', 'checkbox', 'radio', 'button', 'submit', 'reset', 'file', 'color', 'image']);
const layers: Layer[] = [];
let pending: Array<{ cancel: () => void; live: () => boolean }> = [];
let gridMode: GridMode | null = null;
let pressed = 0;
let lastPressed: HTMLElement | null = null;
let installed = false;

/** 文本输入框里的按键（Esc、Cmd+A、方向键）只归输入框自己。 */
export function inTextField(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return true;
  return target instanceof HTMLInputElement && !NOT_TEXT.has(target.type);
}

/** 自己用方向键的控件：滑块、旋转尺、菜单、列表、输入框。 */
export function arrowsOwned(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (inTextField(target) || (target instanceof HTMLInputElement && (target.type === 'range' || target.type === 'radio'))) return true;
  return !!target.closest('[role="slider"], [role="menu"], [role="menuitem"], [role="listbox"], [role="radiogroup"], [role="spinbutton"], [popover], .ruler');
}

/** Mac 上是 Cmd，别的系统是 Ctrl。 */
export function modKey(ev: MouseEvent | KeyboardEvent): boolean {
  const mac = /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent);
  return mac ? ev.metaKey : ev.ctrlKey;
}

/** 有没有还开着的面板（网格、开始页、编辑器的都算）。 */
export function sheetOpen(): boolean {
  return liveLayers().length > 0;
}

export function setGridMode(mode: GridMode | null, owner?: GridMode): void {
  if (mode || !owner || gridMode === owner) gridMode = mode;
}

/** sheet_lifecycle.registerSheet 调：同一轮挂上的遮罩就是这个面板的，Esc 调它的 onCancel。 */
export function noteRegistered(cancel: () => void, live: () => boolean): void {
  pending.push({ cancel, live });
}

function alive(layer: Layer): boolean {
  return layer.scrim.isConnected && !layer.scrim.hidden && (layer.sheet?.isConnected ?? true);
}

function liveLayers(): Layer[] {
  return layers.filter(alive);
}

function focusables(sheet: HTMLElement): HTMLElement[] {
  return [...sheet.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((node) => node.getClientRects().length > 0);
}

function current(): HTMLElement | null {
  const active = document.activeElement;
  return active instanceof HTMLElement && active !== document.body && active.isConnected ? active : null;
}

function fallback(shell: HTMLElement, hooks: KeyHooks): HTMLElement | null {
  if (hooks.screen() === 'editor') return shell.querySelector<HTMLElement>('[data-act="editor-more"]');
  return shell.querySelector<HTMLElement>('.gscroll .cell');
}

function restore(layer: Layer, shell: HTMLElement, hooks: KeyHooks): void {
  if (layer.own) return;
  const active = current();
  if (active && !layer.sheet?.contains(active) && !layer.scrim.contains(active)) return;
  const target = layer.opener?.isConnected ? layer.opener : fallback(shell, hooks);
  target?.focus({ preventScroll: true });
}

function addLayer(scrim: HTMLElement, opener: HTMLElement | null): Layer {
  const next = scrim.nextElementSibling;
  const sheet = next instanceof HTMLElement && !next.classList.contains('scrim') ? next : null;
  const active = current();
  const own = !!(sheet && active && sheet.contains(active));
  const layer: Layer = { scrim, sheet, cancel: null, cancelLive: () => false, opener: own ? lastPressed : opener, own };
  layers.push(layer);
  // 先给按钮：文字框一拿到焦点，手机上就会弹出键盘。
  if (!own && sheet) focusables(sheet).find((node) => !inTextField(node) && !(node instanceof HTMLInputElement && node.type === 'file'))?.focus({ preventScroll: true });
  return layer;
}

function flush(records: MutationRecord[], shell: HTMLElement, hooks: KeyHooks): void {
  const opener = current() ?? (lastPressed?.isConnected ? lastPressed : null);
  // 先收掉关上的（还焦点），再记新开的：从「更多」点进「曲线」时，新面板接着用旧面板的打开者。
  let inherited: HTMLElement | null = null;
  for (let i = layers.length - 1; i >= 0; i -= 1) {
    const layer = layers[i];
    if (alive(layer)) continue;
    layers.splice(i, 1);
    inherited ??= layer.opener;
    restore(layer, shell, hooks);
  }
  const added: Layer[] = [];
  for (const record of records) {
    for (const node of record.addedNodes) {
      if (!(node instanceof HTMLElement)) continue;
      const scrims = node.classList.contains('scrim') ? [node] : [...node.querySelectorAll<HTMLElement>('.scrim')];
      for (const scrim of scrims) {
        if (scrim.isConnected && !scrim.hidden && !layers.some((layer) => layer.scrim === scrim)) added.push(addLayer(scrim, inherited ?? opener));
      }
    }
  }
  const reg = pending;
  pending = [];
  const last = added[added.length - 1];
  const fresh = reg.filter((item) => item.live());
  const pick = fresh[fresh.length - 1];
  if (last && pick) {
    last.cancel = pick.cancel;
    last.cancelLive = pick.live;
  }
}

/**
 * 全图（面板收起来）时先展开，下一次 Esc 才回网格。收起时点任一工具按钮就是展开。
 * studio/desktop-stage 合进来以后，整个函数体换成一行：`return leaveStageFullView();`（stage_zoom.ts）。
 */
function leaveFullView(shell: HTMLElement): boolean {
  const full = shell.querySelector<HTMLElement>('.screen.editor[data-collapsed="1"] .toolbar .tbtn[data-tool="recipe"]');
  full?.click();
  return !!full;
}

function escape(ev: KeyboardEvent, shell: HTMLElement, hooks: KeyHooks): void {
  // 滑块或裁剪框还按着：不切屏也不关面板，松手后一切照常（不会留下 data-drag="1"）。
  if (pressed > 0) return;
  // 原生弹出层（配方条的菜单）由浏览器自己用 Esc 关。
  try {
    if (document.querySelector(':popover-open')) return;
  } catch {
    /* 老浏览器没有 :popover-open。 */
  }
  const top = liveLayers().pop();
  if (top) {
    ev.preventDefault();
    if (top.cancel && top.cancelLive()) top.cancel();
    else top.scrim.click();
    return;
  }
  const screen = hooks.screen();
  if (screen === 'grid' && gridMode?.active()) {
    ev.preventDefault();
    gridMode.leave();
  } else if (screen === 'editor') {
    ev.preventDefault();
    if (!leaveFullView(shell)) hooks.back();
  }
}

function trapTab(ev: KeyboardEvent): void {
  const top = liveLayers().pop();
  if (!top || top.own || !top.sheet) return;
  const nodes = focusables(top.sheet);
  if (!nodes.length) return;
  // 自己走一格：Safari 默认 Tab 跳过按钮，不能靠浏览器。
  ev.preventDefault();
  const active = current();
  const at = active ? nodes.indexOf(active) : -1;
  const next = at < 0 ? (ev.shiftKey ? nodes.length - 1 : 0) : (at + (ev.shiftKey ? -1 : 1) + nodes.length) % nodes.length;
  nodes[next].focus({ preventScroll: true });
}

function onKey(ev: KeyboardEvent, shell: HTMLElement, hooks: KeyHooks): void {
  if (ev.defaultPrevented || ev.isComposing) return;
  if (ev.key === 'Tab' && !ev.altKey && !ev.metaKey && !ev.ctrlKey) {
    trapTab(ev);
    return;
  }
  if (inTextField(ev.target)) return;
  if (ev.key === 'Escape' && !ev.altKey && !ev.metaKey && !ev.ctrlKey && !ev.shiftKey) {
    escape(ev, shell, hooks);
    return;
  }
  if ((ev.key === 'a' || ev.key === 'A') && modKey(ev) && !ev.altKey && !ev.shiftKey) {
    if (hooks.screen() !== 'grid' || sheetOpen() || !gridMode?.active()) return;
    ev.preventDefault();
    gridMode.selectAll();
  }
}

/** 开机装一次：Esc、Tab 圈在面板里、面板开关时的焦点。 */
export function installKeys(shell: HTMLElement, hooks: KeyHooks): void {
  if (installed) return;
  installed = true;
  window.addEventListener('keydown', (ev) => onKey(ev, shell, hooks));
  window.addEventListener(
    'pointerdown',
    (ev) => {
      pressed += 1;
      const hit = ev.target instanceof Element ? ev.target.closest<HTMLElement>(FOCUSABLE) : null;
      if (hit && !hit.classList.contains('scrim')) lastPressed = hit;
    },
    true,
  );
  const release = () => {
    pressed = Math.max(0, pressed - 1);
  };
  window.addEventListener('pointerup', release, true);
  window.addEventListener('pointercancel', release, true);
  window.addEventListener('blur', () => {
    pressed = 0;
  });
  // 在窗口外松手时收不到 pointerup：再动一下、手里没按着，就不算按着了。
  window.addEventListener(
    'pointermove',
    (ev) => {
      if (pressed && ev.buttons === 0 && ev.pointerType === 'mouse') pressed = 0;
    },
    { capture: true, passive: true },
  );
  new MutationObserver((records) => flush(records, shell, hooks)).observe(shell, { childList: true, subtree: true });
}
