import { formatDay, groupByGap, groupCount, layoutRows, visibleRange, type GapMinutes, type TimeGroup, type VRow } from '../lib/group';
import { cardHelpTitle, saveWord } from '../lib/device';
import { calibDetail } from './calib_ui';
import { NOTHING_COPIED, ONLY_SOURCE } from '../lib/paste_text';
import { button, el, icon } from './dom';
import { isComputer } from '../lib/save_mode';
import { fillCell } from './grid_cell';
import { arrowTarget, gridOrder, rangeBetween, rowBox } from './grid_nav';
import { modKey, setGridMode, type GridMode } from './keys';
import { classifyLayout, gridColumns, isIpadLayout, viewportDims } from './layout';
import { nameOf, openable, photoMs, photoName, unread, type AppApi, type Photo } from './model';
import { labelCopy } from './new_photo';
import { groupsText, pasteState, rangeOf } from './paste_state';
import { groupBody, openConfirm, pasteBar, pickBody, PASTE_ICON } from './paste_ui';

const GAPS: GapMinutes[] = [1, 3, 5, 10];
/** 去编辑器前网格滚到哪儿了；从编辑器回来时（boot.back → show(focus)）接着用。 */
let savedTop = 0;

export class GridView {
  private scroller: HTMLElement | null = null;
  private inner: HTMLElement | null = null;
  private sub: HTMLElement | null = null;
  private bar: HTMLElement | null = null;
  private title: HTMLElement | null = null;
  private rows: VRow[] = [];
  private sig = '';
  private frame = 0;
  private selecting = false;
  private readonly selected = new Set<string>();
  private sheet: HTMLElement | null = null;
  private datLine: HTMLElement | null = null;
  /** 「选照片…」（04d）：选要套用的照片，底栏是剪贴板和「套用到 N 张」。 */
  private picking = false;
  /** 组标题那一行的键 → 这一组（套用到这一组、选这一组用）。 */
  private readonly groupRows = new Map<string, TimeGroup>();
  /** 紧跟在上一组照片后的单张独立组行键（无组标题，留出分隔间距和细线）。 */
  private readonly soloRows = new Set<string>();
  private pasteBarEl: HTMLElement | null = null;
  private offPaste: (() => void) | null = null;
  private cols = 3;
  private lastWidth = 0;
  private readonly indexById = new Map<string, number>();
  private readonly laidOutMs = new Map<string, number>();
  private cellNodes = new Map<string, HTMLButtonElement>();
  private groupCountTotal = 0;
  private ro: ResizeObserver | null = null;
  private onResize: (() => void) | null = null;
  /** Shift 点选一段的起点：选择模式里上一次点的那张。 */
  private anchor: string | null = null;
  private readonly mode: GridMode = { active: () => this.selecting && !!this.scroller?.isConnected, leave: () => this.toggleSelect(false), selectAll: () => this.selectAll() };
  private readonly host: HTMLElement;
  private readonly api: AppApi;

  constructor(host: HTMLElement, api: AppApi) {
    this.host = host;
    this.api = api;
  }

  destroy(): void {
    if (this.scroller?.isConnected) savedTop = this.scroller.scrollTop;
    setGridMode(null, this.mode);
    this.ro?.disconnect();
    this.ro = null;
    if (this.onResize) {
      window.removeEventListener('resize', this.onResize);
      this.onResize = null;
    }
    if (this.frame) {
      cancelAnimationFrame(this.frame);
      this.frame = 0;
    }
    this.offPaste?.();
    this.offPaste = null;
    this.cellNodes.clear();
    this.laidOutMs.clear();
  }

  /** `focus`：从编辑器回来，滚回原来的位置，焦点放在编辑器最后看的那张上（同一帧里，手机上不跳）。 */
  show(focus?: string): void {
    setGridMode(this.mode);
    pasteState.sync(this.api);
    pasteState.gridShown();
    if (pasteState.pickNext && pasteState.clip) {
      this.picking = true;
      this.selecting = true;
    }
    pasteState.pickNext = false;
    this.offPaste?.();
    this.offPaste = pasteState.onChange(() => {
      if (this.host.isConnected && this.scroller?.isConnected) this.relayout();
      else this.offPaste?.();
    });
    this.mount();
    this.relayout();
    if (this.picking) this.toggleSelect(true);
    if (focus) this.reveal(focus, savedTop);
  }

