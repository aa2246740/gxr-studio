//! Native parity tool for the GXR core (docs/mobile.md). Python tests run it on a file and
//! compare its output with the Python function on the same input.
//!
//! Output is JSON on stdout. Array subcommands also write a `.npy` file and print its shape.
//! A core error prints `{"error": {"kind", "message"}}` and exits 2; a usage error exits 1.
//!
//! `main` routes by command prefix. Each area implements `cmd_<area>::run`.
#![forbid(unsafe_code)]

mod cmd_camdata;
mod cmd_color;
mod cmd_export;
mod cmd_frame;
mod cmd_geometry;
mod cmd_hdr;
mod cmd_jpeg;
mod cmd_lut;
mod cmd_raw;
mod cmd_source;
mod common;
mod npy;

use std::process::ExitCode;

use gxr_raw::{Json, error_json};

use common::{Failure, Output};

const USAGE: &str = "usage: gxr-cli <command> [args]
  version
  dng-describe <path>
  dng-summary <path>
  dng-mosaic <path> --out <file.npy>
  dng-crop-mosaic <path> --out <file.npy>
  dng-half-codes <path> --out <file.npy>
  dng-normalised-block <path> --rows Y0:Y1 --cols X0:X1 --out <file.npy>
  dng-wb-block <path> --rows Y0:Y1 --out <file.npy>";

fn dispatch(command: &str, rest: &[String]) -> Result<Output, Failure> {
    if command == "version" {
        let json = Json::object([("version", Json::str(env!("CARGO_PKG_VERSION")))]);
        return Ok(Output {
            text: json.render(),
            ok: true,
        });
    }
    if command.starts_with("dng-") {
        return cmd_raw::run(command, rest);
    }
    if command.starts_with("camdata-") {
        return cmd_camdata::run(command, rest);
    }
    if command.starts_with("color-") {
        return cmd_color::run(command, rest);
    }
    if command.starts_with("lut-") || command.starts_with("scene-") {
        return cmd_lut::run(command, rest);
    }
    if command.starts_with("hdr-") {
        return cmd_hdr::run(command, rest);
    }
    if command.starts_with("geom-") {
        return cmd_geometry::run(command, rest);
    }
    if command.starts_with("frame-") || command.starts_with("stamp-") {
        return cmd_frame::run(command, rest);
    }
    if command.starts_with("jpeg-") {
        return cmd_jpeg::run(command, rest);
    }
    if command.starts_with("export-") {
        return cmd_export::run(command, rest);
    }
    if command.starts_with("source-") {
        return cmd_source::run(command, rest);
    }
    // Commands that are not an area prefix keep the previous usage text.
    if rest.is_empty() {
        return Err(Failure::Usage("missing <path>".into()));
    }
    Err(Failure::Usage(format!("unknown command {command:?}")))
}

fn run(args: &[String]) -> Result<Output, Failure> {
    let command = args.first().map(String::as_str).unwrap_or("");
    let rest = if args.is_empty() { &[] } else { &args[1..] };
    dispatch(command, rest)
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(output) => {
            println!("{}", output.text);
            if output.ok {
                ExitCode::SUCCESS
            } else {
                ExitCode::from(2)
            }
        }
        Err(Failure::Core(e)) => {
            println!("{}", error_json(&e).render());
            ExitCode::from(2)
        }
        Err(Failure::Usage(message)) => {
            eprintln!("gxr-cli: {message}\n{USAGE}");
            ExitCode::from(1)
        }
    }
}
