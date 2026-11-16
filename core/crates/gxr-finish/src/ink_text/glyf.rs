//! TrueType outline loading (`loca`/`glyf`/`hmtx`) and FreeType size metrics.
//!
//! Port of FreeType 2.14.3 `src/truetype/ttgload.c`, `ttobjs.c`, `src/sfnt/sfobjs.c`,
//! and `src/base/ftobjs.c` (`FT_Request_Metrics`) without executing hinting bytecode.

use std::collections::BTreeMap;

use gxr_frame::{FontTable, read_font};
use gxr_raw::CoreError;

use super::ftcalc::{
    FtMatrix, FtVector, ft_div_fix, ft_hypot, ft_mul_fix, ft_pix_ceil, ft_pix_floor, ft_pix_round,
    ft_vector_transform,
};

pub const FT_CURVE_TAG_CONIC: u8 = 0;
pub const FT_CURVE_TAG_ON: u8 = 1;
pub const FT_CURVE_TAG_CUBIC: u8 = 2;

pub const FT_OUTLINE_EVEN_ODD_FILL: u32 = 0x0002;
pub const FT_OUTLINE_REVERSE_FILL: u32 = 0x0004;
pub const FT_OUTLINE_OVERLAP: u32 = 0x1000;

const HEAD_FLAG_INTEGER_PPEM: u16 = 8;
const OS2_FLAG_USE_TYPO_METRICS: u16 = 128;

// Simple glyph point flags.
const ON_CURVE_POINT: u8 = 0x01;
const X_SHORT_VECTOR: u8 = 0x02;
const Y_SHORT_VECTOR: u8 = 0x04;
const REPEAT_FLAG: u8 = 0x08;
const X_POSITIVE_OR_SAME: u8 = 0x10;
const Y_POSITIVE_OR_SAME: u8 = 0x20;
const OVERLAP_SIMPLE: u8 = 0x40;

// Composite glyph component flags.
const ARGS_ARE_WORDS: u16 = 0x0001;
const ARGS_ARE_XY_VALUES: u16 = 0x0002;
const ROUND_XY_TO_GRID: u16 = 0x0004;
const WE_HAVE_A_SCALE: u16 = 0x0008;
const MORE_COMPONENTS: u16 = 0x0020;
const WE_HAVE_AN_XY_SCALE: u16 = 0x0040;
const WE_HAVE_A_2X2: u16 = 0x0080;
const USE_MY_METRICS: u16 = 0x0200;
const OVERLAP_COMPOUND: u16 = 0x0400;
const SCALED_COMPONENT_OFFSET: u16 = 0x0800;

