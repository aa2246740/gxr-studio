//! UI levels to an integer [`Request`](crate::knobs::Request), from the exported menu JSON.

use gxr_raw::CoreError;

use crate::json_in::{Value, parse};
use crate::knobs::Request;

/// The fields `request` reads. Other keys in `final.json` are ignored.
#[derive(Clone, Debug)]
pub struct Menu {
    recipes: Vec<Recipe>,
    items: Vec<Item>,
    controls: Vec<i64>,
}

#[derive(Clone, Debug)]
struct Recipe {
    first_item: i64,
    item_count: i64,
}

#[derive(Clone, Debug)]
struct Item {
    control: i64,
    points: Vec<(i64, i64)>,
}

fn err(message: &str) -> CoreError {
    CoreError::new("value", message)
}

fn index_err() -> CoreError {
    CoreError::new("index", "list index out of range")
}

/// Port of gxr_studio/engine/menu.py:9 load
///
/// The file bytes are an argument. This crate does not open `final.json` itself.
pub fn load(bytes: &[u8]) -> Result<Menu, CoreError> {
    let data = parse(bytes)?;
    let recipes = data.get("recipes").and_then(Value::as_array).ok_or_else(|| err("recipes"))?;
    let items = data.get("items").and_then(Value::as_array).ok_or_else(|| err("items"))?;
    let controls = data.get("controls").and_then(Value::as_array).ok_or_else(|| err("controls"))?;
    Ok(Menu {
        recipes: recipes.iter().map(recipe_of).collect::<Result<Vec<_>, _>>()?,
        items: items.iter().map(item_of).collect::<Result<Vec<_>, _>>()?,
        controls: controls.iter().map(dose_index_of).collect::<Result<Vec<_>, _>>()?,
    })
}

fn recipe_of(value: &Value) -> Result<Recipe, CoreError> {
    Ok(Recipe {
        first_item: int_field(value, "first_item")?,
        item_count: int_field(value, "item_count")?,
    })
}

fn item_of(value: &Value) -> Result<Item, CoreError> {
    let points = value.get("points").and_then(Value::as_array).ok_or_else(|| err("points"))?;
    let mut pairs = Vec::with_capacity(points.len());
    for point in points {
        pairs.push((int_field(point, "level")?, int_field(point, "dose")?));
    }
    Ok(Item {
        control: int_field(value, "control")?,
        points: pairs,
    })
}

fn dose_index_of(value: &Value) -> Result<i64, CoreError> {
    int_field(value, "dose_index")
}

fn int_field(value: &Value, key: &str) -> Result<i64, CoreError> {
    value.get(key).and_then(Value::as_int).ok_or_else(|| err(key))
}

/// Port of gxr_studio/engine/menu.py:13 request
pub fn request(menu: &Menu, unit: i64, slot: i64, levels: &[Option<i64>]) -> Result<Request, CoreError> {
    if !(0..3).contains(&unit) || !(1..=8).contains(&slot) {
        return Err(err("unit/slot"));
    }
    let index = usize::try_from(unit * 8 + slot - 1).map_err(|_| index_err())?;
    let recipe = menu.recipes.get(index).ok_or_else(index_err)?;
    let start = usize::try_from(recipe.first_item).map_err(|_| index_err())?;
    let count = usize::try_from(recipe.item_count).map_err(|_| err("menu item count"))?;
    let end = start.checked_add(count).ok_or_else(|| err("menu item count"))?;
    let items = menu.items.get(start..end).ok_or_else(|| err("menu item count"))?;
    // Python slices a short tail and then compares lengths. A start past the end yields [].
    let items = if recipe.first_item < 0 {
        return Err(err("menu item count"));
    } else if start >= menu.items.len() {
        &[]
    } else {
        items
    };
    if levels.len() != items.len() {
        return Err(err("menu item count"));
    }
    let mut amount = 100i64;
    let mut doses = [0i64; 10];
    for (item, level) in items.iter().zip(levels.iter()) {
        let Some(level) = *level else {
            return Err(err("integer UI level required"));
        };
        let point = item.points.iter().find(|(stored, _)| *stored == level);
        let Some((_, dose)) = point else {
            return Err(err("unlisted or capped UI level"));
        };
        let control = usize::try_from(item.control).map_err(|_| index_err())?;
        let dose_index = *menu.controls.get(control).ok_or_else(index_err)?;
        if dose_index < 0 {
            amount = *dose;
        } else {
            let at = usize::try_from(dose_index).map_err(|_| index_err())?;
            *doses.get_mut(at).ok_or_else(index_err)? = *dose;
        }
    }
    Ok(Request {
        amount,
        doses,
        wb_method: 0,
        tone_method: 0,
    })
}

#[cfg(test)]
mod tests {
    use super::{load, request};

    #[test]
    fn one_recipe_maps_levels_to_doses() {
        let raw = br#"{
            "recipes":[{"first_item":0,"item_count":2}],
            "items":[
                {"control":0,"points":[{"level":0,"dose":100},{"level":1,"dose":80}]},
                {"control":1,"points":[{"level":0,"dose":0},{"level":2,"dose":50}]}
            ],
            "controls":[{"dose_index":-1},{"dose_index":0}]
        }"#;
        let menu = load(raw).unwrap();
        let req = request(&menu, 0, 1, &[Some(1), Some(2)]).unwrap();
        assert_eq!(req.amount, 80);
        assert_eq!(req.doses[0], 50);
        assert_eq!(request(&menu, 3, 1, &[Some(1), Some(2)]).unwrap_err().message, "unit/slot");
        assert_eq!(request(&menu, 0, 1, &[Some(1)]).unwrap_err().message, "menu item count");
        assert_eq!(
            request(&menu, 0, 1, &[Some(1), None]).unwrap_err().message,
            "integer UI level required"
        );
        assert_eq!(
            request(&menu, 0, 1, &[Some(9), Some(2)]).unwrap_err().message,
            "unlisted or capped UI level"
        );
    }
}
