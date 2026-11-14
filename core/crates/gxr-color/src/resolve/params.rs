//! Port of `gxr_studio/render/params.py`: edit parsing, canonical serialization, and digest.

use std::collections::BTreeMap;

use gxr_raw::Json;
use gxr_raw::sha256::sha256_hex;

use crate::tones::{Adjust as ToneAdjust, Curves};

use super::error::{EditError, INVALID_REQUEST};
use super::json::{PyJson, PyNum};

pub const UNITS: [&str; 3] = ["jlaunch", "qlaunch", "l08firm"];
pub const KNOB_NAMES: [&str; 10] = [
    "amount",
    "contrast",
    "highlight",
    "shadow",
    "fade",
    "saturation",
    "warmth",
    "tint",
    "split",
    "filter",
];
pub const EXPOSURE_RANGE: f64 = 5.0;
pub const SLIDER_RANGE: i64 = 100;
pub const CURVE_MAX_POINTS: usize = 16;
pub const CURVE_CHANNELS: [&str; 4] = ["master", "r", "g", "b"];

/// Python `round(x, 4)` using IEEE decimal half-to-even rounding (preserving `-0.0`).
pub fn py_round4(x: f64) -> f64 {
    format!("{x:.4}").parse::<f64>().unwrap_or(x)
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RecipeLevel {
    Int(i64),
    BigInt { neg: bool, digits: String },
}

impl RecipeLevel {
    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Self::Int(v) => Some(*v),
            Self::BigInt { .. } => None,
        }
    }

    fn write_json(&self, out: &mut String) {
        match self {
            Self::Int(v) => out.push_str(&v.to_string()),
            Self::BigInt { neg, digits } => {
                if *neg {
                    out.push('-');
                }
                out.push_str(digits);
            }
        }
    }
}

/// Port of `gxr_studio/render/params.py:41 RecipeSel`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RecipeSel {
    pub unit: String,
    pub slot: i64,
    pub levels: Vec<(String, RecipeLevel)>,
    pub kind: String,
    pub id: Option<String>,
}

impl RecipeSel {
    pub fn unit_index(&self) -> Option<usize> {
        UNITS.iter().position(|&u| u == self.unit)
    }

    pub fn to_json(&self) -> Json {
        let levels = Json::Object(
            self.levels
                .iter()
                .map(|(k, v)| match v {
                    RecipeLevel::Int(i) => (k.clone(), Json::Int(*i)),
                    // BigInt cannot appear in normal recipes; fallback to string only if Json is called directly.
                    RecipeLevel::BigInt { neg, digits } => (
                        k.clone(),
                        Json::Str(if *neg {
                            format!("-{digits}")
                        } else {
                            digits.clone()
                        }),
                    ),
                })
                .collect(),
        );
        let mut pairs = vec![
            ("kind", Json::str(&self.kind)),
            ("unit", Json::str(&self.unit)),
            ("slot", Json::Int(self.slot)),
            ("levels", levels),
        ];
        if let Some(id) = &self.id
            && !id.is_empty()
        {
            pairs.push(("id", Json::str(id)));
        }
        Json::object(pairs)
    }

    fn write_canonical(&self, out: &mut String) {
        out.push('{');
        let mut first = true;
        if let Some(id) = &self.id
            && !id.is_empty()
        {
            out.push_str("\"id\":");
            out.push_str(&Json::str(id).render());
            first = false;
        }
        if !first {
            out.push(',');
        }
        out.push_str("\"kind\":");
        out.push_str(&Json::str(&self.kind).render());
        out.push_str(",\"levels\":{");
        for (i, (k, v)) in self.levels.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            out.push_str(&Json::str(k).render());
            out.push(':');
            v.write_json(out);
        }
        out.push_str("},\"slot\":");
        out.push_str(&self.slot.to_string());
        out.push_str(",\"unit\":");
        out.push_str(&Json::str(&self.unit).render());
        out.push('}');
    }

    fn write_display_json(&self, out: &mut String) {
        out.push_str("{\"kind\":");
        out.push_str(&Json::str(&self.kind).render());
        out.push_str(",\"unit\":");
        out.push_str(&Json::str(&self.unit).render());
        out.push_str(",\"slot\":");
        out.push_str(&self.slot.to_string());
        out.push_str(",\"levels\":{");
        for (i, (k, v)) in self.levels.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            out.push_str(&Json::str(k).render());
            out.push(':');
            v.write_json(out);
        }
        out.push('}');
        if let Some(id) = &self.id
            && !id.is_empty()
        {
            out.push_str(",\"id\":");
            out.push_str(&Json::str(id).render());
        }
        out.push('}');
    }
}

