//! Glyph control boxes from an OpenType `CFF ` table, for the CJK fallback font.
//!
//! Python never reads CFF itself: `fonts.FontMetrics` asks FreeType (through Pillow) for the
//! box of a CJK glyph. FreeType's box is the control box of the outline, every on- and off-curve
//! point of every drawn segment, which is what [`Cff::control_box`] returns in font units
//! (16.16 fixed). `cjk.rs` scales it the way FreeType does.
//!
//! Supported: CFF version 1, name-keyed or CID-keyed (FDArray + FDSelect formats 0 and 3),
//! Type 2 charstrings with local and global subroutines, hints, hint masks and the four flex
//! operators (drawn as their two curves). Not supported, reported as `cff_unsupported`: the
//! deprecated arithmetic operators and `seac`-style `endchar`.

use gxr_raw::CoreError;

/// 16.16 fixed point, as CFF and FreeType store coordinates.
pub type Fixed = i64;

const ONE: Fixed = 65536;
const MAX_STACK: usize = 48;
const MAX_SUBR_DEPTH: usize = 10;
const MAX_OPS: usize = 1 << 20;

/// One INDEX: the byte ranges of its entries inside the table.
#[derive(Clone, Debug, Default)]
struct Index {
    items: Vec<(usize, usize)>,
}

impl Index {
    fn get(&self, i: usize) -> Option<(usize, usize)> {
        self.items.get(i).copied()
    }

    fn len(&self) -> usize {
        self.items.len()
    }
}

/// A parsed `CFF ` table.
#[derive(Clone, Debug)]
pub struct Cff {
    data: Vec<u8>,
    charstrings: Index,
    global_subrs: Index,
    /// Local subroutines per font dict (one entry for a name-keyed font).
    local_subrs: Vec<Index>,
    /// Font dict per glyph; empty for a name-keyed font (always dict 0).
    fd_select: Vec<u8>,
}

fn bad(message: &str) -> CoreError {
    CoreError::new("cff_error", message)
}

fn unsupported(message: &str) -> CoreError {
    CoreError::new("cff_unsupported", message)
}

fn byte(data: &[u8], at: usize) -> Result<u8, CoreError> {
    data.get(at).copied().ok_or_else(|| bad("CFF 表读不全"))
}

fn uint(data: &[u8], at: usize, size: usize) -> Result<usize, CoreError> {
    let mut v: usize = 0;
    for k in 0..size {
        let b = usize::from(byte(
            data,
            at.checked_add(k).ok_or_else(|| bad("CFF 表读不全"))?,
        )?);
        v = v
            .checked_mul(256)
            .and_then(|v| v.checked_add(b))
            .ok_or_else(|| bad("CFF 偏移超出范围"))?;
    }
    Ok(v)
}

/// Read an INDEX at `at`; returns it and the offset just past it.
fn read_index(data: &[u8], at: usize) -> Result<(Index, usize), CoreError> {
    let count = uint(data, at, 2)?;
    if count == 0 {
        return Ok((Index::default(), at + 2));
    }
    let off_size = usize::from(byte(data, at + 2)?);
    if !(1..=4).contains(&off_size) {
        return Err(bad("CFF INDEX 的 offSize 无效"));
    }
    let offsets_at = at + 3;
    let n = count.checked_add(1).ok_or_else(|| bad("CFF INDEX 太大"))?;
    let table_len = n
        .checked_mul(off_size)
        .ok_or_else(|| bad("CFF INDEX 太大"))?;
    let base = offsets_at
        .checked_add(table_len)
        .and_then(|v| v.checked_sub(1))
        .ok_or_else(|| bad("CFF INDEX 太大"))?;
    if base >= data.len() {
        return Err(bad("CFF 表读不全"));
    }
    let mut items = Vec::with_capacity(count.min(data.len()));
    let mut prev = uint(data, offsets_at, off_size)?;
    for i in 1..n {
        let next = uint(data, offsets_at + i * off_size, off_size)?;
        if prev == 0 || next < prev {
            return Err(bad("CFF INDEX 偏移无效"));
        }
        let start = base + prev;
        let end = base + next;
        if end > data.len() {
            return Err(bad("CFF 表读不全"));
        }
        items.push((start, end));
        prev = next;
    }
    let end = base
        .checked_add(prev)
        .ok_or_else(|| bad("CFF INDEX 太大"))?;
    Ok((Index { items }, end))
}

