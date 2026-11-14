//! Port of `gxr_studio/lut/identify.py` and `gxr_studio/lut/page.py` (`input_choice`,
//! `_undeclared_choice`).

use gxr_raw::Json;

use crate::cube::Cube;
use crate::identify_scan::{
    FieldDir, OrderedMap, consume_ci, find_ci, find_field_matches, find_first_direction,
    has_space_hint, is_attribution_comment, match_alchemy_suffix, output_spaces, posix_stem,
    search_explicit_input, spaces_in_text, split_domain_first, split_last_direction, ws_end,
};
use crate::spaces::is_display;

/// Port of gxr_studio/lut/identify.py:14 IDENTIFICATION_VERSION.
pub const IDENTIFICATION_VERSION: i64 = 2;

/// Content-based gray-axis metrics (`identify.py:135 content_kind`).
#[derive(Clone, Debug, PartialEq)]
pub struct ContentEvidence {
    pub black: Option<f64>,
    pub white: Option<f64>,
    pub middle: Option<f64>,
    pub mid_slope: Option<f64>,
    pub channel_spread: Option<f64>,
}

/// Result of `identify` + `input_choice`.
#[derive(Clone, Debug, PartialEq)]
pub struct IdentifyResult {
    pub identification_version: i64,
    pub input_space: Option<String>,
    pub input_space_state: String,
    pub input_space_manual: bool,
    pub input_space_guess: Option<String>,
    pub guess_reason: Option<String>,
    pub guess_note: Option<String>,
    pub keyword: Option<String>,
    pub content: Option<String>,
    pub input_declarations: Vec<String>,
    pub output_evidence: OrderedMap,
    pub content_evidence: ContentEvidence,
    pub output_space: String,
    pub output_space_state: String,
    pub technical: bool,
    pub default_input_space: Option<String>,
    pub default_input_source: String,
    pub default_input_note: String,
}

impl IdentifyResult {
    pub fn to_json(&self) -> Json {
        let opt_str = |s: &Option<String>| Json::opt(s.as_deref().map(Json::str));
        let opt_f64 = |v: Option<f64>| match v {
            Some(x) => Json::Float(x),
            None => Json::Null,
        };
        let evidence = Json::object([
            ("keyword", opt_str(&self.keyword)),
            ("content", opt_str(&self.content)),
            (
                "input_declarations",
                Json::Array(self.input_declarations.iter().map(Json::str).collect()),
            ),
            ("output", self.output_evidence.to_json()),
            ("black", opt_f64(self.content_evidence.black)),
            ("white", opt_f64(self.content_evidence.white)),
            ("middle", opt_f64(self.content_evidence.middle)),
            ("mid_slope", opt_f64(self.content_evidence.mid_slope)),
            (
                "channel_spread",
                opt_f64(self.content_evidence.channel_spread),
            ),
        ]);
        Json::object([
            (
                "identification_version",
                Json::Int(self.identification_version),
            ),
            ("input_space", opt_str(&self.input_space)),
            ("input_space_state", Json::str(&self.input_space_state)),
            ("input_space_manual", Json::Bool(self.input_space_manual)),
            ("input_space_guess", opt_str(&self.input_space_guess)),
            ("guess_reason", opt_str(&self.guess_reason)),
            ("guess_note", opt_str(&self.guess_note)),
            ("space_evidence", evidence),
            ("output_space", Json::str(&self.output_space)),
            ("output_space_state", Json::str(&self.output_space_state)),
            ("technical", Json::Bool(self.technical)),
            ("default_input_space", opt_str(&self.default_input_space)),
            (
                "default_input_source",
                Json::str(&self.default_input_source),
            ),
            ("default_input_note", Json::str(&self.default_input_note)),
        ])
    }
}

/// Port of gxr_studio/lut/identify.py:57 _declarations.
fn declarations(cube: &Cube) -> (Vec<String>, Vec<String>, Vec<String>) {
    let mut inputs = Vec::new();
    let mut outputs = Vec::new();
    let mut other = Vec::new();
    for comment in &cube.comments {
        if let Some(after) = consume_ci(comment, 0, "LUMIXPHOTOSTYLE") {
            let rest_start = ws_end(comment, after);
            if rest_start > after && rest_start < comment.len() {
                inputs.push(comment[rest_start..].to_string());
                continue;
            }
        }
        let fields = find_field_matches(comment);
        if fields.is_empty() {
            other.push(comment.clone());
        }
        for (idx, m) in fields.iter().enumerate() {
            let end = fields.get(idx + 1).map_or(comment.len(), |n| n.start);
            let mut value = comment[m.end..end].to_string();
            if m.has_gamma {
                value = format!("Gamma {value}");
            }
            if m.dir == FieldDir::Input {
                inputs.push(value);
            } else {
                outputs.push(value);
            }
        }
    }
    (inputs, outputs, other)
}