/// Port of `gxr_studio/render/params.py:60 Adjust`.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct EditAdjust {
    pub exposure_ev: f64,
    pub warm: i64,
    pub tint: i64,
    pub contrast: i64,
    pub highlight: i64,
    pub shadow: i64,
    pub black_point: i64,
    pub saturation: i64,
    pub vibrance: i64,
    pub vignette: i64,
}

impl EditAdjust {
    pub fn is_identity(&self) -> bool {
        *self == Self::default()
    }

    pub fn tone_is_identity(&self) -> bool {
        self.contrast == 0 && self.highlight == 0 && self.shadow == 0
    }

    pub fn to_tones(&self) -> ToneAdjust {
        ToneAdjust {
            exposure_ev: self.exposure_ev,
            warm: self.warm,
            tint: self.tint,
            contrast: self.contrast,
            highlight: self.highlight,
            shadow: self.shadow,
        }
    }

    pub fn to_json(&self) -> Json {
        let mut pairs = vec![
            ("exposure_ev", Json::Float(self.exposure_ev)),
            (
                "wb",
                Json::object([
                    ("warm", Json::Int(self.warm)),
                    ("tint", Json::Int(self.tint)),
                ]),
            ),
            ("contrast", Json::Int(self.contrast)),
            ("highlight", Json::Int(self.highlight)),
            ("shadow", Json::Int(self.shadow)),
        ];
        for (key, value) in [
            ("black_point", self.black_point),
            ("saturation", self.saturation),
            ("vibrance", self.vibrance),
            ("vignette", self.vignette),
        ] {
            if value != 0 {
                pairs.push((key, Json::Int(value)));
            }
        }
        Json::object(pairs)
    }

    fn to_sorted_json(self) -> Json {
        let mut pairs: Vec<(&str, Json)> = Vec::new();
        if self.black_point != 0 {
            pairs.push(("black_point", Json::Int(self.black_point)));
        }
        pairs.push(("contrast", Json::Int(self.contrast)));
        pairs.push(("exposure_ev", Json::Float(self.exposure_ev)));
        pairs.push(("highlight", Json::Int(self.highlight)));
        if self.saturation != 0 {
            pairs.push(("saturation", Json::Int(self.saturation)));
        }
        pairs.push(("shadow", Json::Int(self.shadow)));
        if self.vibrance != 0 {
            pairs.push(("vibrance", Json::Int(self.vibrance)));
        }
        if self.vignette != 0 {
            pairs.push(("vignette", Json::Int(self.vignette)));
        }
        pairs.push((
            "wb",
            Json::object([
                ("tint", Json::Int(self.tint)),
                ("warm", Json::Int(self.warm)),
            ]),
        ));
        Json::object(pairs)
    }
}

fn curves_to_json(c: &Curves, sorted: bool) -> Json {
    let channels = if sorted {
        [
            ("b", c.b.as_deref()),
            ("g", c.g.as_deref()),
            ("master", c.master.as_deref()),
            ("r", c.r.as_deref()),
        ]
    } else {
        [
            ("master", c.master.as_deref()),
            ("r", c.r.as_deref()),
            ("g", c.g.as_deref()),
            ("b", c.b.as_deref()),
        ]
    };
    let mut pairs = Vec::new();
    for (name, pts) in channels {
        if let Some(pts) = pts {
            let arr = pts
                .iter()
                .map(|&(x, y)| Json::Array(vec![Json::Float(x), Json::Float(y)]))
                .collect();
            pairs.push((name, Json::Array(arr)));
        }
    }
    Json::object(pairs)
}

