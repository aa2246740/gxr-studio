//! Which picture an export starts from, rendered at full size (`finish/serve.py`
//! `color_image` / `_raw_rgb`, `render/service.py` `plan` / `renderer`, `raw/render.py`
//! `render_full` with a recipe renderer, and `lut/photo.py` `color_image`).
//!
//! Out of scope, as on the phone: precise export (`exact_u8`), local recipes from the desktop
//! library, approximate recipes (`approx.ENABLED` is False) and the preview path (`full=False`).

mod api;
mod fail;
mod input;
mod lut;
mod plan;
mod render;
mod select;

pub use api::{
    Pictures, Request, color_image_json, iso_from_number, pack_from_index, plan_json, render,
    selection_json,
};
pub use fail::{Fail, Labels, NO_RAW};
/// One implementation, in `gxr-color` (the resolve unit); re-exported so this path keeps working.
pub use gxr_color::resolve::unit_of_lens;
pub use input::{Edit, RecipeSel, Shot, parse_edit, parse_shot};
pub use plan::{
    CALIBRATED_LABEL, CalibPack, Camera, Context, EXPORT_STEP, ISO_SPLIT, MODEL_NOTE, Plan,
    UNCALIBRATED_LABEL,
};
pub use select::{CAMERA_JPG, PREVIEW, Picked, Sources, check_rgb_shape, color_image};
