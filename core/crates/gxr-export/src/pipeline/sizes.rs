//! Port of `gxr_studio/finish/sizes.py:56 output_sizes`: the framed long edge per export tier,
//! without rendering. The caller passes the source size (`source_size`: the camera JPG's
//! oriented size, the DNG crop window, or the embedded preview's size), which the phone already
//! knows from the file headers.

use gxr_frame::{Fonts, Val, fit_size, frame_json};
use gxr_raw::{CoreError, Json};

use super::prepare::{FinishEdit, LayoutFacts, load_defaults, val_json};
use super::result::ExportFail;

/// `sizes.TIERS`, in its order.
const TIERS: [(&str, Option<i64>); 4] = [
    ("original", None),
    ("large", Some(3000)),
    ("medium", Some(2048)),
    ("small", Some(1600)),
];

/// `spec.OUTPUT_RATIOS`.
fn output_ratio(name: &str) -> Option<(i64, i64)> {
    match name {
        "1:1" => Some((1, 1)),
        "4:5" => Some((4, 5)),
        "3:4" => Some((3, 4)),
        "2:3" => Some((2, 3)),
        "9:16" => Some((9, 16)),
        "16:9" => Some((16, 9)),
        "3:2" => Some((3, 2)),
        _ => None,
    }
}

/// Port of `gxr_studio/finish/layout.py:163 crop_ratio`.
fn crop_ratio(preset: Option<&str>, w: i64, h: i64) -> Result<Option<(i64, i64)>, CoreError> {
    let Some(p) = preset else { return Ok(None) };
    match gxr_finish::crop::preset_pair(p) {
        Ok(Some(_)) => {}
        _ => return Ok(None),
    }
    Ok(gxr_finish::crop::target_ratio(p, w, h, false)?.map(|r| (r.numer, r.denom)))
}

/// `output_sizes(...)` as JSON. `rect` and `preset` are the request's crop, as on the desktop.
#[allow(clippy::too_many_arguments)]
pub fn output_sizes(
    source_w: u64,
    source_h: u64,
    edit: &FinishEdit,
    defaults: &[u8],
    rect: Option<[f64; 4]>,
    preset: Option<&str>,
    layout: &LayoutFacts,
    fonts: &Fonts,
) -> Result<Json, ExportFail> {
    let ([_, _, w, h], _valid) =
        gxr_finish::final_box_u64(source_w, source_h, &edit.geometry, rect, preset)?;
    let (w, h) = (w as i64, h as i64);
    let frame = match &edit.frame {
        Some(f) => f.clone(),
        None => load_defaults(defaults)?.0,
    };
    let meta = layout
        .request
        .get("meta")
        .cloned()
        .unwrap_or(Val::Obj(Vec::new()));
    let (out_w, out_h) = if frame.style == "none" {
        (w, h)
    } else {
        let mut req: Vec<(String, Val)> = layout
            .request
            .as_object()
            .unwrap_or(&[])
            .iter()
            .filter(|(k, _)| !matches!(k.as_str(), "w" | "h"))
            .cloned()
            .collect();
        req.push(("w".into(), Val::Int(w)));
        req.push(("h".into(), Val::Int(h)));
        let spec = gxr_frame::parse_json(frame.to_json().render().as_bytes())?;
        match frame_json(&Val::Obj(req), &spec, fonts)? {
            Json::Object(pairs) => {
                let get = |k: &str| {
                    pairs
                        .iter()
                        .find(|(key, _)| key == k)
                        .and_then(|(_, v)| match v {
                            Json::Int(n) => Some(*n),
                            _ => None,
                        })
                };
                (get("width").unwrap_or(w), get("height").unwrap_or(h))
            }
            _ => (w, h),
        }
    };
    let ratio = if frame.style == "none" {
        crop_ratio(preset, w, h)?
    } else {
        output_ratio(&frame.output_ratio)
    };
    let mut sizes = Vec::new();
    for (tier, edge) in TIERS {
        let (sw, sh) = fit_size(out_w, out_h, edge)?;
        sizes.push((tier, Json::ints(&[sw, sh])));
    }
    Ok(Json::object([
        ("photo", Json::ints(&[w, h])),
        ("frame", frame.to_json()),
        ("meta", val_json(&meta)),
        ("crop_preset", preset.map_or(Json::Null, Json::str)),
        (
            "ratio",
            ratio.map_or(Json::Null, |(a, b)| Json::ints(&[a, b])),
        ),
        ("sizes", Json::object(sizes)),
    ]))
}
