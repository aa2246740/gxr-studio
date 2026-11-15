//! The whole phone export (`finish/serve.py:export_finish` after desktop 1.0.1), as two calls
//! around the browser's ink painting: `export_prepare` and `export_complete`, plus
//! `output_sizes` for the export sheet. See `docs/mobile.md`.

pub mod api;
pub mod complete;
pub mod encode;
pub mod huffopt;
pub mod prepare;
pub mod result;
pub mod sizes;

pub use complete::{Completed, State, checked_rgb, complete_sdr};
pub use encode::{Rgb, exif_spec_json, fit, with_note, write_rgb_with_exif};
pub use huffopt::optimize_huffman;
pub use prepare::{
    ExportOpts, FinishEdit, LayoutFacts, Picked, Plan, Prepared, ShotFacts, load_defaults,
    original_labels, parse_finish_edit, plan, prepare_original, prepare_picked,
};
pub use result::{
    ExportFail, HdrOutcome, Labels, ResultLabels, file_name, result_json, result_labels,
    validate_options,
};
pub use sizes::output_sizes;
