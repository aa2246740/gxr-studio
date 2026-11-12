//! Port of gxr_studio/raw/dng.py:272 Dng.summary, as JSON.

use crate::dng::{Dng, ExifValue};
use crate::json::Json;
use crate::sha256::sha256_hex;

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
        let bp = &self.black_pattern;
        let black_rows: Vec<Json> = (0..bp.rows)
            .map(|r| Json::floats(&bp.values[r * bp.cols..(r + 1) * bp.cols]))
            .collect();
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
                Json::opt(
                    self.lin_table
                        .as_ref()
                        .map(|t| Json::Int(i64::try_from(t.len()).unwrap_or(i64::MAX))),
                ),
            ),
            ("black_level_pattern", Json::Array(black_rows)),
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
