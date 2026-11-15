//! Port of `gxr_studio/finish/hdr_export.py` and `gxr_studio/hdr/capture.py` (`from_dng`,
//! `from_jpeg`, `check_capture`).

pub mod capture;
pub mod finish;
pub mod frame;
pub mod track;

pub use finish::{FinishOutput, NOT_JPEG, ToGridOutput, finish, to_grid};
pub use frame::frame_track;
pub use track::{
    HdrTrackState, NO_RAW, begin, box_track, checked_box_dim_f64, checked_box_offset_f64,
    checked_dim_f64, checked_factor_f64, checked_nonneg_u64_f64, crop_track, parse_params_opt,
    parse_rect4_json, stamp_track, warp_track,
};
