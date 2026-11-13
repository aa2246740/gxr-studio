//! `frame-*` and `stamp-*` commands. Owner: wave-1 unit frame.

use gxr_frame::{parse_frame, parse_json, parse_stamp, read_font, stored_frame};
use gxr_raw::Json;

use crate::common::{option, read, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    match command {
        "frame-sfnt" => frame_sfnt(args),
        "frame-spec" => frame_spec(args),
        "frame-stored" => frame_stored(args),
        "stamp-spec" => stamp_spec(args),
        _ => Err(Failure::Usage(format!("unknown command {command:?}"))),
    }
}

fn frame_sfnt(args: &[String]) -> Result<Output, Failure> {
    let path = option(args, "--font")?;
    let chars = option(args, "--chars")?;
    let bytes = read(path)?;
    let table = read_font(&bytes)?;
    Ok(Output {
        text: table.metrics_json(chars).render(),
        ok: true,
    })
}

fn frame_spec(args: &[String]) -> Result<Output, Failure> {
    let value = spec_value(args)?;
    let text = match parse_frame(&value)? {
        Some(spec) => spec.to_json().render(),
        None => Json::Null.render(),
    };
    Ok(Output { text, ok: true })
}

fn frame_stored(args: &[String]) -> Result<Output, Failure> {
    let value = spec_value(args)?;
    let text = match stored_frame(&value)? {
        Some(spec) => spec.to_json().render(),
        None => Json::Null.render(),
    };
    Ok(Output { text, ok: true })
}

fn stamp_spec(args: &[String]) -> Result<Output, Failure> {
    let value = spec_value(args)?;
    let text = match parse_stamp(&value)? {
        Some(spec) => spec.to_json().render(),
        None => Json::Null.render(),
    };
    Ok(Output { text, ok: true })
}

fn spec_value(args: &[String]) -> Result<gxr_frame::Val, Failure> {
    let path = option(args, "--spec")?;
    let bytes = read(path)?;
    Ok(parse_json(&bytes)?)
}
