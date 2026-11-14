//! The CJK fallback face: the numbers `fonts.FontMetrics` asks Pillow (FreeType, BASIC layout)
//! for when `cjk` is true.
//!
//! Python opens the first file in `stamp._CJK_FONTS` (macOS system fonts; tests put the Noto
//! fixture there). The phone has no system font path, so the face comes in as bytes. Python asks
//! three things, all through FreeType:
//!
//! - `round(face.getlength(ch))` at `CJK_REFERENCE_PX = 1000`;
//! - `face.getbbox(ch, anchor="ls")` at 1000 px, turned into a box in "units" of 1/1000 em;
//! - `face.getmetrics()[0]` (the ascent) at the stamp's pixel size.
//!
//! The FreeType steps reproduced here: `y_scale = FT_DivFix(px*64, upm)`; the scaled ascender is
//! `FT_PIX_CEIL(FT_MulFix(ascender, y_scale))`; a hinted advance is rounded to whole pixels; the
//! glyph box is the outline's control box, floored/ceiled to pixels. Pillow's text box always
//! contains the pen origin and the advance (`bounding_box_and_anchors` starts at zero).
//!
//! Outlines: CFF (`cff.rs`) is what the Noto fixture uses, and the parity test checks it against
//! Pillow. A `glyf` face uses each glyph's header box, which is FreeType's unhinted control box;
//! FreeType's TrueType hinting at 1000 px is not reproduced.

use std::collections::BTreeMap;

use gxr_raw::CoreError;

use crate::cff::{Cff, Fixed};
use crate::sfnt::{
    be_i16, be_u16, cmap, hmtx_advance, read_exact, table_at, table_directory, usize_from,
};

/// `fonts.CJK_REFERENCE_PX`: CJK glyphs are measured at this size and treated as `upm = 1000`.
pub const CJK_REFERENCE_PX: i64 = 1000;

enum Outlines {
    Cff(Box<Cff>),
    /// Header box per glyph in font units, `None` for an empty outline.
    Glyf(Vec<Option<[i64; 4]>>),
}

/// A parsed CJK fallback face.
pub struct CjkFont {
    upm: i64,
    ascender: i64,
    glyph_count: u64,
    cmap: BTreeMap<u32, u64>,
    advances: Vec<u16>,
    outlines: Outlines,
}

/// FreeType `FT_MulFix`: `(a*b + 0x8000) >> 16`, rounding the magnitude.
pub fn ft_mul_fix(a: i64, b: i64) -> i64 {
    let negative = (a < 0) != (b < 0);
    let product = i128::from(a.unsigned_abs()) * i128::from(b.unsigned_abs());
    let c = (product + 0x8000) >> 16;
    let c = i64::try_from(c).unwrap_or(i64::MAX);
    if negative { -c } else { c }
}

/// FreeType `FT_DivFix`: `((a << 16) + b/2) / b`, rounding the magnitude.
pub fn ft_div_fix(a: i64, b: i64) -> i64 {
    if b == 0 {
        return 0x7FFF_FFFF;
    }
    let negative = (a < 0) != (b < 0);
    let ua = i128::from(a.unsigned_abs());
    let ub = i128::from(b.unsigned_abs());
    let q = ((ua << 16) + (ub >> 1)) / ub;
    let q = i64::try_from(q).unwrap_or(i64::MAX);
    if negative { -q } else { q }
}

fn pix_floor(v: i64) -> i64 {
    v & !63
}

fn pix_ceil(v: i64) -> i64 {
    (v + 63) & !63
}

fn pix_round(v: i64) -> i64 {
    (v + 32) & !63
}

