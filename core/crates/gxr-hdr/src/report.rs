//! The JSON that `gxr-cli` prints and `gxr-wasm` returns, built in one place so both give
//! the same bytes.

use gxr_raw::{CoreError, Json};

use crate::curve::GainResult;
use crate::gain::{Plane32, align, check_alignment};
use crate::number::limit_65536;

/// `align(sdr, raw)` with every dataclass field, and the `check_alignment` outcome
/// (`null`, or the `HdrUnsupported` reason and message).
pub fn align_report(sdr: &Plane32, raw: &Plane32) -> Result<Json, CoreError> {
    let al = align(sdr, raw)?;
    let check = match check_alignment(&al) {
        Ok(()) => Json::Null,
        Err(e) => Json::object([("kind", Json::str(e.kind)), ("message", Json::str(e.message))]),
    };
    Ok(Json::object([("alignment", al.to_json()), ("check", check)]))
}

/// `GainMeta`, its ISO 21496-1 fractions (`limit_denominator(65536)`) and the stats dict.
pub fn gain_report(result: &GainResult) -> Result<Json, CoreError> {
    let m = &result.meta;
    let fields = [
        ("gain_min", m.gain_min),
        ("gain_max", m.gain_max),
        ("gamma", m.gamma),
        ("offset_sdr", m.offset_sdr),
        ("offset_hdr", m.offset_hdr),
        ("cap_min", m.cap_min),
        ("cap_max", m.cap_max),
    ];
    let mut fracs = Vec::new();
    for (k, v) in fields {
        let (n, d) = limit_65536(v)?;
        let big = || CoreError::new("bad_input", "分数太大");
        fracs.push((k, Json::ints(&[i64::try_from(n).map_err(|_| big())?, i64::try_from(d).map_err(|_| big())?])));
    }
    Ok(Json::object([
        ("meta", Json::object(fields.iter().map(|&(k, v)| (k, Json::Float(v))))),
        ("fractions", Json::object(fracs)),
        ("stats", result.stats.clone()),
    ]))
}
