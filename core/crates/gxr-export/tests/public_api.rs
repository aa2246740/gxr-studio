//! The public pipeline helpers, called from outside the crate (Sol r1 P2-04): a picture whose
//! size doesn't match its bytes must be a `CoreError`, never a success.

use gxr_export::pipeline::{ExportOpts, Rgb, ShotFacts, fit, prepare_original};

fn shot() -> ShotFacts {
    ShotFacts {
        name: "SYNTHETIC".into(),
        width: 2,
        height: 1,
        has_jpg: true,
        has_dng: false,
    }
}

const OPTS: ExportOpts = ExportOpts {
    quality: 95.0,
    long_edge: None,
    hdr: false,
};

#[test]
fn mismatched_pictures_are_errors() {
    for (name, width, height, data) in [
        ("short_rgb", 2, 1, vec![1, 2, 3]),
        ("huge_empty_rgb", usize::MAX, 1, vec![]),
        ("zero_empty_rgb", 0, 1, vec![]),
    ] {
        let rgb = Rgb {
            width,
            height,
            data,
        };
        let prepared = prepare_original(&shot(), rgb.clone(), OPTS);
        assert!(prepared.is_err(), "{name}: prepare_original accepted it");
        let fitted = fit(rgb, None);
        assert!(fitted.is_err(), "{name}: fit accepted it");
    }
}

#[test]
fn a_matching_picture_still_works() {
    let rgb = Rgb {
        width: 2,
        height: 1,
        data: vec![1, 2, 3, 4, 5, 6],
    };
    assert!(prepare_original(&shot(), rgb.clone(), OPTS).is_ok());
    assert_eq!(fit(rgb.clone(), None).unwrap(), rgb);
}
