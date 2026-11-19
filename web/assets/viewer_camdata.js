// 相机数据第一次读到、或就绪与否变了（启动时面板先画、状态后到；轮询失败后又恢复）：配方面板要按新状态重建，
// 不然配方卡一直灰着。只动配方面板，不打扰正在用的别的面板：
// - 配方面板还没建过：第一次显示时自然按新状态建；
// - 当前不在「配方」：只标记配方面板过期，下次切到「配方」时重建（showTab 处理 dirty）；
// - 在「配方」且正在拖旋钮（beginChange 之后、endChange 之前）：交给 endChange 刷新（W01）；
// - 在「配方」且焦点在配方面板里（比如在输入名字）：等焦点离开配方面板再重建。

import { on } from './state.js';
import { currentView, paintTabMods, refreshPanes } from './viewer.js';

on('camdata-ready-changed', () => {
  const v = currentView();
  if (!v || !v.ed) return;
  const pane = v.panes?.recipe;
  if (!pane?.built) return;
  if (v.tab !== 'recipe') {
    pane.dirty = true;
    return;
  }
  if (v.pending) {
    v.panesStale = true;
    paintTabMods(v);
    return;
  }
  const el = pane.content;
  if (el && el.contains(document.activeElement)) {
    const onOut = (e) => {
      if (el.contains(e.relatedTarget)) return; // 焦点只是在配方面板里移动：继续等
      el.removeEventListener('focusout', onOut);
      setTimeout(() => {
        if (currentView() !== v || pane.content !== el) return; // 换了照片，或面板已经重建过
        if (v.tab !== 'recipe') pane.dirty = true;
        else if (v.pending) v.panesStale = true;
        else refreshPanes();
      }, 0);
    };
    el.addEventListener('focusout', onOut);
    return;
  }
  refreshPanes();
});