/// Port of `gxr_studio/render/params.py:108 EditState` (colour/recipe/adjust/curve/lut parts).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct EditState {
    pub recipe: Option<RecipeSel>,
    pub adjust: EditAdjust,
    pub curve: Option<Curves>,
    pub lut: Option<String>,
}

impl EditState {
    /// Port of `gxr_studio/render/params.py:125 EditState.canonical`.
    pub fn canonical(&self) -> String {
        let mut out = String::new();
        out.push_str("{\"adjust\":");
        out.push_str(&self.adjust.to_sorted_json().render());
        out.push_str(",\"curve\":");
        match &self.curve {
            Some(c) => out.push_str(&curves_to_json(c, true).render()),
            None => out.push_str("null"),
        }
        if let Some(lut) = &self.lut {
            out.push_str(",\"lut\":");
            out.push_str(&Json::str(lut).render());
        }
        out.push_str(",\"recipe\":");
        match &self.recipe {
            Some(r) => r.write_canonical(&mut out),
            None => out.push_str("null"),
        }
        out.push('}');
        out
    }

    /// Port of `gxr_studio/render/params.py:129 EditState.digest`.
    pub fn digest(&self) -> String {
        sha256_hex(self.canonical().as_bytes())
    }

    /// CLI output JSON (`recipe`, `adjust`, `curve`, optional `lut`, `canonical`, `digest`).
    pub fn to_cli_json_string(&self) -> String {
        let mut out = String::from("{\"recipe\":");
        match &self.recipe {
            Some(r) => r.write_display_json(&mut out),
            None => out.push_str("null"),
        }
        out.push_str(",\"adjust\":");
        out.push_str(&self.adjust.to_json().render());
        out.push_str(",\"curve\":");
        match &self.curve {
            Some(c) => out.push_str(&curves_to_json(c, false).render()),
            None => out.push_str("null"),
        }
        if let Some(lut) = &self.lut {
            out.push_str(",\"lut\":");
            out.push_str(&Json::str(lut).render());
        }
        let canon = self.canonical();
        let dig = sha256_hex(canon.as_bytes());
        out.push_str(",\"canonical\":");
        out.push_str(&Json::str(canon).render());
        out.push_str(",\"digest\":");
        out.push_str(&Json::str(dig).render());
        out.push('}');
        out
    }
}

/// Port of `gxr_studio/render/params.py:133 _int_in`.
pub fn int_in(value: &PyJson, lo: i64, hi: i64, what: &str) -> Result<i64, EditError> {
    let bad_int = || {
        EditError::with_detail(
            "bad_adjust",
            INVALID_REQUEST,
            format!("无法保存调整。请输入整数。{what}"),
        )
    };
    let out_of_range = || {
        EditError::with_detail(
            "bad_adjust",
            INVALID_REQUEST,
            format!("无法保存调整。数值超出范围。{what} {lo} 到 {hi}"),
        )
    };
    match value {
        PyJson::Num(PyNum::Int(n)) => {
            if (lo..=hi).contains(n) {
                Ok(*n)
            } else {
                Err(out_of_range())
            }
        }
        PyJson::Num(PyNum::BigInt { .. }) => Err(out_of_range()),
        PyJson::Num(PyNum::Float(f)) => {
            if !f.is_finite() || f.fract() != 0.0 {
                return Err(bad_int());
            }
            if *f < lo as f64 || *f > hi as f64 {
                return Err(out_of_range());
            }
            Ok(*f as i64)
        }
        _ => Err(bad_int()),
    }
}

const ADJUST_KEYS: [&str; 9] = [
    "exposure_ev",
    "wb",
    "contrast",
    "highlight",
    "shadow",
    "black_point",
    "saturation",
    "vibrance",
    "vignette",
];

