//! Port of gxr_studio/finish layout: spec, sfnt, layout, frame_core, frame styles. Owner: wave-1 unit frame.
//!
//! Rust computes every box, text run, glyph column and colour; the browser paints them. No font
//! file and no logo image is embedded: fonts arrive as bytes, and the built-in wordmark is only
//! a box whose proportions come in as `mark` (the phone draws the word "GXR" in Atkinson there).
#![forbid(unsafe_code)]

mod api;
mod cff;
mod cjk;
mod frame_core;
mod frame_new_a;
mod frame_new_b;
mod frame_objects;
mod frame_styles;
mod item;
mod layout;
mod logo;
mod metrics;
mod py;
mod sfnt;
mod spec;
mod value;

/// `meta: dict[str, str]` of the style functions.
pub type Meta = std::collections::BTreeMap<String, String>;

pub use api::{batch_json, fonts_from, fonts_from_pack, frame_json, stamp_json};
pub use cjk::CjkFont;
pub use layout::{
    FrameLayout, StampLayout, fit_size, format_stamp, frame_layout, ratio_size, stamp_layout,
};
pub use logo::LogoInfo;
pub use metrics::{FONT_FILES, Fonts};
pub use sfnt::{FontTable, Glyph, read_font};
pub use spec::{
    FrameSpec, StampSpec, factory_frame, factory_stamp, parse_frame, parse_stamp, stored_frame,
};
pub use value::{Val, parse_json};
