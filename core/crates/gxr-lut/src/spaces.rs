//! Port of `gxr_studio/lut/spaces.py`: D65 RGB spaces, log transfer curves, and `Target.render`.

use gxr_math::{log2 as math_log2, log10 as math_log10, powf, powi};
use gxr_raw::CoreError;
use gxr_raw::linalg::{Mat3, inv, matvec, solve};
use gxr_raw::sha256::sha256_hex;

use crate::bounds::try_vec_cap;
use crate::cube::Cube;

/// Port of gxr_studio/lut/spaces.py:14 DISPLAY.
pub const DISPLAY: [&str; 3] = ["srgb", "rec709", "gamma24"];

/// Port of gxr_studio/lut/spaces.py:15 SPACES.
pub const SPACES: [&str; 12] = [
    "srgb",
    "rec709",
    "gamma24",
    "linear",
    "flog",
    "flog2",
    "llog",
    "llog709",
    "vlog",
    "slog3",
    "slog3-sgamut3",
    "applelog",
];

/// Port of gxr_studio/lut/spaces.py:4 PRIMARIES.
pub fn primaries(gamut: &str) -> Result<[(f64, f64); 3], CoreError> {
    match gamut {
        "rec709" => Ok([(0.64, 0.33), (0.30, 0.60), (0.15, 0.06)]),
        "rec2020" => Ok([(0.708, 0.292), (0.170, 0.797), (0.131, 0.046)]),
        "vgamut" => Ok([(0.730, 0.280), (0.165, 0.840), (0.100, -0.030)]),
        "sgamut3" => Ok([(0.730, 0.280), (0.140, 0.855), (0.100, -0.050)]),
        "sgamut3cine" => Ok([(0.766, 0.275), (0.225, 0.800), (0.089, -0.087)]),
        _ => Err(CoreError::new(
            "bad_gamut",
            format!("请求无效。请刷新页面后重试。色域：{gamut}"),
        )),
    }
}

/// Port of gxr_studio/lut/spaces.py:11 GAMUT.
pub fn default_gamut(space: &str) -> Option<&'static str> {
    match space {
        "srgb" | "rec709" | "gamma24" | "linear" | "llog709" => Some("rec709"),
        "flog" | "flog2" | "llog" | "applelog" => Some("rec2020"),
        "vlog" => Some("vgamut"),
        "slog3" => Some("sgamut3cine"),
        "slog3-sgamut3" => Some("sgamut3"),
        _ => None,
    }
}

pub fn is_display(space: &str) -> bool {
    DISPLAY.contains(&space)
}

pub fn is_valid_space(space: &str) -> bool {
    SPACES.contains(&space)
}

pub fn is_valid_gamut(gamut: &str) -> bool {
    matches!(
        gamut,
        "rec709" | "rec2020" | "vgamut" | "sgamut3" | "sgamut3cine"
    )
}

/// Port of gxr_studio/lut/spaces.py:18 matrix.
pub fn matrix(gamut: &str) -> Result<Mat3, CoreError> {
    let xy = primaries(gamut)?;
    let xyz: Mat3 = [
        [xy[0].0 / xy[0].1, xy[1].0 / xy[1].1, xy[2].0 / xy[2].1],
        [1.0, 1.0, 1.0],
        [
            (1.0 - (xy[0].0 + xy[0].1)) / xy[0].1,
            (1.0 - (xy[1].0 + xy[1].1)) / xy[1].1,
            (1.0 - (xy[2].0 + xy[2].1)) / xy[2].1,
        ],
    ];
    let white = [0.3127 / 0.3290, 1.0, (1.0 - 0.3127 - 0.3290) / 0.3290];
    let scale = solve(&xyz, &white)
        .map_err(|_| CoreError::new("bad_gamut", format!("singular primaries matrix: {gamut}")))?;
    Ok([
        [
            xyz[0][0] * scale[0],
            xyz[0][1] * scale[1],
            xyz[0][2] * scale[2],
        ],
        [
            xyz[1][0] * scale[0],
            xyz[1][1] * scale[1],
            xyz[1][2] * scale[2],
        ],
        [
            xyz[2][0] * scale[0],
            xyz[2][1] * scale[1],
            xyz[2][2] * scale[2],
        ],
    ])
}

