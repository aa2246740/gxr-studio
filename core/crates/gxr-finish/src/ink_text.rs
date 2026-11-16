//! Port of FreeType 2.14.3 outline loading & rasterization and Pillow 12.2.0 text painting.
//!
//! Portions of this software are copyright © 2026 The FreeType Project (www.freetype.org).
//! All rights reserved.

pub mod ftcalc;
pub mod glyf;
#[cfg(test)]
mod glyf_tests;
pub mod gray;
#[cfg(test)]
mod gray_tests;
mod gray_worker;
pub mod text;