  /** 把这一张滚进视野（先回到 `top`）、画出来、给它焦点。 */
  private reveal(id: string, top?: number): void {
    const scroller = this.scroller;
    if (!scroller) return;
    if (top !== undefined) scroller.scrollTop = top;
    const box = rowBox(this.rows, id);
    if (box && box.top < scroller.scrollTop) scroller.scrollTop = box.top;
    else if (box && box.top + box.height > scroller.scrollTop + scroller.clientHeight) scroller.scrollTop = box.top + box.height - scroller.clientHeight;
    this.renderWindow();
    this.cellNodes.get(id)?.focus({ preventScroll: true });
  }

  /** 焦点在格子上：←/→ 按网格顺序走，↑/↓ 走到上下一行的同一列，Home/End 到头尾。 */
  private onArrow(ev: KeyboardEvent): void {
    const id = ev.target instanceof HTMLElement ? ev.target.closest<HTMLElement>('.cell')?.dataset.id : undefined;
    if (!id || ev.altKey || ev.ctrlKey || ev.metaKey || ev.shiftKey) return;
    const next = arrowTarget(this.rows, id, ev.key);
    if (next === null) return;
    ev.preventDefault();
    this.reveal(next);
  }

  /** 缩略图或顺序变了。只重挂看得见的行。 */
  refresh(): void {
    if (!this.scroller) this.mount();
    this.relayout();
  }

  /** 单张照片缩略图或状态就绪时的增量更新：时间未变只刷单格，拍摄时间到达时再重排并复用已有节点。 */
  update(id: string): void {
    if (!this.scroller || !this.inner) {
      this.refresh();
      return;
    }
    const photo = this.api.photo(id);
    const photos = this.api.photos();
    if (!photo || photos.length !== this.laidOutMs.size) {
      this.relayout();
      return;
    }
    const prevMs = this.laidOutMs.get(id);
    if (prevMs === undefined || prevMs !== photoMs(photo)) {
      this.relayout();
      return;
    }
    this.subtitle(photos, this.groupCountTotal);
    const cell = this.cellNodes.get(id);
    if (cell) this.fill(cell, photo);
  }

  showUnreadable(id: string): void {
    const photo = this.api.photo(id);
    if (!photo) return;
    this.openSheet(this.unreadable(photo));
  }

  showCard(ready: number, missing: number): void {
    this.openSheet(this.card(ready, missing));
    this.relayout();
  }

  private mount(): void {
    this.cellNodes.clear();
    this.laidOutMs.clear();
    this.sig = '';
    this.host.replaceChildren();
    const screen = el('section', 'screen');
    const nav = el('header', 'nav');
    const more = button('nb plain', '…');
    more.setAttribute('aria-label', '更多');
    more.addEventListener('click', () => (this.picking ? this.toggleSelect(false) : this.menu()));
    const titleBox = el('div', 't');
    this.title = el('b', '', '照片');
    this.sub = el('small', '', '');
    titleBox.append(this.title, this.sub);
    const choose = button('nb bold', '选择');
    choose.addEventListener('click', () => (this.picking ? this.selectAll() : this.toggleSelect(!this.selecting)));
    const prog = el('div', 'prog');
    prog.append(el('i'));
    nav.append(more, titleBox, choose, prog);

    // 相机配方文件的一行：选了几个用哪个，或者读不了。
    this.datLine = el('p', 'note datline');
    this.datLine.dataset.state = 'dat-note';
    this.datLine.hidden = true;

    this.scroller = el('div', 'gscroll');
    this.inner = el('div', 'inner');
    this.scroller.append(this.inner);
    this.scroller.addEventListener(
      'scroll',
      () => {
        this.api.watch('scroll', true);
        if (this.frame) return;
        this.frame = requestAnimationFrame(() => {
          this.frame = 0;
          this.renderWindow();
        });
      },
      { passive: true },
    );
    this.scroller.addEventListener('keydown', (ev) => this.onArrow(ev));
    this.scroller.addEventListener('pointerup', () => this.api.watch('scroll', false));
    this.scroller.addEventListener('pointercancel', () => this.api.watch('scroll', false));

    this.bar = el('div', 'dock');
    this.bar.hidden = true;
    screen.append(nav, this.datLine, this.scroller, this.bar);
    this.host.append(screen);
    this.watchSize();
  }