#[inline]
fn max_floor(x: f64, lo: f64) -> f64 {
    if x.is_nan() { x } else { x.max(lo) }
}

#[inline]
fn py_sign(x: f64) -> f64 {
    if x.is_nan() {
        x
    } else if x > 0.0 {
        1.0
    } else if x < 0.0 {
        -1.0
    } else {
        0.0
    }
}

/// Port of gxr_studio/lut/spaces.py:25 log10.
#[inline]
pub fn log10(x: f64) -> f64 {
    math_log10(max_floor(x, 1e-30))
}

/// Port of gxr_studio/lut/spaces.py:30 encode (scalar).
pub fn encode_scalar(x: f64, space: &str) -> Result<f64, CoreError> {
    match space {
        "linear" => Ok(x),
        "srgb" => Ok(if x <= 0.0031308 {
            12.92 * x
        } else {
            1.055 * powf(max_floor(x, 0.0), 1.0 / 2.4) - 0.055
        }),
        "rec709" => Ok(if x < 0.018 {
            4.5 * x
        } else {
            1.099 * powf(max_floor(x, 0.0), 0.45) - 0.099
        }),
        "gamma24" => Ok(py_sign(x) * powf(x.abs(), 1.0 / 2.4)),
        "flog" | "flog2" => {
            let (a, b, c, d, e, f, cut) = if space == "flog" {
                (
                    0.555556, 0.009468, 0.344676, 0.790453, 8.735631, 0.092864, 0.00089,
                )
            } else {
                (
                    5.555556, 0.064829, 0.245281, 0.384316, 8.799461, 0.092864, 0.000889,
                )
            };
            Ok(if x < cut {
                e * x + f
            } else {
                c * log10(a * x + b) + d
            })
        }
        "llog" | "llog709" => Ok(if x <= 0.006 {
            8.0 * x + 0.09
        } else {
            0.27 * log10(1.3 * x + 0.0115) + 0.6
        }),
        "vlog" => Ok(if x < 0.01 {
            5.6 * x + 0.125
        } else {
            0.241514 * log10(x + 0.00873) + 0.598206
        }),
        "slog3" | "slog3-sgamut3" => Ok(if x < 0.01125 {
            (95.0 + x * (171.2102946929 - 95.0) / 0.01125) / 1023.0
        } else {
            (420.0 + 261.5 * log10((x + 0.01) / 0.19)) / 1023.0
        }),
        "applelog" => {
            let (r0, rt, c, beta, gamma, delta) = (
                -0.05641088,
                0.01,
                47.28711236,
                0.00964052,
                0.08550479,
                0.69336945,
            );
            Ok(if x >= rt {
                gamma * math_log2(max_floor(x + beta, 1e-30)) + delta
            } else {
                c * powi(max_floor(x - r0, 0.0), 2)
            })
        }
        _ => Err(CoreError::new(
            "value_error",
            format!("不支持输入空间：{space}"),
        )),
    }
}

/// Port of gxr_studio/lut/spaces.py:30 encode (slice).
pub fn encode(xs: &[f64], space: &str) -> Result<Vec<f64>, CoreError> {
    if !is_valid_space(space) {
        return Err(CoreError::new(
            "value_error",
            format!("不支持输入空间：{space}"),
        ));
    }
    let mut out = try_vec_cap(xs.len())?;
    for &v in xs {
        out.push(encode_scalar(v, space)?);
    }
    Ok(out)
}

