//! Port of `gxr_studio/gpu/tables.py`: `display_lut_u8` and `lut_bytes`.

use gxr_raw::linalg::{Mat3, inv, matmul};
use gxr_raw::sha256::sha256_hex;
use gxr_raw::{CoreError, Json};

use crate::bounds::{checked_elements, too_big_core, try_vec_cap, try_vec_filled};
use crate::photo::apply_u8;
use crate::spaces::{Target, default_gamut, is_display, matrix};

/// Port of gxr_studio/gpu/tables.py:14 SPACE_IDS.
pub fn space_id(space: &str) -> Option<i64> {
    match space {
        "srgb" => Some(0),
        "rec709" => Some(1),
        "gamma24" => Some(2),
        "llog" | "llog709" => Some(3),
        "linear" => Some(4),
        "flog" => Some(5),
        "flog2" => Some(6),
        "vlog" => Some(7),
        "slog3" | "slog3-sgamut3" => Some(8),
        "applelog" => Some(9),
        _ => None,
    }
}

/// Port of gxr_studio/gpu/tables.py:28 OUTPUT_SPACES.
pub const OUTPUT_SPACES: [&str; 4] = ["srgb", "rec709", "gamma24", "linear"];

/// Port of gxr_studio/gpu/tables.py:58 _gamut.
pub fn gamut_matrix(space: &str, override_gamut: Option<&str>) -> Result<Mat3, CoreError> {
    let g = match override_gamut {
        Some(g) => g,
        None => default_gamut(space)
            .ok_or_else(|| CoreError::new("bad_space", format!("unknown space: {space}")))?,
    };
    matrix(g)
}

/// Metadata emitted by `lut_bytes` (display or log).
#[derive(Clone, Debug, PartialEq)]
pub enum LutMeta {
    Display,
    Log(Box<LogLutMeta>),
}

/// Metadata fields for a log 3D LUT GPU table.
#[derive(Clone, Debug, PartialEq)]
pub struct LogLutMeta {
    pub n: usize,
    pub input_space_id: i64,
    pub output_space_id: i64,
    pub input_legal: bool,
    pub output_legal: bool,
    pub domain_min: [f64; 3],
    pub domain_max: [f64; 3],
    pub range3: [f64; 2],
    pub xyz_to_input: Mat3,
    pub out_to_srgb: Mat3,
}

impl LutMeta {
    pub fn is_display(&self) -> bool {
        matches!(self, Self::Display)
    }