  private watchSize(): void {
    this.ro?.disconnect();
    this.ro = null;
    if (this.onResize) {
      window.removeEventListener('resize', this.onResize);
      this.onResize = null;
    }
    const check = () => {
      if (!this.scroller?.isConnected) {
        this.destroy();
        return;
      }
      const nextWidth = this.scroller.clientWidth || 390;
      const nextCols = this.computeCols(nextWidth);
      if (nextCols !== this.cols || Math.abs(nextWidth - this.lastWidth) >= 2) this.relayout();
    };
    this.onResize = check;
    window.addEventListener('resize', check);
    if (typeof ResizeObserver === 'function' && this.scroller) {
      this.ro = new ResizeObserver(check);
      this.ro.observe(this.scroller);
    }
  }

  private computeCols(width: number): number {
    const shell = this.host.closest<HTMLElement>('.app');
    const dims = shell ? viewportDims(shell) : { width: window.innerWidth || width, height: window.innerHeight || 844 };
    return gridColumns(width, isIpadLayout(classifyLayout(dims.width, dims.height)));
  }

  private relayout(): void {
    if (!this.scroller || !this.inner) return;
    const photos = this.api.photos();
    this.indexById.clear();
    this.laidOutMs.clear();
    photos.forEach((photo, idx) => {
      this.indexById.set(photo.id, idx + 1);
      this.laidOutMs.set(photo.id, photoMs(photo));
    });
    const groups = groupByGap(
      photos.map((photo) => ({ id: photo.id, ms: photoMs(photo) })),
      this.api.gap(),
      this.api.order(),
    );
    const width = this.scroller.clientWidth || 390;
    const cols = this.computeCols(width);
    this.cols = cols;
    this.lastWidth = width;
    this.inner.style.setProperty('--grid-cols', String(cols));
    const cellW = cols === 3 ? width / 3 : (width - (cols - 1) * 2) / cols;
    const cellH = Math.max(48, cellW * (2 / 3) + 2);
    // 单张自成一组不另占一行组标题；紧跟在上一组照片后时加分隔间距和细线，避免看起来像上一组。
    const soloIds = new Set(groups.filter((g) => g.ids.length <= 1).map((g) => g.ids[0]));
    const kept: VRow[] = [];
    this.soloRows.clear();
    for (const row of layoutRows(groups, cellH, cols)) {
      if (row.kind === 'group' && row.ids.length <= 1) continue;
      const prev = kept[kept.length - 1];
      if (row.kind === 'cells' && row.ids.length === 1 && soloIds.has(row.ids[0]) && prev?.kind === 'cells') {
        this.soloRows.add(row.key);
        kept.push({ ...row, height: row.height + 14 });
      } else {
        kept.push(row);
      }
    }
    this.rows = kept;
    this.groupCountTotal = groups.length;
    this.groupRows.clear();
    for (const group of groups) this.groupRows.set(`group:${group.ids[0] ?? group.startMs}`, group);
    this.subtitle(photos, groups.length);
    this.paintDat();
    for (const item of this.host.querySelectorAll<HTMLButtonElement>('[data-order]')) {
      item.classList.toggle('on', item.dataset.order === this.api.order());
    }
    this.renderWindow();
    this.selectionBar();
    this.paintPasteBar();
  }

  /** 已复制、套用结果和撤销占贴底的那一格；选择模式时让给选择栏。 */
  private paintPasteBar(): void {
    const screen = this.scroller?.parentElement;
    if (!screen) return;
    this.pasteBarEl?.remove();
    this.pasteBarEl = this.selecting ? null : pasteBar(this.api, () => this.relayout());
    if (this.pasteBarEl) screen.append(this.pasteBarEl);
  }

