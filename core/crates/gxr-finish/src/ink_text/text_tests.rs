use super::text::*;
use gxr_raw::CoreError;

#[test]
fn blend_mask_rgba_preserves_transparent_and_opaque_dst_rules() {
    // dst pixel 0: alpha=0 (transparent) -> RGB uses channel_mask = 255, alpha uses mask.
    // dst pixel 1: alpha=100 (>0) -> RGB & alpha both use channel_mask = mask.
    let mut buf = vec![10, 20, 30, 0, 10, 20, 30, 100];
    let mut canvas = CanvasMut {
        data: &mut buf,
        width: 2,
        height: 1,
        mode: CanvasMode::Rgba,
    };
    let mask = RenderedCharMask {
        width: 2,
        height: 1,
        x_offset: 0,
        y_offset: 0,
        mask: vec![128, 128],
    };
    blend_mask_onto(&mut canvas, 0, 0, &mask, [200, 100, 50, 255]).unwrap();
    assert_eq!(&buf[0..4], &[200, 100, 50, 128]);
    assert_eq!(
        &buf[4..8],
        &[
            blend_u8(128, 10, 200),
            blend_u8(128, 20, 100),
            blend_u8(128, 30, 50),
            blend_u8(128, 100, 255),
        ]
    );
}

#[test]
fn blend_mask_onto_rejects_short_mask_and_coordinate_overflow() {
    let mut bytes = [0_u8; 2];
    let mut canvas = CanvasMut {
        data: &mut bytes,
        width: 2,
        height: 1,
        mode: CanvasMode::L,
    };
    let short_mask = RenderedCharMask {
        width: 2,
        height: 1,
        x_offset: 0,
        y_offset: 0,
        mask: vec![255],
    };
    let err: CoreError = blend_mask_onto(&mut canvas, 0, 0, &short_mask, [255; 4]).unwrap_err();
    assert_eq!(err.kind, "bad_geometry");

    let zero_height = RenderedCharMask {
        width: 4,
        height: 0,
        x_offset: 0,
        y_offset: 0,
        mask: Vec::new(),
    };
    blend_mask_onto(&mut canvas, 0, 0, &zero_height, [255; 4]).unwrap();

    let huge_empty = RenderedCharMask {
        width: (i32::MAX as usize) + 1,
        height: 0,
        x_offset: 0,
        y_offset: 0,
        mask: Vec::new(),
    };
    let err_huge = blend_mask_onto(&mut canvas, 0, 0, &huge_empty, [255; 4]).unwrap_err();
    assert_eq!(err_huge.kind, "bad_geometry");

    let valid_mask = RenderedCharMask {
        width: 2,
        height: 1,
        x_offset: 0,
        y_offset: 0,
        mask: vec![255, 255],
    };
    assert_eq!(
        blend_mask_onto(&mut canvas, i64::MAX, 0, &valid_mask, [255; 4])
            .unwrap_err()
            .kind,
        "bad_geometry"
    );
    assert_eq!(
        blend_mask_onto(&mut canvas, 0, i64::MAX, &valid_mask, [255; 4])
            .unwrap_err()
            .kind,
        "bad_geometry"
    );
    blend_mask_onto(&mut canvas, i64::MIN, 0, &valid_mask, [255; 4]).unwrap();
    blend_mask_onto(&mut canvas, 0, i64::MIN, &valid_mask, [255; 4]).unwrap();
    assert_eq!(bytes, [0, 0]);
}

#[test]
fn json_nesting_over_128_is_rejected() {
    let deep = "[".repeat(130) + &"]".repeat(130);
    let err = inktext_draw_u64(&[0], 1, 1, "L", &deep, "[]", &[]).unwrap_err();
    assert_eq!(err.kind, "json");
}

#[test]
fn draw_ops_renders_even_odd_and_rejects_huge_cbox() {
    let ops = r#"[{"op":"outline","points":[[0,0],[0,384],[384,384],[384,0],[128,128],[128,256],[256,256],[256,128]],"tags":[1,1,1,1,1,1,1,1],"contours":[3,7],"flags":2,"x":0,"y":6,"fill":255,"ppem":[16,16]}]"#;
    let res = inktext_draw_u64(&[0_u8; 6 * 6], 6, 6, "L", ops, "[]", &[]).unwrap();
    assert_eq!(res.pixels[3 * 6 + 3], 0);
    assert_eq!(res.pixels[0], 255);

    let huge_ops = r#"[{"op":"outline","points":[[0,0],[0,64],[16777217,64],[16777217,0]],"tags":[1,1,1,1],"contours":[3],"raw":true,"w":4,"h":4,"fill":255}]"#;
    assert!(inktext_draw_u64(&[0_u8; 16], 4, 4, "L", huge_ops, "[]", &[]).is_err());
}
