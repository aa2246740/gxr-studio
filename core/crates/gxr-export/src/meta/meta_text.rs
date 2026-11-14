//! Port of `gxr_studio/finish/meta_text.py` (`_ratio`, `_exif`, `frame_date`, `photo_meta`).
//!
//! Never reads the system clock or local time zone; callers pass `mtime_ms` and `tz_offset_min`
//! explicitly.

use std::collections::BTreeMap;

use gxr_raw::{CoreError, Json};

use super::photo_meta::{NaiveDateTime, read_dng_meta, read_jpeg_meta};
use super::tiff_exif::{ExifValue, load_image_exif, open_image};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PhotoTextResult {
    pub lens: String,
    pub lens_short: String,
    pub aperture: String,
    pub shutter: String,
    pub iso: String,
    pub date: String,
    pub frame_no: String,
    pub taken: Option<NaiveDateTime>,
    pub file_time: Option<NaiveDateTime>,
}

impl PhotoTextResult {
    pub fn to_json(&self) -> Json {
        Json::object([
            (
                "frame",
                Json::object([
                    ("lens", Json::str(&self.lens)),
                    ("lens_short", Json::str(&self.lens_short)),
                    ("aperture", Json::str(&self.aperture)),
                    ("shutter", Json::str(&self.shutter)),
                    ("iso", Json::str(&self.iso)),
                    ("date", Json::str(&self.date)),
                    ("frame_no", Json::str(&self.frame_no)),
                ]),
            ),
            (
                "taken",
                Json::opt(self.taken.as_ref().map(|dt| Json::str(dt.to_iso_string()))),
            ),
            (
                "file_time",
                Json::opt(
                    self.file_time
                        .as_ref()
                        .map(|dt| Json::str(dt.to_iso_string())),
                ),
            ),
        ])
    }
}

/// Port of `gxr_studio/finish/meta_text.py:15 _ratio`.
pub fn ratio(value: Option<&ExifValue>) -> Option<f64> {
    let val = value?;
    let out = val.as_rational_f64()?;
    if out.is_finite() && out > 0.0 {
        Some(out)
    } else {
        None
    }
}

/// Port of `gxr_studio/finish/meta_text.py:39 frame_date`.
pub fn frame_date(moment: &NaiveDateTime) -> String {
    format!("{:04}.{:02}.{:02}", moment.year, moment.month, moment.day)
}

/// Convert epoch milliseconds + `tz_offset_min` (east of UTC) into a local `NaiveDateTime`
/// without accessing the system clock or timezone.
pub fn datetime_from_mtime_ms(
    mtime_ms: f64,
    tz_offset_min: i32,
) -> Result<NaiveDateTime, CoreError> {
    if !mtime_ms.is_finite() {
        return Err(CoreError::new("bad_meta", "mtime_ms must be finite"));
    }
    if !(-1439..=1439).contains(&tz_offset_min) {
        return Err(CoreError::new(
            "bad_meta",
            "tz_offset_min must be strictly between -1440 and 1440",
        ));
    }
    // Python datetime.fromtimestamp(mtime_ms / 1000.0) rounds to nearest microsecond (half to even).
    let total_us_f64 = (mtime_ms * 1000.0).round_ties_even();
    if !(-6.213_559_68e16..=2.534_023_007_999_999e17).contains(&total_us_f64) {
        return Err(CoreError::new("bad_meta", "mtime_ms out of datetime range"));
    }
    #[allow(clippy::cast_possible_truncation)]
    let utc_us = total_us_f64 as i128;
    let offset_us = i128::from(tz_offset_min) * 60 * 1_000_000;
    let local_us = utc_us + offset_us;

    let us_per_day: i128 = 86_400 * 1_000_000;
    let days = local_us.div_euclid(us_per_day);
    let rem_us = local_us.rem_euclid(us_per_day);

    let (year, month, day) = civil_from_days(days);
    if !(1..=9999).contains(&year) {
        return Err(CoreError::new("bad_meta", "mtime_ms year out of 1..=9999"));
    }

    let sec_total = rem_us / 1_000_000;
    let microsecond = (rem_us % 1_000_000) as u32;
    let hour = (sec_total / 3600) as u32;
    let minute = ((sec_total % 3600) / 60) as u32;
    let second = (sec_total % 60) as u32;

    Ok(NaiveDateTime {
        year,
        month,
        day,
        hour,
        minute,
        second,
        microsecond,
    })
}

