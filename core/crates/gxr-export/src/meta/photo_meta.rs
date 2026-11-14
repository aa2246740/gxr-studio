//! Port of `gxr_studio/meta.py` (`parse_exif_datetime`, `plausible`, `_clean`, `_iso`,
//! `_orientation`, `_oriented`, `read_jpeg_meta`, `read_dng_meta`) and `camdata/recipe_tags.py`
//! (`copyright_text`, `parse_recipe_tag`).

use gxr_camdata::recipe_tags::KNOB_ORDER;
use gxr_camdata::{RecipeTag, parse_recipe_tag, parse_recipe_tag_bytes};
use gxr_raw::Json;

use super::exif::clean_text;
use super::tiff_exif::{
    EXIF_IFD_TAG, ExifValue, ImageDoc, load_ifd, load_image_exif, parse_jpeg, parse_tiff,
};

pub const MIN_YEAR: i32 = 1995;
/// Default reference year for `plausible()` so Rust never reads the system clock.
pub const DEFAULT_NOW_YEAR: i32 = 2026;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NaiveDateTime {
    pub year: i32,
    pub month: u32,
    pub day: u32,
    pub hour: u32,
    pub minute: u32,
    pub second: u32,
    pub microsecond: u32,
}

impl NaiveDateTime {
    pub fn new(year: i32, month: u32, day: u32, hour: u32, minute: u32, second: u32) -> Self {
        Self {
            year,
            month,
            day,
            hour,
            minute,
            second,
            microsecond: 0,
        }
    }

    pub fn to_iso_string(&self) -> String {
        if self.microsecond == 0 {
            format!(
                "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
                self.year, self.month, self.day, self.hour, self.minute, self.second
            )
        } else {
            format!(
                "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:06}",
                self.year,
                self.month,
                self.day,
                self.hour,
                self.minute,
                self.second,
                self.microsecond
            )
        }
    }
}

/// Port of `gxr_studio/meta.py:28 PhotoMeta`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PhotoMeta {
    pub width: Option<u32>,
    pub height: Option<u32>,
    pub orientation: i64,
    pub taken: Option<NaiveDateTime>,
    pub iso: Option<i64>,
    pub lens: Option<String>,
    pub model: Option<String>,
    pub error: Option<String>,
    pub copyright: Option<String>,
    pub recipe_tag: Option<RecipeTag>,
}

impl Default for PhotoMeta {
    fn default() -> Self {
        Self {
            width: None,
            height: None,
            orientation: 1,
            taken: None,
            iso: None,
            lens: None,
            model: None,
            error: None,
            copyright: None,
            recipe_tag: None,
        }
    }
}

impl PhotoMeta {
    pub fn from_error(msg: impl Into<String>) -> Self {
        Self {
            error: Some(msg.into()),
            ..Self::default()
        }
    }

    pub fn to_json(&self) -> Json {
        Json::object([
            (
                "width",
                Json::opt(self.width.map(|v| Json::Int(i64::from(v)))),
            ),
            (
                "height",
                Json::opt(self.height.map(|v| Json::Int(i64::from(v)))),
            ),
            ("orientation", Json::Int(self.orientation)),
            (
                "taken",
                Json::opt(self.taken.as_ref().map(|dt| Json::str(dt.to_iso_string()))),
            ),
            ("iso", Json::opt(self.iso.map(Json::Int))),
            ("lens", Json::opt(self.lens.as_deref().map(Json::str))),
            ("model", Json::opt(self.model.as_deref().map(Json::str))),
            ("error", Json::opt(self.error.as_deref().map(Json::str))),
            (
                "copyright",
                Json::opt(self.copyright.as_deref().map(Json::str)),
            ),
            (
                "recipe_tag",
                recipe_tag_inner_json(self.recipe_tag.as_ref()),
            ),
        ])
    }
}

