//! `layout.Metrics` as `fonts.FontMetrics` implements it: built-in faces read from their sfnt
//! tables, CJK through the fallback face (`cjk.rs`).
//!
//! The phone hands the font files in as bytes; nothing here embeds or opens a font file.

use std::collections::BTreeMap;

use gxr_raw::CoreError;

use crate::cjk::{CJK_REFERENCE_PX, CjkFont};
use crate::layout::ascent_px;
use crate::sfnt::{FontTable, read_font};

/// `fonts.FONT_FILES`: the built-in face names and the files they come from.
pub const FONT_FILES: [(&str, &str); 6] = [
    ("atkinson", "AtkinsonHyperlegible-Regular.ttf"),
    ("inter", "Inter-Regular.ttf"),
    ("inter_sb", "Inter-SemiBold.ttf"),
    ("courier", "CourierPrime-Regular.ttf"),
    ("patrick", "PatrickHand-Regular.ttf"),
    ("dseg", "DSEG7Classic-Regular.ttf"),
];

/// One glyph as layout sees it: advance and ink box in font units (1/upm em).
pub type GlyphM = (i64, Option<[i64; 4]>);

/// The faces one layout call may use.
#[derive(Default)]
pub struct Fonts {
    tables: BTreeMap<String, FontTable>,
    cjk: Option<CjkFont>,
}

fn missing_cjk() -> CoreError {
    CoreError::new("missing_cjk", "无法显示中文。请改用英文或数字。")
}

fn zero_division() -> CoreError {
    CoreError::new("zero_division_error", "division by zero")
}

impl Fonts {
    pub fn new() -> Self {
        Self::default()
    }

    /// Add a face. `name` is a `FONT_FILES` key, or `cjk` for the CJK fallback.
    pub fn add(&mut self, name: &str, bytes: &[u8]) -> Result<(), CoreError> {
        if name == "cjk" {
            self.cjk = Some(CjkFont::parse(bytes)?);
            return Ok(());
        }
        if !FONT_FILES.iter().any(|(n, _)| *n == name) {
            return Err(CoreError::new(
                "bad_font",
                format!("没有这个字体代号：{name}"),
            ));
        }
        self.tables.insert(name.to_string(), read_font(bytes)?);
        Ok(())
    }

    fn table(&self, font: &str) -> Result<&FontTable, CoreError> {
        self.tables
            .get(font)
            .ok_or_else(|| CoreError::new("missing_font", format!("缺少字体：{font}")))
    }

    fn cjk(&self) -> Result<&CjkFont, CoreError> {
        self.cjk.as_ref().ok_or_else(missing_cjk)
    }

    /// `FontMetrics.upm`. A zero `upm` is refused where Python would divide by it.
    pub fn upm(&self, font: &str, cjk: bool) -> Result<i64, CoreError> {
        if cjk {
            return Ok(CJK_REFERENCE_PX);
        }
        let upm = self.table(font)?.upm;
        if upm == 0 {
            return Err(zero_division());
        }
        Ok(upm)
    }

    /// `FontMetrics.glyph`.
    pub fn glyph(&self, font: &str, ch: char, cjk: bool) -> Result<GlyphM, CoreError> {
        if cjk {
            return self.cjk()?.glyph(ch);
        }
        let g = self.table(font)?.glyph(u32::from(ch));
        Ok((
            i64::from(g.advance),
            g.ink.map(|b| {
                [
                    i64::from(b[0]),
                    i64::from(b[1]),
                    i64::from(b[2]),
                    i64::from(b[3]),
                ]
            }),
        ))
    }

    /// `FontMetrics.ascent`.
    pub fn ascent(&self, font: &str, px: i64, cjk: bool) -> Result<i64, CoreError> {
        if cjk {
            return Ok(self.cjk()?.ascent(px.max(1)));
        }
        let table = self.table(font)?;
        if table.upm == 0 {
            return Err(zero_division());
        }
        ascent_px(table.ascender, table.upm, px)
    }
}
