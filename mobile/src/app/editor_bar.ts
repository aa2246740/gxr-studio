// 编辑器底栏（56pt + 安全区）：四个带图标的工具键（配方 · 调整 · 裁剪 · 更多）+ 右侧主按钮「存到相册」。

import { button, el, icon } from './dom';

export type BarTool = 'recipe' | 'adjust' | 'crop' | 'more';
export type BandTool = 'recipe' | 'adjust' | 'crop' | 'frame' | 'stamp';

export interface EditedFlags {
  recipe: boolean;
  adjust: boolean;
  crop: boolean;
  more: boolean;
}

const TOOL_ICONS: Record<BarTool, string> = {
  // 胶片 / 色彩配方
  recipe: 'M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6zM8 4v16M16 4v16M4 9h4M4 15h4M16 9h4M16 15h4',
  // 三道调色滑块
  adjust: 'M4 7h6M14 7h6M12 7a2 2 0 1 1-4 0 2 2 0 0 1 4 0zM4 12h10M18 12h2M16 12a2 2 0 1 1-4 0 2 2 0 0 1 4 0zM4 17h4M12 17h8M10 17a2 2 0 1 1-4 0 2 2 0 0 1 4 0z',
  // 裁剪框
  crop: 'M6 2v14a2 2 0 0 0 2 2h14M18 22V8a2 2 0 0 0-2-2H2',
  // 更多（三点）
  more: 'M5 12h.01M12 12h.01M19 12h.01',
};

const TOOL_SPECS: ReadonlyArray<{ id: BarTool; label: string; isTab: boolean }> = [
  { id: 'recipe', label: '配方', isTab: true },
  { id: 'adjust', label: '调整', isTab: true },
  { id: 'crop', label: '裁剪', isTab: true },
  { id: 'more', label: '更多', isTab: false },
];

export interface EditorBarHooks {
  onTool(tool: BarTool): void;
  onExport(): void;
}

export class EditorBar {
  private readonly root: HTMLElement;
  private readonly buttons = new Map<BarTool, HTMLButtonElement>();
  private readonly saveBtn: HTMLButtonElement;

  constructor(hooks: EditorBarHooks) {
    this.root = el('div', 'toolbar');
    const toolsWrap = el('div', 'toolbar-tools');
    toolsWrap.setAttribute('role', 'tablist');

    for (const spec of TOOL_SPECS) {
      const btn = button('tbtn', '');
      btn.dataset.tool = spec.id;
      if (spec.isTab) {
        btn.setAttribute('role', 'tab');
        btn.setAttribute('aria-selected', spec.id === 'recipe' ? 'true' : 'false');
      } else {
        btn.setAttribute('aria-haspopup', 'dialog');
      }
      const ico = icon(TOOL_ICONS[spec.id]);
      if (spec.id === 'more') ico.setAttribute('stroke-width', '2.5');
      btn.append(ico, el('span', 'tlab', spec.label));
      btn.addEventListener('click', () => hooks.onTool(spec.id));
      this.buttons.set(spec.id, btn);
      toolsWrap.append(btn);
    }

    this.saveBtn = button('btn primary save-btn', '存到相册');
    this.saveBtn.dataset.act = 'export';
    this.saveBtn.addEventListener('click', () => hooks.onExport());

    this.root.append(toolsWrap, this.saveBtn);
  }

  el(): HTMLElement {
    return this.root;
  }

  toolButton(id: BarTool): HTMLButtonElement | null {
    return this.buttons.get(id) ?? null;
  }

  setActive(activeBand: BandTool, collapsed: boolean): void {
    const activeBar: BarTool = activeBand === 'frame' || activeBand === 'stamp' ? 'more' : activeBand;
    this.root.dataset.collapsed = collapsed ? '1' : '0';
    for (const [id, btn] of this.buttons) {
      const on = !collapsed && id === activeBar;
      btn.classList.toggle('on', on);
      if (id !== 'more') btn.setAttribute('aria-selected', on ? 'true' : 'false');
    }
  }

  setEdited(flags: EditedFlags): void {
    for (const id of ['recipe', 'adjust', 'crop', 'more'] as const) {
      this.buttons.get(id)?.classList.toggle('edited', flags[id]);
    }
  }
}