impl CjkFont {
    /// Parse a TrueType or CFF-flavoured OpenType face.
    pub fn parse(data: &[u8]) -> Result<Self, CoreError> {
        let tables = table_directory(data)?;
        let head = table_at(&tables, "head")?;
        let hhea = table_at(&tables, "hhea")?;
        let maxp = table_at(&tables, "maxp")?;
        let hmtx = table_at(&tables, "hmtx")?;
        let cmap_at = table_at(&tables, "cmap")?;
        let upm = i64::from(be_u16(data, head + 18)?);
        let glyph_count = u64::from(be_u16(data, maxp + 4)?);
        let metrics = u64::from(be_u16(data, hhea + 34)?);
        let mut ascender = i64::from(be_i16(data, hhea + 4)?);
        let descender = i64::from(be_i16(data, hhea + 6)?);
        if ascender == 0 && descender == 0 {
            // FreeType falls back to OS/2: typo metrics when set, else the Windows ones.
            if let Ok(os2) = table_at(&tables, "OS/2") {
                let typo_asc = i64::from(be_i16(data, os2 + 68)?);
                let typo_desc = i64::from(be_i16(data, os2 + 70)?);
                ascender = if typo_asc != 0 || typo_desc != 0 {
                    typo_asc
                } else {
                    i64::from(be_u16(data, os2 + 74)?)
                };
            }
        }
        let mut advances = Vec::with_capacity(usize_from(glyph_count)?);
        for g in 0..glyph_count {
            advances.push(hmtx_advance(data, hmtx, metrics, g)?);
        }
        let outlines = if let Some(&(at, len)) = tables.get("CFF ") {
            Outlines::Cff(Box::new(Cff::parse(read_exact(data, at, len)?)?))
        } else {
            let loca = table_at(&tables, "loca")?;
            let glyf = table_at(&tables, "glyf")?;
            let long = be_i16(data, head + 50)? == 1;
            let mut boxes = Vec::with_capacity(usize_from(glyph_count)?);
            for g in 0..glyph_count {
                let (start, end) = if long {
                    (
                        u64::from(crate::sfnt::be_u32(data, loca + 4 * g)?),
                        u64::from(crate::sfnt::be_u32(data, loca + 4 * g + 4)?),
                    )
                } else {
                    (
                        2 * u64::from(be_u16(data, loca + 2 * g)?),
                        2 * u64::from(be_u16(data, loca + 2 * g + 2)?),
                    )
                };
                if end <= start {
                    boxes.push(None);
                    continue;
                }
                let at = glyf + start;
                boxes.push(Some([
                    i64::from(be_i16(data, at + 2)?),
                    i64::from(be_i16(data, at + 4)?),
                    i64::from(be_i16(data, at + 6)?),
                    i64::from(be_i16(data, at + 8)?),
                ]));
            }
            Outlines::Glyf(boxes)
        };
        Ok(Self {
            upm,
            ascender,
            glyph_count,
            cmap: cmap(data, cmap_at)?,
            advances,
            outlines,
        })
    }

    /// `FT_Get_Char_Index`: 0 for a missing code point or an id past `numGlyphs`.
    fn glyph_id(&self, code: u32) -> u64 {
        match self.cmap.get(&code) {
            Some(&g) if g < self.glyph_count => g,
            _ => 0,
        }
    }

    fn control_box(&self, gid: u64) -> Result<Option<[Fixed; 4]>, CoreError> {
        match &self.outlines {
            Outlines::Cff(cff) => cff.control_box(usize_from(gid)?),
            Outlines::Glyf(boxes) => Ok(boxes
                .get(usize_from(gid)?)
                .copied()
                .flatten()
                .map(|b| [b[0] << 16, b[1] << 16, b[2] << 16, b[3] << 16])),
        }
    }

    /// `FontMetrics.glyph(font, ch, cjk=True)`: `(round(getlength(ch)), box)` at 1000 px, the box
    /// as `(left, -bottom, right, -top)` of `getbbox(ch, anchor="ls")`, or `None` without area.
    pub fn glyph(&self, ch: char) -> Result<(i64, Option<[i64; 4]>), CoreError> {
        let gid = self.glyph_id(u32::from(ch));
        let scale = ft_div_fix(CJK_REFERENCE_PX * 64, self.upm);
        let advance_units = i64::from(self.advances.get(usize_from(gid)?).copied().unwrap_or(0));
        let advance = pix_round(ft_mul_fix(advance_units, scale));
        // getlength is advance / 64; it is a whole number of pixels, so Python's round is exact.
        let length = advance >> 6;
        let (mut x_min, mut y_min, mut x_max, mut y_max) = (0i64, 0i64, 0i64, 0i64);
        x_max = x_max.max(pix_round(advance) >> 6);
        if let Some([bx0, by0, bx1, by1]) = self.control_box(gid)? {
            let dev = |v: Fixed| -> i64 {
                // 16.16 font units → 26.6 pixels.
                let p = i128::from(v) * i128::from(scale);
                i64::try_from((p + (1i128 << 31)) >> 32).unwrap_or(0)
            };
            x_min = x_min.min(pix_floor(dev(bx0)) >> 6);
            y_min = y_min.min(pix_floor(dev(by0)) >> 6);
            x_max = x_max.max(pix_ceil(dev(bx1)) >> 6);
            y_max = y_max.max(pix_ceil(dev(by1)) >> 6);
        }
        let ink = x_max > x_min && y_max > y_min;
        Ok((length, ink.then_some([x_min, y_min, x_max, y_max])))
    }

    /// `int(face(px).getmetrics()[0])`: FreeType's scaled ascender in whole pixels.
    pub fn ascent(&self, px: i64) -> i64 {
        let scale = ft_div_fix(px * 64, self.upm);
        pix_ceil(ft_mul_fix(self.ascender, scale)) >> 6
    }
}