/// A DICT: operator → operands (integers; reals are kept truncated, none of the keys we read
/// are reals).
fn read_dict(data: &[u8], start: usize, end: usize) -> Result<Vec<(u16, Vec<i64>)>, CoreError> {
    let mut out = Vec::new();
    let mut operands: Vec<i64> = Vec::new();
    let mut i = start;
    while i < end {
        let b0 = byte(data, i)?;
        match b0 {
            0..=21 => {
                let op = if b0 == 12 {
                    i += 1;
                    1200 + u16::from(byte(data, i)?)
                } else {
                    u16::from(b0)
                };
                out.push((op, std::mem::take(&mut operands)));
                i += 1;
            }
            28 => {
                let v = i16::from_be_bytes([byte(data, i + 1)?, byte(data, i + 2)?]);
                operands.push(i64::from(v));
                i += 3;
            }
            29 => {
                let v = i32::from_be_bytes([
                    byte(data, i + 1)?,
                    byte(data, i + 2)?,
                    byte(data, i + 3)?,
                    byte(data, i + 4)?,
                ]);
                operands.push(i64::from(v));
                i += 5;
            }
            30 => {
                // Real number: nibbles until 0xf. The keys read here never hold one.
                i += 1;
                loop {
                    let b = byte(data, i)?;
                    i += 1;
                    if b & 0x0f == 0x0f || b >> 4 == 0x0f {
                        break;
                    }
                }
                operands.push(0);
            }
            32..=246 => {
                operands.push(i64::from(b0) - 139);
                i += 1;
            }
            247..=250 => {
                let b1 = i64::from(byte(data, i + 1)?);
                operands.push((i64::from(b0) - 247) * 256 + b1 + 108);
                i += 2;
            }
            251..=254 => {
                let b1 = i64::from(byte(data, i + 1)?);
                operands.push(-(i64::from(b0) - 251) * 256 - b1 - 108);
                i += 2;
            }
            _ => return Err(bad("CFF DICT 里有无效的字节")),
        }
        if operands.len() > MAX_STACK {
            return Err(bad("CFF DICT 操作数太多"));
        }
    }
    Ok(out)
}

fn dict_get(dict: &[(u16, Vec<i64>)], op: u16) -> Option<&[i64]> {
    dict.iter()
        .rev()
        .find(|(k, _)| *k == op)
        .map(|(_, v)| v.as_slice())
}

fn to_offset(v: i64) -> Result<usize, CoreError> {
    usize::try_from(v).map_err(|_| bad("CFF 偏移无效"))
}

/// The local Subrs INDEX of the Private DICT named by `private` (size, offset).
fn private_subrs(data: &[u8], private: Option<&[i64]>) -> Result<Index, CoreError> {
    let Some(&[size, offset]) = private else {
        return Ok(Index::default());
    };
    let start = to_offset(offset)?;
    let end = start
        .checked_add(to_offset(size)?)
        .ok_or_else(|| bad("CFF 偏移无效"))?;
    if end > data.len() {
        return Err(bad("CFF 表读不全"));
    }
    let dict = read_dict(data, start, end)?;
    match dict_get(&dict, 19) {
        Some(&[rel]) => {
            let at = start
                .checked_add(to_offset(rel)?)
                .ok_or_else(|| bad("CFF 偏移无效"))?;
            Ok(read_index(data, at)?.0)
        }
        _ => Ok(Index::default()),
    }
}

fn read_fd_select(data: &[u8], at: usize, glyphs: usize) -> Result<Vec<u8>, CoreError> {
    let format = byte(data, at)?;
    let mut out = vec![0u8; glyphs];
    match format {
        0 => {
            for (g, slot) in out.iter_mut().enumerate() {
                *slot = byte(data, at + 1 + g)?;
            }
        }
        3 => {
            let ranges = uint(data, at + 1, 2)?;
            let mut first = uint(data, at + 3, 2)?;
            for r in 0..ranges {
                let rec = at + 5 + 3 * r;
                let fd = byte(data, rec + 2)?;
                let next = uint(data, rec + 3, 2)?;
                for slot in out.iter_mut().take(next.min(glyphs)).skip(first) {
                    *slot = fd;
                }
                first = next;
            }
        }
        _ => return Err(unsupported("CFF FDSelect 格式不支持")),
    }
    Ok(out)
}

