// iPad / 横屏布局与编辑器子视图的桥接：直方图面板槽、配方/LUT 卡片横条在面板与底条间的无损挂载切换。

import { button, el, icon } from './dom';
import type { HistWidget } from './editor_hist';
import { NO_GPU_DETAIL, NO_GPU_TITLE } from './editor_media';
import { IpadDock } from './ipad_dock';
import { isIpadLayout, watchViewportLayout, type LayoutMode, type LayoutWatch } from './layout';
import type { LutView } from './lut_view';
import type { AppApi, Photo } from './model';
import { MorePanel } from './more_panel';
import type { MoreSheetHooks } from './more_sheet';
import type { RecipeView } from './recipe_view';

const HIST_ICON = 'M4 19V11M9 19V6M14 19V13M19 19V8M3 19h18';
const WARN_ICON = 'M12 4l9 16H3zM12 10v4M12 17h.01';

/** 无 WebGPU 时的说明条（iPad 放直方图区，手机横屏放带子顶部；有 GPU 时不创建）。 */
export function createNoGpuNotice(): HTMLElement {
  const box = el('div', 'nogpu-notice');
  box.dataset.nogpuNotice = '1';
  const mark = el('span', 'warn');
  mark.setAttribute('aria-hidden', 'true');
  mark.append(icon(WARN_ICON));
  const tx = el('div', 'tx');
  tx.append(el('b', 'nogpu-title', NO_GPU_TITLE), el('small', 'nogpu-detail', NO_GPU_DETAIL));
  box.append(mark, tx);
  return box;
}

export interface IpadHistArea {
  root: HTMLElement;
  slot: HTMLElement;
  btn: HTMLButtonElement;
  sync(open: boolean): void;
}

export function createIpadHistArea(onToggle: () => void): IpadHistArea {
  const root = el('div', 'ipad-hist-area');
  const head = el('div', 'phead');
  const btn = button('histbtn', '');
  btn.dataset.act = 'hist-toggle';
  btn.setAttribute('aria-label', '直方图');
  btn.setAttribute('aria-pressed', 'true');
  btn.append(icon(HIST_ICON));
  btn.addEventListener('click', onToggle);
  head.append(btn);

  const slot = el('div', 'ipad-hist-slot');
  root.append(head, slot);

  return {
    root,
    slot,
    btn,
    sync(open: boolean) {
      btn.classList.toggle('on', open);
      btn.setAttribute('aria-pressed', open ? 'true' : 'false');
      slot.hidden = !open;
    },
  };
}

/** 按当前版式把同一个直方图画布挂到 iPad 面板顶槽或手机舞台浮窗。 */
export function mountHistForLayout(
  mode: LayoutMode,
  histOpen: boolean,
  histArea: IpadHistArea | null,
  stage: HTMLElement | null,
  histWidget: HistWidget | null,
  hasGpu = true,
): void {
  if (!histWidget) return;
  if (!hasGpu) {
    histWidget.root.hidden = true;
    histArea?.sync(false);
    return;
  }
  const ipad = isIpadLayout(mode);
  if (ipad && histArea) {
    histWidget.root.classList.remove('hist-float');
    histWidget.root.style.removeProperty('left');
    histWidget.root.style.removeProperty('right');
    histWidget.root.style.removeProperty('top');
    histWidget.root.style.removeProperty('bottom');
    delete histWidget.root.dataset.place;
    if (histWidget.root.parentElement !== histArea.slot) {
      histArea.slot.append(histWidget.root);
    }
    histWidget.root.hidden = !histOpen;
    histArea.sync(histOpen);
    return;
  }
  histWidget.root.classList.add('hist-float');
  if (stage && histWidget.root.parentElement !== stage) {
    stage.append(histWidget.root);
  }
  histWidget.root.hidden = !histOpen;
}

/** 按当前版式把配方/LUT 卡片横条挂到 iPad 底部常驻槽，或放回手机单带面板内。 */
export function syncStripsForLayout(
  mode: LayoutMode,
  recipeSub: 'recipe' | 'lut',
  ipadDock: IpadDock | null,
  recipes: RecipeView | null,
  luts: LutView | null,
): void {
  const ipad = isIpadLayout(mode);
  recipes?.setIpadMode(ipad);
  luts?.setIpadMode(ipad);

  if (ipad && ipadDock) {
    const host = ipadDock.stripHostEl();
    if (recipes) {
      const wrap = recipes.stripWrapEl();
      if (wrap.parentElement !== host) host.append(wrap);
      wrap.hidden = recipeSub !== 'recipe';
    }
    if (luts) {
      const wrap = luts.stripWrapEl();
      if (wrap.parentElement !== host) host.append(wrap);
      wrap.hidden = recipeSub !== 'lut';
    }
    return;
  }

  if (recipes) {
    const wrap = recipes.stripWrapEl();
    const panel = recipes.panelEl();
    if (wrap.parentElement !== panel) {
      const sw = panel.querySelector(':scope > .mode-switch');
      if (sw) sw.after(wrap);
      else panel.prepend(wrap);
    }
    wrap.hidden = recipes.isFineTuning();
  }
  if (luts) {
    const wrap = luts.stripWrapEl();
    const panel = luts.panelEl();
    if (wrap.parentElement !== panel) {
      const sw = panel.querySelector(':scope > .mode-switch');
      if (sw) sw.after(wrap);
      else panel.prepend(wrap);
    }
    wrap.hidden = false;
  }
}

