//! TrueType outline loading (`loca`/`glyf`/`hmtx`) and FreeType size metrics.
//!
//! Port of FreeType 2.14.3 `src/truetype/ttgload.c`, `ttobjs.c`, `src/sfnt/sfobjs.c`,
//! and `src/base/ftobjs.c` (`FT_Request_Metrics`) without executing hinting bytecode.

use std::collections::BTreeMap;

use gxr_frame::{Cff, read_cmap};
use gxr_raw::CoreError;

use super::cff_outline::load_cff_glyph;
use super::ftcalc::{
    FtMatrix, FtVector, ft_div_fix, ft_hypot, ft_mul_fix, ft_pix_ceil, ft_pix_floor, ft_pix_round,
    ft_vector_transform,
};
use super::glyf_sfnt::{
    be_i16, be_u16, be_u32, invalid_composite, invalid_outline, read_exact, select_asc_desc,
    table_directory, table_span,
};

pub const FT_CURVE_TAG_CONIC: u8 = 0;
pub const FT_CURVE_TAG_ON: u8 = 1;
pub const FT_CURVE_TAG_CUBIC: u8 = 2;

pub const FT_OUTLINE_EVEN_ODD_FILL: u32 = 0x0002;
pub const FT_OUTLINE_REVERSE_FILL: u32 = 0x0004;
pub const FT_OUTLINE_OVERLAP: u32 = 0x1000;

const HEAD_FLAG_INTEGER_PPEM: u16 = 8;

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
const WE_HAVE_INSTRUCTIONS: u16 = 0x0100;
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
    pub hinted: bool,
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

enum FaceOutlines {
    Ttf {
        loca_at: u64,
        glyf_at: u64,
        long_loca: bool,
        auto_hint_default: bool,
        has_prep: bool,
    },
    Cff(Box<Cff>),
}

/// Parsed SFNT font tables needed to load TrueType or CFF outlines and metrics.
pub struct FontFace<'a> {
    data: &'a [u8],
    pub upm: i64,
    pub head_flags: u16,
    pub ascender: i64,
    pub descender: i64,
    pub num_metrics: u64,
    pub num_glyphs: u64,
    hmtx_at: u64,
    hmtx_len: u64,
    cmap: BTreeMap<u32, u32>,
    outlines: FaceOutlines,
}

impl<'a> FontFace<'a> {
    pub fn parse(data: &'a [u8]) -> Result<Self, CoreError> {
        if data.len() >= 4 && &data[..4] == b"ttcf" {
            return Err(CoreError::new("value_error", "字体集合（.ttc）不读"));
        }
        let dir = table_directory(data)?;
        let (head_at, _) = table_span(&dir, "head")?;
        let (hhea_at, _) = table_span(&dir, "hhea")?;
        let (maxp_at, maxp_len) = table_span(&dir, "maxp")?;
        let (hmtx_at, hmtx_len) = table_span(&dir, "hmtx")?;
        let (cmap_at, _) = table_span(&dir, "cmap")?;

        let upm = i64::from(be_u16(data, head_at + 18)?);
        if upm <= 0 {
            return Err(CoreError::new("value_error", "font unitsPerEm is zero"));
        }
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

        let mut cmap = BTreeMap::new();
        for (code, gid) in read_cmap(data, cmap_at)? {
            if gid > 0 && gid < num_glyphs {
                cmap.insert(code, gid as u32);
            }
        }

        let outlines = if let Some(&(cff_at, cff_len)) = dir.get("CFF ")
            && !dir.contains_key("glyf")
        {
            FaceOutlines::Cff(Box::new(Cff::parse(read_exact(data, cff_at, cff_len)?)?))
        } else {
            let (loca_at, _) = table_span(&dir, "loca")?;
            let (glyf_at, _) = table_span(&dir, "glyf")?;
            let has_fpgm = dir.get("fpgm").is_some_and(|&(_, len)| len > 0);
            let prep_len = dir.get("prep").map_or(0, |&(_, len)| len);
            let max_instr = if maxp_len >= 28 {
                be_u16(data, maxp_at + 26)?
            } else {
                1
            };
            let auto_hint_default = (!has_fpgm && prep_len <= 7) || max_instr == 0;
            FaceOutlines::Ttf {
                loca_at,
                glyf_at,
                long_loca,
                auto_hint_default,
                has_prep: prep_len > 0,
            }
        };

        Ok(Self {
            data,
            upm,
            head_flags,
            ascender,
            descender,
            num_metrics,
            num_glyphs,
            hmtx_at,
            hmtx_len,
            cmap,
            outlines,
        })
    }

    pub fn glyph_id(&self, ch: char) -> u32 {
        self.cmap.get(&u32::from(ch)).copied().unwrap_or(0)
    }

    pub fn load_char(&self, metrics: &SizeMetrics, ch: char) -> Result<LoadedGlyph, CoreError> {
        self.load_glyph(metrics, self.glyph_id(ch))
    }