impl Cff {
    /// Parse the `CFF ` table bytes.
    pub fn parse(data: &[u8]) -> Result<Self, CoreError> {
        if byte(data, 0)? != 1 {
            return Err(unsupported("只读 CFF 版本 1"));
        }
        let header = usize::from(byte(data, 2)?);
        let (_names, after_names) = read_index(data, header)?;
        let (top, after_top) = read_index(data, after_names)?;
        let (_strings, after_strings) = read_index(data, after_top)?;
        let (global_subrs, _end) = read_index(data, after_strings)?;
        let (top_start, top_end) = top.get(0).ok_or_else(|| bad("CFF 没有 Top DICT"))?;
        let top = read_dict(data, top_start, top_end)?;
        if let Some(kind) = dict_get(&top, 1206)
            && kind != [2]
        {
            return Err(unsupported("只读 Type 2 字形程序"));
        }
        let Some(&[cs_at]) = dict_get(&top, 17) else {
            return Err(bad("CFF 没有 CharStrings"));
        };
        let charstrings = read_index(data, to_offset(cs_at)?)?.0;
        let (local_subrs, fd_select) = match (dict_get(&top, 1236), dict_get(&top, 1237)) {
            (Some(&[fda]), Some(&[fds])) => {
                let fd_array = read_index(data, to_offset(fda)?)?.0;
                let mut locals = Vec::with_capacity(fd_array.len());
                for i in 0..fd_array.len() {
                    let (s, e) = fd_array.get(i).ok_or_else(|| bad("CFF FDArray 无效"))?;
                    let fd = read_dict(data, s, e)?;
                    locals.push(private_subrs(data, dict_get(&fd, 18))?);
                }
                (
                    locals,
                    read_fd_select(data, to_offset(fds)?, charstrings.len())?,
                )
            }
            _ => (vec![private_subrs(data, dict_get(&top, 18))?], Vec::new()),
        };
        Ok(Self {
            data: data.to_vec(),
            charstrings,
            global_subrs,
            local_subrs,
            fd_select,
        })
    }

    /// Control box `(x_min, y_min, x_max, y_max)` of glyph `gid` in 16.16 font units, or
    /// `None` when the glyph draws nothing.
    pub fn control_box(&self, gid: usize) -> Result<Option<[Fixed; 4]>, CoreError> {
        let (start, end) = self
            .charstrings
            .get(gid)
            .ok_or_else(|| bad("CFF 没有这个字形"))?;
        let fd = usize::from(self.fd_select.get(gid).copied().unwrap_or(0));
        let empty = Index::default();
        let local = self.local_subrs.get(fd).unwrap_or(&empty);
        let mut run = Run {
            cff: self,
            local,
            stack: Vec::with_capacity(MAX_STACK),
            stems: 0,
            width_done: false,
            x: 0,
            y: 0,
            open: None,
            bbox: None,
            ops: 0,
            done: false,
        };
        run.exec(start, end, 0)?;
        Ok(run.bbox)
    }
}

struct Run<'a> {
    cff: &'a Cff,
    local: &'a Index,
    stack: Vec<Fixed>,
    stems: usize,
    width_done: bool,
    x: Fixed,
    y: Fixed,
    /// Start of the current contour, until its first segment adds it.
    open: Option<(Fixed, Fixed)>,
    bbox: Option<[Fixed; 4]>,
    ops: usize,
    done: bool,
}

fn bias(count: usize) -> i64 {
    if count < 1240 {
        107
    } else if count < 33900 {
        1131
    } else {
        32768
    }
}

