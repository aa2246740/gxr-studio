//! Edit JSON parsing and DAT-based recipe resolution (port of `render/params.py` and
//! `render/recipes.py`, without filesystem access).

pub mod error;
pub mod json;
pub mod params;
pub mod recipes;

pub use error::{EditError, INVALID_REQUEST, ResolveError};
pub use json::{MAX_DEPTH, MAX_JSON_BYTES, PyJson, PyNum, parse as parse_py_json};
pub use params::{
    CURVE_CHANNELS, CURVE_MAX_POINTS, EXPOSURE_RANGE, EditAdjust, EditState, KNOB_NAMES,
    RecipeLevel, RecipeSel, SLIDER_RANGE, UNITS, int_in, parse_adjust, parse_curve,
    parse_edit_state, parse_lut, parse_points, parse_recipe, py_round4,
};
pub use recipes::{DatCatalog, MenuCatalog, Resolved, SLOT_SHORT, UNIT_INFO, unit_of_lens};

/// Parse sidecar/edit JSON bytes into `{"recipe", "adjust", "curve", ("lut",), "canonical", "digest"}`.
pub fn parse_edit_json(bytes: &[u8]) -> Result<String, ResolveError> {
    let val = parse_py_json(bytes)?;
    let state = parse_edit_state(&val)?;
    Ok(state.to_cli_json_string())
}

/// Return `{"units": [...]}` for `Menu.listing()` (when `dat` is `None`) or `CameraData.listing()`.
pub fn listing_json(dat: Option<(&[u8], Option<&[u8]>)>) -> Result<String, ResolveError> {
    let json = match dat {
        Some((raw, library)) => DatCatalog::from_bytes(raw, library)?.listing_json()?,
        None => MenuCatalog::embedded()?.listing_json()?,
    };
    Ok(json.render())
}

/// Return `{"controls": [...]}` for `(unit, slot)`.
pub fn controls_json(
    dat: Option<(&[u8], Option<&[u8]>)>,
    unit: i64,
    slot: i64,
) -> Result<String, ResolveError> {
    let json = match dat {
        Some((raw, library)) => DatCatalog::from_bytes(raw, library)?.controls_json(unit, slot)?,
        None => MenuCatalog::embedded()?.controls_json(unit, slot)?,
    };
    Ok(json.render())
}

/// Resolve a `recipe` JSON object against DAT (and optional r5 library) bytes.
pub fn resolve_recipe_json(
    dat: &[u8],
    library: Option<&[u8]>,
    recipe_json: &[u8],
) -> Result<String, ResolveError> {
    let val = parse_py_json(recipe_json)?;
    let sel = parse_recipe(Some(&val))?.ok_or_else(|| {
        EditError::with_detail("bad_recipe", INVALID_REQUEST, "recipe 要是一个对象")
    })?;
    let catalog = DatCatalog::from_bytes(dat, library)?;
    let resolved = catalog.resolve(&sel)?;
    Ok(resolved.to_json().render())
}

/// Resolve `default_selection(unit)` (slot 4) against DAT bytes.
pub fn resolve_default_json(
    dat: &[u8],
    library: Option<&[u8]>,
    unit: &str,
) -> Result<String, ResolveError> {
    let catalog = DatCatalog::from_bytes(dat, library)?;
    let sel = catalog.default_selection(unit)?;
    let resolved = catalog.resolve(&sel)?;
    Ok(resolved.to_json().render())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rounding_matches_python_binary_half_to_even() {
        assert_eq!(py_round4(5e-5), 0.0001);
        assert_eq!(py_round4(0.00025), 0.0003);
        assert_eq!(py_round4(0.12345), 0.1235);
        assert_eq!(py_round4(254.99995), 255.0);
        let neg_zero = py_round4(-1e-5);
        assert_eq!(neg_zero, 0.0);
        assert!(neg_zero.is_sign_negative());
    }

    #[test]
    fn duplicate_keys_and_nesting_depth_match_python_limits() {
        let parsed = parse_py_json(br#"{"a": 1, "b": 2, "a": 3}"#).unwrap();
        let pairs = parsed.as_object().unwrap();
        assert_eq!(pairs.len(), 2);
        assert_eq!(pairs[0].0, "a");
        assert_eq!(pairs[0].1, PyJson::Num(PyNum::Int(3)));
        assert_eq!(pairs[1].0, "b");

        let deep_ok = format!("{}0{}", "[".repeat(MAX_DEPTH), "]".repeat(MAX_DEPTH));
        assert!(parse_py_json(deep_ok.as_bytes()).is_ok());

        let deep_bad = format!(
            "{}0{}",
            "[".repeat(MAX_DEPTH + 1),
            "]".repeat(MAX_DEPTH + 1)
        );
        let err = parse_py_json(deep_bad.as_bytes()).unwrap_err();
        assert_eq!((err.kind, err.message.as_str()), ("json", "JSON 嵌套太深"));
    }

    #[test]
    fn edit_parsing_matches_bool_float_and_bigint_rules() {
        // 1.0 accepted by adjust, rejected by recipe slot/levels
        let adj = parse_adjust(Some(
            &parse_py_json(br#"{"contrast": 1.0, "wb": 0}"#).unwrap(),
        ))
        .unwrap();
        assert_eq!(adj.contrast, 1);

        let bad_bool =
            parse_adjust(Some(&parse_py_json(br#"{"contrast": true}"#).unwrap())).unwrap_err();
        assert_eq!(bad_bool.code, "bad_adjust");
        assert!(bad_bool.detail.contains("请输入整数"));

        let huge_adj = parse_adjust(Some(
            &parse_py_json(br#"{"contrast": 100000000000000000000}"#).unwrap(),
        ))
        .unwrap_err();
        assert_eq!(huge_adj.code, "bad_adjust");
        assert!(huge_adj.detail.contains("数值超出范围"));

        let bad_slot = parse_recipe(Some(
            &parse_py_json(br#"{"unit": "jlaunch", "slot": 1.0}"#).unwrap(),
        ))
        .unwrap_err();
        assert_eq!(bad_slot.code, "bad_recipe");
        assert_eq!(bad_slot.detail, "槽位要在 1 到 8。");

        let huge_level = parse_recipe(Some(
            &parse_py_json(
                br#"{"unit": "jlaunch", "slot": 1, "levels": {"contrast": 100000000000000000000}}"#,
            )
            .unwrap(),
        ))
        .unwrap()
        .unwrap();
        assert_eq!(huge_level.levels.len(), 1);
        let menu = MenuCatalog::embedded().unwrap();
        let err = menu.request_for(&huge_level).unwrap_err();
        assert_eq!(
            err,
            ResolveError::Edit(EditError::with_detail(
                "level_not_allowed",
                "无法保存配方。这个档位不能用。",
                "contrast 01"
            ))
        );
    }

    #[test]
    fn unit_of_lens_and_menu_listing_work() {
        assert_eq!(unit_of_lens(Some("GR LENS 50mm"), None), Some("jlaunch"));
        assert_eq!(unit_of_lens(Some("28mm F2.5"), None), Some("qlaunch"));
        assert_eq!(unit_of_lens(None, Some("GXR MOUNT A12")), Some("l08firm"));
        assert_eq!(unit_of_lens(Some("35mm"), None), None);

        let menu = MenuCatalog::embedded().unwrap();
        let listing = menu.listing().unwrap();
        assert_eq!(listing.len(), 3);
        let c03 = menu.controls(0, 3).unwrap();
        assert_eq!(c03.len(), 5);
    }
}
