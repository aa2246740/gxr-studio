// 相机数据第一次读到、或就绪与否变了（启动时面板先画、状态后到；轮询失败后又恢复）：配方面板按新状态重画，
// 不然配方卡一直灰着。正在拖曲线、滑块（beginChange 之后、endChange 之前），或正在面板里输入文字时不重建面板：
// 标记一下，等这一步结束（endChange）再刷新，和重试保存的做法一样（W01）。

import { on } from './state.js';
import { currentView, paintTabMods, refreshPanes } from './viewer.js';

const TYPING = 'input:not([type=range]):not([type=checkbox]):not([type=radio]), textarea, [contenteditable="true"]';

on('camdata-ready-changed', () => {
  const v = currentView();
  if (!v || !v.ed) return;
  const typing = document.activeElement?.matches?.(TYPING) ?? false;
  if (v.pending || typing) {
    v.panesStale = true;
    paintTabMods(v);
    return;
  }
  refreshPanes();
});
