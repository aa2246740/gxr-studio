//! Port of gxr_studio/raw/dng.py:272 Dng.summary, as JSON.

use crate::dng::{Dng, ExifValue};
use crate::json::Json;
use crate::sha256::sha256_hex;

fn nest_f64(values: &[f64], shape: &[usize]) -> Json {
    if shape.is_empty() {
        return match values.first() {
            Some(&value) => Json::Float(value),
            None => Json::Null,
        };
    }
    if shape.len() == 1 {
        let n = shape[0].min(values.len());
        return Json::floats(&values[..n]);
    }
    let rest = shape[1..]
        .iter()
        .try_fold(1usize, |acc, &dim| acc.checked_mul(dim));
    let Some(row) = rest else {
        return Json::Array(Vec::new());
    };
    let mut rows = Vec::new();
    for r in 0..shape[0] {
        let start = r.saturating_mul(row);
        let end = start.saturating_add(row).min(values.len());
        if start >= values.len() {
            break;
        }
        rows.push(nest_f64(&values[start..end], &shape[1..]));
    }
    Json::Array(rows)
}

fn pair_grid(g: [[i64; 2]; 2]) -> Json {
    Json::Array(vec![Json::ints(&g[0]), Json::ints(&g[1])])
}

fn exif_json(v: &ExifValue) -> Json {
    match v {
        ExifValue::None => Json::Null,
        ExifValue::Int(i) => Json::Int(*i),
        ExifValue::Float(f) => Json::Float(*f),
        ExifValue::Text(s) => Json::str(s.clone()),
        // Python keeps `bytes` here (json.dumps would refuse it); the CLI writes the byte values.
        ExifValue::Bytes(b) => Json::Array(b.iter().map(|&x| Json::Int(i64::from(x))).collect()),
        ExifValue::Ints(x) => Json::ints(x),
        ExifValue::Floats(x) => Json::floats(x),
    }
}

impl Dng<'_> {
    /// Port of gxr_studio/raw/dng.py:272 Dng.summary.
    pub fn summary(&self) -> Json {
        let black_level = nest_f64(&self.black_pattern.values, &self.black_pattern.shape);
        Json::object([
            ("file", Json::str(self.name.clone())),
            ("sha256", Json::str(sha256_hex(self.data))),
            ("endian", Json::str(self.endian())),
            ("raw_size_wh", Json::ints(&[self.width, self.height])),
            ("bits", Json::Int(self.bps)),
            ("cfa_pattern", pair_grid(self.cfa)),
            ("crop_cfa_phase", pair_grid(self.crop_cfa_phase())),
            ("active_area_tlbr", Json::ints(&self.active)),
            (
                "default_crop_origin_xy",
                Json::ints(&[self.crop_origin.0, self.crop_origin.1]),
            ),
            (
                "default_crop_size_wh",
                Json::ints(&[self.crop_size.0, self.crop_size.1]),
            ),
            ("default_scale", Json::floats(&self.default_scale)),
            ("orientation", Json::Int(self.orientation)),
            (
                "linearization_table",
                Json::opt(self.lin_table.as_ref().map(|t| {
                    // A `Vec` length is at most `isize::MAX`, which fits in `i64`.
                    Json::Int(i64::try_from(t.len()).unwrap_or(i64::MAX))
                })),
            ),
            ("black_level_pattern", black_level),
            ("black_delta_h", Json::Bool(self.black_dh.is_some())),
            ("black_delta_v", Json::Bool(self.black_dv.is_some())),
            ("white_level", Json::Float(self.white)),
            (
                "as_shot_neutral",
                Json::opt(self.as_shot_neutral.as_deref().map(Json::floats)),
            ),
            (
                "as_shot_white_xy",
                Json::opt(self.as_shot_white_xy.as_deref().map(Json::floats)),
            ),
            ("analog_balance", Json::floats(&self.analog_balance)),
            ("baseline_exposure_ev", Json::Float(self.baseline_exposure)),
            (
                "exif",
                Json::object(self.exif.iter().map(|(k, v)| (*k, exif_json(v)))),
            ),
        ])
    }
}