  private paintDat(): void {
    if (!this.datLine) return;
    const book = this.api.book();
    const text = book.datError ?? book.datNote ?? '';
    this.datLine.textContent = text;
    this.datLine.hidden = book.datError ? false : !text;
    this.datLine.classList.toggle('bad', !!book.datError);
  }

  private subtitle(photos: Photo[], groups: number): void {
    if (!this.sub) return;
    const { done, total } = this.api.loading();
    const prog = this.host.querySelector<HTMLElement>('.prog i');
    if (done < total) {
      this.sub.textContent = `正在读取缩略图 ${done}/${total}`;
      if (prog) prog.style.width = `${total ? (done / total) * 100 : 0}%`;
      return;
    }
    if (prog) prog.style.width = '0';
    if (this.api.cardOut()) {
      // 拔卡是持续的状态：顺序、缩略图刷新都不能把它盖掉。
      this.sub.textContent = `存储卡已拔出 · ${this.goneCount()} 张没读完`;
      return;
    }
    const day = photos.length ? formatDay(photoMs(photos[0])) : '';
    const sameDay = photos.every((photo) => formatDay(photoMs(photo)) === day);
    const ignored = this.api.ignored();
    const extra = ignored ? ` · 另外 ${ignored} 个文件没有用上` : '';
    const head = sameDay && day ? `${day} · ` : '';
    this.sub.textContent = `${head}${photos.length} 张 · ${groups} 组${extra}`;
  }

  private renderWindow(): void {
    if (!this.scroller || !this.inner) return;
    const range = visibleRange(
      this.rows.map((row) => row.height),
      this.scroller.scrollTop,
      this.scroller.clientHeight || 640,
      480,
    );
    const slice = this.rows.slice(range.start, range.end);
    const rowSig = slice
      .map((row) =>
        row.kind === 'cells'
          ? `${row.key}=${row.ids.join(',')}:${Math.round(row.height)}${this.soloRows.has(row.key) ? '!' : ''}`
          : `${row.key}=${row.label}:${row.ids.length}`,
      )
      .join('|');
    const sig = `${range.start}:${Math.round(range.top)}:${this.cols}:${rowSig}:${this.selecting ? 's' : ''}:${pasteState.clip ? 'c' : ''}`;
    const total = this.rows.reduce((sum, row) => sum + row.height, 0);
    this.inner.style.height = `${total}px`;
    if (sig !== this.sig) {
      this.sig = sig;
      const nextNodes = new Map<string, HTMLButtonElement>();
      const wrap = el('div');
      wrap.style.transform = `translateY(${range.top}px)`;
      for (const row of slice) wrap.append(this.renderRow(row, nextNodes));
      this.cellNodes = nextNodes;
      this.inner.replaceChildren(wrap);
    } else {
      this.paint();
      this.paintHeads();
    }
    this.scroller.classList.toggle('sel', this.selecting);
  }

  private renderRow(row: VRow, nextNodes: Map<string, HTMLButtonElement>): HTMLElement {
    if (row.kind === 'day') return el('div', 'day', row.label);
    if (row.kind === 'group') return this.groupHead(row);
    const cells = el('div', this.soloRows.has(row.key) ? 'cells solo-sep' : 'cells');
    cells.style.height = `${row.height}px`;
    for (const id of row.ids) {
      const photo = this.api.photo(id);
      let cell = this.cellNodes.get(id);
      if (!cell) {
        cell = button('cell', '');
        cell.dataset.id = id;
        cell.setAttribute('aria-label', '照片');
        const ck = el('span', 'ck');
        const bl = el('span', 'bl');
        const raw = el('span', 'raw');
        ck.setAttribute('aria-hidden', 'true');
        bl.setAttribute('aria-hidden', 'true');
        raw.setAttribute('aria-hidden', 'true');
        cell.append(ck, bl, raw);
        cell.addEventListener('click', (ev) => this.onCell(id, ev));
      }
      if (photo) this.fill(cell, photo);
      nextNodes.set(id, cell);
      cells.append(cell);
    }
    return cells;
  }

