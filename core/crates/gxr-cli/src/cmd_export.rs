//! `export-meta-*` commands. Owner: wave-2 unit export-meta.

use gxr_export::meta::{
    DEFAULT_NOW_YEAR, dng_preview_location, photo_meta_from_bytes, read_dng_meta, read_jpeg_meta,
    read_source_exif,
};
use gxr_raw::Json;

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    let json = match command {
        "export-meta-exif" => cmd_exif(args)?,
        "export-meta-photo" => cmd_photo(args)?,
        "export-meta-read" => cmd_read(args)?,
        "export-meta-preview" => cmd_preview(args)?,
        _ => return Err(Failure::Usage(format!("unknown command {command:?}"))),
    };
    Ok(Output {
        text: json.render(),
        ok: true,
    })
}

fn opt_option<'a>(args: &'a [String], name: &str) -> Result<Option<&'a str>, Failure> {
    let Some(at) = args.iter().position(|a| a == name) else {
        return Ok(None);
    };
    args.get(at + 1)
        .map(|s| Some(s.as_str()))
        .ok_or_else(|| Failure::Usage(format!("{name} needs a value")))
}

fn parse_now_year(args: &[String]) -> Result<i32, Failure> {
    match opt_option(args, "--now-year")? {
        Some(s) => s
            .parse::<i32>()
            .map_err(|_| Failure::Usage(format!("bad --now-year {s:?}"))),
        None => Ok(DEFAULT_NOW_YEAR),
    }
}

fn cmd_exif(args: &[String]) -> Result<Json, Failure> {
    let src = common::option(args, "--src")?;
    let bytes = common::read(src)?;
    Ok(read_source_exif(&bytes).to_json())
}

fn cmd_photo(args: &[String]) -> Result<Json, Failure> {
    let jpg = match opt_option(args, "--jpg")? {
        Some(p) => Some(common::read(p)?),
        None => None,
    };
    let dng = match opt_option(args, "--dng")? {
        Some(p) => Some(common::read(p)?),
        None => None,
    };
    let name = opt_option(args, "--name")?.unwrap_or("");
    let mtime_ms = match opt_option(args, "--mtime-ms")? {
        Some(s) => Some(
            s.parse::<f64>()
                .map_err(|_| Failure::Usage(format!("bad --mtime-ms {s:?}")))?,
        ),
        None => None,
    };
    let tz_offset_min = match opt_option(args, "--tz-offset-min")? {
        Some(s) => s
            .parse::<i32>()
            .map_err(|_| Failure::Usage(format!("bad --tz-offset-min {s:?}")))?,
        None => 0,
    };
    let now_year = parse_now_year(args)?;
    let res = photo_meta_from_bytes(
        jpg.as_deref(),
        dng.as_deref(),
        name,
        mtime_ms,
        tz_offset_min,
        now_year,
    )?;
    Ok(res.to_json())
}

fn cmd_read(args: &[String]) -> Result<Json, Failure> {
    let path = common::option(args, "--file")?;
    let bytes = common::read(path)?;
    let now_year = parse_now_year(args)?;
    let is_dng = match opt_option(args, "--kind")? {
        Some("dng" | "tiff") => true,
        Some("jpg" | "jpeg") => false,
        Some(other) => return Err(Failure::Usage(format!("bad --kind {other:?}"))),
        None => is_dng_path(path),
    };
    let meta = if is_dng {
        read_dng_meta(&bytes, now_year)
    } else {
        read_jpeg_meta(&bytes, now_year)
    };
    Ok(meta.to_json())
}

fn is_dng_path(path: &str) -> bool {
    let lower = path.to_ascii_lowercase();
    lower.ends_with(".dng") || lower.ends_with(".tif") || lower.ends_with(".tiff")
}

fn cmd_preview(args: &[String]) -> Result<Json, Failure> {
    let dng_path = common::option(args, "--dng")?;
    let bytes = common::read(dng_path)?;
    Ok(dng_preview_location(&bytes).map_or(Json::Null, |r| r.to_json()))
}
