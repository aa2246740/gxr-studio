// iPad / 横屏布局与编辑器子视图的桥接：直方图面板槽、配方/LUT 卡片横条在面板与底条间的无损挂载切换。

import { button, el, icon } from './dom';
import type { HistWidget } from './editor_hist';
import { IpadDock } from './ipad_dock';
import { isIpadLayout, watchViewportLayout, type LayoutMode, type LayoutWatch } from './layout';
import type { LutView } from './lut_view';
import type { AppApi, Photo } from './model';
import { MorePanel } from './more_panel';
import type { MoreSheetHooks } from './more_sheet';
import type { RecipeView } from './recipe_view';

const HIST_ICON = 'M4 19V11M9 19V6M14 19V13M19 19V8M3 19h18';

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
): void {
  if (!histWidget) return;
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
    if (wrap.parentElement !== panel) panel.prepend(wrap);
    wrap.hidden = recipes.isFineTuning();
  }
  if (luts) {
    const wrap = luts.stripWrapEl();
    const panel = luts.panelEl();
    if (wrap.parentElement !== panel) panel.prepend(wrap);
    wrap.hidden = false;
  }
}

export interface EditorIpadBridge {
  mode(): LayoutMode;
  isIpad(): boolean;
  dockEl(): HTMLElement;
  histAreaEl(): HTMLElement;
  morePanelEl(): HTMLElement;
  refreshMore(photo: Photo): void;
  syncHistUi(open: boolean): void;
  syncSub(sub: 'recipe' | 'lut', lutActive: boolean): void;
  apply(opts: {
    mode: LayoutMode;
    histOpen: boolean;
    recipeSub: 'recipe' | 'lut';
    lutActive: boolean;
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
  moreHooks: MoreSheetHooks;
  onLayoutChange: (mode: LayoutMode) => void;
  onToggleHist: () => void;
  onSubChange: (sub: 'recipe' | 'lut') => void;
  onRecipeFilter: () => void;
}): EditorIpadBridge {
  const watch: LayoutWatch = watchViewportLayout(opts.shell, (mode) => opts.onLayoutChange(mode));
  const histArea = createIpadHistArea(opts.onToggleHist);
  const morePanel = new MorePanel(opts.photo, opts.moreHooks);
  const dock = new IpadDock(opts.api, opts.photo.id, {
    onSubChange: opts.onSubChange,
    onRecipeFilter: opts.onRecipeFilter,
  });

  return {
    mode: () => watch.mode(),
    isIpad: () => isIpadLayout(watch.mode()),
    dockEl: () => dock.el(),
    histAreaEl: () => histArea.root,
    morePanelEl: () => morePanel.panelEl(),
    refreshMore: (photo) => morePanel.refresh(photo),
    syncHistUi: (open) => histArea.sync(open),
    syncSub: (sub, lutActive) => dock.syncSub(sub, lutActive),
    apply: (s) => {
      mountHistForLayout(s.mode, s.histOpen, histArea, s.stage, s.histWidget);
      syncStripsForLayout(s.mode, s.recipeSub, dock, s.recipes, s.luts);
      dock.syncSub(s.recipeSub, s.lutActive);
    },
    destroy: () => {
      watch.destroy();
      dock.destroy();
    },
  };
}
