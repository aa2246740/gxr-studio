//! Hand-written pattern scanners for `gxr_studio/lut/identify.py` and `lut/page.py`.

use crate::spaces::is_display;

/// Insertion-ordered string map (mirrors Python `dict[str, str]` key order).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct OrderedMap(pub Vec<(String, String)>);

impl OrderedMap {
    pub fn insert(&mut self, key: &str, val: &str) {
        if let Some((_, existing)) = self.0.iter_mut().find(|(k, _)| k == key) {
            *existing = val.to_string();
        } else {
            self.0.push((key.to_string(), val.to_string()));
        }
    }

    pub fn extend_from(&mut self, other: Self) {
        for (k, v) in other.0 {
            self.insert(&k, &v);
        }
    }

    pub fn remove(&mut self, key: &str) {
        self.0.retain(|(k, _)| k != key);
    }

    pub fn len(&self) -> usize {
        self.0.len()
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub fn to_json(&self) -> gxr_raw::Json {
        gxr_raw::Json::object(
            self.0
                .iter()
                .map(|(k, v)| (k.as_str(), gxr_raw::Json::str(v.as_str()))),
        )
    }
}

#[inline]
pub fn is_ascii_alnum(b: u8) -> bool {
    b.is_ascii_alphanumeric()
}

#[inline]
pub fn is_py_word_char(ch: char) -> bool {
    ch.is_alphanumeric() || ch == '_'
}

fn prev_char(s: &str, byte_idx: usize) -> Option<char> {
    s[..byte_idx].chars().next_back()
}

fn next_char(s: &str, byte_idx: usize) -> Option<char> {
    s[byte_idx..].chars().next()
}

pub fn starts_ci(s: &str, pat: &str) -> bool {
    s.len() >= pat.len()
        && s.is_char_boundary(pat.len())
        && s[..pat.len()].eq_ignore_ascii_case(pat)
}

pub fn consume_ci(s: &str, pos: usize, pat: &str) -> Option<usize> {
    if starts_ci(&s[pos..], pat) {
        Some(pos + pat.len())
    } else {
        None
    }
}

fn consume_opt_sep(s: &str, pos: usize, seps: &[u8]) -> usize {
    if pos < s.len() && seps.contains(&s.as_bytes()[pos]) {
        pos + 1
    } else {
        pos
    }
}

fn consume_many_bytes(s: &str, mut pos: usize, seps: &[u8]) -> usize {
    while pos < s.len() && seps.contains(&s.as_bytes()[pos]) {
        pos += 1;
    }
    pos
}

pub fn ws_end(s: &str, start: usize) -> usize {
    for (off, ch) in s[start..].char_indices() {
        if !ch.is_whitespace() {
            return start + off;
        }
    }
    s.len()
}

/// Remove `(?:rec[ ._-]*709|srgb)\s+(?:primaries|gamut)` case-insensitively.
fn strip_primaries_gamut(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < text.len() {
        let mut matched_end = None;
        if let Some(after_prefix) = consume_ci(text, i, "rec")
            .map(|p| consume_many_bytes(text, p, b" ._-"))
            .and_then(|p| consume_ci(text, p, "709"))
            .or_else(|| consume_ci(text, i, "srgb"))
        {
            let ws = ws_end(text, after_prefix);
            if ws > after_prefix
                && let Some(end) =
                    consume_ci(text, ws, "primaries").or_else(|| consume_ci(text, ws, "gamut"))
            {
                matched_end = Some(end);
            }
        }
        if let Some(end) = matched_end {
            i = end;
        } else if let Some(ch) = text[i..].chars().next() {
            out.push(ch);
            i += ch.len_utf8();
        } else {
            break;
        }
    }
    out
}

fn match_space_pattern(text: &str, pos: usize, idx: usize) -> Option<usize> {
    let b = text.as_bytes();
    match idx {
        // s[-_ ]?log3[-_ ]s[-_ ]?gamut3(?!cine)
        0 => {
            let p = consume_ci(text, pos, "s")?;
            let p = consume_opt_sep(text, p, b"-_ ");
            let p = consume_ci(text, p, "log3")?;
            if p >= b.len() || !b"-_ ".contains(&b[p]) {
                return None;
            }
            let p = consume_ci(text, p + 1, "s")?;
            let p = consume_opt_sep(text, p, b"-_ ");
            let p = consume_ci(text, p, "gamut3")?;
            if starts_ci(&text[p..], "cine") {
                return None;
            }
            Some(p)
        }
        // f[-_ ]?log2
        1 => {
            let p = consume_ci(text, pos, "f")?;
            let p = consume_opt_sep(text, p, b"-_ ");
            consume_ci(text, p, "log2")
        }
        // f[-_ ]?log
        2 => {
            let p = consume_ci(text, pos, "f")?;
            let p = consume_opt_sep(text, p, b"-_ ");
            consume_ci(text, p, "log")
        }
        // l[-_ ]?log709
        3 => {
            let p = consume_ci(text, pos, "l")?;
            let p = consume_opt_sep(text, p, b"-_ ");
            consume_ci(text, p, "log709")
        }
        // l[-_ ]?log
        4 => {
            let p = consume_ci(text, pos, "l")?;
            let p = consume_opt_sep(text, p, b"-_ ");
            consume_ci(text, p, "log")
        }
        // v[-_ ]?log
        5 => {
            let p = consume_ci(text, pos, "v")?;
            let p = consume_opt_sep(text, p, b"-_ ");
            consume_ci(text, p, "log")
        }
        // s[-_ ]?log3
        6 => {
            let p = consume_ci(text, pos, "s")?;
            let p = consume_opt_sep(text, p, b"-_ ");
            consume_ci(text, p, "log3")
        }
        // apple[-_ ]?log
        7 => {
            let p = consume_ci(text, pos, "apple")?;
            let p = consume_opt_sep(text, p, b"-_ ");
            consume_ci(text, p, "log")
        }
        // srgb
        8 => consume_ci(text, pos, "srgb"),
        // (?:rec|bt)[ ._-]*709
        9 => {
            let p = consume_ci(text, pos, "rec").or_else(|| consume_ci(text, pos, "bt"))?;
            let p = consume_many_bytes(text, p, b" ._-");
            consume_ci(text, p, "709")
        }
        // gamma[ _-]*2[._ ]?4|bt[ ._-]*1886
        10 => {
            if let Some(p) = consume_ci(text, pos, "gamma") {
                let p = consume_many_bytes(text, p, b" _-");
                let p = consume_ci(text, p, "2")?;
                let p = consume_opt_sep(text, p, b"._ ");
                consume_ci(text, p, "4")
            } else {
                let p = consume_ci(text, pos, "bt")?;
                let p = consume_many_bytes(text, p, b" ._-");
                consume_ci(text, p, "1886")
            }
        }
        // linear
        11 => consume_ci(text, pos, "linear"),
        _ => None,
    }
}

const PATTERN_SPACES: [&str; 12] = [
    "slog3-sgamut3",
    "flog2",
    "flog",
    "llog709",
    "llog",
    "vlog",
    "slog3",
    "applelog",
    "srgb",
    "rec709",
    "gamma24",
    "linear",
];

/// Port of gxr_studio/lut/identify.py:35 _spaces.
pub fn spaces_in_text(raw_text: &str) -> OrderedMap {
    let text = strip_primaries_gamut(raw_text);
    let b = text.as_bytes();
    let mut found = OrderedMap::default();
    let mut occupied: Vec<(usize, usize)> = Vec::new();
    for (pat_idx, &space) in PATTERN_SPACES.iter().enumerate() {
        let mut pos = 0;
        while pos < text.len() {
            if !text.is_char_boundary(pos) {
                pos += 1;
                continue;
            }
            let prev_ok = pos == 0 || !is_ascii_alnum(b[pos - 1]);
            if prev_ok
                && let Some(end) = match_space_pattern(&text, pos, pat_idx)
                && (end == text.len() || !is_ascii_alnum(b[end]))
            {
                if !occupied.iter().any(|&(s, e)| s <= pos && pos < e) {
                    found.insert(space, &text[pos..end]);
                    occupied.push((pos, end));
                }
                pos = end.max(pos + 1);
                continue;
            }
            pos += text[pos..].chars().next().map_or(1, char::len_utf8);
        }
    }
    found
}

/// Port of gxr_studio/lut/identify.py:49 _output_spaces.
pub fn output_spaces(text: &str) -> OrderedMap {
    let mut found = OrderedMap::default();
    for (space, word) in spaces_in_text(text).0 {
        if is_display(&space) {
            found.insert(&space, &word);
        }
    }
    if found.0.iter().any(|(k, _)| k == "gamma24") {
        found.remove("rec709");
    }
    found
}

pub struct FieldMatch {
    pub start: usize,
    pub end: usize,
    pub is_input: bool,
    pub has_gamma: bool,
}

fn match_field_at(s: &str, pos: usize) -> Option<FieldMatch> {
    let mut p = if pos == 0 {
        0
    } else if s.as_bytes()[pos] == b';' {
        pos + 1
    } else {
        return None;
    };
    p = ws_end(s, p);
    let (is_input, after_io) = if let Some(next) = consume_ci(s, p, "input") {
        (true, next)
    } else if let Some(next) = consume_ci(s, p, "output") {
        (false, next)
    } else {
        return None;
    };

    let mut after_main = after_io;
    let sfx_start = consume_many_bytes(s, after_io, b" _-");
    let suffix_match = consume_ci(s, sfx_start, "colour")
        .or_else(|| consume_ci(s, sfx_start, "color"))
        .and_then(|q| {
            let q = consume_many_bytes(s, q, b" _-");
            consume_ci(s, q, "space")
        })
        .or_else(|| consume_ci(s, sfx_start, "space"))
        .or_else(|| {
            consume_ci(s, sfx_start, "transfer").map(|q| {
                let q2 = consume_many_bytes(s, q, b" _-");
                consume_ci(s, q2, "function").unwrap_or(q)
            })
        })
        .or_else(|| consume_ci(s, sfx_start, "gamma"));
    if let Some(q) = suffix_match
        && next_char(s, q).is_none_or(|c| !is_py_word_char(c))
    {
        after_main = q;
    } else if next_char(s, after_io).is_some_and(is_py_word_char) {
        return None;
    }

    let mut end = ws_end(s, after_main);
    if end < s.len() && (s.as_bytes()[end] == b':' || s.as_bytes()[end] == b'=') {
        end = ws_end(s, end + 1);
    }
    let has_gamma = s[pos..end].to_ascii_lowercase().contains("gamma");
    Some(FieldMatch {
        start: pos,
        end,
        is_input,
        has_gamma,
    })
}

pub fn find_field_matches(s: &str) -> Vec<FieldMatch> {
    let mut out = Vec::new();
    let mut pos = 0;
    while pos < s.len() {
        if !s.is_char_boundary(pos) {
            pos += 1;
            continue;
        }
        if let Some(m) = match_field_at(s, pos) {
            let next = m.end.max(pos + 1);
            out.push(m);
            pos = next;
        } else {
            pos += s[pos..].chars().next().map_or(1, char::len_utf8);
        }
    }
    out
}

pub fn posix_stem(filename: &str) -> String {
    let trimmed = filename.trim_end_matches('/');
    let base = trimmed.rsplit('/').next().unwrap_or(trimmed);
    if base.is_empty() || base == "." || base == ".." {
        return base.to_string();
    }
    match base.rfind('.') {
        Some(idx) if idx > 0 => base[..idx].to_string(),
        _ => base.to_string(),
    }
}

fn match_direction_at(s: &str, pos: usize) -> Option<(usize, usize)> {
    if starts_ci(&s[pos..], "->") {
        return Some((pos, pos + 2));
    }
    if starts_ci(&s[pos..], "_to_") {
        return Some((pos, pos + 4));
    }
    if starts_ci(&s[pos..], "to")
        && prev_char(s, pos).is_none_or(|c| !is_py_word_char(c))
        && next_char(s, pos + 2).is_none_or(|c| !is_py_word_char(c))
    {
        return Some((pos, pos + 2));
    }
    None
}

pub fn find_first_direction(s: &str) -> Option<(usize, usize)> {
    for (idx, _) in s.char_indices() {
        if let Some(m) = match_direction_at(s, idx) {
            return Some(m);
        }
    }
    None
}

pub fn split_last_direction(s: &str) -> &str {
    let mut last_end = 0;
    let mut pos = 0;
    while pos < s.len() {
        if !s.is_char_boundary(pos) {
            pos += 1;
            continue;
        }
        if let Some((_, end)) = match_direction_at(s, pos) {
            last_end = end;
            pos = end;
        } else {
            pos += s[pos..].chars().next().map_or(1, char::len_utf8);
        }
    }
    &s[last_end..]
}

pub fn search_explicit_input(text: &str) -> Option<&str> {
    for (idx, _) in text.char_indices() {
        if prev_char(text, idx).is_some_and(is_py_word_char) {
            continue;
        }
        let after = consume_ci(text, idx, "input").or_else(|| consume_ci(text, idx, "from"));
        let Some(mut p) = after else {
            continue;
        };
        let sep_start = p;
        while p < text.len() && b" :=_-".contains(&text.as_bytes()[p]) {
            p += 1;
        }
        if p > sep_start && p < text.len() {
            return Some(&text[p..]);
        }
    }
    None
}

pub fn split_domain_first(s: &str) -> &str {
    for (idx, ch) in s.char_indices() {
        if ch == ';' || match_direction_at(s, idx).is_some() {
            return &s[..idx];
        }
        if b"_ -".contains(&(ch as u8)) {
            let p = consume_many_bytes(s, idx, b"_ -");
            if p > idx
                && let Some(q) = consume_ci(s, p, "style")
                    .or_else(|| consume_ci(s, p, "look"))
                    .or_else(|| consume_ci(s, p, "target"))
            {
                let q2 = consume_many_bytes(s, q, b" :=_-");
                if q2 > q {
                    return &s[..idx];
                }
            }
        }
    }
    s
}

pub fn match_alchemy_suffix(dest: &str) -> Option<&str> {
    let mut best: Option<&str> = None;
    for (idx, ch) in dest.char_indices() {
        if idx == 0 || (ch != '_' && ch != ' ') {
            continue;
        }
        let log_start = idx + 1;
        let log_end = consume_ci(dest, log_start, "apple")
            .map(|p| consume_opt_sep(dest, p, b"- "))
            .and_then(|p| consume_ci(dest, p, "log"))
            .or_else(|| {
                let first = dest.as_bytes().get(log_start)?;
                if !matches!(first.to_ascii_lowercase(), b'v' | b'f' | b'l' | b's') {
                    return None;
                }
                let p = consume_opt_sep(dest, log_start + 1, b"- ");
                let mut p = consume_ci(dest, p, "log")?;
                if p < dest.len() && dest.as_bytes()[p].is_ascii_digit() {
                    p += 1;
                }
                Some(p)
            });
        let Some(end) = log_end else {
            continue;
        };
        let mut cur = end;
        while cur < dest.len() {
            let sep = dest.as_bytes()[cur];
            if sep != b'_' && sep != b' ' {
                break;
            }
            let sfx = cur + 1;
            if let Some(n) = consume_ci(dest, sfx, "srgb") {
                cur = n;
            } else if let Some(n) = consume_ci(dest, sfx, "rec")
                .and_then(|p| consume_ci(dest, consume_many_bytes(dest, p, b" ._-"), "709"))
            {
                cur = n;
            } else {
                let mut d = sfx;
                while d < dest.len() && dest.as_bytes()[d].is_ascii_digit() {
                    d += 1;
                }
                if d > sfx {
                    cur = d;
                } else {
                    break;
                }
            }
        }
        if cur == dest.len() {
            best = Some(&dest[log_start..end]);
        }
    }
    best
}

pub fn has_space_hint(text: &str) -> bool {
    let b = text.as_bytes();
    let mut pos = 0;
    while pos < text.len() {
        if !text.is_char_boundary(pos) {
            pos += 1;
            continue;
        }
        let prev_ok = pos == 0 || !is_ascii_alnum(b[pos - 1]);
        if prev_ok && match_space_hint_at(text, pos).is_some() {
            return true;
        }
        pos += text[pos..].chars().next().map_or(1, char::len_utf8);
    }
    false
}

fn match_space_hint_at(text: &str, pos: usize) -> Option<usize> {
    let b = text.as_bytes();
    let mut candidates = [pos, pos];
    let mut count = 1usize;
    if let Some(p) = consume_ci(text, pos, "apple").or_else(|| consume_ci(text, pos, "red")) {
        candidates[count] = consume_opt_sep(text, p, b"-_ ");
        count += 1;
    } else if matches!(
        b.get(pos).map(u8::to_ascii_lowercase),
        Some(b'a' | b'f' | b'v' | b's' | b'l' | b'c' | b'd')
    ) {
        candidates[count] = consume_opt_sep(text, pos + 1, b"-_ ");
        count += 1;
    }
    for &start in &candidates[..count] {
        let end = consume_ci(text, start, "log")
            .or_else(|| consume_ci(text, start, "linear"))
            .or_else(|| consume_ci(text, start, "gamma"));
        if let Some(mut p) = end {
            while p < text.len() && is_ascii_alnum(b[p]) {
                p += 1;
            }
            if p == text.len() || !is_ascii_alnum(b[p]) {
                return Some(p);
            }
        }
    }
    None
}

pub fn is_attribution_comment(text: &str) -> bool {
    let trimmed = text.trim_start();
    let after = consume_ci(trimmed, 0, "source")
        .or_else(|| consume_ci(trimmed, 0, "author"))
        .or_else(|| consume_ci(trimmed, 0, "license"))
        .or_else(|| {
            trimmed
                .strip_prefix("来源")
                .map(|r| trimmed.len() - r.len())
        })
        .or_else(|| {
            trimmed
                .strip_prefix("作者")
                .map(|r| trimmed.len() - r.len())
        })
        .or_else(|| {
            trimmed
                .strip_prefix("许可")
                .map(|r| trimmed.len() - r.len())
        });
    let Some(p) = after else {
        return false;
    };
    let p = ws_end(trimmed, p);
    trimmed.as_bytes().get(p) == Some(&b':')
}
