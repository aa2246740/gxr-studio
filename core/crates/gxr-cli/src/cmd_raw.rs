//! `dng-*` subcommands. The names, arguments and output bytes stay as they were in `main`.

use std::io::Write;

use gxr_raw::{CoreError, Dng, Json};

use crate::common::{self, Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    if command == "dng-demosaic" || command == "dng-neutral" {
        return run_render(command, args).map(|json| Output {
            text: json.render(),
            ok: true,
        });
    }
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

fn parse_i64(args: &[String], name: &str) -> Result<i64, Failure> {
    let raw = common::option(args, name)?;
    raw.parse::<i64>()
        .map_err(|_| Failure::Usage(format!("bad {name} {raw:?}, expected integer")))
}

fn write_u8_npy(out: &str, values: &[u8], shape: &[usize]) -> Result<Json, Failure> {
    let dims_str: Vec<String> = shape.iter().map(ToString::to_string).collect();
    let shape_text = if dims_str.len() == 1 {
        format!("({},)", dims_str[0])
    } else {
        format!("({})", dims_str.join(", "))
    };
    let mut dict = format!("{{'descr': '|u1', 'fortran_order': False, 'shape': {shape_text}, }}");
    let total = 10 + dict.len() + 1;
    dict.push_str(&" ".repeat((64 - total % 64) % 64));
    dict.push('\n');
    let len = u16::try_from(dict.len()).unwrap_or(u16::MAX);
    let mut file = std::fs::File::create(out)
        .map_err(|e| Failure::Usage(format!("cannot write {out}: {e}")))?;
    file.write_all(b"\x93NUMPY\x01\x00")
        .and_then(|()| file.write_all(&len.to_le_bytes()))
        .and_then(|()| file.write_all(dict.as_bytes()))
        .and_then(|()| file.write_all(values))
        .and_then(|()| file.flush())
        .map_err(|e| Failure::Usage(format!("cannot write {out}: {e}")))?;
    let mut dims = Vec::with_capacity(shape.len());
    for &dim in shape {
        let dim = i64::try_from(dim).map_err(|_| {
            Failure::Core(CoreError::raw_decode(
                "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size.",
            ))
        })?;
        dims.push(dim);
    }
    Ok(Json::object([
        ("out", Json::str(out)),
        ("dtype", Json::str("uint8")),
        ("shape", Json::ints(&dims)),
    ]))
}

fn run_render(command: &str, args: &[String]) -> Result<Json, Failure> {
    match command {
        "dng-demosaic" => {
            let path = common::option(args, "--dng")?;
            let y0 = parse_i64(args, "--y0")?;
            let y1 = parse_i64(args, "--y1")?;
            let out = common::option(args, "--out")?;
            let data = common::read(path)?;
            let dng = Dng::open(&data, &common::base_name(path))?;
            let strip = gxr_raw::demosaic_strip(&dng, y0, y1)?;
            common::write_npy(
                out,
                &strip.values,
                &[strip.height, strip.width, 3],
                "float32",
            )
        }
        "dng-neutral" => {
            let path = common::option(args, "--dng")?;
            let out = common::option(args, "--out")?;
            let data = common::read(path)?;
            let dng = Dng::open(&data, &common::base_name(path))?;
            let img = gxr_raw::render_full(&dng)?;
            write_u8_npy(out, &img.values, &[img.height, img.width, 3])
        }
        _ => Err(Failure::Usage(format!("unknown command {command:?}"))),
    }
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
