//! Port of `gxr_studio/finish/spec.py` (Geometry and `parse_geometry`).

use gxr_raw::{CoreError, Json};

use super::json::{JsonValue, parse_json};

pub const SLIDER: i64 = 100;
const INVALID_REQUEST: &str = "请求无效。请刷新页面后重试。";

/// Port of `gxr_studio/finish/spec.py:33 Geometry`
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Geometry {
    pub straighten: f64,
    pub vertical: i64,
    pub horizontal: i64,
    pub distortion: i64,
    pub rotate90: i64,
    pub flip: bool,
}

impl Default for Geometry {
    fn default() -> Self {
        Self {
            straighten: 0.0,
            vertical: 0,
            horizontal: 0,
            distortion: 0,
            rotate90: 0,
            flip: false,
        }
    }
}

impl Geometry {
    /// Port of `gxr_studio/finish/spec.py:42 Geometry.is_identity`
    pub fn is_identity(&self) -> bool {
        self.straighten.abs() < 1e-9
            && self.vertical == 0
            && self.horizontal == 0
            && self.distortion == 0
            && self.rotate90.rem_euclid(4) == 0
            && !self.flip
    }

    /// Port of `gxr_studio/finish/spec.py:52 Geometry.expands`
    pub fn expands(&self) -> bool {
        self.vertical != 0 || self.horizontal != 0 || self.distortion != 0
    }

    /// Port of `gxr_studio/finish/spec.py:56 Geometry.to_json`
    pub fn to_json(&self) -> Json {
        Json::object([
            ("straighten", Json::Float(self.straighten)),
            ("vertical", Json::Int(self.vertical)),
            ("horizontal", Json::Int(self.horizontal)),
            ("distortion", Json::Int(self.distortion)),
            ("rotate90", Json::Int(self.rotate90.rem_euclid(4))),
            ("flip", Json::Bool(self.flip)),
        ])
    }
}

/// Port of `gxr_studio/finish/spec.py:154 _num`
fn parse_num(value: &JsonValue, lo: f64, hi: f64) -> Result<f64, CoreError> {
    let Some(v) = value.as_f64() else {
        return Err(CoreError::new("bad_geometry", INVALID_REQUEST));
    };
    if !v.is_finite() || v < lo || v > hi {
        return Err(CoreError::new("bad_geometry", INVALID_REQUEST));
    }
    Ok(v)
}

/// Port of `gxr_studio/finish/spec.py:160 _slider`
fn parse_slider(value: &JsonValue) -> Result<i64, CoreError> {
    let number = parse_num(value, -(SLIDER as f64), SLIDER as f64)?;
    if number.fract() != 0.0 {
        return Err(CoreError::new("bad_geometry", INVALID_REQUEST));
    }
    Ok(number as i64)
}

/// Exact decimal half-to-even rounding to 4 decimal places, matching Python `round(x, 4)`.
pub(crate) fn round4(v: f64) -> f64 {
    format!("{v:.4}").parse::<f64>().unwrap_or(v)
}

/// Port of `gxr_studio/finish/spec.py:167 parse_geometry`
pub fn parse_geometry(raw: &str) -> Result<Geometry, CoreError> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Ok(Geometry::default());
    }
    let val = parse_json(
        trimmed,
        "bad_geometry",
        "纠正参数格式不对。请重新打开这张照片后再试。",
    )?;
    parse_geometry_value(&val)
}

/// Port of `gxr_studio/finish/spec.py:167 parse_geometry` on a parsed `JsonValue`.
pub fn parse_geometry_value(val: &JsonValue) -> Result<Geometry, CoreError> {
    if matches!(val, JsonValue::Null) {
        return Ok(Geometry::default());
    }
    let JsonValue::Object(pairs) = val else {
        return Err(CoreError::new(
            "bad_geometry",
            "纠正参数格式不对。请重新打开这张照片后再试。",
        ));
    };
    for (k, _) in pairs {
        if !matches!(
            k.as_str(),
            "straighten" | "vertical" | "horizontal" | "distortion" | "rotate90" | "flip"
        ) {
            return Err(CoreError::new("bad_geometry", INVALID_REQUEST));
        }
    }
    let turns = match val.get("rotate90") {
        None => 0,
        Some(v) => {
            let Some(i) = v.as_strict_int() else {
                return Err(CoreError::new("bad_geometry", INVALID_REQUEST));
            };
            if !(0..=3).contains(&i) {
                return Err(CoreError::new("bad_geometry", INVALID_REQUEST));
            }
            i
        }
    };
    let flipped = match val.get("flip") {
        None => false,
        Some(v) => v
            .as_bool()
            .ok_or_else(|| CoreError::new("bad_geometry", INVALID_REQUEST))?,
    };
    let straighten = match val.get("straighten") {
        None => 0.0,
        Some(v) => round4(parse_num(v, -45.0, 45.0)?),
    };
    let vertical = match val.get("vertical") {
        None => 0,
        Some(v) => parse_slider(v)?,
    };
    let horizontal = match val.get("horizontal") {
        None => 0,
        Some(v) => parse_slider(v)?,
    };
    let distortion = match val.get("distortion") {
        None => 0,
        Some(v) => parse_slider(v)?,
    };
    Ok(Geometry {
        straighten,
        vertical,
        horizontal,
        distortion,
        rotate90: turns,
        flip: flipped,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round4_matches_python_decimal_ties() {
        assert_eq!(round4(0.12345), 0.1235);
        assert_eq!(round4(1.00005), 1.0001);
        assert_eq!(round4(2.00005), 2.0);
        assert_eq!(round4(4.56785), 4.5678);
        assert_eq!(round4(10.12345), 10.1235);
        assert_eq!(round4(44.99995), 44.9999);
    }

    #[test]
    fn parse_geometry_validates_strictly() {
        assert_eq!(parse_geometry("null").unwrap(), Geometry::default());
        assert_eq!(parse_geometry("{}").unwrap(), Geometry::default());
        assert_eq!(
            parse_geometry(
                r#"{"straighten": 0.12345, "vertical": 40.0, "rotate90": 1, "flip": true}"#
            )
            .unwrap(),
            Geometry {
                straighten: 0.1235,
                vertical: 40,
                horizontal: 0,
                distortion: 0,
                rotate90: 1,
                flip: true,
            }
        );
        assert_eq!(
            parse_geometry(r#"{"rotate90": 1.0}"#).unwrap_err().kind,
            "bad_geometry"
        );
        assert_eq!(
            parse_geometry(r#"{"vertical": 1.5}"#).unwrap_err().kind,
            "bad_geometry"
        );
        assert_eq!(
            parse_geometry(r#"{"straighten": 45.1}"#).unwrap_err().kind,
            "bad_geometry"
        );
        assert_eq!(
            parse_geometry(r#"{"straighten": NaN}"#).unwrap_err().kind,
            "bad_geometry"
        );
        assert_eq!(
            parse_geometry(r#"{"extra": 1}"#).unwrap_err().kind,
            "bad_geometry"
        );
        assert_eq!(parse_geometry("[]").unwrap_err().kind, "bad_geometry");
    }
}