/**
 * iPad 才有的两块（面板顶上的直方图槽、照片下面的两条）只在 iPad 版式时挂进 DOM，
 * 手机上整个摘掉：手机竖屏的 DOM 和以前一样，选择器也不会多出一份同名的隐藏控件。
 */
export function attachIpadNodes(
  ipad: boolean,
  histRoot: HTMLElement,
  band: HTMLElement | null,
  dock: HTMLElement,
  screen: HTMLElement | null,
): void {
  if (!ipad || !band || !screen) {
    histRoot.remove();
    dock.remove();
    return;
  }
  if (histRoot.parentElement !== band) band.insertBefore(histRoot, band.querySelector(':scope > .band-body'));
  if (dock.parentElement !== screen) screen.append(dock);
}

export interface EditorIpadBridge {
  mode(): LayoutMode;
  isIpad(): boolean;
  morePanelEl(): HTMLElement;
  refreshMore(photo: Photo): void;
  syncHistUi(open: boolean): void;
  syncSub(sub: 'recipe' | 'lut', lutActive: boolean): void;
  apply(opts: {
    mode: LayoutMode;
    histOpen: boolean;
    recipeSub: 'recipe' | 'lut';
    lutActive: boolean;
    band: HTMLElement | null;
    stage: HTMLElement | null;
    histWidget: HistWidget | null;
    recipes: RecipeView | null;
    luts: LutView | null;
  }): void;
  destroy(): void;
}

export function createEditorIpadBridge(opts: {
  shell: HTMLElement;
  api: AppApi;
  photo: Photo;
  hasGpu: boolean;
  moreHooks: MoreSheetHooks;
  onLayoutChange: (mode: LayoutMode) => void;
  onToggleHist: () => void;
  onSubChange: (sub: 'recipe' | 'lut') => void;
  onRecipeFilter: () => void;
}): EditorIpadBridge {
  const watch: LayoutWatch = watchViewportLayout(opts.shell, (mode) => opts.onLayoutChange(mode));
  const histArea = createIpadHistArea(opts.onToggleHist);
  const noGpuNotice = opts.hasGpu ? null : createNoGpuNotice();
  if (!opts.hasGpu) histArea.root.dataset.nogpu = '1';
  const morePanel = new MorePanel(opts.photo, opts.moreHooks);
  const dock = new IpadDock(opts.api, opts.photo.id, {
    onSubChange: opts.onSubChange,
    onRecipeFilter: opts.onRecipeFilter,
  });

  return {
    mode: () => watch.mode(),
    isIpad: () => isIpadLayout(watch.mode()),
    morePanelEl: () => morePanel.panelEl(),
    refreshMore: (photo) => morePanel.refresh(photo),
    syncHistUi: (open) => histArea.sync(opts.hasGpu && open),
    syncSub: (sub, lutActive) => dock.syncSub(sub, lutActive),
    apply: (s) => {
      const ipad = isIpadLayout(s.mode);
      attachIpadNodes(ipad, histArea.root, s.band, dock.el(), s.stage?.parentElement ?? null);
      mountHistForLayout(s.mode, s.histOpen, histArea, s.stage, s.histWidget, opts.hasGpu);
      if (noGpuNotice) {
        if (ipad) {
          if (noGpuNotice.parentElement !== histArea.root) histArea.root.append(noGpuNotice);
        } else if (s.mode === 'phone-landscape' && s.band) {
          const body = s.band.querySelector(':scope > .band-body');
          if (body && noGpuNotice.parentElement !== body) body.prepend(noGpuNotice);
        } else {
          noGpuNotice.remove();
        }
      }
      syncStripsForLayout(s.mode, s.recipeSub, dock, s.recipes, s.luts);
      dock.syncSub(s.recipeSub, s.lutActive);
      dock.setActive(ipad);
    },
    destroy: () => {
      watch.destroy();
      dock.destroy();
      dock.el().remove();
      histArea.root.remove();
      noGpuNotice?.remove();
    },
  };
}
