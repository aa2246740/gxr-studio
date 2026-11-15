//! The byte-and-JSON entry points `gxr-cli` and `gxr-wasm` share, so both run the same code on
//! the same inputs. Sizes and counts arrive as `f64` (WASM numbers) and are checked here before
//! anything is narrowed or copied.

use gxr_frame::{Fonts, Val, parse_json};
use gxr_raw::{CoreError, Json};

use super::complete::{Completed, State, checked_rgb, complete_sdr};
use super::encode::Rgb;
use super::prepare::{
    ExportOpts, LayoutFacts, Picked, ShotFacts, parse_finish_edit, plan, prepare_original,
    prepare_picked,
};
use super::result::{ExportFail, Labels};
use super::sizes::output_sizes;

/// A JS number that must be a whole, finite, non-negative size (`u64`).
pub fn dim_from_f64(v: f64, what: &str) -> Result<u64, CoreError> {
    if !v.is_finite() || v.fract() != 0.0 || !(0.0..=9_007_199_254_740_991.0).contains(&v) {
        return Err(CoreError::new("bad_image", format!("{what} 要是非负整数")));
    }
    Ok(v as u64)
}

/// A picture handed in by the caller: bytes plus its claimed size.
pub struct Picture<'a> {
    pub data: &'a [u8],
    pub width: f64,
    pub height: f64,
}

impl Picture<'_> {
    fn to_rgb(&self) -> Result<Rgb, CoreError> {
        let w = dim_from_f64(self.width, "宽")?;
        let h = dim_from_f64(self.height, "高")?;
        let (wu, hu) = checked_rgb(self.data, w, h)?;
        let mut data = Vec::new();
        data.try_reserve_exact(self.data.len())
            .map_err(|_| CoreError::new("too_big", "图像太大"))?;
        data.extend_from_slice(self.data);
        Ok(Rgb {
            width: wu,
            height: hu,
            data,
        })
    }
}

/// Everything `export_prepare` reads besides the pictures.
pub struct PrepareArgs<'a> {
    pub shot: &'a [u8],
    pub side: &'a [u8],
    pub defaults: &'a [u8],
    pub layout: &'a [u8],
    pub base: &'a str,
    pub labels: &'a [u8],
    pub from_raw: bool,
    pub opts: ExportOpts,
}

/// `export_prepare` for a chosen base picture (`picked`, with `from_raw` and the labels
/// `color_image` gave) or the untouched camera JPG (`jpg`). Returns the canvas, the prepared
/// JSON, and whether the untouched camera JPG branch was taken.
pub fn prepare(
    args: &PrepareArgs<'_>,
    picked: Option<Picture<'_>>,
    jpg: Option<Picture<'_>>,
    fonts: &Fonts,
) -> Result<(Rgb, Json, bool), ExportFail> {
    let shot = ShotFacts::parse(&parse_json(args.shot)?)?;
    let (plan, side) = plan(&shot, args.side, args.defaults, args.base)?;
    if plan.original {
        let jpg = jpg.ok_or_else(|| CoreError::new("bad_request", "缺相机 JPG"))?;
        let p = prepare_original(&shot, jpg.to_rgb()?, args.opts)?;
        return Ok((p.canvas, p.json, true));
    }
    let picked = picked.ok_or_else(|| CoreError::new("bad_request", "缺底图"))?;
    let labels = if args.labels.iter().all(u8::is_ascii_whitespace) {
        Labels::default()
    } else {
        Labels::from_val(&parse_json(args.labels)?)?
    };
    let layout = LayoutFacts::parse(args.layout)?;
    let picked = Picked {
        rgb: picked.to_rgb()?,
        from_raw: args.from_raw,
        labels,
    };
    let p = prepare_picked(&shot, &plan, &side, picked, &layout, fonts, args.opts)?;
    Ok((p.canvas, p.json, false))
}

/// `export_complete` without HDR: the inked canvas, the state JSON from `prepare` and the source
/// EXIF as gxr-jpeg JSON (`base` / `exif`, without 270).
pub fn complete(
    canvas: Picture<'_>,
    state: &[u8],
    exif_json: &str,
) -> Result<Completed, ExportFail> {
    let w = dim_from_f64(canvas.width, "宽")?;
    let h = dim_from_f64(canvas.height, "高")?;
    checked_rgb(canvas.data, w, h)?;
    let state = State::parse(state)?;
    let source = gxr_jpeg::parse_exif_json(exif_json)?;
    let (done, _rgb) = complete_sdr(canvas.data, w, h, &state, &source)?;
    Ok(done)
}

fn parse_rect(text: &[u8]) -> Result<Option<[f64; 4]>, CoreError> {
    if text.iter().all(u8::is_ascii_whitespace) {
        return Ok(None);
    }
    let bad = || CoreError::new("bad_rect", "裁剪框格式不对");
    match parse_json(text)? {
        Val::Null => Ok(None),
        Val::Arr(items) if items.len() == 4 => {
            let mut r = [0.0_f64; 4];
            for (slot, it) in r.iter_mut().zip(items.iter()) {
                *slot = match it {
                    Val::Int(n) => *n as f64,
                    Val::Float(f) => *f,
                    _ => return Err(bad()),
                };
            }
            Ok(Some(r))
        }
        _ => Err(bad()),
    }
}

/// Inputs of `output_sizes`.
pub struct SizesArgs<'a> {
    pub width: f64,
    pub height: f64,
    pub side: &'a [u8],
    pub defaults: &'a [u8],
    pub layout: &'a [u8],
    pub rect: &'a [u8],
    pub preset: Option<&'a str>,
}

/// `output_sizes` for a source of `width × height`.
pub fn sizes(args: &SizesArgs<'_>, fonts: &Fonts) -> Result<Json, ExportFail> {
    let w = dim_from_f64(args.width, "宽")?;
    let h = dim_from_f64(args.height, "高")?;
    let side_text: &[u8] = if args.side.iter().all(u8::is_ascii_whitespace) {
        b"{}"
    } else {
        args.side
    };
    let side = parse_json(side_text)?;
    if side.as_object().is_none() {
        return Err(CoreError::new("bad_request", "请求无效：sidecar 要是对象").into());
    }
    let edit = parse_finish_edit(side_text, &side)?;
    let rect = parse_rect(args.rect)?;
    let layout = LayoutFacts::parse(args.layout)?;
    output_sizes(
        w,
        h,
        &edit,
        args.defaults,
        rect,
        args.preset,
        &layout,
        fonts,
    )
}
