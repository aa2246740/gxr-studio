//! `frame-*` and `stamp-*` commands. Owner: wave-1 unit frame.

use gxr_frame::read_font;

use crate::common::{option, read, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    match command {
        "frame-sfnt" => frame_sfnt(args),
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