/// Port of gxr_studio/lut/identify.py:78 _named_input.
fn named_input(name: &str, title: &str) -> OrderedMap {
    let stem = posix_stem(name);
    let named = [title, stem.as_str()];
    let mut marked = OrderedMap::default();
    for text in named {
        if let Some(after) = search_explicit_input(text) {
            let domain = split_domain_first(after);
            marked.extend_from(spaces_in_text(domain));
        }
    }
    if let Some((_, end)) = find_ci(&stem, "_to_") {
        let dest = &stem[end..];
        if let Some(log_word) = match_alchemy_suffix(dest) {
            marked.extend_from(spaces_in_text(log_word));
        }
    }
    if !marked.is_empty() {
        return marked;
    }
    let mut directional = OrderedMap::default();
    for text in named {
        if let Some((start, _)) = find_first_direction(text) {
            directional.extend_from(spaces_in_text(&text[..start]));
        }
    }
    if !directional.is_empty() {
        return directional;
    }
    let mut found = OrderedMap::default();
    for text in named {
        found.extend_from(spaces_in_text(text));
    }
    let mut logs = OrderedMap::default();
    for (space, word) in &found.0 {
        if !is_display(space) {
            logs.insert(space, word);
        }
    }
    if logs.is_empty() { found } else { logs }
}

/// Port of gxr_studio/lut/identify.py:110 guess_space.
pub fn guess_space(name: &str, title: &str) -> (Option<String>, Option<String>) {
    let found = named_input(name, title);
    if found.len() == 1 {
        let (space, word) = &found.0[0];
        return (
            Some(space.clone()),
            Some(format!("文件名/标题里有“{word}”")),
        );
    }
    (None, None)
}

/// Port of gxr_studio/lut/identify.py:118 _output.
fn identify_output(
    cube: &Cube,
    filename: &str,
    declarations: &[String],
    other: &[String],
) -> (String, String, OrderedMap) {
    let mut found = OrderedMap::default();
    let state = if !declarations.is_empty() {
        for text in declarations {
            found.extend_from(output_spaces(text));
        }
        "known"
    } else {
        let stem = posix_stem(filename);
        for text in std::iter::once(cube.title.as_str())
            .chain(std::iter::once(stem.as_str()))
            .chain(other.iter().map(String::as_str))
        {
            let destination = split_last_direction(text);
            found.extend_from(output_spaces(destination));
        }
        "guessed"
    };
    if found.len() == 1 {
        return (found.0[0].0.clone(), state.to_string(), found);
    }
    let fallback_state = if found.len() > 1 {
        "conflict"
    } else {
        "unknown"
    };
    ("srgb".to_string(), fallback_state.to_string(), found)
}

/// Port of gxr_studio/lut/identify.py:135 content_kind.
pub fn content_kind(cube: &Cube) -> (Option<String>, ContentEvidence) {
    let empty_info = ContentEvidence {
        black: None,
        white: None,
        middle: None,
        mid_slope: None,
        channel_spread: None,
    };
    let mut probes = Vec::with_capacity(33);
    for i in 0..33 {
        let v = if i == 32 {
            1.0
        } else {
            (i as f64) * (1.0 / 32.0)
        };
        probes.push([v, v, v]);
    }
    let Ok((gray, _)) = cube.apply(&probes) else {
        return (None, empty_info);
    };
    let mut y = [0.0f64; 33];
    let mut spread: f64 = 0.0;
    for (i, px) in gray.iter().enumerate() {
        if !px.iter().all(|v| v.is_finite()) {
            return (None, empty_info);
        }
        let mean = (px[0] + px[1] + px[2]) / 3.0;
        if !mean.is_finite() {
            return (None, empty_info);
        }
        y[i] = mean;
        let mx = px[0].max(px[1]).max(px[2]);
        let mn = px[0].min(px[1]).min(px[2]);
        let ptp = mx - mn;
        if !ptp.is_finite() {
            return (None, empty_info);
        }
        spread = spread.max(ptp);
    }
    let mut slopes = [0.0f64; 32];
    for i in 0..32 {
        let s = (y[i + 1] - y[i]) * 32.0;
        if !s.is_finite() {
            return (None, empty_info);
        }
        slopes[i] = s;
    }
    let mid_slope = (y[18] - y[14]) * 8.0;
    if !mid_slope.is_finite() {
        return (None, empty_info);
    }
    let info = ContentEvidence {
        black: Some(y[0]),
        white: Some(y[32]),
        middle: Some(y[16]),
        mid_slope: Some(mid_slope),
        channel_spread: Some(spread),
    };
    let min_slope = slopes.iter().copied().fold(f64::INFINITY, f64::min);
    if min_slope < -0.01 || spread > 0.08 || y[0].abs() > 0.06 || !(0.80..=1.20).contains(&y[32]) {
        return (None, info);
    }
    let low_mean = (slopes[0] + slopes[1] + slopes[2] + slopes[3]) / 4.0;
    let high_mean = (slopes[28] + slopes[29] + slopes[30] + slopes[31]) / 4.0;
    if mid_slope > 1.5 && low_mean < 0.45 && high_mean < 0.45 {
        return (None, info);
    }
    if mid_slope > 1.5 {
        return (Some("log".to_string()), info);
    }
    if (0.40..=0.55).contains(&y[16]) {
        return (Some("display".to_string()), info);
    }
    (None, info)
}

