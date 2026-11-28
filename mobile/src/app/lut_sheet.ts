// LUT 面板弹层：输入色彩空间选择（12 空间分组 + 范围/色域）、长按操作菜单（重命名 / 删除）及确认框。

import {
  DISPLAY_SPACES,
  LUT_ERRORS,
  OUTPUT_GAMUTS,
  SOURCE_TAGS,
  SPACES,
  SPACE_GROUPS,
  cleanLutLabel,
  computeInputChoice,
  type GamutId,
  type LutRecord,
  type RangeKind,
  type SpaceId,
} from '../lib/lut';
import { button, el, icon } from './dom';
import type { LutBook } from './lut_book';

const CHECK = 'M5 12.5l4.5 4.5L19 7.5';

export function closeLutSheets(host: HTMLElement): void {
  for (const node of host.querySelectorAll('.lut-scrim, .lut-sheet, .lut-dlg')) node.remove();
}

function mountOverlay(host: HTMLElement, node: HTMLElement, onScrim: () => void): { close: () => void } {
  closeLutSheets(host);
  const scrim = el('div', 'scrim lut-scrim');
  scrim.addEventListener('click', onScrim);
  host.append(scrim, node);
  return {
    close: () => {
      scrim.remove();
      node.remove();
    },
  };
}

export interface SpaceSheetOptions {
  hasRaw: boolean;
  onUpdated: (record: LutRecord) => void;
}

