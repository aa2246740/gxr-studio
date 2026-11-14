//! Recipe knobs, the forward colour model, and the bake the phone uploads.
#![forbid(unsafe_code)]
// The numeric ports index arrays in the order numpy walks them and write each accumulation
// as `s = s + a * b`, so a reader can match them to the Python line by line. Both lints only
// ask for a different spelling of the same IEEE operations.
#![allow(clippy::needless_range_loop, clippy::assign_op_pattern)]

mod api;
mod calib;
mod chain;
mod colour;
mod cplx;
mod crc;
mod hull;
mod json_in;
mod knobs;
mod matrix;
mod menu;
mod model;
mod npy;
mod npz;
mod params;
mod pretable;
pub mod resolve;
mod sector;
mod size;
mod tones;

pub use api::{apply_with_params, bake_summary, bake_with_params};
pub use calib::{Context, CtxArrays, ISO_SPLIT, Pick, context, load_ctx, model_for, unit_model};
pub use chain::{
    ChromaBake, STEP, bake_bytes, bake_chroma, chroma_float, chroma_u8,
    from_bytes as bake_from_bytes, quantize,
};
pub use colour::{decode_word, stock_words_of_entry};
pub use cplx::C;
pub use json_in::{Value, parse as parse_json};
pub use knobs::{Request, Stats, apply, dec, enc, rd, request_for};
pub use menu::{Menu, load as load_menu, request as menu_request};
pub use model::{
    Codes, Coefficients, ENTRY_BYTES, Flavour, Model, UnitModel, input_codes, predict, quantize_f64,
};
pub use npy::{Npy, decode as decode_npy, encode as encode_npy};
pub use params::{bake_meta, knobs_request, meta_of, tones_of};
pub use size::ARRAY_TOO_BIG;
pub use tones::{Adjust, Curves, Table, channel_gains, display_table};
