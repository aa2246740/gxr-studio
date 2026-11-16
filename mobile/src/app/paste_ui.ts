// 复制调整（04a）、套用到哪里（04b）、套用前确认（04c）和网格底栏（02a「已复制」、04d、04e）。
// 字都是设计稿和桌面 paste_core.js 的原话；逻辑在 lib/paste.ts 和 paste_state.ts。

import { groupByGap } from '../lib/group';
import { shelf, shelves } from '../lib/recipes';
import { NOT_SET, PASTE_GROUPS, chosenGroups, rememberGroups, type GroupId } from '../lib/paste_text';
import { groupsPresent } from '../lib/paste';
import { button, el, icon } from './dom';
import { photoMs, type AppApi, type Knobs, type Photo } from './model';
import { groupsAnd, groupsText, pasteState, rangeOf } from './paste_state';
import './paste.css';

export const PASTE_ICON = {
  check: 'M5 12.5l4.5 4.5L19 7.5',
  copy: 'M8.5 8.5h11.5v11.5H8.5zM15.5 8.5V7A2.5 2.5 0 0 0 13 4.5H7A2.5 2.5 0 0 0 4.5 7v6A2.5 2.5 0 0 0 7 15.5h1.5',
  undo: 'M9 14L4 9l5-5M4 9h9a6 6 0 0 1 0 12h-3',
} as const;

/** 调整的名字和格式（编辑器 SLIDERS 的名字、formatKnob 的写法）。 */
const KNOB_LABEL: Array<[keyof Knobs, string]> = [
  ['exposure_ev', '曝光'],
  ['contrast', '对比度'],
  ['highlight', '高光'],
  ['shadow', '阴影'],
  ['black_point', '黑点'],
  ['warm', '色温'],
  ['tint', '色调'],
  ['saturation', '饱和度'],
  ['vibrance', '自然饱和度'],
  ['vignette', '暗角'],
];

function knobText(key: keyof Knobs, value: number): string {
  if (key === 'exposure_ev') return value > 0 ? `+${value.toFixed(1)}` : value.toFixed(1);
  return value > 0 ? `+${value}` : String(value);
}

/** 04b 那句「Kodachrome 25，曝光 +0.7」：配方的名字和动过的调整。 */
export function clipSummary(api: AppApi): string {
  const clip = pasteState.clip;
  if (!clip) return '';
  const parts: string[] = [];
  const dat = api.book().dat;
  if (clip.recipe) {
    const card = dat ? shelf(shelves(dat.listing, clip.recipe.choice.unit), 'all').find((item) => item.id === clip.recipe?.choice.id) : null;
    if (card) parts.push(card.name);
  }
  if (clip.knobs) {
    for (const [key, label] of KNOB_LABEL) {
      const value = clip.knobs[key] ?? 0;
      if (value !== 0) parts.push(`${label} ${knobText(key, value)}`);
    }
  }
  return parts.join('，');
}

/** 照片现在的分组（当前的间隔和顺序）。 */
export function groupsNow(api: AppApi): ReturnType<typeof groupByGap> {
  return groupByGap(
    api.photos().map((photo) => ({ id: photo.id, ms: photoMs(photo) })),
    api.gap(),
    api.order(),
  );
}

function thumbs(api: AppApi, ids: readonly string[], source: string | null, small = false): HTMLElement {
  const row = el('div', small ? 'pthumbs sm' : 'pthumbs');
  for (const id of ids.slice(0, 6)) {
    const photo = api.photo(id);
    const box = el('div', id === source ? 't src' : 't');
    box.dataset.id = id;
    if (photo?.bitmap) {
      const canvas = document.createElement('canvas');
      canvas.width = photo.bitmap.width;
      canvas.height = photo.bitmap.height;
      canvas.getContext('2d')?.drawImage(photo.bitmap, 0, 0);
      box.append(canvas);
    }
    row.append(box);
  }
  return row;
}

interface Overlay {
  close(): void;
}

function overlay(host: HTMLElement, node: HTMLElement, onScrim: () => void): Overlay {
  const scrim = el('div', 'scrim paste-scrim');
  scrim.addEventListener('click', onScrim);
  host.append(scrim, node);
  return {
    close: () => {
      scrim.remove();
      node.remove();
    },
  };
}