/// Port of gxr_studio/lut/identify.py:204 _technical.
fn technical(cube: &Cube) -> bool {
    let values = [0.125, 0.375, 0.625, 0.875];
    let mut probes = Vec::with_capacity(64);
    for &r in &values {
        for &g in &values {
            for &b in &values {
                probes.push([r, g, b]);
            }
        }
    }
    let ref_probes = [
        [values[0], values[0], values[0]],
        [values[1], values[1], values[1]],
        [values[2], values[2], values[2]],
        [values[3], values[3], values[3]],
    ];
    let Ok((out, _)) = cube.apply(&probes) else {
        return false;
    };
    let Ok((reference, _)) = cube.apply(&ref_probes) else {
        return false;
    };
    if !out.iter().flatten().all(|v| v.is_finite())
        || !reference.iter().flatten().all(|v| v.is_finite())
    {
        return false;
    }
    let ref_max = reference
        .iter()
        .flatten()
        .copied()
        .fold(f64::NEG_INFINITY, f64::max);
    let ref_min = reference
        .iter()
        .flatten()
        .copied()
        .fold(f64::INFINITY, f64::min);
    if ref_max > 1.2 || ref_min < -0.1 {
        return false;
    }
    let max_ptp = reference
        .iter()
        .map(|px| px[0].max(px[1]).max(px[2]) - px[0].min(px[1]).min(px[2]))
        .fold(0.0f64, f64::max);
    if max_ptp > 0.015 {
        return false;
    }
    for pair in reference.windows(2) {
        if (0..3).any(|c| pair[1][c] - pair[0][c] < -0.001) {
            return false;
        }
    }
    for channel in 0..3 {
        for (i, &value) in values.iter().enumerate() {
            let mut max_diff = 0.0f64;
            for (p_idx, probe) in probes.iter().enumerate() {
                if probe[channel] == value {
                    max_diff = max_diff.max((out[p_idx][channel] - reference[i][channel]).abs());
                }
            }
            if max_diff > 0.005 {
                return false;
            }
        }
    }
    true
}