export function openLutSpaceSheet(host: HTMLElement, lutBook: LutBook, initial: LutRecord, options: SpaceSheetOptions): void {
  let current = initial;
  let inputRange: RangeKind = current.input_range ?? 'full';
  let outputSpace: SpaceId = current.output_space ?? 'srgb';
  let outputSpaceEdited = false;
  let outputGamut: GamutId | '' = current.output_gamut ?? '';
  let outputRange: RangeKind = current.output_range ?? 'full';
  let busy = false;

  const sheet = el('div', 'ms lut-sheet lut-space-sheet');
  sheet.dataset.state = 'lut-space';
  sheet.dataset.cubeId = current.cube_id;

  const head = el('div', 'hd');
  head.append(el('h2', '', '输入色彩空间'));
  const done = button('nb bold', '完成');
  head.append(done);

  const sub = el('div', 'lut-sheet-meta');
  const note = el('p', 'lead lut-sheet-note', '');
  const list = el('div', 'lut-space-list');
  const adv = el('div', 'lut-space-adv');
  const errLine = el('p', 'bad small lut-sheet-err', '');
  errLine.hidden = true;

  const view = mountOverlay(host, sheet, () => view.close());
  done.addEventListener('click', () => view.close());

  const saveSpace = async (space: SpaceId, closeAfter: boolean) => {
    if (busy) return;
    busy = true;
    errLine.hidden = true;
    try {
      const res = await lutBook.setSpace(current.cube_id, {
        input_space: space,
        ...(outputSpaceEdited ? { output_space: outputSpace } : {}),
        output_gamut: outputGamut || null,
        input_range: inputRange,
        output_range: outputRange,
      });
      if (!sheet.isConnected) return;
      if (!res.ok) {
        errLine.textContent = res.message;
        errLine.hidden = false;
        return;
      }
      current = res.record;
      outputSpace = current.output_space ?? outputSpace;
      options.onUpdated(res.record);
      if (closeAfter) view.close();
      else renderBody();
    } catch {
      if (!sheet.isConnected) return;
      errLine.textContent = '保存未生效，请重试。';
      errLine.hidden = false;
    } finally {
      busy = false;
    }
  };

  const makeSeg = <T extends string>(
    label: string,
    key: string,
    items: ReadonlyArray<readonly [T, string]>,
    value: T,
    pick: (v: T) => void,
  ): HTMLElement => {
    const row = el('div', 'frow lut-adv-row');
    row.dataset.field = key;
    row.append(el('span', 'dim small', label));
    const seg = el('div', 'seg');
    seg.setAttribute('role', 'group');
    seg.setAttribute('aria-label', label);
    for (const [val, text] of items) {
      const item = button(val === value ? 'on' : '', text);
      item.dataset.value = val;
      item.addEventListener('click', () => {
        pick(val);
        for (const child of seg.children) child.classList.toggle('on', child === item);
        if (current.input_space) void saveSpace(current.input_space, false);
      });
      seg.append(item);
    }
    row.append(seg);
    return row;
  };

  const renderBody = () => {
    sub.replaceChildren();
    const titleRow = el('div', 'row gap8');
    titleRow.append(el('b', 'lut-sheet-name', current.name));
    const choice = computeInputChoice(current);
    const badge = el(
      'span',
      choice.default_input_source === 'required' ? 'tag warn' : 'tag ok',
      choice.default_input_source === 'required' ? '需选择' : SOURCE_TAGS[choice.default_input_source],
    );
    titleRow.append(badge);
    sub.append(titleRow);
    const infoParts: string[] = [];
    if (current.guess_reason) infoParts.push(current.guess_reason);
    if (current.author) infoParts.push(`作者：${current.author}`);
    if (current.license) infoParts.push(`许可：${current.license}`);
    if (infoParts.length) sub.append(el('p', 'faint small', infoParts.join(' · ')));

    note.textContent = choice.default_input_note;

    list.replaceChildren();
    // 还没选过（文件里没写明）：现在按默认用的那个（比如 sRGB）先标上，和上面那句说明一致（P3-18）。
    const picked = current.input_space ?? choice.default_input_space;
    for (const group of SPACE_GROUPS) {
      list.append(el('div', 'lut-space-group', group.label));
      for (const s of SPACES) {
        if (s.kind !== group.kind) continue;
        const on = picked === s.id;
        const row = button(on ? 'optrow lut-space-opt on' : 'optrow lut-space-opt', '');
        row.dataset.space = s.id;
        row.setAttribute('aria-pressed', on ? 'true' : 'false');
        const cc = el('span', on ? 'cc on' : 'cc');
        if (on) cc.append(icon(CHECK));
        const tx = el('div', 'tx');
        tx.append(el('b', '', s.label));
        if (s.kind === 'log' && !options.hasRaw) {
          tx.append(el('small', 'dim', LUT_ERRORS.lut_needs_raw));
        }
        row.append(cc, tx);
        row.addEventListener('click', () => {
          void saveSpace(s.id, true);
        });
        list.append(row);
      }
    }

    const displayItems = DISPLAY_SPACES.map((s) => [s.id, s.shortLabel] as const);
    adv.replaceChildren(
      makeSeg<RangeKind>(
        '输入范围',
        'input_range',
        [
          ['full', '满量程'],
          ['legal', '法律范围'],
        ],
        inputRange,
        (v) => {
          inputRange = v;
        },
      ),
      makeSeg<SpaceId>('输出色彩空间', 'output_space', displayItems, outputSpace, (v) => {
        outputSpace = v;
        outputSpaceEdited = true;
      }),
      makeSeg<GamutId | ''>('输出色域', 'output_gamut', OUTPUT_GAMUTS, outputGamut, (v) => {
        outputGamut = v;
      }),
      makeSeg<RangeKind>(
        '输出范围',
        'output_range',
        [
          ['full', '满量程'],
          ['legal', '法律范围'],
        ],
        outputRange,
        (v) => {
          outputRange = v;
        },
      ),
    );
  };

  renderBody();
  sheet.append(el('div', 'grab'), head, sub, note, errLine, list, adv);
}

export interface ActionSheetOptions {
  onChanged: () => void;
  onDeleted: (cubeId: string) => void;
}

