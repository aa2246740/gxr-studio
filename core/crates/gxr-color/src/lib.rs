//! Recipe knobs, the forward colour model, and the bake the phone uploads.
#![forbid(unsafe_code)]

mod crc;
mod json_in;
mod knobs;
mod menu;

pub use json_in::{Value, parse as parse_json};
pub use knobs::{Request, Stats, apply, dec, enc, rd, request_for};
pub use menu::{Menu, load as load_menu, request as menu_request};