/// Port of gxr_studio/lut/spaces.py:58 decode (scalar).
pub fn decode_scalar(x: f64, space: &str) -> Result<f64, CoreError> {
    match space {
        "linear" => Ok(x),
        "srgb" => Ok(if x <= 0.04045 {
            x / 12.92
        } else {
            powf(max_floor((x + 0.055) / 1.055, 0.0), 2.4)
        }),
        "rec709" => Ok(if x < 0.081 {
            x / 4.5
        } else {
            powf(max_floor((x + 0.099) / 1.099, 0.0), 1.0 / 0.45)
        }),
        "gamma24" => Ok(py_sign(x) * powf(x.abs(), 2.4)),
        "flog" | "flog2" => {
            let (a, b, c, d, e, f, cut) = if space == "flog" {
                (
                    0.555556,
                    0.009468,
                    0.344676,
                    0.790453,
                    8.735631,
                    0.092864,
                    0.100537775223865,
                )
            } else {
                (
                    5.555556,
                    0.064829,
                    0.245281,
                    0.384316,
                    8.799461,
                    0.092864,
                    0.100686685370811,
                )
            };
            Ok(if x < cut {
                (x - f) / e
            } else {
                (powf(10.0, (x - d) / c) - b) / a
            })
        }
        "llog" | "llog709" => Ok(if x <= 0.1380 {
            (x - 0.09) / 8.0
        } else {
            (powf(10.0, (x - 0.6) / 0.27) - 0.0115) / 1.3
        }),
        "vlog" => Ok(if x < 0.181 {
            (x - 0.125) / 5.6
        } else {
            powf(10.0, (x - 0.598206) / 0.241514) - 0.00873
        }),
        "slog3" | "slog3-sgamut3" => Ok(if x < 171.2102946929 / 1023.0 {
            (x * 1023.0 - 95.0) * 0.01125 / (171.2102946929 - 95.0)
        } else {
            powf(10.0, (x * 1023.0 - 420.0) / 261.5) * 0.19 - 0.01
        }),
        "applelog" => {
            let (r0, rt, c, beta, gamma, delta) = (
                -0.05641088,
                0.01,
                47.28711236,
                0.00964052,
                0.08550479,
                0.69336945,
            );
            Ok(if x >= c * powi(rt - r0, 2) {
                powf(2.0, (x - delta) / gamma) - beta
            } else {
                (max_floor(x, 0.0) / c).sqrt() + r0
            })
        }
        _ => Err(CoreError::new(
            "value_error",
            format!("不支持输出空间：{space}"),
        )),
    }
}

/// Port of gxr_studio/lut/spaces.py:58 decode (slice).
pub fn decode(xs: &[f64], space: &str) -> Result<Vec<f64>, CoreError> {
    if !is_valid_space(space) {
        return Err(CoreError::new(
            "value_error",
            format!("不支持输出空间：{space}"),
        ));
    }
    let mut out = try_vec_cap(xs.len())?;
    for &v in xs {
        out.push(decode_scalar(v, space)?);
    }
    Ok(out)
}

fn resolved_gamut<'a>(space: &str, gamut: Option<&'a str>) -> Result<&'a str, CoreError> {
    if let Some(g) = gamut {
        return Ok(g);
    }
    default_gamut(space)
        .ok_or_else(|| CoreError::new("value_error", format!("不支持空间：{space}")))
}

/// Port of gxr_studio/lut/spaces.py:85 xyz_to_encoded.
pub fn xyz_to_encoded(
    xyz: &[[f64; 3]],
    space: &str,
    gamut: Option<&str>,
) -> Result<Vec<[f64; 3]>, CoreError> {
    if !is_valid_space(space) {
        return Err(CoreError::new(
            "value_error",
            format!("不支持输入空间：{space}"),
        ));
    }
    let g = resolved_gamut(space, gamut)?;
    let inv_m = inv(&matrix(g)?)
        .map_err(|_| CoreError::new("bad_gamut", format!("singular gamut matrix: {g}")))?;
    let mut out = try_vec_cap(xyz.len())?;
    for px in xyz {
        let lin = matvec(&inv_m, px);
        out.push([
            encode_scalar(lin[0], space)?,
            encode_scalar(lin[1], space)?,
            encode_scalar(lin[2], space)?,
        ]);
    }
    Ok(out)
}

