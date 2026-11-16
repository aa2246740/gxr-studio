//! Port of FreeType 2.14.3 outline loading & rasterization and Pillow 12.2.0 text painting.
//!
//! Portions of this software are copyright © 2026 The FreeType Project (www.freetype.org).
//! All rights reserved.

mod cff_outline;
pub mod frame;
pub mod ftcalc;
pub mod glyf;
mod glyf_sfnt;
#[cfg(test)]
mod glyf_tests;
pub mod gray;
#[cfg(test)]
mod gray_tests;
mod gray_worker;
pub mod mark;
pub mod run;
pub mod stamp;
pub mod text;
#[cfg(test)]
mod text_tests;
