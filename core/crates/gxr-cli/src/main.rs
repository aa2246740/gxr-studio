//! Native parity tool for the GXR core (docs/mobile.md). Python tests run it on a file and
//! compare its output with the Python function on the same input.
//!
//! Output is JSON on stdout. Array subcommands also write a `.npy` file and print its shape.
//! A core error prints `{"error": {"kind", "message"}}` and exits 2; a usage error exits 1.
#![forbid(unsafe_code)]

mod npy;

use std::process::ExitCode;

use gxr_raw::{CoreError, Dng, Json, error_json};

const USAGE: &str = "usage: gxr-cli <command> [args]
  version
  dng-describe <path>
  dng-summary <path>
  dng-mosaic <path> --out <file.npy>
  dng-crop-mosaic <path> --out <file.npy>
  dng-half-codes <path> --out <file.npy>
  dng-normalised-block <path> --rows Y0:Y1 --cols X0:X1 --out <file.npy>
  dng-wb-block <path> --rows Y0:Y1 --out <file.npy>";

/// A failure: a usage problem (exit 1) or a core error (exit 2).
enum Failure {
    Usage(String),
    Core(CoreError),
}

impl From<CoreError> for Failure {
    fn from(e: CoreError) -> Self {
        Self::Core(e)
    }
}

/// `--name value` from the arguments after the path.
fn option<'a>(args: &'a [String], name: &str) -> Result<&'a str, Failure> {
    let at = args
        .iter()
        .position(|a| a == name)
        .ok_or_else(|| Failure::Usage(format!("missing {name}")))?;
    args.get(at + 1)
        .map(String::as_str)
        .ok_or_else(|| Failure::Usage(format!("{name} needs a value")))
}

/// `A:B` as two integers.
fn range(text: &str) -> Result<(i64, i64), Failure> {
    let bad = || Failure::Usage(format!("bad range {text:?}, expected A:B"));
    let (a, b) = text.split_once(':').ok_or_else(bad)?;
    Ok((a.parse().map_err(|_| bad())?, b.parse().map_err(|_| bad())?))
}

/// Python `Dng(path)` reads the file first; `open_dng` turns `OSError` into this text.
fn read(path: &str) -> Result<Vec<u8>, Failure> {
    std::fs::read(path).map_err(|e| {
        let text = e.to_string();
        let strerror = text.split(" (os error").next().unwrap_or(&text).to_string();
        Failure::Core(CoreError::raw_decode(format!(
            "读不了这个文件（{strerror}）"
        )))
    })
}

/// The file's base name, as `Path(path).name`.
fn base_name(path: &str) -> String {
    std::path::Path::new(path)
        .file_name()
        .map_or_else(String::new, |n| n.to_string_lossy().into_owned())
}

fn write_npy<T: npy::NpyElement>(
    out: &str,
    values: &[T],
    shape: &[usize],
    dtype: &str,
) -> Result<Json, Failure> {
    npy::write(out, values, shape)
        .map_err(|e| Failure::Usage(format!("cannot write {out}: {e}")))?;
    let dims: Vec<i64> = shape
        .iter()
        .map(|&d| i64::try_from(d).unwrap_or(i64::MAX))
        .collect();
    Ok(Json::object([
        ("out", Json::str(out)),
        ("dtype", Json::str(dtype)),
        ("shape", Json::ints(&dims)),
    ]))
}

/// The JSON text to print, and whether the core succeeded.
fn run(args: &[String]) -> Result<(String, bool), Failure> {
    let command = args.first().map(String::as_str).unwrap_or("");
    if command == "version" {
        let json = Json::object([("version", Json::str(env!("CARGO_PKG_VERSION")))]);
        return Ok((json.render(), true));
    }
    let path = args
        .get(1)
        .ok_or_else(|| Failure::Usage("missing <path>".into()))?;
    if command == "dng-describe" {
        // the same function gxr-wasm calls, so both print the same bytes
        let (ok, text) = gxr_raw::describe_json(&read(path)?, &base_name(path));
        return Ok((text, ok));
    }
    run_json(command, path, &args[2..]).map(|json| (json.render(), true))
}

fn run_json(command: &str, path: &str, rest: &[String]) -> Result<Json, Failure> {
    match command {
        "dng-summary" => {
            let data = read(path)?;
            Ok(Dng::open(&data, &base_name(path))?.summary())
        }
        "dng-mosaic" | "dng-crop-mosaic" => {
            let out = option(rest, "--out")?;
            let data = read(path)?;
            let dng = Dng::open(&data, &base_name(path))?;
            let m = if command == "dng-mosaic" {
                dng.mosaic()?.clone()
            } else {
                dng.crop_mosaic()?
            };
            write_npy(out, &m.values, &[m.height, m.width], "uint16")
        }
        "dng-half-codes" => {
            let out = option(rest, "--out")?;
            let data = read(path)?;
            let dng = Dng::open(&data, &base_name(path))?;
            let codes = gxr_raw::orient_array(&gxr_raw::half_res_codes(&dng)?, dng.orientation)?;
            write_npy(
                out,
                &codes.values,
                &[codes.height, codes.width, 3],
                "float32",
            )
        }
        "dng-normalised-block" => {
            let out = option(rest, "--out")?;
            let (y0, y1) = range(option(rest, "--rows")?)?;
            let (x0, x1) = range(option(rest, "--cols")?)?;
            let data = read(path)?;
            let dng = Dng::open(&data, &base_name(path))?;
            let block = dng.normalised_block(y0, y1, x0, x1)?;
            write_npy(out, &block.values, &[block.rows, block.cols], "float64")
        }
        "dng-wb-block" => {
            let out = option(rest, "--out")?;
            let (y0, y1) = range(option(rest, "--rows")?)?;
            let data = read(path)?;
            let dng = Dng::open(&data, &base_name(path))?;
            let block = gxr_raw::wb_mosaic_block(&dng, y0, y1)?;
            write_npy(out, &block.values, &[block.height, block.width], "float32")
        }
        _ => Err(Failure::Usage(format!("unknown command {command:?}"))),
    }
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok((text, ok)) => {
            println!("{text}");
            if ok {
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
