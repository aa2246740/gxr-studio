//! Vendored `jpeg-encoder 0.6.0` (MIT OR Apache-2.0, with `fdct.rs` derived from IJG).
//! Scalar safe-Rust path only (`avx2` SIMD module omitted for `#![forbid(unsafe_code)]`).

#![allow(dead_code, clippy::all)]

mod encoder;
mod encoder_scans;
mod error;
mod fdct;
mod huffman;
mod image_buffer;
mod marker;
mod quantization;
mod writer;

pub use encoder::{ColorType, Encoder, SamplingFactor};
pub use error::EncodingError;
#[allow(unused_imports)]
pub use image_buffer::rgb_to_ycbcr;
pub use writer::Density;