/// Port of gxr_studio/lut/spaces.py:90 to_srgb.
pub fn to_srgb(
    encoded: &[[f64; 3]],
    space: &str,
    gamut: Option<&str>,
) -> Result<Vec<[f64; 3]>, CoreError> {
    if !is_valid_space(space) {
        return Err(CoreError::new(
            "value_error",
            format!("不支持输出空间：{space}"),
        ));
    }
    let g = resolved_gamut(space, gamut)?;
    let m = matrix(g)?;
    let inv_709 = inv(&matrix("rec709")?)
        .map_err(|_| CoreError::new("bad_gamut", "singular rec709 matrix"))?;
    let mut out = try_vec_cap(encoded.len())?;
    for px in encoded {
        let lin = [
            decode_scalar(px[0], space)?,
            decode_scalar(px[1], space)?,
            decode_scalar(px[2], space)?,
        ];
        let xyz = matvec(&m, &lin);
        let lin_709 = matvec(&inv_709, &xyz);
        out.push([
            encode_scalar(lin_709[0], "srgb")?,
            encode_scalar(lin_709[1], "srgb")?,
            encode_scalar(lin_709[2], "srgb")?,
        ]);
    }
    Ok(out)
}

/// Validated space parameters (`LutLibrary._RENDER_SPEC`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RenderSpec {
    pub input_space: String,
    pub output_space: String,
    pub input_gamut: Option<String>,
    pub output_gamut: Option<String>,
    pub input_range: String,
    pub output_range: String,
}

impl RenderSpec {
    /// Port of gxr_studio/lut/service.py:149 LutService.target validation.
    pub fn validated(
        input_space: &str,
        output_space: &str,
        input_gamut: Option<&str>,
        output_gamut: Option<&str>,
        input_range: &str,
        output_range: &str,
    ) -> Result<Self, CoreError> {
        if !is_valid_space(input_space) {
            return Err(CoreError::new("bad_space", "请求无效。请刷新页面后重试。"));
        }
        if !is_valid_space(output_space) {
            return Err(CoreError::new("bad_space", "请求无效。请刷新页面后重试。"));
        }
        for g in [input_gamut, output_gamut].into_iter().flatten() {
            if !is_valid_gamut(g) {
                return Err(CoreError::new("bad_gamut", "请求无效。请刷新页面后重试。"));
            }
        }
        for r in [input_range, output_range] {
            if r != "full" && r != "legal" {
                return Err(CoreError::new("bad_range", "请求无效。请刷新页面后重试。"));
            }
        }
        Ok(Self {
            input_space: input_space.to_string(),
            output_space: output_space.to_string(),
            input_gamut: input_gamut.map(str::to_string),
            output_gamut: output_gamut.map(str::to_string),
            input_range: input_range.to_string(),
            output_range: output_range.to_string(),
        })
    }

    /// Parse a `--space JSON` object without serde.
    pub fn from_json(text: &str) -> Result<Self, CoreError> {
        let map = parse_flat_json_object(text)?;
        let input_space = match map.get("input_space").or_else(|| map.get("space")) {
            Some(Some(s)) => s.as_str(),
            _ => {
                return Err(CoreError::new("lut_space_required", "请选择输入色彩空间"));
            }
        };
        let output_space = match map.get("output_space").or_else(|| map.get("output")) {
            Some(Some(s)) => s.as_str(),
            Some(None) => return Err(CoreError::new("bad_space", "请求无效。请刷新页面后重试。")),
            None => "srgb",
        };
        let input_gamut = match map.get("input_gamut") {
            Some(Some(s)) => Some(s.as_str()),
            _ => None,
        };
        let output_gamut = match map.get("output_gamut") {
            Some(Some(s)) => Some(s.as_str()),
            _ => None,
        };
        let input_range = match map.get("input_range") {
            Some(Some(s)) => s.as_str(),
            Some(None) => return Err(CoreError::new("bad_range", "请求无效。请刷新页面后重试。")),
            None => "full",
        };
        let output_range = match map.get("output_range") {
            Some(Some(s)) => s.as_str(),
            Some(None) => return Err(CoreError::new("bad_range", "请求无效。请刷新页面后重试。")),
            None => "full",
        };
        Self::validated(
            input_space,
            output_space,
            input_gamut,
            output_gamut,
            input_range,
            output_range,
        )
    }
}