fn recipe_tag_inner_json(tag: Option<&RecipeTag>) -> Json {
    let Some(t) = tag else {
        return Json::Null;
    };
    let levels_json = match &t.levels {
        None => Json::Null,
        Some(levels) => Json::object(
            KNOB_ORDER
                .iter()
                .zip(levels.iter())
                .map(|(&name, &val)| (name, Json::opt(val.map(Json::Int)))),
        ),
    };
    Json::object([
        ("version", Json::Int(t.version)),
        ("catalog_identity", Json::str(&t.catalog_identity)),
        ("unit", Json::str(&t.unit)),
        ("position", Json::Int(t.position)),
        (
            "knob_code",
            Json::opt(t.knob_code.as_deref().map(Json::str)),
        ),
        ("levels", levels_json),
    ])
}

/// Port of `gxr_studio/meta.py:59 plausible`.
pub fn plausible(dt: &NaiveDateTime, now_year: i32) -> bool {
    (MIN_YEAR..=now_year + 1).contains(&dt.year)
}

/// Port of `gxr_studio/meta.py:41 parse_exif_datetime`.
pub fn parse_exif_datetime(value: Option<&ExifValue>, now_year: i32) -> Option<NaiveDateTime> {
    let text = clean_text(value)?;
    if text.starts_with("0000") || !has_four_digits(&text) {
        return None;
    }
    let candidate: String = text.chars().take(19).collect();
    for kind in [
        DateFmt::YmdHms(':'),
        DateFmt::YmdHms('-'),
        DateFmt::YmdHm(':'),
        DateFmt::YmdHms('/'),
    ] {
        if let Some(dt) = parse_strptime_like(&candidate, kind) {
            return plausible(&dt, now_year).then_some(dt);
        }
    }
    None
}

fn has_four_digits(s: &str) -> bool {
    let mut run = 0usize;
    for b in s.bytes() {
        if b.is_ascii_digit() {
            run += 1;
            if run >= 4 {
                return true;
            }
        } else {
            run = 0;
        }
    }
    false
}

#[derive(Clone, Copy)]
enum DateFmt {
    YmdHms(char),
    YmdHm(char),
}

fn parse_strptime_like(s: &str, fmt: DateFmt) -> Option<NaiveDateTime> {
    let sep = match fmt {
        DateFmt::YmdHms(c) | DateFmt::YmdHm(c) => c,
    };
    let (date_part, time_part) = s.split_once(' ')?;
    let mut d_parts = date_part.split(sep);
    let y_str = d_parts.next()?;
    let m_str = d_parts.next()?;
    let day_str = d_parts.next()?;
    if d_parts.next().is_some()
        || y_str.len() != 4
        || !y_str.bytes().all(|b| b.is_ascii_digit())
        || !(1..=2).contains(&m_str.len())
        || !m_str.bytes().all(|b| b.is_ascii_digit())
        || !(1..=2).contains(&day_str.len())
        || !day_str.bytes().all(|b| b.is_ascii_digit())
    {
        return None;
    }
    let year: i32 = y_str.parse().ok()?;
    let month: u32 = m_str.parse().ok()?;
    let day: u32 = day_str.parse().ok()?;

    let mut t_parts = time_part.split(':');
    let h_str = t_parts.next()?;
    let min_str = t_parts.next()?;
    let (hour, minute, second) = match fmt {
        DateFmt::YmdHms(_) => {
            let sec_str = t_parts.next()?;
            if t_parts.next().is_some()
                || !(1..=2).contains(&h_str.len())
                || !h_str.bytes().all(|b| b.is_ascii_digit())
                || !(1..=2).contains(&min_str.len())
                || !min_str.bytes().all(|b| b.is_ascii_digit())
                || !(1..=2).contains(&sec_str.len())
                || !sec_str.bytes().all(|b| b.is_ascii_digit())
            {
                return None;
            }
            (
                h_str.parse().ok()?,
                min_str.parse().ok()?,
                sec_str.parse().ok()?,
            )
        }
        DateFmt::YmdHm(_) => {
            if t_parts.next().is_some()
                || !(1..=2).contains(&h_str.len())
                || !h_str.bytes().all(|b| b.is_ascii_digit())
                || !(1..=2).contains(&min_str.len())
                || !min_str.bytes().all(|b| b.is_ascii_digit())
            {
                return None;
            }
            (h_str.parse().ok()?, min_str.parse().ok()?, 0u32)
        }
    };

    validate_date_time(year, month, day, hour, minute, second)
}