/// Port of gxr_studio/lut/page.py:28 _undeclared_choice.
pub fn undeclared_choice(
    cube: &Cube,
    filename: &str,
    state: &str,
    content: Option<&str>,
    input_declarations: &[String],
) -> (Option<String>, String, String) {
    if state == "conflict" {
        return (
            None,
            "required".to_string(),
            "文件里的颜色信息不一致，请按 LUT 的说明选择。".to_string(),
        );
    }
    if !input_declarations.is_empty() {
        return (
            None,
            "required".to_string(),
            "暂时无法识别文件写明的输入颜色，请按 LUT 的说明选择。".to_string(),
        );
    }
    let naming = format!("{filename} {}", cube.title);
    if has_space_hint(&naming) && guess_space(filename, &cube.title).0.is_none() {
        return (
            None,
            "required".to_string(),
            "无法确定这个 LUT 的输入颜色，请按 LUT 的说明选择。".to_string(),
        );
    }
    let comments: Vec<&str> = cube
        .comments
        .iter()
        .map(String::as_str)
        .filter(|text| {
            !find_field_matches(text)
                .iter()
                .any(|m| m.dir == FieldDir::Output)
                && !is_attribution_comment(text)
        })
        .collect();
    let mut guesses: Vec<String> = Vec::new();
    for text in &comments {
        if let (Some(space), _) = guess_space("", text)
            && !guesses.contains(&space)
        {
            guesses.push(space);
        }
    }
    if guesses.len() == 1 {
        let space = guesses[0].clone();
        let kind = if is_display(&space) { "display" } else { "log" };
        if content.is_none() || content == Some(kind) {
            return (
                Some(space),
                "guessed".to_string(),
                "根据文件注释推测，请核对 LUT 的说明。".to_string(),
            );
        }
    }
    let mut combined = format!("{filename} {}", cube.title);
    for c in &comments {
        combined.push(' ');
        combined.push_str(c);
    }
    if !guesses.is_empty() || content == Some("log") || has_space_hint(&combined) {
        return (
            None,
            "required".to_string(),
            "无法确定这个 LUT 的输入颜色，请按 LUT 的说明选择。".to_string(),
        );
    }
    (
        Some("srgb".to_string()),
        "fallback".to_string(),
        "文件没有写明输入颜色，先用创意 LUT 常用的 sRGB（Rec.709 色域）。请核对 LUT 的说明。"
            .to_string(),
    )
}

/// Port of gxr_studio/lut/identify.py:162 identify and gxr_studio/lut/page.py:13 input_choice.
pub fn identify(cube: &Cube, filename: &str) -> IdentifyResult {
    let (inputs, outputs, other) = declarations(cube);
    let named = named_input(filename, &cube.title);
    let mut declared = OrderedMap::default();
    let mut unsupported = false;
    for text in &inputs {
        let found = spaces_in_text(text);
        if found.is_empty() {
            unsupported = true;
        }
        declared.extend_from(found);
    }
    let candidates = if inputs.is_empty() { &named } else { &declared };
    let keyword = if candidates.len() == 1 {
        Some(candidates.0[0].0.clone())
    } else {
        None
    };
    let reason = if !inputs.is_empty() {
        Some(format!("输入色彩空间：{}", inputs.join(" / ")))
    } else {
        guess_space(filename, &cube.title).1
    };
    let (content, evidence) = content_kind(cube);
    let keyword_kind = keyword
        .as_deref()
        .map(|k| if is_display(k) { "display" } else { "log" });

    let (state, selected) = if !inputs.is_empty() {
        if keyword.is_some() && !unsupported {
            ("known".to_string(), keyword.clone())
        } else if !declared.is_empty() {
            ("conflict".to_string(), None)
        } else {
            ("unknown".to_string(), None)
        }
    } else if candidates.len() > 1 {
        ("conflict".to_string(), None)
    } else if let (Some(kw), Some(cnt)) = (&keyword, &content) {
        if keyword_kind == Some(cnt.as_str()) {
            ("known".to_string(), Some(kw.clone()))
        } else {
            ("conflict".to_string(), None)
        }
    } else if let Some(kw) = &keyword {
        ("guessed".to_string(), Some(kw.clone()))
    } else {
        ("unknown".to_string(), None)
    };

    let (output_space, output_space_state, output_evidence) =
        identify_output(cube, filename, &outputs, &other);
    let guess_note = if selected.is_none() {
        Some("请选择输入色彩空间".to_string())
    } else {
        None
    };

    let (default_input_space, default_input_source, default_input_note) =
        if let Some(space) = &selected {
            if !inputs.is_empty() {
                (
                    Some(space.clone()),
                    "declared".to_string(),
                    "按 LUT 文件写明的输入色彩空间选择。".to_string(),
                )
            } else {
                (
                    Some(space.clone()),
                    "guessed".to_string(),
                    "根据文件名、标题或注释推测，请核对 LUT 的说明。".to_string(),
                )
            }
        } else {
            undeclared_choice(cube, filename, &state, content.as_deref(), &inputs)
        };

    IdentifyResult {
        identification_version: IDENTIFICATION_VERSION,
        input_space: selected,
        input_space_state: state,
        input_space_manual: false,
        input_space_guess: keyword.clone(),
        guess_reason: reason,
        guess_note,
        keyword,
        content,
        input_declarations: inputs,
        output_evidence,
        content_evidence: evidence,
        output_space,
        output_space_state,
        technical: technical(cube),
        default_input_space,
        default_input_source,
        default_input_note,
    }
}