export function openLutActionSheet(host: HTMLElement, lutBook: LutBook, record: LutRecord, options: ActionSheetOptions): void {
  const sheet = el('div', 'ms lut-sheet lut-action-sheet');
  sheet.dataset.state = 'lut-actions';
  sheet.dataset.cubeId = record.cube_id;

  const head = el('div', 'hd');
  head.append(el('h2', '', record.name));
  const cancel = button('nb', '取消');
  head.append(cancel);

  const renameBtn = button('btn lg block', '重命名');
  renameBtn.dataset.act = 'lut-rename';
  const deleteBtn = button('btn lg block danger', '删除');
  deleteBtn.dataset.act = 'lut-delete';

  const acts = el('div', 'lut-act-stack');
  acts.append(renameBtn, deleteBtn);
  sheet.append(el('div', 'grab'), head, el('p', 'faint small lut-action-file', record.filename), acts);

  const view = mountOverlay(host, sheet, () => view.close());
  cancel.addEventListener('click', () => view.close());
  renameBtn.addEventListener('click', () => {
    view.close();
    openRenameDialog(host, lutBook, record, options.onChanged);
  });
  deleteBtn.addEventListener('click', () => {
    view.close();
    openDeleteDialog(host, lutBook, record, options.onDeleted);
  });
}

function openRenameDialog(host: HTMLElement, lutBook: LutBook, record: LutRecord, onRenamed: () => void): void {
  const dlg = el('div', 'dlg lut-dlg');
  dlg.dataset.state = 'lut-rename';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'lut-name-input';
  input.value = record.name;
  input.maxLength = 255;
  input.setAttribute('aria-label', 'LUT 名称');

  const err = el('p', 'bad small', '');
  err.hidden = true;

  const acts = el('div', 'acts');
  const cancel = button('btn lg', '取消');
  const save = button('btn primary lg', '保存');
  save.dataset.act = 'lut-rename-save';
  acts.append(cancel, save);

  dlg.append(el('h2', '', '重命名 LUT'), input, err, acts);
  const view = mountOverlay(host, dlg, () => view.close());
  cancel.addEventListener('click', () => view.close());
  let busy = false;
  const submit = async () => {
    if (busy) return;
    err.hidden = true;
    try {
      cleanLutLabel(input.value);
    } catch (e) {
      err.textContent = e instanceof Error ? e.message : LUT_ERRORS.bad_name;
      err.hidden = false;
      return;
    }
    busy = true;
    save.disabled = true;
    try {
      const res = await lutBook.rename(record.cube_id, input.value);
      if (!dlg.isConnected) return;
      if (!res.ok) {
        err.textContent = res.message;
        err.hidden = false;
        return;
      }
      view.close();
      onRenamed();
    } catch {
      if (!dlg.isConnected) return;
      err.textContent = '保存未生效，请重试。';
      err.hidden = false;
    } finally {
      busy = false;
      save.disabled = false;
    }
  };
  save.addEventListener('click', () => void submit());
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      if (e.isComposing || e.keyCode === 229) return;
      void submit();
    }
  });
  requestAnimationFrame(() => input.select());
}

function openDeleteDialog(host: HTMLElement, lutBook: LutBook, record: LutRecord, onDeleted: (cubeId: string) => void): void {
  const dlg = el('div', 'dlg lut-dlg');
  dlg.dataset.state = 'lut-delete';
  const err = el('p', 'bad small', '');
  err.hidden = true;
  const acts = el('div', 'acts');
  const cancel = button('btn lg', '取消');
  const go = button('btn danger lg', '删除');
  go.dataset.act = 'lut-delete-confirm';
  acts.append(cancel, go);

  dlg.append(
    el('h2', '', `删除「${record.name}」？`),
    el('p', '', '删掉以后，选过它的照片需要重新导入才能导出。'),
    err,
    acts,
  );
  const view = mountOverlay(host, dlg, () => view.close());
  cancel.addEventListener('click', () => view.close());
  let busy = false;
  go.addEventListener('click', async () => {
    if (busy) return;
    busy = true;
    go.disabled = true;
    err.hidden = true;
    try {
      const res = await lutBook.removeChecked(record.cube_id);
      if (!dlg.isConnected) return;
      if (!res.ok) {
        err.textContent = res.message;
        err.hidden = false;
        return;
      }
      view.close();
      onDeleted(record.cube_id);
    } catch {
      if (!dlg.isConnected) return;
      err.textContent = '删除没有成功，请再试一次。';
      err.hidden = false;
    } finally {
      busy = false;
      go.disabled = false;
    }
  });
}