pub(crate) fn is_leap_year(year: i32) -> bool {
    (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0)
}

pub(crate) fn days_in_month(year: i32, month: u32) -> u32 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if is_leap_year(year) {
                29
            } else {
                28
            }
        }
        _ => 0,
    }
}

fn validate_date_time(
    year: i32,
    month: u32,
    day: u32,
    hour: u32,
    minute: u32,
    second: u32,
) -> Option<NaiveDateTime> {
    if !(1..=9999).contains(&year) || !(1..=12).contains(&month) {
        return None;
    }
    let max_day = days_in_month(year, month);
    if !(1..=max_day).contains(&day) || hour > 23 || minute > 59 || second > 59 {
        return None;
    }
    Some(NaiveDateTime {
        year,
        month,
        day,
        hour,
        minute,
        second,
        microsecond: 0,
    })
}

/// Port of `gxr_studio/meta.py:72 _clean`.
pub fn clean_meta_str(value: Option<&ExifValue>) -> Option<String> {
    clean_text(value)
}

/// Port of `gxr_studio/meta.py:81 _iso`.
fn parse_iso(value: Option<&ExifValue>) -> Result<Option<i64>, &'static str> {
    let Some(val) = value else {
        return Ok(None);
    };
    let first_owned: ExifValue;
    let target = match val {
        ExifValue::Ints(v) => match v.first() {
            Some(&x) => {
                first_owned = ExifValue::Int(x);
                &first_owned
            }
            None => return Ok(None),
        },
        ExifValue::Floats(v) => match v.first() {
            Some(&x) => {
                first_owned = ExifValue::Float(x);
                &first_owned
            }
            None => return Ok(None),
        },
        ExifValue::Rationals(v) => match v.first() {
            Some(&(n, d)) => {
                first_owned = ExifValue::Rational(n, d);
                &first_owned
            }
            None => return Ok(None),
        },
        other => other,
    };
    target.to_python_int()
}

/// Port of `gxr_studio/meta.py:90 _oriented`.
pub fn oriented(w: u32, h: u32, orientation: i64) -> (u32, u32) {
    if matches!(orientation, 5..=8) {
        (h, w)
    } else {
        (w, h)
    }
}

/// Port of `gxr_studio/meta.py:94 _orientation`.
pub fn parse_orientation(value: Option<&ExifValue>) -> i64 {
    let Some(val) = value else {
        return 1;
    };
    match val.to_python_int() {
        Ok(Some(v)) if (1..=8).contains(&v) => v,
        _ => 1,
    }
}

/// Port of `gxr_studio/camdata/recipe_tags.py:31 copyright_text`.
pub fn extract_copyright_text(value: Option<&ExifValue>) -> Option<String> {
    match value? {
        ExifValue::Str(s) => Some(s.strip_suffix('\0').unwrap_or(s).to_string()),
        ExifValue::Bytes(b) => {
            let s: String = b.iter().map(|&byte| char::from(byte)).collect();
            Some(s.strip_suffix('\0').unwrap_or(&s).to_string())
        }
        _ => None,
    }
}

/// Port of `gxr_studio/camdata/recipe_tags.py:38 parse_recipe_tag`.
pub fn extract_recipe_tag(value: Option<&ExifValue>) -> Option<RecipeTag> {
    match value? {
        ExifValue::Str(s) => parse_recipe_tag(s),
        ExifValue::Bytes(b) => parse_recipe_tag_bytes(b),
        _ => None,
    }
}

