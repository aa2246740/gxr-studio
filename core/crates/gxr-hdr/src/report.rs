//! The JSON that `gxr-cli` prints and `gxr-wasm` returns, built in one place so both give
//! the same bytes.
//!
//! JSON never carries NaN or Infinity (`JSON.parse` refuses them). A non-finite float at the
//! report boundary becomes one `CoreError` instead (Sol r1 P2-03). The pipeline itself is
//! unchanged: `compute_gain` still uses RAW NaN for pixels outside the photograph.

use gxr_raw::{CoreError, Json};

use crate::curve::GainResult;
use crate::gain::{Plane32, align, check_alignment};
use crate::number::limit_65536;

/// The one error for a report that would contain NaN or Infinity.
pub fn non_finite() -> CoreError {
    CoreError::new("value_error", "结果含 NaN 或无穷大")
}

/// Refuse a JSON value holding any non-finite float, at any depth.
pub fn ensure_finite(json: &Json) -> Result<(), CoreError> {
    match json {
        Json::Float(v) if !v.is_finite() => Err(non_finite()),
        Json::Array(items) => items.iter().try_for_each(ensure_finite),
        Json::Object(pairs) => pairs.iter().try_for_each(|(_, v)| ensure_finite(v)),
        _ => Ok(()),
    }
}

/// `align(sdr, raw)` with every dataclass field, and the `check_alignment` outcome
/// (`null`, or the `HdrUnsupported` reason and message).
pub fn align_report(sdr: &Plane32, raw: &Plane32) -> Result<Json, CoreError> {
    let al = align(sdr, raw)?;
    let check = match check_alignment(&al) {
        Ok(()) => Json::Null,
        Err(e) => Json::object([
            ("kind", Json::str(e.kind)),
            ("message", Json::str(e.message)),
        ]),
    };
    let report = Json::object([("alignment", al.to_json()), ("check", check)]);
    ensure_finite(&report)?;
    Ok(report)
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
    let meta = Json::object(fields.iter().map(|&(k, v)| (k, Json::Float(v))));
    // Checked before the fractions, which cannot represent NaN or Infinity either.
    ensure_finite(&meta)?;
    ensure_finite(&result.stats)?;
    let mut fracs = Vec::new();
    for (k, v) in fields {
        let (n, d) = limit_65536(v)?;
        let big = || CoreError::new("bad_input", "分数太大");
        fracs.push((
            k,
            Json::ints(&[
                i64::try_from(n).map_err(|_| big())?,
                i64::try_from(d).map_err(|_| big())?,
            ]),
        ));
    }
    Ok(Json::object([
        ("meta", meta),
        ("fractions", Json::object(fracs)),
        ("stats", result.stats.clone()),
    ]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn non_finite_floats_never_reach_json() {
        for bad in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let nested = Json::object([("a", Json::Array(vec![Json::Int(1), Json::Float(bad)]))]);
            let err = ensure_finite(&nested).unwrap_err();
            assert_eq!(
                (err.kind, err.message.as_str()),
                ("value_error", "结果含 NaN 或无穷大")
            );
        }
        assert!(ensure_finite(&Json::object([("a", Json::Float(1.5))])).is_ok());
    }
}