/// Port of `gxr_studio/render/params.py:142 parse_adjust`.
pub fn parse_adjust(raw: Option<&PyJson>) -> Result<EditAdjust, EditError> {
    let Some(raw) = raw else {
        return Ok(EditAdjust::default());
    };
    if matches!(raw, PyJson::Null) {
        return Ok(EditAdjust::default());
    }
    let Some(pairs) = raw.as_object() else {
        return Err(EditError::with_detail(
            "bad_adjust",
            INVALID_REQUEST,
            "adjust 要是一个对象",
        ));
    };
    for (k, _) in pairs {
        if !ADJUST_KEYS.contains(&k.as_str()) {
            return Err(EditError::with_detail(
                "bad_adjust",
                INVALID_REQUEST,
                "有无法识别的调整项。",
            ));
        }
    }
    let ev = match raw.get("exposure_ev") {
        None => 0.0,
        Some(PyJson::Num(PyNum::Int(i))) => {
            if (-5..=5).contains(i) {
                *i as f64
            } else {
                return Err(EditError::with_detail(
                    "bad_adjust",
                    INVALID_REQUEST,
                    "曝光要在 ±5 挡以内。",
                ));
            }
        }
        Some(PyJson::Num(PyNum::Float(f))) if f.is_finite() && f.abs() <= EXPOSURE_RANGE => *f,
        _ => {
            return Err(EditError::with_detail(
                "bad_adjust",
                INVALID_REQUEST,
                "曝光要在 ±5 挡以内。",
            ));
        }
    };
    let wb_val = raw.get("wb");
    let wb_pairs: &[(String, PyJson)] = match wb_val {
        None => &[],
        Some(v) if !v.is_truthy() => &[],
        Some(PyJson::Object(wb)) => {
            for (k, _) in wb {
                if k != "warm" && k != "tint" {
                    return Err(EditError::with_detail(
                        "bad_adjust",
                        INVALID_REQUEST,
                        "白平衡只能有色温和色调",
                    ));
                }
            }
            wb.as_slice()
        }
        Some(_) => {
            return Err(EditError::with_detail(
                "bad_adjust",
                INVALID_REQUEST,
                "白平衡只能有色温和色调",
            ));
        }
    };
    let wb_get = |key: &str| wb_pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v);
    let zero = PyJson::Num(PyNum::Int(0));
    let get_or_zero = |key: &str| raw.get(key).unwrap_or(&zero);
    Ok(EditAdjust {
        exposure_ev: py_round4(ev),
        warm: int_in(
            wb_get("warm").unwrap_or(&zero),
            -SLIDER_RANGE,
            SLIDER_RANGE,
            "色温偏移",
        )?,
        tint: int_in(
            wb_get("tint").unwrap_or(&zero),
            -SLIDER_RANGE,
            SLIDER_RANGE,
            "色调偏移",
        )?,
        contrast: int_in(
            get_or_zero("contrast"),
            -SLIDER_RANGE,
            SLIDER_RANGE,
            "对比度",
        )?,
        highlight: int_in(
            get_or_zero("highlight"),
            -SLIDER_RANGE,
            SLIDER_RANGE,
            "高光",
        )?,
        shadow: int_in(get_or_zero("shadow"), -SLIDER_RANGE, SLIDER_RANGE, "阴影")?,
        black_point: int_in(
            get_or_zero("black_point"),
            -SLIDER_RANGE,
            SLIDER_RANGE,
            "黑点",
        )?,
        saturation: int_in(
            get_or_zero("saturation"),
            -SLIDER_RANGE,
            SLIDER_RANGE,
            "饱和度",
        )?,
        vibrance: int_in(
            get_or_zero("vibrance"),
            -SLIDER_RANGE,
            SLIDER_RANGE,
            "自然饱和度",
        )?,
        vignette: int_in(get_or_zero("vignette"), -SLIDER_RANGE, SLIDER_RANGE, "暗角")?,
    })
}