    pub fn to_json(&self) -> Json {
        match self {
            Self::Display => Json::object([
                ("n", Json::Int(256)),
                ("display", Json::Bool(true)),
                ("table8", Json::Bool(true)),
            ]),
            Self::Log(m) => Json::object([
                ("n", Json::Int(m.n as i64)),
                ("display", Json::Bool(false)),
                ("table8", Json::Bool(false)),
                ("input_space_id", Json::Int(m.input_space_id)),
                ("output_space_id", Json::Int(m.output_space_id)),
                ("input_legal", Json::Bool(m.input_legal)),
                ("output_legal", Json::Bool(m.output_legal)),
                ("domain_min", Json::floats(&m.domain_min)),
                ("domain_max", Json::floats(&m.domain_max)),
                ("range3", Json::floats(&m.range3)),
                (
                    "xyz_to_input",
                    Json::Array(m.xyz_to_input.iter().map(|r| Json::floats(r)).collect()),
                ),
                (
                    "out_to_srgb",
                    Json::Array(m.out_to_srgb.iter().map(|r| Json::floats(r)).collect()),
                ),
            ]),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub enum LutTableResult {
    Table { bytes: Vec<u8>, meta: LutMeta },
    Refused { reason: &'static str },
}

impl LutTableResult {
    pub fn summary_json(&self) -> Json {
        match self {
            Self::Table { bytes, meta } => Json::object([
                ("meta", meta.to_json()),
                ("sha256", Json::str(sha256_hex(bytes))),
                ("bytes", Json::Int(bytes.len() as i64)),
            ]),
            Self::Refused { reason } => {
                Json::object([("meta", Json::Null), ("reason", Json::str(*reason))])
            }
        }
    }
}

/// Port of gxr_studio/gpu/tables.py:65 display_lut_u8.
pub fn display_lut_u8(target: &Target<'_>) -> Result<Vec<u8>, CoreError> {
    let slice_len = checked_elements(&[256, 256])?;
    let total_bytes = checked_elements(&[256, 256, 256, 4])?;
    let mut out = try_vec_cap(total_bytes)?;
    let mut rgb = try_vec_filled(slice_len, [0.0f64; 3])?;
    for b in 0..256u16 {
        let bf = f64::from(b);
        for g in 0..256u16 {
            let gf = f64::from(g);
            let row_off = usize::from(g) * 256;
            for r in 0..256u16 {
                rgb[row_off + usize::from(r)] = [f64::from(r), gf, bf];
            }
        }
        let mapped = apply_u8(target, &rgb, None)?;
        for px in mapped {
            out.extend_from_slice(&[px[0], px[1], px[2], 255]);
        }
    }
    Ok(out)
}

/// Port of gxr_studio/gpu/tables.py:88 lut_bytes.
pub fn lut_bytes(target: &Target<'_>) -> Result<LutTableResult, CoreError> {
    if is_display(&target.input_space) {
        let bytes = display_lut_u8(target)?;
        return Ok(LutTableResult::Table {
            bytes,
            meta: LutMeta::Display,
        });
    }
    let cube = target.cube;
    if cube.table1.is_some() {
        return Ok(LutTableResult::Refused {
            reason: "lut_1d_shaper",
        });
    }
    let Some(t3) = &cube.table3 else {
        return Ok(LutTableResult::Refused {
            reason: "lut_no_3d",
        });
    };
    let Some(in_id) = space_id(&target.input_space) else {
        return Ok(LutTableResult::Refused {
            reason: "lut_input_space",
        });
    };
    if !OUTPUT_SPACES.contains(&target.output_space.as_str()) {
        return Ok(LutTableResult::Refused {
            reason: "lut_output_space",
        });
    }
    let Some(out_id) = space_id(&target.output_space) else {
        return Ok(LutTableResult::Refused {
            reason: "lut_output_space",
        });
    };
    let n = t3.size;
    let n64 = u64::try_from(n).map_err(|_| too_big_core())?;
    let cube_elems = checked_elements(&[n64, n64, n64])?;
    if n < 2 || t3.rows.len() != cube_elems {
        return Ok(LutTableResult::Refused {
            reason: "lut_shape",
        });
    }

    let byte_len = checked_elements(&[cube_elems as u64, 16])?;
    let mut rgba = try_vec_cap(byte_len)?;
    for row in &t3.rows {
        #[allow(clippy::cast_possible_truncation)]
        let vals = [row[0] as f32, row[1] as f32, row[2] as f32, 0.0f32];
        for v in vals {
            rgba.extend_from_slice(&v.to_le_bytes());
        }
    }

    let m_in = gamut_matrix(&target.input_space, target.input_gamut.as_deref())?;
    let m_out = gamut_matrix(&target.output_space, target.output_gamut.as_deref())?;
    let m709 = matrix("rec709")?;
    let inv_in = inv(&m_in).map_err(|_| CoreError::new("bad_gamut", "singular input gamut"))?;
    let inv_709 = inv(&m709).map_err(|_| CoreError::new("bad_gamut", "singular rec709 gamut"))?;
    let out_to_srgb = matmul(&inv_709, &m_out);

    Ok(LutTableResult::Table {
        bytes: rgba,
        meta: LutMeta::Log(Box::new(LogLutMeta {
            n,
            input_space_id: in_id,
            output_space_id: out_id,
            input_legal: target.input_range == "legal",
            output_legal: target.output_range == "legal",
            domain_min: cube.domain_min,
            domain_max: cube.domain_max,
            range3: cube.range3,
            xyz_to_input: inv_in,
            out_to_srgb,
        })),
    })
}