    /// Port of FreeType `TT_Load_Glyph` / `cff_slot_load` with bytecode hinting skipped.
    pub fn load_glyph(&self, metrics: &SizeMetrics, gid: u32) -> Result<LoadedGlyph, CoreError> {
        if u64::from(gid) >= self.num_glyphs {
            return Err(CoreError::new("value_error", "invalid glyph index"));
        }
        let (loca_at, glyf_at, long_loca, auto_hint_default, has_prep) = match &self.outlines {
            FaceOutlines::Cff(cff) => {
                let (advance, _lsb) = self.hmtx_metrics(gid)?;
                return load_cff_glyph(
                    cff,
                    gid,
                    advance,
                    metrics.hinted_x_scale,
                    metrics.hinted_y_scale,
                );
            }
            FaceOutlines::Ttf {
                loca_at,
                glyf_at,
                long_loca,
                auto_hint_default,
                has_prep,
            } => (
                *loca_at,
                *glyf_at,
                *long_loca,
                *auto_hint_default,
                *has_prep,
            ),
        };
        let mut outline = Outline::default();
        let mut state = LoaderState {
            x_scale: metrics.hinted_x_scale,
            y_scale: metrics.hinted_y_scale,
            loca_at,
            glyf_at,
            long_loca,
            pp1: FtVector::default(),
            pp2: FtVector::default(),
            linear: 0,
            hinted: auto_hint_default || has_prep,
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
            hinted: state.hinted,
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

        let (start, byte_len) = self.glyph_span(state.loca_at, state.long_loca, gid)?;
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

        let at = state
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
        if ins_len > 0 {
            state.hinted = true;
        }
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
            if (sub.flags & WE_HAVE_INSTRUCTIONS) != 0 {
                state.hinted = true;
            }
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

    fn glyph_span(&self, loca_at: u64, long_loca: bool, gid: u32) -> Result<(u64, u64), CoreError> {
        let g = u64::from(gid);
        let (a, b) = if long_loca {
            let at = loca_at + g * 4;
            (
                u64::from(be_u32(self.data, at)?),
                u64::from(be_u32(self.data, at + 4)?),
            )
        } else {
            let at = loca_at + g * 2;
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
    loca_at: u64,
    glyf_at: u64,
    long_loca: bool,
    pp1: FtVector,
    pp2: FtVector,
    linear: i64,
    hinted: bool,
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
    let mut c = 0_usize;
    let mut out = Vec::new();
    let rd_i16 = |c: &mut usize| -> Result<i64, CoreError> {
        let b = body.get(*c..*c + 2).ok_or_else(invalid_composite)?;
        *c += 2;
        Ok(i64::from(i16::from_be_bytes([b[0], b[1]])))
    };
    let rd_u16 = |c: &mut usize| -> Result<i64, CoreError> {
        let b = body.get(*c..*c + 2).ok_or_else(invalid_composite)?;
        *c += 2;
        Ok(i64::from(u16::from_be_bytes([b[0], b[1]])))
    };
    loop {
        if c + 4 > body.len() {
            return Err(invalid_composite());
        }
        let flags = u16::from_be_bytes([body[c], body[c + 1]]);
        let index = u16::from_be_bytes([body[c + 2], body[c + 3]]);
        c += 4;
        if u64::from(index) >= num_glyphs {
            return Err(invalid_composite());
        }
        let (arg1, arg2) = match (flags & ARGS_ARE_XY_VALUES != 0, flags & ARGS_ARE_WORDS != 0) {
            (true, true) => (rd_i16(&mut c)?, rd_i16(&mut c)?),
            (true, false) => {
                let b = body.get(c..c + 2).ok_or_else(invalid_composite)?;
                c += 2;
                (i64::from(b[0] as i8), i64::from(b[1] as i8))
            }
            (false, true) => (rd_u16(&mut c)?, rd_u16(&mut c)?),
            (false, false) => {
                let b = body.get(c..c + 2).ok_or_else(invalid_composite)?;
                c += 2;
                (i64::from(b[0]), i64::from(b[1]))
            }
        };
        let mut transform = FtMatrix::default();
        if (flags & WE_HAVE_A_SCALE) != 0 {
            let s = rd_i16(&mut c)? * 4;
            transform.xx = s;
            transform.yy = s;
        } else if (flags & WE_HAVE_AN_XY_SCALE) != 0 {
            transform.xx = rd_i16(&mut c)? * 4;
            transform.yy = rd_i16(&mut c)? * 4;
        } else if (flags & WE_HAVE_A_2X2) != 0 {
            transform.xx = rd_i16(&mut c)? * 4;
            transform.yx = rd_i16(&mut c)? * 4;
            transform.xy = rd_i16(&mut c)? * 4;
            transform.yy = rd_i16(&mut c)? * 4;
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
