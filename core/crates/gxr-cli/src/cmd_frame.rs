//! `frame-*` and `stamp-*` commands. Owner: wave-1 unit frame.
//!
//! Fonts are `--font NAME=PATH`, one per face. `NAME` is a `FONT_FILES` key (`atkinson`,
//! `inter`, `inter_sb`, `courier`, `patrick`, `dseg`) or `cjk` for the CJK fallback face.

use gxr_frame::{
    CjkFont, Fonts, Val, batch_json, frame_json, parse_frame, parse_json, parse_stamp, read_font,
    stamp_json, stored_frame,
};
use gxr_raw::Json;

use crate::common::{Failure, Output, option, read};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    match command {
        "frame-sfnt" => frame_sfnt(args),
        "frame-cjk" => frame_cjk(args),
        "frame-spec" => frame_spec(args),
        "frame-stored" => frame_stored(args),
        "stamp-spec" => stamp_spec(args),
        "frame-layout" => layout(args, false),
        "stamp-layout" => layout(args, true),
        "frame-batch" => batch(args),
        _ => Err(Failure::Usage(format!("unknown command {command:?}"))),
    }
}

fn ok(text: String) -> Result<Output, Failure> {
    Ok(Output { text, ok: true })
}

fn frame_sfnt(args: &[String]) -> Result<Output, Failure> {
    let bytes = read(option(args, "--font")?)?;
    let table = read_font(&bytes)?;
    ok(table.metrics_json(option(args, "--chars")?).render())
}

/// What `FontMetrics` reports for CJK text from the fallback face: per character
/// `[advance, box | null]` at 1000 px, and the ascent at each `--px`.
fn frame_cjk(args: &[String]) -> Result<Output, Failure> {
    let face = CjkFont::parse(&read(option(args, "--font")?)?)?;
    let mut glyphs = Vec::new();
    for ch in option(args, "--chars")?.chars() {
        let (adv, b) = face.glyph(ch)?;
        glyphs.push(Json::Array(vec![
            Json::Int(adv),
            b.map_or(Json::Null, |b| Json::ints(&b)),
        ]));
    }
    let mut ascents = Vec::new();
    for px in option(args, "--px")?.split(',') {
        let px: i64 = px
            .parse()
            .map_err(|_| Failure::Usage(format!("bad --px value {px:?}")))?;
        if !(1..=1 << 20).contains(&px) {
            return Err(Failure::Usage(format!("--px out of range: {px}")));
        }
        ascents.push(Json::Int(face.ascent(px)));
    }
    ok(Json::object([
        ("glyphs", Json::Array(glyphs)),
        ("ascents", Json::Array(ascents)),
    ])
    .render())
}

fn spec_value(args: &[String]) -> Result<Val, Failure> {
    json_file(option(args, "--spec")?)
}

fn json_file(path: &str) -> Result<Val, Failure> {
    Ok(parse_json(&read(path)?)?)
}

fn frame_spec(args: &[String]) -> Result<Output, Failure> {
    let parsed = parse_frame(&spec_value(args)?)?;
    ok(parsed.map_or(Json::Null, |s| s.to_json()).render())
}

fn frame_stored(args: &[String]) -> Result<Output, Failure> {
    let parsed = stored_frame(&spec_value(args)?)?;
    ok(parsed.map_or(Json::Null, |s| s.to_json()).render())
}

fn stamp_spec(args: &[String]) -> Result<Output, Failure> {
    let parsed = parse_stamp(&spec_value(args)?)?;
    ok(parsed.map_or(Json::Null, |s| s.to_json()).render())
}

/// Every `--font NAME=PATH`.
fn fonts(args: &[String]) -> Result<Fonts, Failure> {
    let mut fonts = Fonts::new();
    let mut i = 0;
    while i < args.len() {
        if args[i] == "--font" {
            let spec = args
                .get(i + 1)
                .ok_or_else(|| Failure::Usage("--font needs a value".into()))?;
            let (name, path) = spec
                .split_once('=')
                .ok_or_else(|| Failure::Usage(format!("--font wants NAME=PATH, got {spec:?}")))?;
            fonts.add(name, &read(path)?)?;
            i += 2;
        } else {
            i += 1;
        }
    }
    Ok(fonts)
}

fn layout(args: &[String], stamp: bool) -> Result<Output, Failure> {
    let request = json_file(option(args, "--meta")?)?;
    let spec = spec_value(args)?;
    let fonts = fonts(args)?;
    let out = if stamp {
        stamp_json(&request, &spec, &fonts)?
    } else {
        frame_json(&request, &spec, &fonts)?
    };
    ok(out.render())
}

fn batch(args: &[String]) -> Result<Output, Failure> {
    let cases = json_file(option(args, "--cases")?)?;
    let fonts = fonts(args)?;
    ok(batch_json(&cases, &fonts)?.render())
}