/// Port of gxr_studio/lut/library.py:62 _token.
pub fn render_token(cube_id: &str, spec: &RenderSpec) -> String {
    let json = gxr_raw::Json::object([
        ("cube", gxr_raw::Json::str(cube_id)),
        (
            "spec",
            gxr_raw::Json::object([
                (
                    "input_gamut",
                    gxr_raw::Json::opt(spec.input_gamut.clone().map(gxr_raw::Json::Str)),
                ),
                ("input_range", gxr_raw::Json::str(&spec.input_range)),
                ("input_space", gxr_raw::Json::str(&spec.input_space)),
                (
                    "output_gamut",
                    gxr_raw::Json::opt(spec.output_gamut.clone().map(gxr_raw::Json::Str)),
                ),
                ("output_range", gxr_raw::Json::str(&spec.output_range)),
                ("output_space", gxr_raw::Json::str(&spec.output_space)),
            ]),
        ),
    ])
    .render();
    let hex = sha256_hex(json.as_bytes());
    hex[..16].to_string()
}

/// Render statistics returned by `Target::render`.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct RenderStats {
    pub input_clipped_share: f64,
    pub display_gamut_clipped_share: f64,
}

/// Port of gxr_studio/lut/spaces.py:96 Target.
#[derive(Clone, Debug, PartialEq)]
pub struct Target<'a> {
    pub cube: &'a Cube,
    pub input_space: String,
    pub output_space: String,
    pub input_gamut: Option<String>,
    pub output_gamut: Option<String>,
    pub input_range: String,
    pub output_range: String,
}

impl<'a> Target<'a> {
    pub fn from_spec(cube: &'a Cube, spec: &RenderSpec) -> Self {
        Self {
            cube,
            input_space: spec.input_space.clone(),
            output_space: spec.output_space.clone(),
            input_gamut: spec.input_gamut.clone(),
            output_gamut: spec.output_gamut.clone(),
            input_range: spec.input_range.clone(),
            output_range: spec.output_range.clone(),
        }
    }

    pub fn new(
        cube: &'a Cube,
        input_space: &str,
        output_space: &str,
        input_gamut: Option<&str>,
        output_gamut: Option<&str>,
        input_range: &str,
        output_range: &str,
    ) -> Result<Self, CoreError> {
        let spec = RenderSpec::validated(
            input_space,
            output_space,
            input_gamut,
            output_gamut,
            input_range,
            output_range,
        )?;
        Ok(Self::from_spec(cube, &spec))
    }

    /// Port of gxr_studio/lut/spaces.py:103 Target.render.
    pub fn render(
        &self,
        stock_rgb: &[[f64; 3]],
        xyz: Option<&[[f64; 3]]>,
    ) -> Result<(Vec<[f64; 3]>, RenderStats), CoreError> {
        let mut x = if is_display(&self.input_space) {
            let m709 = matrix("rec709")?;
            let mut xyz_display = try_vec_cap(stock_rgb.len())?;
            for px in stock_rgb {
                let lin = [
                    decode_scalar(px[0] / 255.0, "srgb")?,
                    decode_scalar(px[1] / 255.0, "srgb")?,
                    decode_scalar(px[2] / 255.0, "srgb")?,
                ];
                xyz_display.push(matvec(&m709, &lin));
            }
            xyz_to_encoded(&xyz_display, &self.input_space, self.input_gamut.as_deref())?
        } else {
            let Some(xyz_slice) = xyz else {
                return Err(CoreError::new(
                    "value_error",
                    "Log LUT 需要 DNG 的场景 XYZ，不能从 JPEG 猜测",
                ));
            };
            if xyz_slice.len() != stock_rgb.len() {
                return Err(CoreError::new(
                    "value_error",
                    format!(
                        "xyz length {} does not match rgb length {}",
                        xyz_slice.len(),
                        stock_rgb.len()
                    ),
                ));
            }
            xyz_to_encoded(xyz_slice, &self.input_space, self.input_gamut.as_deref())?
        };

        if self.input_range == "legal" {
            for px in &mut x {
                for v in px.iter_mut() {
                    *v = (64.0 + 876.0 * *v) / 1023.0;
                }
            }
        }

        let (mut out, input_clipped_share) = self.cube.apply(&x)?;

        if self.output_range == "legal" {
            for px in &mut out {
                for v in px.iter_mut() {
                    *v = (1023.0 * *v - 64.0) / 876.0;
                }
            }
        }

        let srgb = to_srgb(&out, &self.output_space, self.output_gamut.as_deref())?;
        let mut gamut_clipped = 0usize;
        let mut rgb = try_vec_cap(srgb.len())?;
        for px in &srgb {
            if px.iter().any(|&v| !(0.0..=1.0).contains(&v)) {
                gamut_clipped += 1;
            }
            rgb.push([
                clip_01(px[0]) * 255.0,
                clip_01(px[1]) * 255.0,
                clip_01(px[2]) * 255.0,
            ]);
        }
        let display_gamut_clipped_share = if srgb.is_empty() {
            0.0
        } else {
            gamut_clipped as f64 / srgb.len() as f64
        };
        Ok((
            rgb,
            RenderStats {
                input_clipped_share,
                display_gamut_clipped_share,
            },
        ))
    }
}

