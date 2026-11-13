//! Port of `gxr_studio/lut/cube.py`: bounded `.cube` reader and tetrahedral lookup.

use std::collections::HashMap;
use std::path::Path;

use gxr_raw::sha256::sha256_hex;
use gxr_raw::{CoreError, Json};

/// Port of gxr_studio/lut/cube.py:14 MAX_BYTES.
pub const MAX_BYTES: usize = 64 * 1024 * 1024;
/// Port of gxr_studio/lut/cube.py:15 MAX_1D.
pub const MAX_1D: usize = 65536;
/// Port of gxr_studio/lut/cube.py:16 MAX_3D.
pub const MAX_3D: usize = 129;

/// A 3D LUT table stored in `[b][g][r]` C-order (R changes fastest, as in `.cube`).
#[derive(Clone, Debug, PartialEq)]
pub struct Table3 {
    pub size: usize,
    pub rows: Vec<[f64; 3]>,
}

impl Table3 {
    #[inline]
    pub fn at(&self, r: usize, g: usize, b: usize) -> [f64; 3] {
        self.rows[(b * self.size + g) * self.size + r]
    }
}

/// Port of gxr_studio/lut/cube.py:40 Cube.
#[derive(Clone, Debug, PartialEq)]
pub struct Cube {
    pub table1: Option<Vec<[f64; 3]>>,
    pub table3: Option<Table3>,
    pub domain_min: [f64; 3],
    pub domain_max: [f64; 3],
    pub range1: [f64; 2],
    pub range3: [f64; 2],
    pub title: String,
    pub source_sha256: String,
    pub comments: Vec<String>,
}

/// Port of gxr_studio/lut/cube.py:19 comment_start (returns char index or -1).
pub fn comment_start(line: &str) -> i64 {
    match comment_start_byte(line) {
        Some(byte_idx) => i64::try_from(line[..byte_idx].chars().count()).unwrap_or(-1),
        None => -1,
    }
}

/// Internal byte-offset version of `comment_start` so slicing `line[idx + 1..]` is safe.
fn comment_start_byte(line: &str) -> Option<usize> {
    if !line.trim_start().starts_with("TITLE") {
        return line.find('#');
    }
    let mut quote: Option<char> = None;
    let mut escaped = false;
    for (idx, ch) in line.char_indices() {
        if escaped {
            escaped = false;
        } else if ch == '\\' && quote != Some('\'') {
            escaped = true;
        } else if let Some(q) = quote {
            if ch == q {
                quote = None;
            }
        } else if ch == '"' || ch == '\'' {
            quote = Some(ch);
        } else if ch == '#' {
            return Some(idx);
        }
    }
    None
}