fn strip_ricoh_ws_prefix(s: &str) -> String {
    if let Some(rest) = s.strip_prefix("RICOH") {
        let trimmed = rest.trim_start();
        if trimmed.len() < rest.len() {
            return trimmed.to_string();
        }
    }
    s.to_string()
}

/// Port of `gxr_studio/meta.py:102 read_jpeg_meta`.
pub fn read_jpeg_meta(bytes: &[u8], now_year: i32) -> PhotoMeta {
    let jpg = match parse_jpeg(bytes) {
        Ok(j) => j,
        Err(msg) => {
            return PhotoMeta::from_error(format!(
                "UnidentifiedImageError: cannot identify image file ({msg})"
            ));
        }
    };
    let exif = load_image_exif(bytes, &ImageDoc::Jpeg(jpg.clone()));
    let orientation = parse_orientation(exif.base.get(&274));
    let (w, h) = oriented(jpg.width, jpg.height, orientation);
    let taken = parse_exif_datetime(exif.sub.get(&36867), now_year)
        .or_else(|| parse_exif_datetime(exif.base.get(&306), now_year));
    let iso = match parse_iso(exif.sub.get(&34855)) {
        Ok(v) => v,
        Err(msg) => return PhotoMeta::from_error(msg),
    };
    let copy_val = exif.base.get(&33432);
    PhotoMeta {
        width: Some(w),
        height: Some(h),
        orientation,
        taken,
        iso,
        lens: clean_meta_str(exif.sub.get(&42036)),
        model: clean_meta_str(exif.base.get(&272)),
        error: None,
        copyright: extract_copyright_text(copy_val),
        recipe_tag: extract_recipe_tag(copy_val),
    }
}

fn extract_crop_pair(val: Option<&ExifValue>) -> Result<Option<(u32, u32)>, &'static str> {
    let Some(v) = val else {
        return Ok(None);
    };
    let pair = match v {
        ExifValue::Ints(xs) if xs.len() == 2 => (Some(xs[0]), Some(xs[1])),
        ExifValue::Rationals(rs) if rs.len() == 2 => (
            ExifValue::Rational(rs[0].0, rs[0].1).to_python_int()?,
            ExifValue::Rational(rs[1].0, rs[1].1).to_python_int()?,
        ),
        ExifValue::Floats(fs) if fs.len() == 2 => (
            ExifValue::Float(fs[0]).to_python_int()?,
            ExifValue::Float(fs[1]).to_python_int()?,
        ),
        _ => return Ok(None),
    };
    let (Some(w), Some(h)) = pair else {
        return Ok(None);
    };
    Ok(u32::try_from(w).ok().zip(u32::try_from(h).ok()))
}