/// Howard Hinnant's civil-from-days algorithm (days since 1970-01-01).
fn civil_from_days(days: i128) -> (i32, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = (yoe as i128) + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = (y + i128::from(u8::from(m <= 2))) as i32;
    (year, m as u32, d as u32)
}

/// Format positive finite `f64` identically to Python's `f"{x:g}"` (6 significant digits,
/// half-to-even rounding, exponent `< -4` or `>= 6`).
pub fn format_py_g(x: f64) -> String {
    if !x.is_finite() {
        return "0".to_string();
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0" } else { "0" }.to_string();
    }
    let sign = if x < 0.0 { "-" } else { "" };
    let abs_x = x.abs();
    // Exact shortest/high-precision decimal scientific representation from Rust std.
    let raw = format!("{abs_x:.20e}");
    let (mantissa_str, exp_str) = raw.split_once('e').unwrap_or((&raw, "0"));
    let mut exp: i32 = exp_str.parse().unwrap_or(0);
    let mut digits: Vec<u8> = mantissa_str
        .bytes()
        .filter(u8::is_ascii_digit)
        .map(|b| b - b'0')
        .collect();

    // Round to 6 significant digits using half-to-even.
    if digits.len() > 6 {
        let round_digit = digits[6];
        let rest_nonzero = digits[7..].iter().any(|&d| d != 0);
        let round_up =
            round_digit > 5 || (round_digit == 5 && (rest_nonzero || !digits[5].is_multiple_of(2)));
        digits.truncate(6);
        if round_up {
            let mut carry = 1u8;
            for d in digits.iter_mut().rev() {
                let sum = *d + carry;
                *d = sum % 10;
                carry = sum / 10;
                if carry == 0 {
                    break;
                }
            }
            if carry > 0 {
                digits.insert(0, carry);
                digits.truncate(6);
                exp += 1;
            }
        }
    }

    // Strip trailing zeros from significant digits (leaving at least 1 digit).
    let mut sig_len = digits.len();
    while sig_len > 1 && digits[sig_len - 1] == 0 {
        sig_len -= 1;
    }
    let sig = &digits[..sig_len];

    let mut out = String::from(sign);
    if !(-4..6).contains(&exp) {
        out.push(char::from(b'0' + sig[0]));
        if sig.len() > 1 {
            out.push('.');
            for &d in &sig[1..] {
                out.push(char::from(b'0' + d));
            }
        }
        let exp_sign = if exp < 0 { '-' } else { '+' };
        let exp_abs = exp.unsigned_abs();
        out.push('e');
        out.push(exp_sign);
        out.push_str(&format!("{exp_abs:02}"));
    } else if exp < 0 {
        out.push_str("0.");
        let zeros = (-exp - 1) as usize;
        for _ in 0..zeros {
            out.push('0');
        }
        for &d in sig {
            out.push(char::from(b'0' + d));
        }
    } else {
        let int_digits = (exp as usize) + 1;
        if sig.len() <= int_digits {
            for &d in sig {
                out.push(char::from(b'0' + d));
            }
            for _ in sig.len()..int_digits {
                out.push('0');
            }
        } else {
            for &d in &sig[..int_digits] {
                out.push(char::from(b'0' + d));
            }
            out.push('.');
            for &d in &sig[int_digits..] {
                out.push(char::from(b'0' + d));
            }
        }
    }
    out
}