/// Port of `gxr_studio/render/params.py:170 parse_curve`.
pub fn parse_curve(raw: Option<&PyJson>) -> Result<Option<Curves>, EditError> {
    let Some(raw) = raw else {
        return Ok(None);
    };
    if matches!(raw, PyJson::Null) {
        return Ok(None);
    }
    let Some(pairs) = raw.as_object() else {
        return Err(EditError::new(
            "bad_curve",
            "曲线只能包含主曲线、红、绿、蓝四个通道。",
        ));
    };
    for (k, _) in pairs {
        if !CURVE_CHANNELS.contains(&k.as_str()) {
            return Err(EditError::new(
                "bad_curve",
                "曲线只能包含主曲线、红、绿、蓝四个通道。",
            ));
        }
    }
    let master = parse_points(raw.get("master"))?;
    let r = parse_points(raw.get("r"))?;
    let g = parse_points(raw.get("g"))?;
    let b = parse_points(raw.get("b"))?;
    if master.is_none() && r.is_none() && g.is_none() && b.is_none() {
        return Ok(None);
    }
    Ok(Some(Curves { master, r, g, b }))
}

/// Port of `gxr_studio/render/params.py:180 _parse_points`.
pub fn parse_points(pts: Option<&PyJson>) -> Result<Option<Vec<(f64, f64)>>, EditError> {
    let Some(pts) = pts else {
        return Ok(None);
    };
    if matches!(pts, PyJson::Null) {
        return Ok(None);
    }
    let Some(arr) = pts.as_array() else {
        return Err(EditError::new("bad_curve", "每条曲线要有 2 到 16 个点。"));
    };
    if !(2..=CURVE_MAX_POINTS).contains(&arr.len()) {
        return Err(EditError::new("bad_curve", "每条曲线要有 2 到 16 个点。"));
    }
    let mut out = Vec::with_capacity(arr.len());
    let coord = |v: &PyJson| -> Result<f64, EditError> {
        let bad = || EditError::new("bad_curve", "曲线上的点须包含两个 0 到 255 之间的数值。");
        let f = match v {
            PyJson::Num(PyNum::Int(i)) if (0..=255).contains(i) => *i as f64,
            PyJson::Num(PyNum::Float(f)) if f.is_finite() && (0.0..=255.0).contains(f) => *f,
            _ => return Err(bad()),
        };
        Ok(py_round4(f))
    };
    for p in arr {
        let Some(pair) = p.as_array() else {
            return Err(EditError::new(
                "bad_curve",
                "曲线上的点须包含两个 0 到 255 之间的数值。",
            ));
        };
        if pair.len() != 2 {
            return Err(EditError::new(
                "bad_curve",
                "曲线上的点须包含两个 0 到 255 之间的数值。",
            ));
        }
        out.push((coord(&pair[0])?, coord(&pair[1])?));
    }
    if out.windows(2).any(|w| w[1].0 <= w[0].0) {
        return Err(EditError::new(
            "bad_curve",
            "曲线上的点须按从左到右排列，横坐标不能相同。",
        ));
    }
    if out.iter().all(|(x, y)| x == y) {
        return Ok(None);
    }
    Ok(Some(out))
}

