//! Recipe knobs, the forward colour model, and the bake the phone uploads.
#![forbid(unsafe_code)]

mod calib;
mod colour;
mod crc;
mod json_in;
mod knobs;
mod menu;
mod npy;
mod npz;

pub use calib::{Context, context};
pub use colour::{decode_word, stock_words_of_entry};
pub use json_in::{Value, parse as parse_json};
pub use knobs::{Request, Stats, apply, dec, enc, rd, request_for};
pub use menu::{Menu, load as load_menu, request as menu_request};
