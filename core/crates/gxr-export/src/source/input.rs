//! The shot facts and the edit, as JSON, in place of the desktop's library record and
//! `EditState`.
//!
//! - shot: `{"files": {"dng": {"relpath": ...}, "jpg": {...}}, "lens": str|null, "iso": int|null}`.
//!   Only which files exist, the DNG's file name, the lens and the ISO are read.
//! - edit: `{"recipe": null|{"kind", "unit", "slot", "id"}, "lut": null|str, "adjust": {...},
//!   "curves": {...}|null}`. `recipe` is `RecipeSel.to_json()` after `parse_recipe` (the
//!   resolve unit); `adjust` and `curves` are what `color-apply` reads (`tones_of`).

use gxr_color::{Adjust, Curves, Value, parse_json, tones_of};
use gxr_raw::CoreError;

/// `RecipeSel.unit` values (`params.UNITS`).
pub const UNITS: [&str; 3] = ["jlaunch", "qlaunch", "l08firm"];

fn bad(message: impl Into<String>) -> CoreError {
    CoreError::new("bad_request", message)
}

/// What the source selection reads from the library record.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Shot {
    pub has_dng: bool,
    pub has_jpg: bool,
    /// `Path(files["dng"]["relpath"]).name`, the name `open_dng` puts in its errors.
    pub dng_name: String,
    pub lens: Option<String>,
    pub iso: Option<i64>,
}

/// `RecipeSel` without the levels (they only matter to `resolve`, which is an input here).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RecipeSel {
    pub kind: String,
    pub unit: String,
    pub slot: i64,
    pub id: Option<String>,
}

/// `EditState` as far as the source picture uses it.
#[derive(Clone, Debug, PartialEq)]
pub struct Edit {
    pub recipe: Option<RecipeSel>,
    pub lut: Option<String>,
    pub adjust: Adjust,
    pub curves: Option<Curves>,
}

impl Edit {
    /// `EditState()`.
    pub fn empty() -> Self {
        Self {
            recipe: None,
            lut: None,
            adjust: Adjust::default(),
            curves: None,
        }
    }
}

fn name_of(relpath: &str) -> String {
    // `Path(relpath).name` on POSIX: the last non-empty component.
    relpath
        .rsplit('/')
        .find(|part| !part.is_empty())
        .map_or_else(String::new, str::to_string)
}

/// Parse the shot JSON.
pub fn parse_shot(bytes: &[u8]) -> Result<Shot, CoreError> {
    let v = parse_json(bytes)?;
    if !matches!(v, Value::Object(_)) {
        return Err(bad("shot must be an object"));
    }
    let files = match v.get("files") {
        Some(f @ Value::Object(_)) => f,
        _ => return Err(bad("shot.files must be an object")),
    };
    let has_dng = files.get("dng").is_some();
    let has_jpg = files.get("jpg").is_some();
    let dng_name = match files.get("dng").and_then(|d| d.get("relpath")) {
        Some(Value::Str(s)) => name_of(s),
        _ => String::new(),
    };
    let lens = match v.get("lens") {
        None | Some(Value::Null) => None,
        Some(Value::Str(s)) => Some(s.clone()),
        Some(_) => return Err(bad("shot.lens must be a string or null")),
    };
    let iso = match v.get("iso") {
        None | Some(Value::Null) => None,
        Some(Value::Int(i)) => Some(*i),
        Some(_) => return Err(bad("shot.iso must be an integer or null")),
    };
    Ok(Shot {
        has_dng,
        has_jpg,
        dng_name,
        lens,
        iso,
    })
}

fn parse_recipe(v: &Value) -> Result<Option<RecipeSel>, CoreError> {
    match v {
        Value::Null => Ok(None),
        Value::Object(_) => {
            let kind = match v.get("kind") {
                None => "builtin".to_string(),
                Some(Value::Str(s)) => s.clone(),
                Some(_) => return Err(bad("recipe.kind must be a string")),
            };
            let unit = match v.get("unit") {
                Some(Value::Str(s)) if UNITS.contains(&s.as_str()) => s.clone(),
                _ => return Err(bad("recipe.unit must be jlaunch, qlaunch or l08firm")),
            };
            let slot = match v.get("slot") {
                Some(Value::Int(i)) if (1..=8).contains(i) => *i,
                _ => return Err(bad("recipe.slot must be an integer 1..8")),
            };
            let id = match v.get("id") {
                None | Some(Value::Null) => None,
                Some(Value::Str(s)) => Some(s.clone()),
                Some(_) => return Err(bad("recipe.id must be a string")),
            };
            Ok(Some(RecipeSel {
                kind,
                unit,
                slot,
                id,
            }))
        }
        _ => Err(bad("edit.recipe must be an object or null")),
    }
}

/// Parse the edit JSON.
pub fn parse_edit(bytes: &[u8]) -> Result<Edit, CoreError> {
    let v = parse_json(bytes)?;
    if !matches!(v, Value::Object(_)) {
        return Err(bad("edit must be an object"));
    }
    let recipe = match v.get("recipe") {
        None => None,
        Some(r) => parse_recipe(r)?,
    };
    let lut = match v.get("lut") {
        None | Some(Value::Null) => None,
        Some(Value::Str(s)) => Some(s.clone()),
        Some(_) => return Err(bad("edit.lut must be a string or null")),
    };
    let (adjust, curves) = tones_of(&v)?;
    Ok(Edit {
        recipe,
        lut,
        adjust,
        curves,
    })
}

/// Parse a `{"adjust", "curves"}` JSON (the `color-apply` parameters).
pub fn parse_tones(bytes: &[u8]) -> Result<(Adjust, Option<Curves>), CoreError> {
    tones_of(&parse_json(bytes)?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shot_reads_files_lens_and_iso() {
        let s =
            parse_shot(br#"{"files":{"dng":{"relpath":"a/b/R0001.DNG"}},"lens":"50mm","iso":200}"#)
                .unwrap();
        assert!(s.has_dng && !s.has_jpg);
        assert_eq!(s.dng_name, "R0001.DNG");
        assert_eq!((s.lens.as_deref(), s.iso), (Some("50mm"), Some(200)));
        assert_eq!(
            parse_shot(br#"{"files":{},"iso":1.5}"#).unwrap_err().kind,
            "bad_request"
        );
    }

    #[test]
    fn edit_reads_recipe_and_lut() {
        let e = parse_edit(
            br#"{"recipe":{"kind":"builtin","unit":"qlaunch","slot":3,"levels":{}},"lut":"x"}"#,
        )
        .unwrap();
        let r = e.recipe.unwrap();
        assert_eq!((r.unit.as_str(), r.slot), ("qlaunch", 3));
        assert_eq!(e.lut.as_deref(), Some("x"));
        assert!(parse_edit(br#"{"recipe":{"unit":"x","slot":3}}"#).is_err());
        assert!(parse_edit(br#"{"recipe":{"unit":"jlaunch","slot":9}}"#).is_err());
    }
}