/// Python `str.splitlines()` splits on `\n`, `\r\n`, `\r`, `\x0b`, `\x0c`, `\x1c`..`\x1e`, `\x85`, `\u2028`, `\u2029`.
fn py_splitlines(text: &str) -> Vec<&str> {
    if text.is_empty() {
        return Vec::new();
    }
    let mut lines = Vec::new();
    let mut start = 0;
    let mut iter = text.char_indices().peekable();
    while let Some((idx, ch)) = iter.next() {
        let is_break = matches!(
            ch,
            '\n' | '\r'
                | '\x0b'
                | '\x0c'
                | '\x1c'
                | '\x1d'
                | '\x1e'
                | '\u{0085}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if is_break {
            lines.push(&text[start..idx]);
            if ch == '\r'
                && let Some(&(_, '\n')) = iter.peek()
            {
                let _ = iter.next();
                start = idx + 2;
                continue;
            }
            start = idx + ch.len_utf8();
        }
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

/// Port of Python `shlex.split(line, comments=True, posix=True)`.
fn shlex_split_posix_comments(line: &str) -> Result<Vec<String>, CoreError> {
    let mut tokens = Vec::new();
    let mut current = String::new();
    let mut has_token = false;
    let mut quote: Option<char> = None;
    let mut chars = line.chars().peekable();
    while let Some(ch) = chars.next() {
        match quote {
            None => {
                if matches!(ch, ' ' | '\t' | '\r' | '\n') {
                    if has_token {
                        tokens.push(std::mem::take(&mut current));
                        has_token = false;
                    }
                } else if ch == '#' {
                    if has_token {
                        tokens.push(std::mem::take(&mut current));
                        has_token = false;
                    }
                    break;
                } else if ch == '\\' {
                    let Some(next) = chars.next() else {
                        return Err(CoreError::new("value_error", "No escaped character"));
                    };
                    current.push(next);
                    has_token = true;
                } else if ch == '"' || ch == '\'' {
                    quote = Some(ch);
                    has_token = true;
                } else {
                    current.push(ch);
                    has_token = true;
                }
            }
            Some('\'') => {
                if ch == '\'' {
                    quote = None;
                } else {
                    current.push(ch);
                }
            }
            Some('"') => {
                if ch == '"' {
                    quote = None;
                } else if ch == '\\' {
                    if let Some(&next) = chars.peek() {
                        if next == '"' || next == '\\' {
                            let _ = chars.next();
                            current.push(next);
                        } else {
                            current.push('\\');
                        }
                    } else {
                        return Err(CoreError::new("value_error", "No closing quotation"));
                    }
                } else {
                    current.push(ch);
                }
            }
            Some(_) => {}
        }
    }
    if quote.is_some() {
        return Err(CoreError::new("value_error", "No closing quotation"));
    }
    if has_token {
        tokens.push(current);
    }
    Ok(tokens)
}

fn is_known_header(key: &str) -> bool {
    matches!(
        key,
        "LUT_1D_SIZE"
            | "LUT_3D_SIZE"
            | "DOMAIN_MIN"
            | "DOMAIN_MAX"
            | "TITLE"
            | "LUT_1D_INPUT_RANGE"
            | "LUT_3D_INPUT_RANGE"
    )
}

fn parse_py_float(s: &str) -> Result<f64, ()> {
    let trimmed = s.trim();
    if trimmed.is_empty() {
        return Err(());
    }
    trimmed.parse::<f64>().map_err(|_| ())
}

#[derive(Default)]
struct ParsedHeaders {
    n1: Option<usize>,
    n3: Option<usize>,
    domain_min: Option<[f64; 3]>,
    domain_max: Option<[f64; 3]>,
    range1: Option<[f64; 2]>,
    range3: Option<[f64; 2]>,
    title: Option<String>,
    seen: HashMap<String, ()>,
}

impl Cube {
    /// Port of gxr_studio/lut/cube.py:53 Cube.read (from filesystem path).
    pub fn read(path: &Path) -> Result<Self, CoreError> {
        let meta = std::fs::metadata(path).ok();
        let ok = meta
            .as_ref()
            .is_some_and(|m| m.is_file() && m.len() <= MAX_BYTES as u64);
        if !ok {
            return Err(CoreError::new(
                "value_error",
                "cube 文件不存在或超过 64 MiB",
            ));
        }
        let raw = std::fs::read(path)
            .map_err(|_| CoreError::new("value_error", "cube 文件不存在或超过 64 MiB"))?;
        Self::read_bytes(&raw)
    }

    /// Port of gxr_studio/lut/cube.py:53 Cube.read (from raw `.cube` bytes).
    pub fn read_bytes(raw: &[u8]) -> Result<Self, CoreError> {
        if raw.len() > MAX_BYTES {
            return Err(CoreError::new(
                "value_error",
                "cube 文件不存在或超过 64 MiB",
            ));
        }
        let digest = sha256_hex(raw);
        let text = std::str::from_utf8(raw)
            .map_err(|e| CoreError::new("value_error", format!("utf-8 decode error: {e}")))?;
        let text = text.strip_prefix('\u{feff}').unwrap_or(text);
        Self::parse(text, &digest)
    }

    /// Port of gxr_studio/lut/cube.py:61 Cube.parse.
    pub fn parse(text: &str, digest: &str) -> Result<Self, CoreError> {
        if text.len() > MAX_BYTES {
            return Err(CoreError::new("value_error", "cube 超过 64 MiB"));
        }
        let mut hdr = ParsedHeaders::default();
        let mut data: Vec<[f64; 3]> = Vec::new();
        let mut comments: Vec<String> = Vec::new();

        for (idx, line) in py_splitlines(text).into_iter().enumerate() {
            let no = idx + 1;
            if let Some(start) = comment_start_byte(line) {
                comments.push(line[start + 1..].trim().to_string());
            }
            let words: Vec<String> = if line.trim_start().starts_with("TITLE") {
                shlex_split_posix_comments(line)?
            } else {
                let code = line.split('#').next().unwrap_or("");
                code.split_whitespace().map(str::to_string).collect()
            };
            if words.is_empty() {
                continue;
            }
            let key = &words[0];
            if is_known_header(key) {
                if !data.is_empty() || hdr.seen.contains_key(key.as_str()) {
                    return Err(CoreError::new(
                        "value_error",
                        format!("line {no}: duplicate or late header {key}"),
                    ));
                }
                hdr.seen.insert(key.clone(), ());
                let values = &words[1..];
                match key.as_str() {
                    "TITLE" => {
                        if values.len() != 1 {
                            return Err(CoreError::new(
                                "value_error",
                                format!("line {no}: TITLE needs one quoted string"),
                            ));
                        }
                        hdr.title = Some(values[0].clone());
                    }
                    "LUT_1D_SIZE" | "LUT_3D_SIZE" => {
                        if values.len() != 1 {
                            return Err(CoreError::new(
                                "value_error",
                                format!("line {no}: invalid LUT size"),
                            ));
                        }
                        let n: i64 = values[0].parse().map_err(|_| {
                            CoreError::new(
                                "value_error",
                                format!("invalid literal for int() with base 10: {:?}", values[0]),
                            )
                        })?;
                        let max_n = if key == "LUT_1D_SIZE" { MAX_1D } else { MAX_3D } as i64;
                        if !(2..=max_n).contains(&n) {
                            return Err(CoreError::new(
                                "value_error",
                                format!("line {no}: LUT size out of range"),
                            ));
                        }
                        let nu = usize::try_from(n)
                            .map_err(|_| CoreError::new("value_error", "LUT size out of range"))?;
                        if key == "LUT_1D_SIZE" {
                            hdr.n1 = Some(nu);
                        } else {
                            hdr.n3 = Some(nu);
                        }
                    }
                    "DOMAIN_MIN" | "DOMAIN_MAX" => {
                        if values.len() != 3 {
                            return Err(CoreError::new(
                                "value_error",
                                format!("line {no}: invalid {key}"),
                            ));
                        }
                        let mut arr = [0.0f64; 3];
                        for (i, s) in values.iter().enumerate() {
                            let v = parse_py_float(s).map_err(|_| {
                                CoreError::new(
                                    "value_error",
                                    format!("could not convert string to float: {s:?}"),
                                )
                            })?;
                            if !v.is_finite() {
                                return Err(CoreError::new(
                                    "value_error",
                                    format!("line {no}: non-finite header"),
                                ));
                            }
                            arr[i] = v;
                        }
                        if key == "DOMAIN_MIN" {
                            hdr.domain_min = Some(arr);
                        } else {
                            hdr.domain_max = Some(arr);
                        }
                    }
                    "LUT_1D_INPUT_RANGE" | "LUT_3D_INPUT_RANGE" => {
                        if values.len() != 2 {
                            return Err(CoreError::new(
                                "value_error",
                                format!("line {no}: invalid {key}"),
                            ));
                        }
                        let mut arr = [0.0f64; 2];
                        for (i, s) in values.iter().enumerate() {
                            let v = parse_py_float(s).map_err(|_| {
                                CoreError::new(
                                    "value_error",
                                    format!("could not convert string to float: {s:?}"),
                                )
                            })?;
                            if !v.is_finite() {
                                return Err(CoreError::new(
                                    "value_error",
                                    format!("line {no}: non-finite header"),
                                ));
                            }
                            arr[i] = v;
                        }
                        if key == "LUT_1D_INPUT_RANGE" {
                            hdr.range1 = Some(arr);
                        } else {
                            hdr.range3 = Some(arr);
                        }
                    }
                    _ => {}
                }
                continue;
            }

            let mut row = Vec::with_capacity(words.len());
            for s in &words {
                let v = parse_py_float(s).map_err(|_| {
                    CoreError::new(
                        "value_error",
                        format!("line {no}: unsupported directive {key}"),
                    )
                })?;
                row.push(v);
            }
            if row.len() != 3 || !row.iter().all(|v| v.is_finite()) {
                return Err(CoreError::new(
                    "value_error",
                    format!("line {no}: expected three finite numbers"),
                ));
            }
            let n1 = hdr.n1.unwrap_or(0);
            let n3 = hdr.n3.unwrap_or(0);
            let expected = expected_rows(n1, n3)?;
            if (n1 == 0 && n3 == 0) || data.len() >= expected {
                return Err(CoreError::new(
                    "value_error",
                    format!("line {no}: unexpected data row"),
                ));
            }
            data.push([row[0], row[1], row[2]]);
        }

        let n1 = hdr.n1.unwrap_or(0);
        let n3 = hdr.n3.unwrap_or(0);
        let expected = expected_rows(n1, n3)?;
        if (n1 == 0 && n3 == 0) || data.len() != expected {
            return Err(CoreError::new("value_error", "LUT 数据行数与声明不符"));
        }

        let lo = hdr.domain_min.unwrap_or([0.0, 0.0, 0.0]);
        let hi = hdr.domain_max.unwrap_or([1.0, 1.0, 1.0]);
        if (0..3).any(|c| hi[c] <= lo[c]) {
            return Err(CoreError::new(
                "value_error",
                "DOMAIN_MAX 必须大于 DOMAIN_MIN",
            ));
        }
        let r1 = hdr.range1.unwrap_or([0.0, 1.0]);
        let r3 = hdr.range3.unwrap_or([0.0, 1.0]);
        if r1[1] <= r1[0] || r3[1] <= r3[0] {
            return Err(CoreError::new("value_error", "INPUT_RANGE 必须递增"));
        }
        if (hdr.domain_min.is_some() || hdr.domain_max.is_some())
            && (hdr.range1.is_some() || hdr.range3.is_some())
        {
            return Err(CoreError::new(
                "value_error",
                "DOMAIN 与 INPUT_RANGE 同时声明时含义不明确，拒绝猜测",
            ));
        }

        let table1 = if n1 > 0 {
            Some(data[..n1].to_vec())
        } else {
            None
        };
        let table3 = if n3 > 0 {
            Some(Table3 {
                size: n3,
                rows: data[n1..].to_vec(),
            })
        } else {
            None
        };

        Ok(Self {
            table1,
            table3,
            domain_min: lo,
            domain_max: hi,
            range1: r1,
            range3: r3,
            title: hdr.title.unwrap_or_default(),
            source_sha256: digest.to_string(),
            comments,
        })
    }

    /// Port of gxr_studio/lut/cube.py:136 Cube.apply.
    /// Returns `(output, input_clipped_share)`.
    pub fn apply(&self, rgb: &[[f64; 3]]) -> Result<(Vec<[f64; 3]>, f64), CoreError> {
        for px in rgb {
            if !px.iter().all(|v| v.is_finite()) {
                return Err(CoreError::new("value_error", "LUT 输入必须为有限 RGB"));
            }
        }
        let n = rgb.len();
        let d_span = [
            self.domain_max[0] - self.domain_min[0],
            self.domain_max[1] - self.domain_min[1],
            self.domain_max[2] - self.domain_min[2],
        ];
        let mut x: Vec<[f64; 3]> = rgb
            .iter()
            .map(|px| {
                [
                    (px[0] - self.domain_min[0]) / d_span[0],
                    (px[1] - self.domain_min[1]) / d_span[1],
                    (px[2] - self.domain_min[2]) / d_span[2],
                ]
            })
            .collect();
        let mut clipped = vec![false; n];

        if let Some(t1) = &self.table1 {
            let r1_span = self.range1[1] - self.range1[0];
            let m = t1.len();
            let nodes = linspace_0_1(m);
            for (i, px) in x.iter_mut().enumerate() {
                for (c, val) in px.iter_mut().enumerate() {
                    let v = (*val - self.range1[0]) / r1_span;
                    if !(0.0..=1.0).contains(&v) {
                        clipped[i] = true;
                    }
                    *val = interp_1d(v, &nodes, t1, c);
                }
            }
        }

        if let Some(t3) = &self.table3 {
            let r3_span = self.range3[1] - self.range3[0];
            for (i, px) in x.iter_mut().enumerate() {
                for v in px.iter_mut() {
                    let norm = (*v - self.range3[0]) / r3_span;
                    if !(0.0..=1.0).contains(&norm) {
                        clipped[i] = true;
                    }
                    *v = norm.clamp(0.0, 1.0);
                }
            }
            x = Self::tetrahedral_table(t3, &x);
        }

        let share = if n == 0 {
            0.0
        } else {
            let count = clipped.iter().filter(|&&b| b).count();
            count as f64 / n as f64
        };
        Ok((x, share))
    }

    /// Port of gxr_studio/lut/cube.py:156 Cube.tetrahedral (`x` already clipped to `[0, 1]`).
    pub fn tetrahedral(&self, x: &[[f64; 3]]) -> Result<Vec<[f64; 3]>, CoreError> {
        let Some(t3) = &self.table3 else {
            return Err(CoreError::new("value_error", "LUT 没有 3D 表"));
        };
        Ok(Self::tetrahedral_table(t3, x))
    }

    fn tetrahedral_table(t3: &Table3, x: &[[f64; 3]]) -> Vec<[f64; 3]> {
        let n = t3.size;
        let max_idx = n - 2;
        let nf = (n - 1) as f64;
        let mut out = Vec::with_capacity(x.len());

        for px in x {
            let pos = [px[0] * nf, px[1] * nf, px[2] * nf];
            // `px` is clipped to [0, 1], so `pos >= 0` and truncation toward zero is exact.
            #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
            let base = [
                (pos[0] as usize).min(max_idx),
                (pos[1] as usize).min(max_idx),
                (pos[2] as usize).min(max_idx),
            ];
            let f = [
                pos[0] - (base[0] as f64),
                pos[1] - (base[1] as f64),
                pos[2] - (base[2] as f64),
            ];
            // Stable argsort(-f): ties preserve lower channel index (0=R, 1=G, 2=B).
            let mut order = [0usize, 1, 2];
            if f[order[0]] < f[order[1]] {
                order.swap(0, 1);
            }
            if f[order[1]] < f[order[2]] {
                order.swap(1, 2);
            }
            if f[order[0]] < f[order[1]] {
                order.swap(0, 1);
            }

            let mut b = base;
            let v0 = t3.at(b[0], b[1], b[2]);
            b[order[0]] += 1;
            let v1 = t3.at(b[0], b[1], b[2]);
            b[order[1]] += 1;
            let v2 = t3.at(b[0], b[1], b[2]);
            b[order[2]] += 1;
            let v3 = t3.at(b[0], b[1], b[2]);

            let u0 = f[order[0]];
            let u1 = f[order[1]];
            let u2 = f[order[2]];
            out.push([
                v0[0] + u0 * (v1[0] - v0[0]) + u1 * (v2[0] - v1[0]) + u2 * (v3[0] - v2[0]),
                v0[1] + u0 * (v1[1] - v0[1]) + u1 * (v2[1] - v1[1]) + u2 * (v3[1] - v2[1]),
                v0[2] + u0 * (v1[2] - v0[2]) + u1 * (v2[2] - v1[2]) + u2 * (v3[2] - v2[2]),
            ]);
        }
        out
    }

    pub fn size_1d(&self) -> usize {
        self.table1.as_ref().map_or(0, Vec::len)
    }

    pub fn size_3d(&self) -> usize {
        self.table3.as_ref().map_or(0, |t| t.size)
    }

    pub fn kind(&self) -> String {
        match (self.table1.is_some(), self.table3.is_some()) {
            (true, true) => "1D+3D".to_string(),
            (true, false) => "1D".to_string(),
            (false, true) => "3D".to_string(),
            (false, false) => String::new(),
        }
    }

    pub fn summary_json(&self) -> Json {
        let shape_1d = self
            .table1
            .as_ref()
            .map(|t| Json::ints(&[t.len() as i64, 3]));
        let shape_3d = self.table3.as_ref().map(|t| {
            let n = t.size as i64;
            Json::ints(&[n, n, n, 3])
        });
        Json::object([
            ("title", Json::str(&self.title)),
            ("sha256", Json::str(&self.source_sha256)),
            ("kind", Json::str(self.kind())),
            ("size_1d", Json::Int(self.size_1d() as i64)),
            ("size_3d", Json::Int(self.size_3d() as i64)),
            ("shape_1d", Json::opt(shape_1d)),
            ("shape_3d", Json::opt(shape_3d)),
            ("domain_min", Json::floats(&self.domain_min)),
            ("domain_max", Json::floats(&self.domain_max)),
            ("range_1d", Json::floats(&self.range1)),
            ("range_3d", Json::floats(&self.range3)),
            (
                "comments",
                Json::Array(self.comments.iter().map(Json::str).collect()),
            ),
        ])
    }
}

fn expected_rows(n1: usize, n3: usize) -> Result<usize, CoreError> {
    n3.checked_mul(n3)
        .and_then(|v| v.checked_mul(n3))
        .and_then(|v| v.checked_add(n1))
        .ok_or_else(|| CoreError::new("value_error", "LUT size overflow"))
}

fn linspace_0_1(num: usize) -> Vec<f64> {
    if num == 0 {
        return Vec::new();
    }
    if num == 1 {
        return vec![0.0];
    }
    let step = 1.0 / ((num - 1) as f64);
    let mut out = Vec::with_capacity(num);
    for i in 0..num - 1 {
        out.push((i as f64) * step);
    }
    out.push(1.0);
    out
}

/// Port of numpy's `np.interp(x, xp, fp[:, channel])` for strictly increasing `xp` on `[0, 1]`.
fn interp_1d(x: f64, xp: &[f64], fp: &[[f64; 3]], channel: usize) -> f64 {
    if x.is_nan() {
        return f64::NAN;
    }
    let len = xp.len();
    if x >= xp[len - 1] {
        return fp[len - 1][channel];
    }
    if x < xp[0] {
        return fp[0][channel];
    }
    let idx = xp.partition_point(|&v| v <= x);
    let j = idx.saturating_sub(1).min(len - 2);
    if x == xp[j] {
        return fp[j][channel];
    }
    let slope = (fp[j + 1][channel] - fp[j][channel]) / (xp[j + 1] - xp[j]);
    let val = slope * (x - xp[j]) + fp[j][channel];
    if val.is_finite() {
        val
    } else {
        let alt = slope * (x - xp[j + 1]) + fp[j + 1][channel];
        if alt.is_finite() { alt } else { fp[j][channel] }
    }
}