  private paint(): void {
    if (!this.inner) return;
    for (const cell of this.inner.querySelectorAll<HTMLButtonElement>('.cell')) {
      const photo = this.api.photo(cell.dataset.id || '');
      if (photo) this.fill(cell, photo);
    }
  }

  private fill(cell: HTMLButtonElement, photo: Photo): void {
    fillCell(cell, photo, this.selected.has(photo.id), this.indexById.get(photo.id));
    labelCopy(cell, photo); // 同名另外留下的那张标「(2)」
  }

  /** 选择模式里：Shift 点选上次点的那张到这张，Cmd/Ctrl 点一张勾上或取消。不在选择模式时 Cmd/Ctrl 点一张就进选择模式并勾上它。 */
  private onCell(id: string, ev?: MouseEvent): void {
    const photo = this.api.photo(id);
    if (!photo) return;
    if (!this.selecting && ev && modKey(ev)) this.toggleSelect(true);
    if (this.selecting) {
      const range = ev?.shiftKey && this.anchor ? rangeBetween(gridOrder(this.rows), this.anchor, id) : null;
      if (range) { this.selected.clear(); for (const one of range) this.selected.add(one); } // 和访达一样：Shift 点选换成锚点到这张这一段
      else if (this.selected.has(id)) this.selected.delete(id);
      else this.selected.add(id);
      if (!range) this.anchor = id;
      this.selectionChanged();
      return;
    }
    if (photo.status === 'bad' || unread(photo)) this.api.dropPending(); // 点开了提示面板：还在等编辑器分包的点开作废
    if (photo.status === 'bad') this.showUnreadable(id);
    else if (unread(photo)) this.showCard(this.readyCount(), this.goneCount());
    else if (openable(photo)) this.api.open(id);
  }

  private toggleSelect(on: boolean): void {
    this.api.dropPending(); // 进出多选、勾选变了：还在等编辑器分包的导出或点开作废
    this.selecting = on;
    if (!on) {
      this.selected.clear();
      this.anchor = null;
      this.picking = false;
    }
    const choose = this.host.querySelector('.nb.bold');
    if (choose) choose.textContent = on ? '取消' : '选择';
    this.sig = '';
    this.renderWindow();
    this.selectionBar();
    this.paintPasteBar();
    this.paintTitle();
  }

  private selectionBar(): void {
    if (!this.bar) return;
    this.bar.hidden = !this.selecting;
    this.bar.replaceChildren();
    this.bar.classList.toggle('pastebar', this.picking);
    if (!this.selecting) return;
    if (this.picking) {
      this.pickBar();
      return;
    }
    const apply = button('fbtn ctl grow', '套用调整');
    apply.dataset.act = 'paste-selection';
    apply.disabled = this.selected.size === 0;
    apply.addEventListener('click', () => this.pasteSelection());
    const exp = button('fbtn fill grow', `导出 ${this.selected.size} 张`);
    exp.dataset.act = 'export';
    exp.disabled = this.selected.size === 0;
    exp.addEventListener('click', () => this.api.exportPhotos([...this.selected], () => this.toggleSelect(false)));
    this.bar.append(apply, exp);
    this.paintTitle();
  }

  /** 组标题：时间范围、张数；有剪贴板时显示「套用到这一组」，选择时显示「已选 x/y」和「选这一组 / 取消这一组」。 */
  private groupHead(row: VRow): HTMLElement {
    const head = el('div', 'gh');
    head.dataset.kind = 'group';
    head.dataset.row = row.key;
    const group = this.groupRows.get(row.key);
    head.append(el('span', 'rng', group?.rangeLabel ?? row.label), el('span', 'cnt', ''));
    this.paintHead(head);
    return head;
  }