/// Port of `gxr_studio/finish/meta_text.py:44 photo_meta`.
///
/// Mirrors `meta_text._exif` behavior on corrupt inputs: if `open_image` fails, `sub` is empty and
/// `file_time` is `None`.
pub fn photo_meta_from_bytes(
    jpg: Option<&[u8]>,
    dng: Option<&[u8]>,
    name: &str,
    mtime_ms: Option<f64>,
    tz_offset_min: i32,
    now_year: i32,
) -> Result<PhotoTextResult, CoreError> {
    let mtime_dt = match mtime_ms {
        Some(ms) => Some(datetime_from_mtime_ms(ms, tz_offset_min)?),
        None => {
            if !(-1439..=1439).contains(&tz_offset_min) {
                return Err(CoreError::new(
                    "bad_meta",
                    "tz_offset_min must be strictly between -1440 and 1440",
                ));
            }
            None
        }
    };

    let (bytes, is_jpg) = match (jpg, dng) {
        (Some(j), _) => (j, true),
        (None, Some(d)) => (d, false),
        (None, None) => {
            return Err(CoreError::new(
                "bad_input",
                "need at least one of jpg or dng",
            ));
        }
    };

    let parsed = if is_jpg {
        read_jpeg_meta(bytes, now_year)
    } else {
        read_dng_meta(bytes, now_year)
    };

    let (sub, file_time): (BTreeMap<u16, ExifValue>, Option<NaiveDateTime>) =
        match open_image(bytes) {
            Ok(doc) => (load_image_exif(bytes, &doc).sub, mtime_dt),
            Err(_) => (BTreeMap::new(), None),
        };

    let shutter = ratio(sub.get(&33434));
    let aperture = ratio(sub.get(&33437));
    let shutter_text = match shutter {
        None => String::new(),
        Some(s) if s >= 1.0 => format!("{}s", format_py_g(s)),
        Some(s) => {
            let denom = (1.0 / s).round_ties_even();
            #[allow(clippy::cast_possible_truncation)]
            let denom_i = if denom.is_finite() && denom >= 1.0 {
                if denom > (i64::MAX as f64) {
                    i64::MAX
                } else {
                    denom as i64
                }
            } else {
                1
            };
            format!("1/{denom_i}s")
        }
    };

    let raw_lens = parsed.lens.unwrap_or_default();
    let lens = raw_lens
        .strip_prefix("RICOH ")
        .unwrap_or(&raw_lens)
        .trim()
        .to_string();
    let taken = parsed.taken;
    let moment = taken.as_ref().or(file_time.as_ref());
    let date = moment.map(frame_date).unwrap_or_default();
    let iso = match parsed.iso {
        Some(v) if v != 0 => format!("ISO {v}"),
        _ => String::new(),
    };
    let lens_short = if lens.contains("50") {
        "A12 50mm".to_string()
    } else {
        lens.clone()
    };

    let name_chars: Vec<char> = name.chars().collect();
    let start = name_chars.len().saturating_sub(4);
    let frame_no: String = name_chars[start..].iter().collect();

    Ok(PhotoTextResult {
        lens,
        lens_short,
        aperture: match aperture {
            None => String::new(),
            Some(a) => format!("f/{}", format_py_g(a)),
        },
        shutter: shutter_text,
        iso,
        date,
        frame_no,
        taken,
        file_time,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn format_py_g_matches_python_general_format() {
        assert_eq!(format_py_g(2.0), "2");
        assert_eq!(format_py_g(2.8), "2.8");
        assert_eq!(format_py_g(0.0), "0");
        assert_eq!(format_py_g(-0.0), "-0");
        assert_eq!(format_py_g(0.00005), "5e-05");
        assert_eq!(format_py_g(0.0001), "0.0001");
        assert_eq!(format_py_g(1_000_000.0), "1e+06");
        assert_eq!(format_py_g(999_999.0), "999999");
        assert_eq!(format_py_g(1.234_567_8), "1.23457");
        assert_eq!(format_py_g(100_000.0), "100000");
    }

    #[test]
    fn datetime_from_mtime_ms_handles_tz_and_pre_epoch() {
        // 2024-03-01 00:00:00 UTC = 1709251200_000 ms; with +480 min (+08:00) -> 08:00:00
        let dt = datetime_from_mtime_ms(1_709_251_200_000.0, 480).expect("valid mtime");
        assert_eq!(dt, NaiveDateTime::new(2024, 3, 1, 8, 0, 0));
        assert_eq!(frame_date(&dt), "2024.03.01");

        // Pre-1970: -86400_000 ms -> 1969-12-31 00:00:00 UTC
        let pre = datetime_from_mtime_ms(-86_400_000.0, 0).expect("pre-1970");
        assert_eq!(pre, NaiveDateTime::new(1969, 12, 31, 0, 0, 0));

        // Bank-rounding to nearest microsecond across second boundary
        let carry = datetime_from_mtime_ms(999.999_9, 0).expect("carry");
        assert_eq!(carry, NaiveDateTime::new(1970, 1, 1, 0, 0, 1));

        // Invalid tz_offset_min or non-finite mtime
        assert!(datetime_from_mtime_ms(f64::NAN, 0).is_err());
        assert!(datetime_from_mtime_ms(0.0, 1440).is_err());
        assert!(datetime_from_mtime_ms(0.0, -1440).is_err());
    }

    #[test]
    fn ratio_handles_zero_denom_and_variants() {
        assert_eq!(ratio(Some(&ExifValue::Rational(1, 0))), None);
        assert_eq!(ratio(Some(&ExifValue::Rational(28, 10))), Some(2.8));
        assert_eq!(ratio(Some(&ExifValue::Int(4))), Some(4.0));
        assert_eq!(ratio(Some(&ExifValue::Float(f64::NAN))), None);
    }
}