/// Port of `gxr_studio/meta.py:138 read_dng_meta`.
pub fn read_dng_meta(bytes: &[u8], now_year: i32) -> PhotoMeta {
    let tiff = match parse_tiff(bytes) {
        Ok(t) => t,
        Err(msg) => {
            return PhotoMeta::from_error(format!(
                "UnidentifiedImageError: cannot identify image file ({msg})"
            ));
        }
    };
    let tags = &tiff.tag_v2.tags;
    let orientation = parse_orientation(tags.get(&274));
    let mut width = tiff.width;
    let mut height = tiff.height;

    if let Some(sub_offs) = tags.get(&330).and_then(ExifValue::as_ints) {
        for off in sub_offs {
            let Ok(off_u64) = u64::try_from(off) else {
                continue;
            };
            let Ok(ifd) = load_ifd(bytes, tiff.endian, tiff.bigtiff, off_u64, None, false) else {
                continue;
            };
            let new_subfile_ok = match ifd.tags.get(&254) {
                None => true,
                Some(ExifValue::Int(0)) => true,
                Some(ExifValue::Float(f)) if *f == 0.0 => true,
                _ => false,
            };
            let has_w = ifd.tags.get(&256).is_some_and(ExifValue::is_truthy);
            let has_h = ifd.tags.get(&257).is_some_and(ExifValue::is_truthy);
            if new_subfile_ok && has_w && has_h {
                match extract_crop_pair(ifd.tags.get(&50720)) {
                    Ok(Some((cw, ch))) => {
                        width = cw;
                        height = ch;
                    }
                    Ok(None) => {
                        let iw = ifd
                            .tags
                            .get(&256)
                            .map_or(Ok(None), ExifValue::to_python_int);
                        let ih = ifd
                            .tags
                            .get(&257)
                            .map_or(Ok(None), ExifValue::to_python_int);
                        match (iw, ih) {
                            (Ok(Some(w)), Ok(Some(h))) => {
                                if let (Ok(uw), Ok(uh)) = (u32::try_from(w), u32::try_from(h)) {
                                    width = uw;
                                    height = uh;
                                }
                            }
                            (Err(msg), _) | (_, Err(msg)) => return PhotoMeta::from_error(msg),
                            _ => {}
                        }
                    }
                    Err(msg) => return PhotoMeta::from_error(msg),
                }
                break;
            }
        }
    }

    let exif = load_image_exif(bytes, &ImageDoc::Tiff(tiff.clone()));
    let _ = EXIF_IFD_TAG;
    let (w, h) = oriented(width, height, orientation);
    let taken = parse_exif_datetime(exif.sub.get(&36867), now_year)
        .or_else(|| parse_exif_datetime(tags.get(&306), now_year));
    let lens = clean_meta_str(exif.sub.get(&42036))
        .or_else(|| clean_meta_str(tags.get(&50708)).map(|unique| strip_ricoh_ws_prefix(&unique)));
    let iso = match parse_iso(exif.sub.get(&34855)) {
        Ok(v) => v,
        Err(msg) => return PhotoMeta::from_error(msg),
    };
    let copy_val = tags.get(&33432);
    PhotoMeta {
        width: Some(w),
        height: Some(h),
        orientation,
        taken,
        iso,
        lens,
        model: clean_meta_str(tags.get(&272)),
        error: None,
        copyright: extract_copyright_text(copy_val),
        recipe_tag: extract_recipe_tag(copy_val),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn datetime_plausibility_and_formats() {
        assert!(!plausible(
            &NaiveDateTime::new(1994, 12, 31, 23, 59, 59),
            2026
        ));
        assert!(plausible(&NaiveDateTime::new(1995, 1, 1, 0, 0, 0), 2026));
        assert!(plausible(
            &NaiveDateTime::new(2027, 12, 31, 23, 59, 59),
            2026
        ));
        assert!(!plausible(&NaiveDateTime::new(2028, 1, 1, 0, 0, 0), 2026));

        for raw in [
            "2024:02:29 12:30:45",
            "2024-02-29 12:30:45",
            "2024:02:29 12:30",
            "2024/02/29 12:30:45",
        ] {
            let parsed = parse_exif_datetime(Some(&ExifValue::Str(raw.to_string())), 2026);
            assert!(parsed.is_some(), "failed on {raw}");
        }
        assert_eq!(
            parse_exif_datetime(
                Some(&ExifValue::Str("2024-02-29T12:30:45".to_string())),
                2026
            ),
            None
        );
        assert_eq!(
            parse_exif_datetime(
                Some(&ExifValue::Str("0000:00:00 00:00:00".to_string())),
                2026
            ),
            None
        );
        assert_eq!(
            parse_exif_datetime(
                Some(&ExifValue::Str("1994:05:01 10:00:00".to_string())),
                2026
            ),
            None
        );
    }

    #[test]
    fn oriented_swaps_for_rotations_5_to_8() {
        for o in [1, 2, 3, 4] {
            assert_eq!(oriented(4000, 3000, o), (4000, 3000));
        }
        for o in [5, 6, 7, 8] {
            assert_eq!(oriented(4000, 3000, o), (3000, 4000));
        }
    }
}
