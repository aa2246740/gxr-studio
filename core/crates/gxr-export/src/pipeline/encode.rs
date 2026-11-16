//! The last two steps of `export_finish` before HDR: `_fit` to the long edge, then one JPEG
//! encode with `_finish`'s arguments (`write_rgb_with_exif`).

use gxr_finish::{Filter, Mode, resample_u8_u64};
use gxr_jpeg::{ExifSpec, ExifVal, encode_jpeg_with};
use gxr_raw::{CoreError, Json};

/// A uint8 RGB picture, row-major.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Rgb {
    pub width: usize,
    pub height: usize,
    pub data: Vec<u8>,
}

/// Port of `gxr_studio/finish/serve.py:94 _fit`: `Image.resize(fit_size(w, h, max_edge),
/// LANCZOS)` when the long side is over `max_edge`, else the array itself.
pub fn fit(rgb: Rgb, max_edge: Option<i64>) -> Result<Rgb, CoreError> {
    // A public entry: check the picture before the no-resize return (Sol r1 P2-04).
    super::complete::checked_rgb(
        &rgb.data,
        super::prepare::dim_u64(rgb.width)?,
        super::prepare::dim_u64(rgb.height)?,
    )?;
    let Some(edge) = max_edge.filter(|&e| e != 0) else {
        return Ok(rgb);
    };
    let (w, h) = (
        i64::try_from(rgb.width).map_err(|_| too_big())?,
        i64::try_from(rgb.height).map_err(|_| too_big())?,
    );
    if w.max(h) <= edge {
        return Ok(rgb);
    }
    let (ow, oh) = gxr_frame::fit_size(w, h, Some(edge))?;
    let (ow, oh) = (
        u64::try_from(ow).map_err(|_| too_big())?,
        u64::try_from(oh).map_err(|_| too_big())?,
    );
    let data = resample_u8_u64(
        &rgb.data,
        rgb.width as u64,
        rgb.height as u64,
        Mode::Rgb,
        Filter::Lanczos,
        ow,
        oh,
    )?;
    Ok(Rgb {
        width: usize::try_from(ow).map_err(|_| too_big())?,
        height: usize::try_from(oh).map_err(|_| too_big())?,
        data,
    })
}

fn too_big() -> CoreError {
    CoreError::new("too_big", "图像太大")
}

/// `source_exif`'s `base[270] = note`, on the source EXIF the caller read (export-meta). The
/// rationals stay as read: gxr-jpeg writes them the way Pillow does.
pub fn with_note(mut spec: ExifSpec, note: &str) -> ExifSpec {
    spec.base.insert(270, ExifVal::Str(note.to_string()));
    spec
}

/// `ExifSpec` as the JSON `gxr_jpeg::parse_exif_json` reads back to the same spec.
pub fn exif_spec_json(spec: &ExifSpec) -> Result<String, CoreError> {
    let map = |m: &std::collections::BTreeMap<u16, ExifVal>| -> Result<Json, CoreError> {
        let mut pairs = Vec::new();
        for (tag, v) in m {
            pairs.push((tag.to_string(), exif_val_json(v)?));
        }
        Ok(Json::Object(pairs))
    };
    Ok(Json::object([("base", map(&spec.base)?), ("exif", map(&spec.exif)?)]).render())
}

fn exif_val_json(v: &ExifVal) -> Result<Json, CoreError> {
    let empty = || CoreError::new("bad_exif", "EXIF array cannot be empty");
    Ok(match v {
        ExifVal::Str(s) => Json::str(s.clone()),
        ExifVal::Bytes(b) => Json::object([(
            "bytes",
            Json::Array(b.iter().map(|&x| Json::Int(i64::from(x))).collect()),
        )]),
        ExifVal::Ints(v) => match v.as_slice() {
            [] => return Err(empty()),
            [one] => Json::Int(*one),
            many => Json::ints(many),
        },
        ExifVal::Floats(v) => match v.as_slice() {
            [] => return Err(empty()),
            [one] => Json::Float(*one),
            many => Json::floats(many),
        },
        ExifVal::Rationals(v) => {
            if v.is_empty() {
                return Err(empty());
            }
            Json::object([(
                "rationals",
                Json::Array(v.iter().map(|&(n, d)| Json::ints(&[n, d])).collect()),
            )])
        }
    })
}

/// Port of `gxr_studio/export.py:363 write_rgb_with_exif` (JPEG): one encode of the finished
/// array with `_finish`'s arguments, `optimize=True` included, and the source EXIF with
/// `base[270] = note`.
pub fn write_rgb_with_exif(
    rgb: &Rgb,
    source: &ExifSpec,
    note: &str,
    quality: i64,
) -> Result<Vec<u8>, CoreError> {
    let spec = with_note(source.clone(), note);
    let json = exif_spec_json(&spec)?;
    let w = u32::try_from(rgb.width).map_err(|_| too_big())?;
    let h = u32::try_from(rgb.height).map_err(|_| too_big())?;
    let q = i32::try_from(quality)
        .map_err(|_| CoreError::new("bad_quality", "品质要在 1 到 100 之间。"))?;
    encode_jpeg_with(&rgb.data, w, h, q, &json, true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exif_json_round_trips() {
        let mut spec = ExifSpec::default();
        spec.base.insert(271, ExifVal::Str("RICOH".into()));
        spec.base.insert(33432, ExifVal::Bytes(vec![0xA9, 0x20]));
        spec.exif.insert(34855, ExifVal::Ints(vec![200]));
        spec.exif.insert(33434, ExifVal::Rationals(vec![(1, 250)]));
        spec.exif.insert(37500, ExifVal::Ints(vec![1, 2]));
        spec.exif.insert(41728, ExifVal::Floats(vec![0.1]));
        let back = gxr_jpeg::parse_exif_json(&exif_spec_json(&spec).unwrap()).unwrap();
        assert_eq!(back.base, spec.base);
        assert_eq!(back.exif, spec.exif);
    }

    #[test]
    fn fit_rejects_mismatched_pictures() {
        for (w, h, n) in [(2, 1, 3), (0, 1, 0), (usize::MAX, 1, 0)] {
            let rgb = Rgb {
                width: w,
                height: h,
                data: vec![7; n],
            };
            assert!(fit(rgb, None).is_err(), "{w}x{h} with {n} bytes");
        }
    }

    #[test]
    fn fit_keeps_small_pictures() {
        let rgb = Rgb {
            width: 2,
            height: 1,
            data: vec![1, 2, 3, 4, 5, 6],
        };
        assert_eq!(fit(rgb.clone(), Some(64)).unwrap(), rgb);
        assert_eq!(fit(rgb.clone(), None).unwrap(), rgb);
    }
}
