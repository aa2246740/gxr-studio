//! Port of gxr_studio/finish layout: spec, sfnt, layout, frame_core, frame styles. Owner: wave-1 unit frame.
#![forbid(unsafe_code)]

mod sfnt;
mod spec;
mod value;

pub use sfnt::{FontTable, Glyph, read_font};
pub use spec::{FrameSpec, StampSpec, factory_frame, factory_stamp, parse_frame, parse_stamp, stored_frame};
pub use value::{Val, parse_json};