fn is_hex64(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

/// Port of `gxr_studio/render/params.py:201 parse_recipe`.
pub fn parse_recipe(raw: Option<&PyJson>) -> Result<Option<RecipeSel>, EditError> {
    let Some(raw) = raw else {
        return Ok(None);
    };
    if matches!(raw, PyJson::Null) {
        return Ok(None);
    }
    if raw.as_object().is_none() {
        return Err(EditError::with_detail(
            "bad_recipe",
            INVALID_REQUEST,
            "recipe 要是一个对象",
        ));
    }
    let kind = match raw.get("kind") {
        None => "builtin",
        Some(PyJson::Str(s))
            if matches!(
                s.as_str(),
                "builtin" | "record" | "look" | "camera_user" | "local"
            ) =>
        {
            s.as_str()
        }
        Some(_) => {
            return Err(EditError::with_detail(
                "recipe_kind_unsupported",
                INVALID_REQUEST,
                "配方类型无法识别。",
            ));
        }
    };
    let identity = if kind != "builtin" {
        match raw.get("id") {
            Some(PyJson::Str(s)) if is_hex64(s) => Some(s.clone()),
            _ => {
                return Err(EditError::with_detail(
                    "bad_recipe",
                    INVALID_REQUEST,
                    "配方条目无法识别。",
                ));
            }
        }
    } else {
        None
    };
    let unit = match raw.get("unit") {
        Some(PyJson::Str(u)) if UNITS.contains(&u.as_str()) => u.clone(),
        _ => {
            return Err(EditError::with_detail(
                "bad_recipe",
                INVALID_REQUEST,
                "镜头单元无法识别。",
            ));
        }
    };
    let slot = match raw.get("slot") {
        Some(PyJson::Num(PyNum::Int(s))) if (1..=8).contains(s) => *s,
        _ => {
            return Err(EditError::with_detail(
                "bad_recipe",
                INVALID_REQUEST,
                "槽位要在 1 到 8。",
            ));
        }
    };
    let mut sorted_levels: BTreeMap<String, RecipeLevel> = BTreeMap::new();
    match raw.get("levels") {
        None => {}
        Some(v) if !v.is_truthy() => {}
        Some(PyJson::Object(pairs)) => {
            let mut keys: Vec<&(String, PyJson)> = pairs.iter().collect();
            keys.sort_by(|a, b| a.0.cmp(&b.0));
            for (name, v) in keys {
                if !KNOB_NAMES.contains(&name.as_str()) {
                    return Err(EditError::with_detail(
                        "bad_recipe",
                        INVALID_REQUEST,
                        format!("旋钮无法使用。{name}"),
                    ));
                }
                let lv = match v {
                    PyJson::Num(PyNum::Int(i)) => RecipeLevel::Int(*i),
                    PyJson::Num(PyNum::BigInt { neg, digits }) => RecipeLevel::BigInt {
                        neg: *neg,
                        digits: digits.clone(),
                    },
                    _ => {
                        return Err(EditError::with_detail(
                            "bad_recipe",
                            INVALID_REQUEST,
                            format!("档位要是整数。{name}"),
                        ));
                    }
                };
                sorted_levels.insert(name.clone(), lv);
            }
        }
        Some(_) => {
            return Err(EditError::with_detail(
                "bad_recipe",
                INVALID_REQUEST,
                "levels 格式不对",
            ));
        }
    }
    if let Some(memory) = raw.get("memory")
        && !matches!(memory, PyJson::Null | PyJson::Object(_))
    {
        return Err(EditError::with_detail(
            "bad_recipe",
            INVALID_REQUEST,
            "memory 要是对象",
        ));
    }
    Ok(Some(RecipeSel {
        unit,
        slot,
        levels: sorted_levels.into_iter().collect(),
        kind: kind.to_string(),
        id: identity,
    }))
}

/// Port of `gxr_studio/render/params.py:271 parse_lut`.
pub fn parse_lut(raw: Option<&PyJson>) -> Result<Option<String>, EditError> {
    let Some(raw) = raw else {
        return Ok(None);
    };
    if matches!(raw, PyJson::Null) {
        return Ok(None);
    }
    match raw.as_str() {
        Some(s) if is_hex64(s) => Ok(Some(s.to_string())),
        _ => Err(EditError::new("bad_lut", INVALID_REQUEST)),
    }
}

/// Parse `recipe`, `adjust`, `curve`, and optional `lut` from a JSON object.
pub fn parse_edit_state(raw: &PyJson) -> Result<EditState, super::error::ResolveError> {
    if raw.as_object().is_none() {
        return Err(gxr_raw::CoreError::new("json", "edit must be a JSON object").into());
    }
    let recipe = parse_recipe(raw.get("recipe"))?;
    let adjust = parse_adjust(raw.get("adjust"))?;
    let curve = parse_curve(raw.get("curve"))?;
    let lut = parse_lut(raw.get("lut"))?;
    Ok(EditState {
        recipe,
        adjust,
        curve,
        lut,
    })
}