/** 04a：选要复制的组。勾选记在本机（桌面同一个键），没设置过的组标「这张没有设置」。 */
export function openCopySheet(host: HTMLElement, api: AppApi, photo: Photo, onCopied: () => void): void {
  closePasteSheets(host);
  const chosen = new Set<GroupId>(chosenGroups());
  const present = new Set(groupsPresent(photo));
  const sheet = el('div', 'ms paste-sheet');
  sheet.dataset.state = 'copy-options';
  const head = el('div', 'hd');
  head.append(el('h2', '', '复制调整'));
  const cancel = button('nb', '取消');
  head.append(cancel);
  const rows = el('div', 'popts');
  const go = button('btn primary lg block', '');
  const note = el('p', 'pnote faint');
  note.hidden = true;
  const paint = (): void => {
    for (const row of rows.querySelectorAll<HTMLButtonElement>('.optrow')) {
      const on = chosen.has(row.dataset.group as GroupId);
      row.classList.toggle('on', on);
      row.setAttribute('aria-pressed', on ? 'true' : 'false');
      const cc = row.querySelector('.cc');
      cc?.classList.toggle('on', on);
      cc?.replaceChildren(...(on ? [icon(PASTE_ICON.check)] : []));
    }
    go.textContent = `复制 ${chosen.size} 项`;
    go.disabled = chosen.size === 0;
  };
  for (const [id, name, hint] of PASTE_GROUPS) {
    const row = button('optrow', '');
    row.dataset.group = id;
    const text = el('div', 'tx');
    const small = el('small', '', hint);
    if (!present.has(id)) small.append(el('i', '', NOT_SET));
    text.append(el('b', '', name), small);
    row.append(el('span', 'cc'), text);
    row.addEventListener('click', () => {
      if (chosen.has(id)) chosen.delete(id);
      else chosen.add(id);
      paint();
    });
    rows.append(row);
  }
  sheet.append(el('div', 'grab'), head, el('p', 'lead', '套用时只改勾选的项目，别的保持不变。勾选会记住，下次还是这几项。'), rows, note, go);
  const view = overlay(host, sheet, () => view.close());
  cancel.addEventListener('click', () => view.close());
  go.addEventListener('click', () => {
    const ids = PASTE_GROUPS.map((g) => g[0]).filter((id) => chosen.has(id));
    rememberGroups(ids);
    const result = pasteState.copy(api, photo, ids);
    if (!result.ok) {
      // 桌面：勾选的项目里没有修改，没有复制。剪贴板不变。
      note.textContent = result.line;
      note.hidden = false;
      return;
    }
    view.close();
    openWhereSheet(host, api, photo, result.line, onCopied);
  });
  paint();
}

/** 04b：复制好了，套用到这一组、自己选，或先不套用。 */
export function openWhereSheet(host: HTMLElement, api: AppApi, photo: Photo, title: string, onApplied: () => void): void {
  closePasteSheets(host);
  const sheet = el('div', 'ms paste-sheet');
  sheet.dataset.state = 'copied-where';
  const head = el('div', 'hd start');
  const ok = el('span', 'okc');
  ok.append(icon(PASTE_ICON.check));
  head.append(ok, el('h2', '', title));
  const summary = clipSummary(api);
  const lead = el('p', 'lead', `来自 ${photo.stem}${summary ? `：${summary}` : ''}。要套用到哪些照片？`);
  const group = groupsNow(api).find((item) => item.ids.includes(photo.id));
  const ids = group?.ids ?? [photo.id];
  const targets = pasteState.targets(api, ids);
  const card = el('div', 'gpick');
  const ghd = el('div', 'ghd');
  ghd.append(el('b', 'num', group?.rangeLabel ?? ''), el('span', 'faint', `这张所在的一组 · ${ids.length} 张`));
  const apply = button('btn primary lg block', `套用到这一组（${targets.length} 张）`);
  apply.dataset.act = 'apply-group';
  apply.disabled = targets.length === 0;
  card.append(ghd, thumbs(api, ids, photo.id), apply, el('p', 'gapline faint', `按拍摄时间自动分组：前后隔不到 ${api.gap()} 分钟的算一组`));
  const acts = el('div', 'acts');
  const pick = button('btn lg', '选照片…');
  const later = button('btn lg ghost', '先不套用');
  acts.append(pick, later);
  sheet.append(el('div', 'grab'), head, lead, card, acts, el('p', 'pfoot faint', '先不套用时，复制的内容会留着：回到照片网格，点组标题上的「套用到这一组」。'));
  const view = overlay(host, sheet, () => view.close());
  later.addEventListener('click', () => view.close());
  pick.addEventListener('click', () => {
    view.close();
    pasteState.pickNext = true;
    api.back();
  });
  apply.addEventListener('click', () => {
    view.close();
    openConfirm(host, api, {
      title: `套用到这一组的 ${targets.length} 张？`,
      body: groupBody(group?.rangeLabel ?? '', targets.length, true, photo.stem),
      targets,
      range: group?.rangeLabel ?? rangeOf(targets),
      afterApply: onApplied,
      leaveFirst: true,
    });
  });
}

