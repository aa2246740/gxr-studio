//! Port of gxr_studio/camdata/recipe_tags.py: EXIF Copyright `GXR1` recipe tag parser.

use gxr_raw::Json;

pub const MAX_TAG_BYTES: usize = 47;
pub const COPYRIGHT_FIELD_CHARS: usize = 69;
pub const RECIPE_TAG_VERSION: i64 = 3;
pub const KNOB_ORDER: [&str; 10] = [
    "amount",
    "contrast",
    "highlight",
    "shadow",
    "fade",
    "saturation",
    "warmth",
    "tint",
    "split",
    "filter",
];
const ALPHABET: &[u8] = b"0123456789ABCDEFGHI";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RecipeTag {
    pub version: i64,
    pub catalog_identity: String,
    pub unit: String,
    pub position: i64,
    pub knob_code: Option<String>,
    pub levels: Option<[Option<i64>; 10]>,
}

/// Port of gxr_studio/camdata/recipe_tags.py:34 parse_recipe_tag (bytes input).
pub fn parse_recipe_tag_bytes(raw: &[u8]) -> Option<RecipeTag> {
    if raw.len() > COPYRIGHT_FIELD_CHARS + 1 || !raw.is_ascii() {
        return None;
    }
    let text = std::str::from_utf8(raw).ok()?;
    parse_recipe_tag(text)
}

/// Port of gxr_studio/camdata/recipe_tags.py:34 parse_recipe_tag (str input).
pub fn parse_recipe_tag(value: &str) -> Option<RecipeTag> {
    if value.len() > COPYRIGHT_FIELD_CHARS + 1 || !value.is_ascii() {
        return None;
    }
    let mut text = value.strip_suffix('\0').unwrap_or(value);
    if text.len() == COPYRIGHT_FIELD_CHARS {
        text = text.trim_end_matches(' ');
    }
    if text.len() >= MAX_TAG_BYTES {
        return None;
    }

    // Match ^GXR1 ([0-9a-fA-F]{10}) (28|50|MT) (0[1-9]|[1-9][0-9])(?: ([0-9A-I-]{10}))?$
    let bytes = text.as_bytes();
    if !(bytes.len() == 21 || bytes.len() == 32) {
        return None;
    }
    if &bytes[..5] != b"GXR1 " || bytes[15] != b' ' || bytes[18] != b' ' {
        return None;
    }
    let id_bytes = &bytes[5..15];
    if !id_bytes.iter().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    let unit_bytes = &bytes[16..18];
    if unit_bytes != b"28" && unit_bytes != b"50" && unit_bytes != b"MT" {
        return None;
    }
    let pos_bytes = &bytes[19..21];
    if !pos_bytes[0].is_ascii_digit() || !pos_bytes[1].is_ascii_digit() {
        return None;
    }
    let position = i64::from((pos_bytes[0] - b'0') * 10 + (pos_bytes[1] - b'0'));
    if !(1..=99).contains(&position) {
        return None;
    }

    let (knob_code, levels) = if bytes.len() == 32 {
        if bytes[21] != b' ' {
            return None;
        }
        let code_bytes = &bytes[22..32];
        let mut decoded = [None; 10];
        for (i, &ch) in code_bytes.iter().enumerate() {
            if ch == b'-' {
                decoded[i] = None;
            } else {
                let idx = ALPHABET.iter().position(|&c| c == ch)?;
                decoded[i] = if i == 0 { None } else { Some(idx as i64 - 9) };
            }
        }
        let code_str = std::str::from_utf8(code_bytes).ok()?.to_string();
        (Some(code_str), Some(decoded))
    } else {
        (None, None)
    };

    let catalog_identity = std::str::from_utf8(id_bytes).ok()?.to_ascii_lowercase();
    let unit = std::str::from_utf8(unit_bytes).ok()?.to_string();
    Some(RecipeTag {
        version: RECIPE_TAG_VERSION,
        catalog_identity,
        unit,
        position,
        knob_code,
        levels,
    })
}

pub fn recipe_tag_to_json(tag: Option<&RecipeTag>) -> Json {
    let tag_json = match tag {
        None => Json::Null,
        Some(t) => {
            let levels_json = match &t.levels {
                None => Json::Null,
                Some(levels) => Json::object(
                    KNOB_ORDER
                        .iter()
                        .zip(levels.iter())
                        .map(|(&name, &val)| (name, Json::opt(val.map(Json::Int)))),
                ),
            };
            Json::object([
                ("version", Json::Int(t.version)),
                ("catalog_identity", Json::str(&t.catalog_identity)),
                ("unit", Json::str(&t.unit)),
                ("position", Json::Int(t.position)),
                (
                    "knob_code",
                    Json::opt(t.knob_code.as_deref().map(Json::str)),
                ),
                ("levels", levels_json),
            ])
        }
    };
    Json::object([("tag", tag_json)])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_valid_and_rejects_invalid_tags() {
        let t = parse_recipe_tag("GXR1 6a3a5a3c02 28 06 A8999B999-").expect("valid");
        assert_eq!(t.catalog_identity, "6a3a5a3c02");
        assert_eq!(t.unit, "28");
        assert_eq!(t.position, 6);
        assert_eq!(t.knob_code.as_deref(), Some("A8999B999-"));
        let lv = t.levels.expect("levels");
        assert_eq!(lv[0], None);
        assert_eq!(lv[1], Some(-1));
        assert_eq!(lv[5], Some(2));
        assert_eq!(lv[9], None);

        let legacy = parse_recipe_tag("GXR1 d25d2f0a68 50 01").expect("legacy");
        assert_eq!(legacy.knob_code, None);
        assert_eq!(legacy.levels, None);

        assert!(parse_recipe_tag("GXR1 6a3a5a3c02 28 00").is_none());
        assert!(parse_recipe_tag("GXR1 6a3a5a3c02 28 06 A8999B999-\n").is_none());
        let padded = format!("{:<69}\0", "GXR1 0123456789 50 17 799999999-");
        assert!(parse_recipe_tag(&padded).is_some());
    }
}
