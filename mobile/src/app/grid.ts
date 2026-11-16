import { formatDay, groupByGap, groupCount, layoutRows, visibleRange, type GapMinutes, type VRow } from '../lib/group';
import { UNIT_LABEL } from '../lib/calib';
import { button, el } from './dom';
import { openable, photoMs, unread, type AppApi, type Photo } from './model';

const GAPS: GapMinutes[] = [1, 3, 5, 10];

function calibDetail(api: AppApi): string {
  const pack = api.book().pack;
  if (!pack) return '还没导入校准数据';
  return `已导入：${pack.units.map((row) => UNIT_LABEL[row.unit]).join('、')}`;
}

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

  constructor(
    private readonly host: HTMLElement,
    private readonly api: AppApi,
  ) {}

  show(): void {
    this.mount();
    this.relayout();
  }

  /** 缩略图或顺序变了。只重挂看得见的行。 */
  refresh(): void {
    if (!this.scroller) this.mount();
    this.relayout();
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
    this.host.replaceChildren();
    const screen = el('section', 'screen');
    const nav = el('header', 'nav');
    const more = button('nb plain', '…');
    more.setAttribute('aria-label', '更多');
    more.addEventListener('click', () => this.menu());
    const titleBox = el('div', 't');
    this.title = el('b', '', '照片');
    this.sub = el('small', '', '');
    titleBox.append(this.title, this.sub);
    const choose = button('nb bold', '选择');
    choose.addEventListener('click', () => this.toggleSelect(!this.selecting));
    const prog = el('div', 'prog');
    prog.append(el('i'));
    nav.append(more, titleBox, choose, prog);

    const order = el('div', 'order');
    const seg = el('div', 'seg');
    seg.setAttribute('role', 'group');
    seg.setAttribute('aria-label', '排序');
    for (const [value, label] of [
      ['oldest', '从早到晚'],
      ['newest', '从晚到早'],
    ] as const) {
      const item = button('', label);
      item.dataset.order = value;
      if (this.api.order() === value) item.classList.add('on');
      item.addEventListener('click', () => this.api.setOrder(value));
      seg.append(item);
    }
    order.append(seg);
    // 相机配方文件的一行：选了几个用哪个，或者读不了。
    this.datLine = el('p', 'note datline');
    this.datLine.dataset.state = 'dat-note';
    this.datLine.hidden = true;
    order.append(this.datLine);

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
    this.scroller.addEventListener('pointerup', () => this.api.watch('scroll', false));
    this.scroller.addEventListener('pointercancel', () => this.api.watch('scroll', false));

    this.bar = el('div', 'dock');
    this.bar.hidden = true;
    screen.append(nav, order, this.scroller, this.bar);
    this.host.append(screen);
  }

  private relayout(): void {
    if (!this.scroller || !this.inner) return;
    const photos = this.api.photos();
    const groups = groupByGap(
      photos.map((photo) => ({ id: photo.id, ms: photoMs(photo) })),
      this.api.gap(),
      this.api.order(),
    );
    const width = this.scroller.clientWidth || 390;
    const cellH = Math.max(48, (width / 3) * (2 / 3) + 2);
    this.rows = layoutRows(groups, cellH);
    this.subtitle(photos, groups.length);
    this.paintDat();
    for (const item of this.host.querySelectorAll<HTMLButtonElement>('[data-order]')) {
      item.classList.toggle('on', item.dataset.order === this.api.order());
    }
    this.sig = '';
    this.renderWindow();
    this.selectionBar();
  }

  private paintDat(): void {
    if (!this.datLine) return;
    const book = this.api.book();
    const text = book.datError ?? book.datNote ?? '';
    this.datLine.textContent = text;
    this.datLine.hidden = !text;
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
    const sig = `${range.start}:${slice.map((row) => row.key).join('|')}:${this.selecting ? 's' : ''}`;
    const total = this.rows.reduce((sum, row) => sum + row.height, 0);
    this.inner.style.height = `${total}px`;
    if (sig !== this.sig) {
      this.sig = sig;
      const wrap = el('div');
      wrap.style.transform = `translateY(${range.top}px)`;
      for (const row of slice) wrap.append(this.renderRow(row));
      this.inner.replaceChildren(wrap);
    }
    this.paint();
    this.scroller.classList.toggle('sel', this.selecting);
  }

  private renderRow(row: VRow): HTMLElement {
    if (row.kind === 'day') return el('div', 'day', row.label);
    if (row.kind === 'group') {
      const head = el('div', 'gh');
      head.dataset.kind = 'group';
      head.append(el('span', 'rng', row.label));
      const apply = button('go', '');
      apply.disabled = true;
      apply.append(el('span', '', '套用到这一组'), el('small', '', '下一步做'));
      head.append(apply);
      return head;
    }
    const cells = el('div', 'cells');
    cells.style.height = `${row.height}px`;
    for (const id of row.ids) {
      const photo = this.api.photo(id);
      const cell = button('cell', '');
      cell.dataset.id = id;
      cell.append(el('span', 'ck'), el('span', 'bl'), el('span', 'raw'));
      cell.addEventListener('click', () => this.onCell(id));
      if (photo) this.fill(cell, photo);
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
    cell.classList.toggle('skel', photo.status === 'pending');
    cell.classList.toggle('err', photo.status === 'bad');
    cell.classList.toggle('gone', unread(photo));
    cell.classList.toggle('on', this.selected.has(photo.id));
    const raw = cell.querySelector('.raw');
    const bl = cell.querySelector('.bl');
    if (raw) {
      raw.textContent = photo.status === 'bad' ? '读不了' : unread(photo) ? '卡' : !photo.bitmap && photo.status === 'ready' && !photo.jpg ? '只有 RAW' : '';
    }
    if (bl) {
      bl.replaceChildren();
      if (photo.jpg && !photo.dng) bl.append(el('span', 'bdg', 'JPG'));
    }
    if (!photo.bitmap) return;
    let canvas = cell.querySelector('canvas');
    if (!canvas) {
      canvas = document.createElement('canvas');
      cell.prepend(canvas);
    }
    if (canvas.dataset.drawn === photo.id) return;
    canvas.width = photo.bitmap.width;
    canvas.height = photo.bitmap.height;
    canvas.getContext('2d')?.drawImage(photo.bitmap, 0, 0);
    canvas.dataset.drawn = photo.id;
  }

  private onCell(id: string): void {
    const photo = this.api.photo(id);
    if (!photo) return;
    if (this.selecting) {
      if (this.selected.has(id)) this.selected.delete(id);
      else this.selected.add(id);
      this.selectionBar();
      this.paint();
      return;
    }
    if (photo.status === 'bad') this.showUnreadable(id);
    else if (unread(photo)) this.showCard(this.readyCount(), this.goneCount());
    else if (openable(photo)) this.api.open(id);
  }

  private toggleSelect(on: boolean): void {
    this.selecting = on;
    if (!on) this.selected.clear();
    const choose = this.host.querySelector('.nb.bold');
    if (choose) choose.textContent = on ? '取消' : '选择';
    if (this.title) this.title.textContent = on ? `已选 ${this.selected.size} 张` : '照片';
    this.sig = '';
    this.renderWindow();
    this.selectionBar();
  }

  private selectionBar(): void {
    if (!this.bar) return;
    this.bar.hidden = !this.selecting;
    this.bar.replaceChildren();
    if (!this.selecting) return;
    const apply = button('fbtn ctl grow', `套用调整 · 下一步做`);
    apply.disabled = true;
    const exp = button('fbtn fill grow', `导出 ${this.selected.size} 张 · 下一步做`);
    exp.disabled = true;
    this.bar.append(apply, exp);
    if (this.title) this.title.textContent = `已选 ${this.selected.size} 张`;
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
    row.append(el('h2', 'bad', `${photo.stem} 读不了`));
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
    if (first) first.textContent = `已经读进来的 ${ready} 张照片不受影响，可以接着换配方、存到相册。`;
    if (second) second.textContent = `还有 ${missing} 张没读完。把卡插回去，再点「重新选照片」，选同样的照片；做过的设置会保留。`;
  }

  /** 拔卡提示还开着就改上面的数字；已经关掉的不重新打开。 */
  updateCard(ready: number, missing: number): void {
    if (this.sheet?.dataset.state === 'card-removed' && this.sheet.isConnected) this.cardText(this.sheet, ready, missing);
  }

  private menu(): void {
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
    sheet.append(el('div', 'grab'), row, card);
    const rows: Array<[string, string, () => void]> = [
      ['添加照片', '再从存储卡里选几张', () => this.api.addPhotos()],
      ['胶片柜', '下一步做', () => undefined],
      ['校准数据', calibDetail(this.api), () => this.api.showCalib()],
      ['怎么把卡插到 iPhone', this.api.hasDat() ? '相机配方文件已选上' : '需要配方时再选 GXRCE.DAT', () => this.api.showHelp()],
    ];
    for (const [title, detail, run] of rows) {
      const line = button('lrow', '');
      const text = el('div');
      text.append(el('b', '', title), el('small', '', detail));
      line.append(text);
      line.disabled = title === '胶片柜';
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
      this.api.clear();
    });
    sheet.append(wipe);
    this.openSheet(sheet);
  }
}