  private paintHead(head: HTMLElement): void {
    const group = this.groupRows.get(head.dataset.row ?? '');
    const cnt = head.querySelector('.cnt');
    if (!group || !cnt) return;
    let go = head.querySelector<HTMLButtonElement>('.go');
    const needGo = this.selecting || !!pasteState.clip;
    if (!needGo) {
      go?.remove();
      cnt.textContent = `${group.ids.length} 张`;
      return;
    }
    if (!go) {
      go = button('go', '');
      go.addEventListener('click', () => {
        if (this.selecting) this.toggleGroup(group);
        else this.applyGroup(group);
      });
      head.append(go);
    }
    const picked = group.ids.filter((id) => this.selected.has(id)).length;
    if (this.selecting) {
      cnt.textContent = picked ? `已选 ${picked}/${group.ids.length}` : `${group.ids.length} 张`;
      go.className = 'go';
      go.dataset.act = 'select-group';
      go.replaceChildren(el('span', '', picked < group.ids.length ? '选这一组' : '取消这一组'));
      return;
    }
    cnt.textContent = `${group.ids.length} 张`;
    go.dataset.act = 'apply-group';
    // 刚套用过的那一组是灰的（04e），还能再点。
    const bar = pasteState.bar;
    const from = pasteState.clip?.from.id;
    const done = bar?.kind === 'applied' && group.ids.some((id) => bar.ids.has(id)) && group.ids.every((id) => id === from || bar.ids.has(id));
    go.className = !done ? 'go' : 'go idle';
    go.replaceChildren(...(done ? [] : [icon(PASTE_ICON.copy)]), el('span', '', '套用到这一组'));
  }

  private paintHeads(): void {
    for (const head of this.host.querySelectorAll<HTMLElement>('.gh[data-row]')) this.paintHead(head);
  }

  private paintTitle(): void {
    if (!this.title) return;
    if (this.picking) {
      this.title.textContent = '选要套用的照片';
      if (this.sub) this.sub.textContent = `已选 ${this.selected.size} 张`;
    } else this.title.textContent = this.selecting ? `已选 ${this.selected.size} 张` : '照片';
    const more = this.host.querySelector<HTMLButtonElement>('.nav .nb.plain, .nav .nb.pick-cancel');
    const choose = this.host.querySelector<HTMLButtonElement>('.nav .nb.bold');
    if (more) {
      more.className = this.picking ? 'nb pick-cancel' : 'nb plain';
      more.textContent = this.picking ? '取消' : '…';
    }
    if (choose && this.picking) choose.textContent = '全选';
  }

  /** 「套用到这一组」：这一组里除了复制来源的每一张。没复制过就说一句。 */
  private applyGroup(group: TimeGroup): void {
    const clip = pasteState.clip;
    if (!clip) {
      pasteState.info(NOTHING_COPIED);
      return;
    }
    const targets = pasteState.targets(this.api, group.ids);
    if (!targets.length) {
      pasteState.info(ONLY_SOURCE);
      return;
    }
    const inside = group.ids.includes(clip.from.id);
    this.api.dropPending();
    openConfirm(this.host, this.api, {
      title: `套用到这一组的 ${targets.length} 张？`,
      body: groupBody(group.rangeLabel, targets.length, inside, nameOf(this.api, clip.from)),
      targets,
      range: group.rangeLabel,
      afterApply: () => this.relayout(),
    });
  }

  /** 选择栏的「套用调整」、04d 的「套用到 N 张」：选中的照片里去掉复制来源。 */
  private pasteSelection(): void {
    const clip = pasteState.clip;
    if (!clip) {
      pasteState.info(NOTHING_COPIED);
      this.toggleSelect(false);
      return;
    }
    const order = this.api.photos().map((photo) => photo.id).filter((id) => this.selected.has(id));
    const targets = pasteState.targets(this.api, order);
    if (!targets.length) {
      pasteState.info(ONLY_SOURCE);
      this.toggleSelect(false);
      return;
    }
    this.api.dropPending();
    openConfirm(this.host, this.api, {
      title: `套用到 ${targets.length} 张？`,
      body: pickBody(targets.length, nameOf(this.api, clip.from)),
      targets,
      range: rangeOf(targets),
      afterApply: () => this.relayout(),
      onGo: () => this.toggleSelect(false),
    });
  }