#[inline]
fn clip_01(x: f64) -> f64 {
    if x.is_nan() { x } else { x.clamp(0.0, 1.0) }
}

/// Minimal flat JSON object parser (`{"key": "val" | null, ...}`) without serde.
fn parse_flat_json_object(
    text: &str,
) -> Result<std::collections::HashMap<String, Option<String>>, CoreError> {
    let bad = || CoreError::new("value_error", "invalid space JSON");
    let bytes = text.as_bytes();
    let mut pos = skip_ws(bytes, 0);
    if pos >= bytes.len() || bytes[pos] != b'{' {
        return Err(bad());
    }
    pos = skip_ws(bytes, pos + 1);
    let mut out = std::collections::HashMap::new();
    if pos < bytes.len() && bytes[pos] == b'}' {
        pos = skip_ws(bytes, pos + 1);
        return if pos == bytes.len() {
            Ok(out)
        } else {
            Err(bad())
        };
    }
    loop {
        let (key, next_pos) = parse_json_string(text, pos).ok_or_else(bad)?;
        pos = skip_ws(bytes, next_pos);
        if pos >= bytes.len() || bytes[pos] != b':' {
            return Err(bad());
        }
        pos = skip_ws(bytes, pos + 1);
        let val = if bytes[pos..].starts_with(b"null") {
            pos += 4;
            None
        } else if bytes.get(pos) == Some(&b'"') {
            let (s, p) = parse_json_string(text, pos).ok_or_else(bad)?;
            pos = p;
            Some(s)
        } else {
            return Err(bad());
        };
        out.insert(key, val);
        pos = skip_ws(bytes, pos);
        if pos >= bytes.len() {
            return Err(bad());
        }
        if bytes[pos] == b',' {
            pos = skip_ws(bytes, pos + 1);
        } else if bytes[pos] == b'}' {
            pos = skip_ws(bytes, pos + 1);
            return if pos == bytes.len() {
                Ok(out)
            } else {
                Err(bad())
            };
        } else {
            return Err(bad());
        }
    }
}

fn skip_ws(bytes: &[u8], mut pos: usize) -> usize {
    while pos < bytes.len() && matches!(bytes[pos], b' ' | b'\t' | b'\r' | b'\n') {
        pos += 1;
    }
    pos
}

fn parse_json_string(text: &str, start: usize) -> Option<(String, usize)> {
    let bytes = text.as_bytes();
    if bytes.get(start) != Some(&b'"') {
        return None;
    }
    let mut out = String::new();
    let mut chars = text[start + 1..].char_indices();
    while let Some((idx, ch)) = chars.next() {
        match ch {
            '"' => return Some((out, start + 1 + idx + 1)),
            '\\' => {
                let (_, esc) = chars.next()?;
                match esc {
                    '"' => out.push('"'),
                    '\\' => out.push('\\'),
                    '/' => out.push('/'),
                    'b' => out.push('\u{0008}'),
                    'f' => out.push('\u{000c}'),
                    'n' => out.push('\n'),
                    'r' => out.push('\r'),
                    't' => out.push('\t'),
                    'u' => {
                        let mut hex = String::with_capacity(4);
                        for _ in 0..4 {
                            hex.push(chars.next()?.1);
                        }
                        let code = u32::from_str_radix(&hex, 16).ok()?;
                        let c = char::from_u32(code)?;
                        out.push(c);
                    }
                    _ => return None,
                }
            }
            c if (c as u32) < 0x20 => return None,
            c => out.push(c),
        }
    }
    None
}
