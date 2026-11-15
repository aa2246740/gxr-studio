//! `export-meta-*` commands. Owner: wave-2 unit export-meta.

use gxr_export::meta::{
    DEFAULT_NOW_YEAR, dng_preview_location, photo_meta_from_bytes, read_dng_meta, read_jpeg_meta,
    read_source_exif,
};
use gxr_raw::Json;

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    if command == "export-meta-read" {
        return Ok(Output {
            text: cmd_read(args)?,
            ok: true,
        });
    }
    if command == "export-meta-batch" {
        return Ok(Output {
            text: cmd_batch(args)?,
            ok: true,
        });
    }
    let json = match command {
        "export-meta-exif" => cmd_exif(args)?,
        "export-meta-photo" => cmd_photo(args)?,
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
            .parse::<f64>()
            .map_err(|_| Failure::Usage(format!("bad --tz-offset-min {s:?}")))?,
        None => 0.0,
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

fn cmd_read(args: &[String]) -> Result<String, Failure> {
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
    Ok(meta.to_json_string())
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

fn open_json(bytes: &[u8]) -> Json {
    match gxr_export::meta::tiff_exif::open_image(bytes) {
        Ok(gxr_export::meta::tiff_exif::ImageDoc::Jpeg(j)) => Json::object([
            ("ok", Json::Bool(true)),
            ("width", Json::Int(i64::from(j.width))),
            ("height", Json::Int(i64::from(j.height))),
        ]),
        Ok(gxr_export::meta::tiff_exif::ImageDoc::Tiff(t)) => Json::object([
            ("ok", Json::Bool(true)),
            ("width", Json::Int(i64::from(t.width))),
            ("height", Json::Int(i64::from(t.height))),
        ]),
        Err(msg) => {
            let (kind, detail) = msg.split_once(':').unwrap_or(("Error", msg.as_str()));
            Json::object([
                ("ok", Json::Bool(false)),
                ("kind", Json::str(kind)),
                ("message", Json::str(detail.trim_start())),
            ])
        }
    }
}

fn cmd_batch(args: &[String]) -> Result<String, Failure> {
    let manifest_path = common::option(args, "--manifest")?;
    let manifest = std::fs::read_to_string(manifest_path)
        .map_err(|e| Failure::Usage(format!("cannot read {manifest_path}: {e}")))?;
    let mtime_ms = match opt_option(args, "--mtime-ms")? {
        Some(s) => Some(
            s.parse::<f64>()
                .map_err(|_| Failure::Usage(format!("bad --mtime-ms {s:?}")))?,
        ),
        None => None,
    };
    let tz_offset_min = match opt_option(args, "--tz-offset-min")? {
        Some(s) => s
            .parse::<f64>()
            .map_err(|_| Failure::Usage(format!("bad --tz-offset-min {s:?}")))?,
        None => 0.0,
    };
    let now_year = parse_now_year(args)?;

    let mut lines = Vec::new();
    for raw_line in manifest.lines() {
        let trimmed = raw_line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let (path, name) = trimmed.split_once('\t').unwrap_or((trimmed, ""));
        let bytes = common::read(path)?;
        let open = open_json(&bytes).render();
        let read = read_jpeg_meta(&bytes, now_year).to_json_string();
        let photo = match photo_meta_from_bytes(
            Some(&bytes),
            None,
            name,
            mtime_ms,
            tz_offset_min,
            now_year,
        ) {
            Ok(res) => Json::object([("ok", res.to_json())]).render(),
            Err(e) => gxr_raw::error_json(&e).render(),
        };
        let exif = read_source_exif(&bytes).to_json().render();
        lines.push(format!(
            "{{\"open\":{open},\"read\":{read},\"photo\":{photo},\"exif\":{exif}}}"
        ));
    }
    Ok(lines.join("\n"))
}