  /** 04d 的底栏：剪贴板里是什么、来自哪张，和「套用到 N 张」。 */
  private pickBar(): void {
    if (!this.bar) return;
    const clip = pasteState.clip;
    if (!clip) {
      this.toggleSelect(false);
      return;
    }
    const n = pasteState.targets(this.api, [...this.selected]).length;
    const acc = el('span', 'accc');
    acc.append(icon(PASTE_ICON.copy));
    const text = el('div', 'tx');
    text.append(el('b', '', groupsText(clip.groups)), el('small', '', `来自 ${nameOf(this.api, clip.from)}`));
    const go = button('fbtn fill', `套用到 ${n} 张`);
    go.dataset.act = 'paste-picked';
    go.disabled = n === 0;
    go.addEventListener('click', () => this.pasteSelection());
    this.bar.append(acc, text, go);
    this.paintTitle();
  }

  private toggleGroup(group: TimeGroup): void {
    const all = group.ids.every((id) => this.selected.has(id));
    for (const id of group.ids) {
      if (all) this.selected.delete(id);
      else this.selected.add(id);
    }
    this.selectionChanged();
  }

  private selectAll(): void {
    for (const photo of this.api.photos()) this.selected.add(photo.id);
    this.selectionChanged();
  }

  /** 勾选变了：还在等编辑器分包的「导出 N 张」作废（AppApi.dropPending），再重画选择栏和格子。 */
  private selectionChanged(): void {
    this.api.dropPending();
    this.selectionBar();
    this.paint();
    this.paintHeads();
  }

  private readyCount(): number {
    return this.api.photos().filter((photo) => openable(photo)).length;
  }

  private goneCount(): number {
    return this.api.photos().filter((photo) => unread(photo)).length;
  }

  private openSheet(node: HTMLElement): void {
    this.closeSheet();
    const scrim = el('div', 'scrim');
    scrim.addEventListener('click', () => this.closeSheet());
    this.host.append(scrim, node);
    this.sheet = node;
  }

  private closeSheet(): void {
    this.host.querySelector('.scrim')?.remove();
    this.sheet?.remove();
    this.sheet = null;
  }

  private unreadable(photo: Photo): HTMLElement {
    const sheet = el('div', 'ms');
    sheet.dataset.state = 'unreadable';
    const row = el('div', 'hd');
    row.append(el('h2', 'bad', `${photoName(photo)} 读不了`));
    sheet.append(
      el('div', 'grab'),
      row,
      el('p', 'lead', '文件可能没拷完整，或者已经损坏。别的照片不受影响。'),
      el('p', 'lead', '可以在相机里看看这张还能不能打开；能打开的话，重新选一次这张。'),
    );
    const acts = el('div', 'acts');
    const drop = button('btn lg', '从这次去掉');
    drop.addEventListener('click', () => {
      this.closeSheet();
      this.api.remove(photo.id);
    });
    const again = button('btn primary lg', '重新选这张');
    again.addEventListener('click', () => {
      this.closeSheet();
      this.api.addPhotos();
    });
    acts.append(drop, again);
    sheet.append(acts);
    return sheet;
  }

  private card(ready: number, missing: number): HTMLElement {
    const sheet = el('div', 'dlg');
    sheet.dataset.state = 'card-removed';
    sheet.append(
      el('h2', '', '存储卡被拔出了'),
      el('p', 'ready', ''),
      el('p', 'missing', ''),
    );
    this.cardText(sheet, ready, missing);
    const acts = el('div', 'acts');
    acts.classList.add('col');
    const again = button('btn primary lg', '重新选照片');
    again.addEventListener('click', () => {
      this.closeSheet();
      this.api.addPhotos();
    });
    const keep = button('btn lg ghost', '先用读进来的');
    keep.addEventListener('click', () => this.closeSheet());
    acts.append(again, keep);
    sheet.append(acts);
    return sheet;
  }

  private cardText(sheet: HTMLElement, ready: number, missing: number): void {
    const first = sheet.querySelector('p.ready');
    const second = sheet.querySelector('p.missing');
    if (first) first.textContent = `已经读进来的 ${ready} 张照片不受影响，可以接着换配方、${saveWord()}。`;
    if (second) second.textContent = `还有 ${missing} 张没读完。把卡插回去，再点「重新选照片」，选同样的照片；做过的设置会保留。`;
  }

