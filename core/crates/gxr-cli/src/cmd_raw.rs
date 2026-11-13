//! `dng-*` subcommands. The names, arguments and output bytes stay as they were in `main`.

use gxr_raw::{Dng, Json};

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    let path = args
        .first()
        .ok_or_else(|| Failure::Usage("missing <path>".into()))?;
    if command == "dng-describe" {
        // the same function gxr-wasm calls, so both print the same bytes
        let (ok, text) = gxr_raw::describe_json(&common::read(path)?, &common::base_name(path));
        return Ok(Output { text, ok });
    }
    run_json(command, path, &args[1..]).map(|json| Output {
        text: json.render(),
        ok: true,
    })
}

fn run_json(command: &str, path: &str, rest: &[String]) -> Result<Json, Failure> {
    match command {
        "dng-summary" => {
            let data = common::read(path)?;
            Ok(Dng::open(&data, &common::base_name(path))?.summary())
        }
        "dng-mosaic" | "dng-crop-mosaic" => {
            let out = common::option(rest, "--out")?;
            let data = common::read(path)?;
            let dng = Dng::open(&data, &common::base_name(path))?;
            let m = if command == "dng-mosaic" {
                dng.mosaic()?.clone()
            } else {
                dng.crop_mosaic()?
            };
            common::write_npy(out, &m.values, &[m.height, m.width], "uint16")
        }
        "dng-half-codes" => {
            let out = common::option(rest, "--out")?;
            let data = common::read(path)?;
            let dng = Dng::open(&data, &common::base_name(path))?;
            let codes = gxr_raw::orient_array(&gxr_raw::half_res_codes(&dng)?, dng.orientation)?;
            common::write_npy(
                out,
                &codes.values,
                &[codes.height, codes.width, 3],
                "float32",
            )
        }
        "dng-normalised-block" => {
            let out = common::option(rest, "--out")?;
            let (y0, y1) = common::range(common::option(rest, "--rows")?)?;
            let (x0, x1) = common::range(common::option(rest, "--cols")?)?;
            let data = common::read(path)?;
            let dng = Dng::open(&data, &common::base_name(path))?;
            let block = dng.normalised_block(y0, y1, x0, x1)?;
            common::write_npy(out, &block.values, &[block.rows, block.cols], "float64")
        }
        "dng-wb-block" => {
            let out = common::option(rest, "--out")?;
            let (y0, y1) = common::range(common::option(rest, "--rows")?)?;
            let data = common::read(path)?;
            let dng = Dng::open(&data, &common::base_name(path))?;
            let block = gxr_raw::wb_mosaic_block(&dng, y0, y1)?;
            common::write_npy(out, &block.values, &[block.height, block.width], "float32")
        }
        _ => Err(Failure::Usage(format!("unknown command {command:?}"))),
    }
}