impl Run<'_> {
    fn add(&mut self, x: Fixed, y: Fixed) {
        self.bbox = Some(match self.bbox {
            None => [x, y, x, y],
            Some([x0, y0, x1, y1]) => [x0.min(x), y0.min(y), x1.max(x), y1.max(y)],
        });
    }

    fn begin_segment(&mut self) {
        if let Some((x, y)) = self.open.take() {
            self.add(x, y);
        }
    }

    fn move_to(&mut self, dx: Fixed, dy: Fixed) {
        self.x += dx;
        self.y += dy;
        self.open = Some((self.x, self.y));
    }

    fn line(&mut self, dx: Fixed, dy: Fixed) {
        self.begin_segment();
        self.x += dx;
        self.y += dy;
        self.add(self.x, self.y);
    }

    fn curve(&mut self, d: [Fixed; 6]) {
        self.begin_segment();
        let (x1, y1) = (self.x + d[0], self.y + d[1]);
        let (x2, y2) = (x1 + d[2], y1 + d[3]);
        let (x3, y3) = (x2 + d[4], y2 + d[5]);
        self.add(x1, y1);
        self.add(x2, y2);
        self.add(x3, y3);
        self.x = x3;
        self.y = y3;
    }

    /// Drop the optional leading width argument the first stack-clearing operator may carry.
    fn take_width(&mut self, odd_means_width: bool) {
        if !self.width_done {
            self.width_done = true;
            if odd_means_width && !self.stack.is_empty() {
                self.stack.remove(0);
            }
        }
    }

    fn arg(&self, i: usize) -> Result<Fixed, CoreError> {
        self.stack
            .get(i)
            .copied()
            .ok_or_else(|| bad("CFF 字形程序的参数不够"))
    }

    fn exec(&mut self, start: usize, end: usize, depth: usize) -> Result<(), CoreError> {
        if depth > MAX_SUBR_DEPTH {
            return Err(bad("CFF 子程序嵌套太深"));
        }
        let data = &self.cff.data;
        let mut i = start;
        while i < end && !self.done {
            self.ops += 1;
            if self.ops > MAX_OPS {
                return Err(bad("CFF 字形程序太长"));
            }
            let b0 = byte(data, i)?;
            i += 1;
            match b0 {
                28 => {
                    let v = i16::from_be_bytes([byte(data, i)?, byte(data, i + 1)?]);
                    i += 2;
                    self.push(i64::from(v) * ONE)?;
                }
                32..=246 => self.push((i64::from(b0) - 139) * ONE)?,
                247..=250 => {
                    let b1 = i64::from(byte(data, i)?);
                    i += 1;
                    self.push(((i64::from(b0) - 247) * 256 + b1 + 108) * ONE)?;
                }
                251..=254 => {
                    let b1 = i64::from(byte(data, i)?);
                    i += 1;
                    self.push((-(i64::from(b0) - 251) * 256 - b1 - 108) * ONE)?;
                }
                255 => {
                    let v = i32::from_be_bytes([
                        byte(data, i)?,
                        byte(data, i + 1)?,
                        byte(data, i + 2)?,
                        byte(data, i + 3)?,
                    ]);
                    i += 4;
                    self.push(i64::from(v))?;
                }
                12 => {
                    let b1 = byte(data, i)?;
                    i += 1;
                    self.escape(b1)?;
                }
                19 | 20 => {
                    // hintmask / cntrmask: pending arguments are an implicit vstem.
                    self.take_width(self.stack.len() % 2 == 1);
                    self.stems += self.stack.len() / 2;
                    self.stack.clear();
                    i += self.stems.div_ceil(8);
                }
                10 | 29 => {
                    let n = self
                        .stack
                        .pop()
                        .ok_or_else(|| bad("CFF 字形程序的参数不够"))?;
                    let subrs = if b0 == 10 {
                        self.local
                    } else {
                        &self.cff.global_subrs
                    };
                    let k = n.div_euclid(ONE) + bias(subrs.len());
                    let k = usize::try_from(k).map_err(|_| bad("CFF 子程序编号无效"))?;
                    let (s, e) = subrs.get(k).ok_or_else(|| bad("CFF 子程序编号无效"))?;
                    self.exec(s, e, depth + 1)?;
                }
                11 => return Ok(()),
                _ => self.operator(b0)?,
            }
        }
        Ok(())
    }

    fn push(&mut self, v: Fixed) -> Result<(), CoreError> {
        if self.stack.len() >= MAX_STACK {
            return Err(bad("CFF 字形程序的栈溢出"));
        }
        self.stack.push(v);
        Ok(())
    }

    fn operator(&mut self, op: u8) -> Result<(), CoreError> {
        let n = self.stack.len();
        match op {
            1 | 3 | 18 | 23 => {
                self.take_width(n % 2 == 1);
                self.stems += self.stack.len() / 2;
            }
            21 => {
                self.take_width(n == 3);
                self.move_to(self.arg(0)?, self.arg(1)?);
            }
            22 => {
                self.take_width(n == 2);
                self.move_to(self.arg(0)?, 0);
            }
            4 => {
                self.take_width(n == 2);
                self.move_to(0, self.arg(0)?);
            }
            5 => {
                let mut k = 0;
                while k + 1 < n {
                    self.line(self.arg(k)?, self.arg(k + 1)?);
                    k += 2;
                }
            }
            6 | 7 => {
                let mut horizontal = op == 6;
                for k in 0..n {
                    let d = self.arg(k)?;
                    if horizontal {
                        self.line(d, 0);
                    } else {
                        self.line(0, d);
                    }
                    horizontal = !horizontal;
                }
            }
            8 => {
                let mut k = 0;
                while k + 5 < n {
                    self.curve(self.six(k)?);
                    k += 6;
                }
            }
            24 => {
                let mut k = 0;
                while k + 5 < n.saturating_sub(2) {
                    self.curve(self.six(k)?);
                    k += 6;
                }
                self.line(self.arg(k)?, self.arg(k + 1)?);
            }
            25 => {
                let mut k = 0;
                while k + 1 < n.saturating_sub(6) {
                    self.line(self.arg(k)?, self.arg(k + 1)?);
                    k += 2;
                }
                self.curve(self.six(k)?);
            }
            26 => {
                let mut k = 0;
                let mut dx1 = 0;
                if n % 4 == 1 {
                    dx1 = self.arg(0)?;
                    k = 1;
                }
                while k + 3 < n {
                    let a = [
                        dx1,
                        self.arg(k)?,
                        self.arg(k + 1)?,
                        self.arg(k + 2)?,
                        0,
                        self.arg(k + 3)?,
                    ];
                    self.curve(a);
                    dx1 = 0;
                    k += 4;
                }
            }
            27 => {
                let mut k = 0;
                let mut dy1 = 0;
                if n % 4 == 1 {
                    dy1 = self.arg(0)?;
                    k = 1;
                }
                while k + 3 < n {
                    let a = [
                        self.arg(k)?,
                        dy1,
                        self.arg(k + 1)?,
                        self.arg(k + 2)?,
                        self.arg(k + 3)?,
                        0,
                    ];
                    self.curve(a);
                    dy1 = 0;
                    k += 4;
                }
            }
            30 | 31 => self.alternating(op == 31)?,
            14 => {
                self.take_width(n == 1 || n == 5);
                if self.stack.len() == 4 {
                    return Err(unsupported("CFF endchar 组合字（seac）不支持"));
                }
                self.done = true;
            }
            _ => return Err(unsupported("CFF 字形程序里有不支持的操作符")),
        }
        self.stack.clear();
        Ok(())
    }

    fn six(&self, k: usize) -> Result<[Fixed; 6], CoreError> {
        Ok([
            self.arg(k)?,
            self.arg(k + 1)?,
            self.arg(k + 2)?,
            self.arg(k + 3)?,
            self.arg(k + 4)?,
            self.arg(k + 5)?,
        ])
    }

    /// `hvcurveto` (`start_horizontal`) and `vhcurveto`.
    fn alternating(&mut self, start_horizontal: bool) -> Result<(), CoreError> {
        let n = self.stack.len();
        let mut k = 0;
        let mut horizontal = start_horizontal;
        while k + 3 < n {
            let last = k + 8 > n;
            let extra = if last && n - k == 5 {
                self.arg(k + 4)?
            } else {
                0
            };
            let (a, b, c, d) = (
                self.arg(k)?,
                self.arg(k + 1)?,
                self.arg(k + 2)?,
                self.arg(k + 3)?,
            );
            if horizontal {
                self.curve([a, 0, b, c, extra, d]);
            } else {
                self.curve([0, a, b, c, d, extra]);
            }
            horizontal = !horizontal;
            k += 4;
        }
        Ok(())
    }

    fn escape(&mut self, op: u8) -> Result<(), CoreError> {
        let a = |k: usize| self.arg(k);
        match op {
            35 => {
                let first = [a(0)?, a(1)?, a(2)?, a(3)?, a(4)?, a(5)?];
                let second = [a(6)?, a(7)?, a(8)?, a(9)?, a(10)?, a(11)?];
                self.curve(first);
                self.curve(second);
            }
            34 => {
                let (dx1, dx2, dy2, dx3, dx4, dx5, dx6) =
                    (a(0)?, a(1)?, a(2)?, a(3)?, a(4)?, a(5)?, a(6)?);
                self.curve([dx1, 0, dx2, dy2, dx3, 0]);
                self.curve([dx4, 0, dx5, -dy2, dx6, 0]);
            }
            36 => {
                let (dx1, dy1, dx2, dy2, dx3) = (a(0)?, a(1)?, a(2)?, a(3)?, a(4)?);
                let (dx4, dx5, dy5, dx6) = (a(5)?, a(6)?, a(7)?, a(8)?);
                self.curve([dx1, dy1, dx2, dy2, dx3, 0]);
                self.curve([dx4, 0, dx5, dy5, dx6, -(dy1 + dy2 + dy5)]);
            }
            37 => {
                let d: Vec<Fixed> = (0..11).map(a).collect::<Result<_, _>>()?;
                let dx = d[0] + d[2] + d[4] + d[6] + d[8];
                let dy = d[1] + d[3] + d[5] + d[7] + d[9];
                let (d6x, d6y) = if dx.abs() > dy.abs() {
                    (d[10], -dy)
                } else {
                    (-dx, d[10])
                };
                self.curve([d[0], d[1], d[2], d[3], d[4], d[5]]);
                self.curve([d[6], d[7], d[8], d[9], d6x, d6y]);
            }
            _ => return Err(unsupported("CFF 字形程序里有不支持的操作符")),
        }
        self.stack.clear();
        Ok(())
    }
}