/** 04c 的说明句。来源在这一组里时用设计稿的原句；否则去掉「除了这张以外」。 */
export function groupBody(range: string, n: number, sourceInside: boolean, stem: string): string {
  const groups = pasteState.clip ? groupsAnd(pasteState.clip.groups) : '';
  const who = sourceInside ? `${range} 这一组里，除了这张以外的 ${n} 张` : `${range} 这一组的 ${n} 张`;
  return `${who}会换成 ${stem} 的${groups}。别的设置不变。`;
}

/** 选的照片（04d、选择栏）的说明句。 */
export function pickBody(n: number, stem: string): string {
  const groups = pasteState.clip ? groupsAnd(pasteState.clip.groups) : '';
  return `选中的 ${n} 张会换成 ${stem} 的${groups}。别的设置不变。`;
}

export interface ConfirmOptions {
  title: string;
  body: string;
  targets: Photo[];
  range: string;
  afterApply: () => void;
  /** 从编辑器套用：先回到网格，结果写在网格的底栏上（设计稿 04e）。 */
  leaveFirst?: boolean;
  /** 点了「套用到 N 张」、开始套用之前（选择模式在这时退出）。 */
  onGo?: () => void;
}

/** 04c：套用前确认。 */
export function openConfirm(host: HTMLElement, api: AppApi, options: ConfirmOptions): void {
  closePasteSheets(host);
  const dlg = el('div', 'dlg paste-dlg');
  dlg.dataset.state = 'confirm';
  const acts = el('div', 'acts');
  const cancel = button('btn lg', '取消');
  const go = button('btn primary lg', `套用到 ${options.targets.length} 张`);
  go.dataset.act = 'confirm';
  acts.append(cancel, go);
  const strip = el('div', 'pcenter');
  strip.append(thumbs(api, options.targets.map((photo) => photo.id), null, true));
  dlg.append(el('h2', '', options.title), el('p', '', options.body), strip, el('p', 'pundo', '套用后可以撤销。存储卡上的照片不会变。'), acts);
  const view = overlay(host, dlg, () => view.close());
  cancel.addEventListener('click', () => view.close());
  go.addEventListener('click', () => {
    view.close();
    options.onGo?.();
    if (options.leaveFirst) api.back();
    void pasteState.apply(api, options.targets, options.range).then(() => options.afterApply());
  });
}

export function closePasteSheets(host: HTMLElement): void {
  for (const node of host.querySelectorAll('.paste-scrim, .paste-sheet, .paste-dlg')) node.remove();
}

/** 网格底栏：已复制（02a）、套用结果和撤销（04e）、一行提示。没有要写的就是 null。 */
export function pasteBar(api: AppApi, onChange: () => void): HTMLElement | null {
  const bar = pasteState.bar;
  const clip = pasteState.clip;
  const dock = el('div', 'dock pastebar');
  if (bar?.kind === 'applied') {
    dock.dataset.state = 'applied';
    const ok = el('span', 'okc');
    ok.append(icon(PASTE_ICON.check));
    const text = el('div', 'tx');
    text.append(el('b', '', bar.line), el('small', '', bar.sub));
    const undo = button('fbtn ctl', '撤销');
    undo.prepend(icon(PASTE_ICON.undo));
    undo.dataset.act = 'undo';
    undo.addEventListener('click', () => {
      pasteState.revert(api);
      onChange();
    });
    dock.append(ok, text, undo);
    return dock;
  }
  if (bar?.kind === 'info') {
    dock.dataset.state = 'info';
    const text = el('div', 'tx');
    text.append(el('b', '', bar.line));
    dock.append(text);
    return dock;
  }
  if (!clip) return null;
  dock.dataset.state = 'copied';
  const acc = el('span', 'accc');
  acc.append(icon(PASTE_ICON.copy));
  const text = el('div', 'tx');
  text.append(el('b', '', `已复制：${groupsText(clip.groups)}`), el('small', '', `来自 ${clip.from.stem} · 点组标题上的「套用到这一组」`));
  const drop = button('nb', '清除');
  drop.dataset.act = 'clear-clip';
  drop.addEventListener('click', () => {
    pasteState.drop();
    onChange();
  });
  dock.append(acc, text, drop);
  return dock;
}