const MAX_COMPOSITE_DEPTH: usize = 100;
const MAX_GLYPH_POINTS: usize = 65_535;
const MAX_GLYPH_CONTOURS: usize = 16_384;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Outline {
    pub points: Vec<FtVector>,
    pub tags: Vec<u8>,
    pub contours: Vec<u16>,
    pub flags: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LoadedGlyph {
    pub outline: Outline,
    /// Grid-fitted horizontal advance in 26.6 (`ft_glyphslot_grid_fit_metrics`).
    pub hori_advance: i64,
    /// Raw scaled horizontal advance in 26.6 (`pp2.x - pp1.x`).
    pub raw_hori_advance: i64,
    pub linear_hori_advance: i64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SizeMetrics {
    pub x_ppem: u16,
    pub y_ppem: u16,
    pub root_x_scale: i64,
    pub root_y_scale: i64,
    pub root_ascender: i64,
    pub root_descender: i64,
    pub hinted_x_scale: i64,
    pub hinted_y_scale: i64,
}

impl SizeMetrics {
    /// Port of `FT_Set_Pixel_Sizes(face, 0, px)` + `tt_size_reset`.
    pub fn from_pixel_size(face: &FontFace<'_>, px: u32) -> Result<Self, CoreError> {
        if px == 0 || px > u32::from(u16::MAX) {
            return Err(CoreError::new("value_error", "invalid pixel size"));
        }
        let dim = i64::from(px) << 6;
        Self::from_26_6(face, dim, dim)
    }

    /// Port of `FT_Request_Metrics` (nominal 26.6 width/height) + `tt_size_reset`.
    pub fn from_26_6(face: &FontFace<'_>, w_26_6: i64, h_26_6: i64) -> Result<Self, CoreError> {
        if h_26_6 <= 0 {
            return Err(CoreError::new("value_error", "invalid pixel size"));
        }
        let w = if w_26_6 <= 0 { h_26_6 } else { w_26_6 };
        let x_ppem_i64 = (w + 32) >> 6;
        let y_ppem_i64 = (h_26_6 + 32) >> 6;
        if x_ppem_i64 <= 0
            || y_ppem_i64 <= 0
            || x_ppem_i64 > i64::from(u16::MAX)
            || y_ppem_i64 > i64::from(u16::MAX)
        {
            return Err(CoreError::new("value_error", "invalid pixel size"));
        }
        let x_ppem = x_ppem_i64 as u16;
        let y_ppem = y_ppem_i64 as u16;

        let root_y_scale = ft_div_fix(h_26_6, face.upm);
        let root_x_scale = if w == h_26_6 {
            root_y_scale
        } else {
            ft_div_fix(w, face.upm)
        };
        let root_ascender = ft_pix_ceil(ft_mul_fix(face.ascender, root_y_scale));
        let root_descender = ft_pix_floor(ft_mul_fix(face.descender, root_y_scale));

        let (hinted_x_scale, hinted_y_scale) = if (face.head_flags & HEAD_FLAG_INTEGER_PPEM) != 0 {
            (
                ft_div_fix(i64::from(x_ppem) << 6, face.upm),
                ft_div_fix(i64::from(y_ppem) << 6, face.upm),
            )
        } else {
            (root_x_scale, root_y_scale)
        };

        Ok(Self {
            x_ppem,
            y_ppem,
            root_x_scale,
            root_y_scale,
            root_ascender,
            root_descender,
            hinted_x_scale,
            hinted_y_scale,
        })
    }
}

/// Parsed TrueType font tables needed to load unhinted outlines and metrics.
pub struct FontFace<'a> {
    data: &'a [u8],
    pub upm: i64,
    pub head_flags: u16,
    pub long_loca: bool,
    pub ascender: i64,
    pub descender: i64,
    pub num_metrics: u64,
    pub num_glyphs: u64,
    hmtx_at: u64,
    hmtx_len: u64,
    loca_at: u64,
    glyf_at: u64,
    table: FontTable,
}

impl<'a> FontFace<'a> {
    pub fn parse(data: &'a [u8]) -> Result<Self, CoreError> {
        let table = read_font(data)?;
        if table.upm <= 0 {
            return Err(CoreError::new("value_error", "font unitsPerEm is zero"));
        }
        let dir = table_directory(data)?;
        let (head_at, _) = table_span(&dir, "head")?;
        let (hhea_at, _) = table_span(&dir, "hhea")?;
        let (maxp_at, _) = table_span(&dir, "maxp")?;
        let (hmtx_at, hmtx_len) = table_span(&dir, "hmtx")?;
        let (loca_at, _) = table_span(&dir, "loca")?;
        let (glyf_at, _) = table_span(&dir, "glyf")?;

        let head_flags = be_u16(data, head_at + 16)?;
        let long_loca = be_i16(data, head_at + 50)? == 1;
        let hhea_asc = be_i16(data, hhea_at + 4)?;
        let hhea_desc = be_i16(data, hhea_at + 6)?;
        let num_metrics = u64::from(be_u16(data, hhea_at + 34)?);
        let num_glyphs = u64::from(be_u16(data, maxp_at + 4)?);
        if num_glyphs == 0 {
            return Err(CoreError::new("value_error", "font has zero glyphs"));
        }

        let (ascender, descender) = select_asc_desc(data, &dir, hhea_asc, hhea_desc)?;

        Ok(Self {
            data,
            upm: table.upm,
            head_flags,
            long_loca,
            ascender,
            descender,
            num_metrics,
            num_glyphs,
            hmtx_at,
            hmtx_len,
            loca_at,
            glyf_at,
            table,
        })
    }

    pub fn font_table(&self) -> &FontTable {
        &self.table
    }

    pub fn glyph_id(&self, ch: char) -> u32 {
        self.table.glyphs.get(&u32::from(ch)).map_or(0, |g| g.id)
    }

    pub fn load_char(&self, metrics: &SizeMetrics, ch: char) -> Result<LoadedGlyph, CoreError> {
        self.load_glyph(metrics, self.glyph_id(ch))
    }

    /// Port of FreeType `TT_Load_Glyph` with bytecode hinting skipped (v40 backward compat).
    pub fn load_glyph(&self, metrics: &SizeMetrics, gid: u32) -> Result<LoadedGlyph, CoreError> {
        let mut outline = Outline::default();
        let mut state = LoaderState {
            x_scale: metrics.hinted_x_scale,
            y_scale: metrics.hinted_y_scale,
            pp1: FtVector::default(),
            pp2: FtVector::default(),
            linear: 0,
            visited: Vec::new(),
        };
        self.load_truetype_glyph(&mut state, &mut outline, gid, 0)?;
        if state.pp1.x != 0 {
            for pt in &mut outline.points {
                pt.x = pt.x.wrapping_sub(state.pp1.x);
            }
        }
        let raw_hori_advance = state.pp2.x.wrapping_sub(state.pp1.x);
        Ok(LoadedGlyph {
            outline,
            hori_advance: ft_pix_round(raw_hori_advance),
            raw_hori_advance,
            linear_hori_advance: state.linear,
        })
    }

    fn load_truetype_glyph(
        &self,
        state: &mut LoaderState,
        outline: &mut Outline,
        gid: u32,
        recurse_count: usize,
    ) -> Result<(), CoreError> {
        if recurse_count > MAX_COMPOSITE_DEPTH {
            return Err(CoreError::new(
                "value_error",
                "composite recursion depth exceeded",
            ));
        }
        if u64::from(gid) >= self.num_glyphs {
            return Err(CoreError::new("value_error", "invalid glyph index"));
        }

        let (start, byte_len) = self.glyph_span(gid)?;
        let (advance, lsb) = self.hmtx_metrics(gid)?;
        state.linear = i64::from(advance);

        if byte_len == 0 {
            let pp1_x = -i64::from(lsb);
            state.pp1 = FtVector {
                x: ft_mul_fix(pp1_x, state.x_scale),
                y: 0,
            };
            state.pp2 = FtVector {
                x: ft_mul_fix(pp1_x + i64::from(advance), state.x_scale),
                y: 0,
            };
            return Ok(());
        }

        let at = self
            .glyf_at
            .checked_add(start)
            .ok_or_else(invalid_outline)?;
        if byte_len < 10 {
            return Err(invalid_outline());
        }
        let n_contours = be_i16(self.data, at)?;
        let x_min = be_i16(self.data, at + 2)?;

        let pp1_x = i64::from(x_min) - i64::from(lsb);
        state.pp1 = FtVector { x: pp1_x, y: 0 };
        state.pp2 = FtVector {
            x: pp1_x + i64::from(advance),
            y: 0,
        };

        if n_contours == 0 {
            state.pp1.x = ft_mul_fix(state.pp1.x, state.x_scale);
            state.pp2.x = ft_mul_fix(state.pp2.x, state.x_scale);
            return Ok(());
        }

        let body = read_exact(self.data, at + 10, byte_len - 10)?;
        if n_contours > 0 {
            self.load_simple(state, outline, body, n_contours as usize)
        } else {
            self.load_composite(state, outline, body, gid, recurse_count)
        }
    }

    fn load_simple(
        &self,
        state: &mut LoaderState,
        outline: &mut Outline,
        body: &[u8],
        n_contours: usize,
    ) -> Result<(), CoreError> {
        if n_contours > MAX_GLYPH_CONTOURS {
            return Err(invalid_outline());
        }
        let end_pts_bytes = n_contours * 2;
        if body.len() < end_pts_bytes + 2 {
            return Err(invalid_outline());
        }
        let base_pt = outline.points.len();
        let mut prev_end: i32 = -1;
        let mut last_end = 0_u16;
        for c in 0..n_contours {
            let end_pt = u16::from_be_bytes([body[c * 2], body[c * 2 + 1]]);
            if i32::from(end_pt) < prev_end {
                return Err(invalid_outline());
            }
            prev_end = i32::from(end_pt);
            last_end = end_pt;
            let shifted = base_pt
                .checked_add(usize::from(end_pt))
                .and_then(|v| u16::try_from(v).ok())
                .ok_or_else(invalid_outline)?;
            outline.contours.push(shifted);
        }

        let n_points = usize::from(last_end) + 1;
        if base_pt + n_points > MAX_GLYPH_POINTS {
            return Err(invalid_outline());
        }

        let mut cursor = end_pts_bytes;
        let ins_len = usize::from(u16::from_be_bytes([body[cursor], body[cursor + 1]]));
        cursor += 2;
        cursor = cursor.checked_add(ins_len).ok_or_else(invalid_outline)?;
        if cursor > body.len() {
            return Err(invalid_outline());
        }

        let mut flags = Vec::with_capacity(n_points);
        while flags.len() < n_points {
            let f = *body.get(cursor).ok_or_else(invalid_outline)?;
            cursor += 1;
            flags.push(f);
            if (f & REPEAT_FLAG) != 0 {
                let count = usize::from(*body.get(cursor).ok_or_else(invalid_outline)?);
                cursor += 1;
                if flags.len() + count > n_points {
                    return Err(invalid_outline());
                }
                for _ in 0..count {
                    flags.push(f);
                }
            }
        }
        if (flags[0] & OVERLAP_SIMPLE) != 0 {
            outline.flags |= FT_OUTLINE_OVERLAP;
        }

        let mut xs = Vec::with_capacity(n_points);
        let mut x = 0_i64;
        for &f in &flags {
            let delta = if (f & X_SHORT_VECTOR) != 0 {
                let b = i64::from(*body.get(cursor).ok_or_else(invalid_outline)?);
                cursor += 1;
                if (f & X_POSITIVE_OR_SAME) != 0 { b } else { -b }
            } else if (f & X_POSITIVE_OR_SAME) == 0 {
                let hi = *body.get(cursor).ok_or_else(invalid_outline)?;
                let lo = *body.get(cursor + 1).ok_or_else(invalid_outline)?;
                cursor += 2;
                i64::from(i16::from_be_bytes([hi, lo]))
            } else {
                0
            };
            x += delta;
            xs.push(x);
        }

        let mut y = 0_i64;
        for (idx, &f) in flags.iter().enumerate() {
            let delta = if (f & Y_SHORT_VECTOR) != 0 {
                let b = i64::from(*body.get(cursor).ok_or_else(invalid_outline)?);
                cursor += 1;
                if (f & Y_POSITIVE_OR_SAME) != 0 { b } else { -b }
            } else if (f & Y_POSITIVE_OR_SAME) == 0 {
                let hi = *body.get(cursor).ok_or_else(invalid_outline)?;
                let lo = *body.get(cursor + 1).ok_or_else(invalid_outline)?;
                cursor += 2;
                i64::from(i16::from_be_bytes([hi, lo]))
            } else {
                0
            };
            y += delta;
            outline.points.push(FtVector {
                x: ft_mul_fix(xs[idx], state.x_scale),
                y: ft_mul_fix(y, state.y_scale),
            });
            outline.tags.push(f & ON_CURVE_POINT);
        }

        // Phantom points pp1, pp2 stay unrounded in v40 backward-compatibility mode.
        state.pp1.x = ft_mul_fix(state.pp1.x, state.x_scale);
        state.pp2.x = ft_mul_fix(state.pp2.x, state.x_scale);
        Ok(())
    }

    fn load_composite(
        &self,
        state: &mut LoaderState,
        outline: &mut Outline,
        body: &[u8],
        gid: u32,
        recurse_count: usize,
    ) -> Result<(), CoreError> {
        if state.visited.contains(&gid) {
            return Err(invalid_composite());
        }
        state.visited.push(gid);
        let result = self.load_composite_inner(state, outline, body, recurse_count);
        state.visited.pop();
        result
    }

    fn load_composite_inner(
        &self,
        state: &mut LoaderState,
        outline: &mut Outline,
        body: &[u8],
        recurse_count: usize,
    ) -> Result<(), CoreError> {
        let start_point = outline.points.len();
        let subglyphs = parse_subglyphs(body, self.num_glyphs)?;

        state.pp1.x = ft_mul_fix(state.pp1.x, state.x_scale);
        state.pp2.x = ft_mul_fix(state.pp2.x, state.x_scale);

        for sub in &subglyphs {
            let saved_pp1 = state.pp1;
            let saved_pp2 = state.pp2;
            let saved_linear = state.linear;
            let num_base_points = outline.points.len();

            self.load_truetype_glyph(state, outline, u32::from(sub.index), recurse_count + 1)?;

            if (sub.flags & USE_MY_METRICS) == 0 {
                state.pp1 = saved_pp1;
                state.pp2 = saved_pp2;
                state.linear = saved_linear;
            }

            let num_points = outline.points.len();
            if num_points == num_base_points {
                continue;
            }

            let have_scale =
                (sub.flags & (WE_HAVE_A_SCALE | WE_HAVE_AN_XY_SCALE | WE_HAVE_A_2X2)) != 0;
            if have_scale {
                for pt in &mut outline.points[num_base_points..num_points] {
                    *pt = ft_vector_transform(*pt, &sub.transform);
                }
            }

            let (dx, dy) = if (sub.flags & ARGS_ARE_XY_VALUES) == 0 {
                let k = start_point + usize::try_from(sub.arg1).map_err(|_| invalid_composite())?;
                let l =
                    num_base_points + usize::try_from(sub.arg2).map_err(|_| invalid_composite())?;
                if k >= num_base_points || l >= num_points {
                    return Err(invalid_composite());
                }
                let p1 = outline.points[k];
                let p2 = outline.points[l];
                (p1.x.wrapping_sub(p2.x), p1.y.wrapping_sub(p2.y))
            } else {
                let mut x = sub.arg1;
                let mut y = sub.arg2;
                if x == 0 && y == 0 {
                    continue;
                }
                if have_scale && (sub.flags & SCALED_COMPONENT_OFFSET) != 0 {
                    let mac_x = ft_hypot(sub.transform.xx, sub.transform.xy);
                    let mac_y = ft_hypot(sub.transform.yy, sub.transform.yx);
                    x = ft_mul_fix(x, mac_x);
                    y = ft_mul_fix(y, mac_y);
                }
                x = ft_mul_fix(x, state.x_scale);
                y = ft_mul_fix(y, state.y_scale);
                if (sub.flags & ROUND_XY_TO_GRID) != 0 {
                    // In v40 backward compatibility mode, X is unrounded; only Y rounds.
                    y = ft_pix_round(y);
                }
                (x, y)
            };

            if dx != 0 || dy != 0 {
                for pt in &mut outline.points[num_base_points..num_points] {
                    pt.x = pt.x.wrapping_add(dx);
                    pt.y = pt.y.wrapping_add(dy);
                }
            }
        }

        if let Some(first) = subglyphs.first()
            && (first.flags & OVERLAP_COMPOUND) != 0
        {
            outline.flags |= FT_OUTLINE_OVERLAP;
        }
        Ok(())
    }

    fn glyph_span(&self, gid: u32) -> Result<(u64, u64), CoreError> {
        let g = u64::from(gid);
        let (a, b) = if self.long_loca {
            let at = self.loca_at + g * 4;
            (
                u64::from(be_u32(self.data, at)?),
                u64::from(be_u32(self.data, at + 4)?),
            )
        } else {
            let at = self.loca_at + g * 2;
            (
                u64::from(be_u16(self.data, at)?) * 2,
                u64::from(be_u16(self.data, at + 2)?) * 2,
            )
        };
        if b < a {
            return Err(invalid_outline());
        }
        Ok((a, b - a))
    }

    fn hmtx_metrics(&self, gid: u32) -> Result<(u16, i16), CoreError> {
        if self.num_metrics == 0 {
            return Err(CoreError::new("value_error", "hmtx has zero metrics"));
        }
        let g = u64::from(gid);
        if g < self.num_metrics {
            let at = self.hmtx_at + g * 4;
            Ok((be_u16(self.data, at)?, be_i16(self.data, at + 2)?))
        } else {
            let adv = be_u16(self.data, self.hmtx_at + (self.num_metrics - 1) * 4)?;
            let lsb_rel = self.num_metrics * 4 + (g - self.num_metrics) * 2;
            let lsb = if lsb_rel + 2 <= self.hmtx_len {
                be_i16(self.data, self.hmtx_at + lsb_rel)?
            } else {
                0
            };
            Ok((adv, lsb))
        }
    }
}

struct LoaderState {
    x_scale: i64,
    y_scale: i64,
    pp1: FtVector,
    pp2: FtVector,
    linear: i64,
    visited: Vec<u32>,
}

struct SubGlyph {
    flags: u16,
    index: u16,
    arg1: i64,
    arg2: i64,
    transform: FtMatrix,
}

fn parse_subglyphs(body: &[u8], num_glyphs: u64) -> Result<Vec<SubGlyph>, CoreError> {
    let mut cursor = 0_usize;
    let mut out = Vec::new();
    loop {
        if cursor + 4 > body.len() {
            return Err(invalid_composite());
        }
        let flags = u16::from_be_bytes([body[cursor], body[cursor + 1]]);
        let index = u16::from_be_bytes([body[cursor + 2], body[cursor + 3]]);
        cursor += 4;
        if u64::from(index) >= num_glyphs {
            return Err(invalid_composite());
        }

        let (arg1, arg2) = if (flags & ARGS_ARE_XY_VALUES) != 0 {
            if (flags & ARGS_ARE_WORDS) != 0 {
                if cursor + 4 > body.len() {
                    return Err(invalid_composite());
                }
                let a = i64::from(i16::from_be_bytes([body[cursor], body[cursor + 1]]));
                let b = i64::from(i16::from_be_bytes([body[cursor + 2], body[cursor + 3]]));
                cursor += 4;
                (a, b)
            } else {
                if cursor + 2 > body.len() {
                    return Err(invalid_composite());
                }
                let a = i64::from(body[cursor] as i8);
                let b = i64::from(body[cursor + 1] as i8);
                cursor += 2;
                (a, b)
            }
        } else if (flags & ARGS_ARE_WORDS) != 0 {
            if cursor + 4 > body.len() {
                return Err(invalid_composite());
            }
            let a = i64::from(u16::from_be_bytes([body[cursor], body[cursor + 1]]));
            let b = i64::from(u16::from_be_bytes([body[cursor + 2], body[cursor + 3]]));
            cursor += 4;
            (a, b)
        } else {
            if cursor + 2 > body.len() {
                return Err(invalid_composite());
            }
            let a = i64::from(body[cursor]);
            let b = i64::from(body[cursor + 1]);
            cursor += 2;
            (a, b)
        };

        let mut transform = FtMatrix::default();
        if (flags & WE_HAVE_A_SCALE) != 0 {
            if cursor + 2 > body.len() {
                return Err(invalid_composite());
            }
            let s = i64::from(i16::from_be_bytes([body[cursor], body[cursor + 1]])) * 4;
            cursor += 2;
            transform.xx = s;
            transform.yy = s;
        } else if (flags & WE_HAVE_AN_XY_SCALE) != 0 {
            if cursor + 4 > body.len() {
                return Err(invalid_composite());
            }
            transform.xx = i64::from(i16::from_be_bytes([body[cursor], body[cursor + 1]])) * 4;
            transform.yy = i64::from(i16::from_be_bytes([body[cursor + 2], body[cursor + 3]])) * 4;
            cursor += 4;
        } else if (flags & WE_HAVE_A_2X2) != 0 {
            if cursor + 8 > body.len() {
                return Err(invalid_composite());
            }
            transform.xx = i64::from(i16::from_be_bytes([body[cursor], body[cursor + 1]])) * 4;
            transform.yx = i64::from(i16::from_be_bytes([body[cursor + 2], body[cursor + 3]])) * 4;
            transform.xy = i64::from(i16::from_be_bytes([body[cursor + 4], body[cursor + 5]])) * 4;
            transform.yy = i64::from(i16::from_be_bytes([body[cursor + 6], body[cursor + 7]])) * 4;
            cursor += 8;
        }

        out.push(SubGlyph {
            flags,
            index,
            arg1,
            arg2,
            transform,
        });
        if (flags & MORE_COMPONENTS) == 0 {
            break;
        }
    }
    Ok(out)
}

fn select_asc_desc(
    data: &[u8],
    dir: &BTreeMap<String, (u64, u64)>,
    hhea_asc: i16,
    hhea_desc: i16,
) -> Result<(i64, i64), CoreError> {
    if let Some(&(os2_at, os2_len)) = dir.get("OS/2")
        && os2_len >= 78
    {
        let fs_sel = be_u16(data, os2_at + 62)?;
        let typo_asc = be_i16(data, os2_at + 68)?;
        let typo_desc = be_i16(data, os2_at + 70)?;
        let win_asc = be_u16(data, os2_at + 74)?;
        let win_desc = be_u16(data, os2_at + 76)?;
        if (fs_sel & OS2_FLAG_USE_TYPO_METRICS) != 0 {
            return Ok((i64::from(typo_asc), i64::from(typo_desc)));
        }
        if hhea_asc != 0 || hhea_desc != 0 {
            return Ok((i64::from(hhea_asc), i64::from(hhea_desc)));
        }
        if typo_asc != 0 || typo_desc != 0 {
            return Ok((i64::from(typo_asc), i64::from(typo_desc)));
        }
        return Ok((i64::from(win_asc), -i64::from(win_desc)));
    }
    Ok((i64::from(hhea_asc), i64::from(hhea_desc)))
}

fn table_directory(data: &[u8]) -> Result<BTreeMap<String, (u64, u64)>, CoreError> {
    let count = u64::from(be_u16(data, 4)?);
    let mut out = BTreeMap::new();
    for i in 0..count {
        let at = 12 + 16 * i;
        let tag = read_exact(data, at, 4)?;
        let mut name = String::with_capacity(4);
        for &byte in tag {
            name.push(char::from(byte));
        }
        let offset = u64::from(be_u32(data, at + 8)?);
        let length = u64::from(be_u32(data, at + 12)?);
        out.insert(name, (offset, length));
    }
    Ok(out)
}

fn table_span(dir: &BTreeMap<String, (u64, u64)>, name: &str) -> Result<(u64, u64), CoreError> {
    dir.get(name)
        .copied()
        .ok_or_else(|| CoreError::new("key_error", format!("'{name}'")))
}

fn invalid_outline() -> CoreError {
    CoreError::new("value_error", "invalid TrueType glyph outline")
}

fn invalid_composite() -> CoreError {
    CoreError::new("value_error", "invalid TrueType composite glyph")
}

fn read_exact(data: &[u8], at: u64, n: u64) -> Result<&[u8], CoreError> {
    let start = usize::try_from(at).map_err(|_| invalid_outline())?;
    let len = usize::try_from(n).map_err(|_| invalid_outline())?;
    let end = start.checked_add(len).ok_or_else(invalid_outline)?;
    data.get(start..end).ok_or_else(invalid_outline)
}

fn be_u16(data: &[u8], at: u64) -> Result<u16, CoreError> {
    let b = read_exact(data, at, 2)?;
    Ok(u16::from_be_bytes([b[0], b[1]]))
}

fn be_i16(data: &[u8], at: u64) -> Result<i16, CoreError> {
    let b = read_exact(data, at, 2)?;
    Ok(i16::from_be_bytes([b[0], b[1]]))
}

fn be_u32(data: &[u8], at: u64) -> Result<u32, CoreError> {
    let b = read_exact(data, at, 4)?;
    Ok(u32::from_be_bytes([b[0], b[1], b[2], b[3]]))
}