  /** 拔卡提示还开着就改上面的数字；已经关掉的不重新打开。 */
  updateCard(ready: number, missing: number): void {
    if (this.sheet?.dataset.state === 'card-removed' && this.sheet.isConnected) this.cardText(this.sheet, ready, missing);
  }

  private menu(): void {
    this.api.dropPending(); // 打开「更多」：还在等编辑器分包的点开或导出作废（帮助、校准、清空都从这里进）
    const photos = this.api.photos();
    const times = photos.map((photo) => photoMs(photo));
    const sheet = el('div', 'ms');
    const row = el('div', 'hd');
    row.append(el('h2', '', '更多'));
    const done = button('nb', '完成');
    done.addEventListener('click', () => this.closeSheet());
    row.append(done);
    const card = el('div', 'card');
    card.append(el('b', '', '分组间隔'), el('p', 's', '相邻两张拍摄时间隔得比这个久，就分成新的一组。'));
    const seg = el('div', 'seg');
    for (const gap of GAPS) {
      const item = button('', `${gap} 分钟`);
      if (gap === this.api.gap()) item.classList.add('on');
      item.addEventListener('click', () => {
        this.api.setGap(gap);
        this.closeSheet();
      });
      seg.append(item);
    }
    const counts = GAPS.map((gap) => `${gap} 分钟 ${groupCount(times, gap)} 组`).join(' · ');
    card.append(seg, el('p', 'faint', `现在是 ${groupCount(times, this.api.gap())} 组；${counts}`));
    const sortCard = el('div', 'card');
    sortCard.append(el('b', '', '排序'));
    const sortSeg = el('div', 'seg');
    for (const [value, label] of [
      ['oldest', '从早到晚'],
      ['newest', '从晚到早'],
    ] as const) {
      const item = button(value === this.api.order() ? 'on' : '', label);
      item.dataset.order = value;
      item.addEventListener('click', () => {
        this.api.setOrder(value);
        this.closeSheet();
      });
      sortSeg.append(item);
    }
    sortCard.append(sortSeg);
    sheet.append(el('div', 'grab'), row, sortCard, card);
    const rows: Array<[string, string, () => void]> = [
      ['添加照片', '再从存储卡里选几张', () => this.api.addPhotos()],
      ['校准数据', calibDetail(this.api), () => this.api.showCalib()],
      [cardHelpTitle(), this.api.hasDat() ? '相机配方文件已选上' : '需要配方时再选 GXRCE.DAT 和 GXRFX.BIN', () => this.api.showHelp()],
    ];
    for (const [title, detail, run] of rows) {
      const line = button('lrow', '');
      const text = el('div');
      text.append(el('b', '', title), el('small', '', detail));
      line.append(text);
      line.addEventListener('click', () => {
        this.closeSheet();
        run();
      });
      sheet.append(line);
    }
    const wipe = button('lrow', '');
    const wipeText = el('div');
    wipeText.append(el('b', 'bad', '清空照片，重新选'), el('small', '', '只清掉这次的照片，不动存储卡'));
    wipe.append(wipeText);
    wipe.addEventListener('click', () => {
      this.closeSheet();
      if (isComputer()) this.openSheet(this.confirmClear());
      else this.api.clear();
    });
    sheet.append(wipe);
    this.openSheet(sheet);
  }

  /** 电脑上鼠标容易点错：清空前先问一句（手机、iPad 仍是点一下就清空）。Esc 和遮罩都是取消。 */
  private confirmClear(): HTMLElement {
    const dlg = el('div', 'dlg clear-dlg');
    dlg.dataset.state = 'confirm-clear';
    const acts = el('div', 'acts');
    const cancel = button('btn lg', '取消');
    cancel.dataset.act = 'clear-cancel';
    cancel.addEventListener('click', () => this.closeSheet());
    const go = button('btn danger lg', '清空');
    go.dataset.act = 'clear-confirm';
    go.addEventListener('click', () => {
      this.closeSheet();
      this.api.clear();
    });
    acts.append(cancel, go);
    dlg.append(el('h2', '', `清空这 ${this.api.photos().length} 张照片？`), el('p', '', '调整也会一起丢掉。'), acts);
    return dlg;
  }
}
