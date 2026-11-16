use super::*;
use crate::hdr_track::frame::{frame_placement, frame_track};
use gxr_finish::geometry::try_vec_f32;
use gxr_finish::{Filter, resample_f32};
use gxr_hdr::block_mean;
use gxr_jpeg::encode_jpeg;

fn reference_to_grid(
    layer: &[f32],
    lw: u64,
    lh: u64,
    out_w: u64,
    out_h: u64,
    factor: usize,
) -> Result<ToGridOutput, CoreError> {
    let (lwu, lhu, _, _) = checked_float_layer_slice(layer, lw, lh, 2)?;
    let (owu, ohu, _) = checked_mask_dims(out_w, out_h)?;
    let n = u64::try_from(lwu).unwrap() * u64::try_from(lhu).unwrap();
    let mut p0 = try_vec_f32(n, 0.0_f32)?;
    let mut p1 = try_vec_f32(n, 0.0_f32)?;
    let mut p2 = try_vec_f32(n, 0.0_f32)?;
    for (i, px) in layer.chunks_exact(2).enumerate() {
        if px[0].is_finite() {
            p0[i] = px[0];
            p1[i] = px[1];
            p2[i] = 1.0_f32;
        }
    }
    if (lwu, lhu) != (owu, ohu) {
        p0 = resample_f32(&p0, lwu, lhu, Filter::Box, owu, ohu)?;
        p1 = resample_f32(&p1, lwu, lhu, Filter::Box, owu, ohu)?;
        p2 = resample_f32(&p2, lwu, lhu, Filter::Box, owu, ohu)?;
    }
    let (y_grid, gh, gw) = block_mean(&p0, ohu, owu, factor)?;
    let (clip_grid, _, _) = block_mean(&p1, ohu, owu, factor)?;
    let (w_grid, _, _) = block_mean(&p2, ohu, owu, factor)?;
    let mut raw_y = vec![f32::NAN; gh * gw];
    let mut clip = Vec::with_capacity(gh * gw);
    for i in 0..gh * gw {
        let w = w_grid[i];
        let denom = if w > W_MIN_EPS { w } else { W_MIN_EPS };
        if w > W_PHOTO_THRESH {
            raw_y[i] = y_grid[i] / denom;
        }
        clip.push((clip_grid[i] / denom) > CLIP_THRESH);
    }
    Ok(ToGridOutput {
        raw_y,
        clip,
        gh,
        gw,
    })
}

fn assert_grid_bit_exact(got: &ToGridOutput, want: &ToGridOutput) {
    assert_eq!((got.gh, got.gw), (want.gh, want.gw));
    assert_eq!(got.clip, want.clip);
    assert_eq!(got.raw_y.len(), want.raw_y.len());
    for (i, (&g, &w)) in got.raw_y.iter().zip(want.raw_y.iter()).enumerate() {
        assert_eq!(
            g.to_bits(),
            w.to_bits(),
            "bit mismatch at idx {i}: {g} vs {w}"
        );
    }
}

#[test]
fn finish_returns_not_jpeg_problem_no_curve_and_container_reasons() {
    let rgb = vec![128_u8; 8 * 8 * 3];
    let payload = encode_jpeg(&rgb, 8, 8, 92, "{}").unwrap();
    let inactive = HdrTrackState::inactive("not_aligned");
    let p = Params::default();

    let png = finish(&payload, &inactive, "png", &rgb, 8, 8, &p).unwrap();
    assert_eq!(
        png,
        FinishOutput::Skipped {
            bytes: payload.clone(),
            reason: "not_jpeg".to_string(),
        }
    );

    let no_raw = finish(&payload, &inactive, "jpeg", &rgb, 8, 8, &p).unwrap();
    assert_eq!(
        no_raw,
        FinishOutput::Skipped {
            bytes: payload.clone(),
            reason: "not_aligned".to_string(),
        }
    );

    // Active flat scene triggers `no_curve`
    let active = HdrTrackState {
        active: true,
        shape: Some([8, 8, 2]),
        problem: None,
        layer: vec![0.1_f32; 8 * 8 * 2],
    };
    let dark = finish(&payload, &active, "jpeg", &rgb, 8, 8, &p).unwrap();
    assert_eq!(
        dark,
        FinishOutput::Skipped {
            bytes: payload,
            reason: "no_curve".to_string(),
        }
    );
}

#[test]
fn finish_produces_hdr_and_maps_container_errors() {
    let (w, h) = (128_usize, 128_usize);
    let mut rgb = vec![0_u8; w * h * 3];
    let mut layer = vec![0.0_f32; w * h * 2];
    for y in 0..h {
        for x in 0..w {
            let hi = x >= w / 2;
            let sdr: u8 = if hi { 220 } else { 80 };
            let idx = (y * w + x) * 3;
            rgb[idx] = sdr;
            rgb[idx + 1] = sdr;
            rgb[idx + 2] = sdr;
            let lidx = (y * w + x) * 2;
            layer[lidx] = if hi { 2.5 } else { 0.08 };
            layer[lidx + 1] = 0.0;
        }
    }
    let state = HdrTrackState {
        active: true,
        shape: Some([h, w, 2]),
        problem: None,
        layer,
    };
    let p = Params::default();
    let bad = finish(b"not-a-jpeg", &state, "jpeg", &rgb, w as u64, h as u64, &p).unwrap();
    assert_eq!(
        bad,
        FinishOutput::Skipped {
            bytes: b"not-a-jpeg".to_vec(),
            reason: "container".to_string(),
        }
    );

    let payload = encode_jpeg(&rgb, w as u32, h as u32, 92, "{}").unwrap();
    let ok = finish(&payload, &state, "jpeg", &rgb, w as u64, h as u64, &p).unwrap();
    assert!(matches!(ok, FinishOutput::Hdr { .. }));
    assert!(ok.bytes().len() > payload.len());
}

#[test]
fn to_grid_masks_partial_nan_blocks_and_resizes_with_box() {
    let mut layer = vec![0.5_f32; 4 * 4 * 2];
    layer[1] = 1.0; // clip at (0, 0)
    let out_valid = to_grid(&layer, 4, 4, 4, 4, 4).unwrap();
    assert_eq!((out_valid.gh, out_valid.gw), (1, 1));
    assert_eq!(out_valid.raw_y[0], 0.5);
    assert!(out_valid.clip[0]);

    layer[0] = f32::NAN;
    let out_nan = to_grid(&layer, 4, 4, 4, 4, 4).unwrap();
    assert!(out_nan.raw_y[0].is_nan());

    let mut big = Vec::with_capacity(16 * 16 * 2);
    for _ in 0..(16 * 16) {
        big.push(0.25_f32);
        big.push(0.0_f32);
    }
    let resized = to_grid(&big, 16, 16, 8, 8, 4).unwrap();
    assert_eq!((resized.gh, resized.gw), (2, 2));
    assert!(resized.raw_y.iter().all(|&v| (v - 0.25).abs() < 1e-6));
    assert!(resized.clip.iter().all(|&c| !c));
}

#[test]
fn streaming_placed_grid_and_finish_are_bit_exact_with_materialised_frame_track() {
    // Odd dims, partial NaN/Inf pixels, placed offset inside larger canvas, BOX resize + gw==1.
    let (lw, lh) = (19_usize, 13_usize);
    let mut layer = Vec::with_capacity(lw * lh * 2);
    for y in 0..lh {
        for x in 0..lw {
            if (x == 3 && y == 4) || (x == 17 && y == 11) {
                layer.push(f32::NAN);
                layer.push(0.0);
            } else if x == 8 && y == 7 {
                layer.push(f32::INFINITY);
                layer.push(1.0);
            } else {
                layer.push(((y * lw + x) as f32 * 0.017) + 0.05);
                layer.push(if (x + y) % 5 == 0 { 1.0 } else { 0.0 });
            }
        }
    }
    let state = HdrTrackState {
        active: true,
        shape: Some([lh, lw, 2]),
        problem: None,
        layer,
    };
    let layout = r#"{"width":31,"height":27,"photo":[5,6],"bg":[20,20,24],"items":[{"k":"photo","x":5,"y":6,"w":19,"h":13}],"bgfx":null}"#;
    let mat = frame_track(&state, layout).unwrap();
    let placed = frame_placement(state.clone(), layout).unwrap();
    let p = placed.placement.unwrap();

    for (ow, oh, factor) in [
        (31, 27, 4), // unresized, odd canvas + edge padding
        (23, 19, 4), // BOX downscale both axes
        (31, 17, 3), // BOX vertical-only downscale
        (17, 27, 5), // BOX horizontal-only downscale
        (41, 33, 4), // BOX upscale
        (7, 5, 8),   // gw == 1 single-column block_mean
    ] {
        let want = reference_to_grid(&mat.layer, 31, 27, ow, oh, factor).unwrap();
        let got = to_grid_placed(
            &placed.state.layer,
            lw as u64,
            lh as u64,
            p.canvas_w as u64,
            p.canvas_h as u64,
            p.photo_x,
            p.photo_y,
            ow,
            oh,
            factor,
        )
        .unwrap();
        assert_grid_bit_exact(&got, &want);
    }

    // Tall-narrow (>100:1) vertical-first Pillow branch
    let narrow_w = 3_usize;
    let narrow_h = 320_usize;
    let mut narrow_layer = Vec::with_capacity(narrow_w * narrow_h * 2);
    for i in 0..(narrow_w * narrow_h) {
        narrow_layer.push((i as f32) * 0.003 + 0.1);
        narrow_layer.push(if i % 7 == 0 { 1.0 } else { 0.0 });
    }
    let want_narrow =
        reference_to_grid(&narrow_layer, narrow_w as u64, narrow_h as u64, 2, 160, 4).unwrap();
    let got_narrow = to_grid_placed(
        &narrow_layer,
        narrow_w as u64,
        narrow_h as u64,
        narrow_w as u64,
        narrow_h as u64,
        0,
        0,
        2,
        160,
        4,
    )
    .unwrap();
    assert_grid_bit_exact(&got_narrow, &want_narrow);

    // Full Ultra HDR finish_placed == finish(frame_track) byte-for-byte
    let (fw, fh) = (128_usize, 128_usize);
    let mut flayer = vec![0.0_f32; fw * fh * 2];
    for y in 0..fh {
        for x in 0..fw {
            let hi = x >= fw / 2;
            let idx = (y * fw + x) * 2;
            flayer[idx] = if hi { 2.5 } else { 0.08 };
        }
    }
    let fstate = HdrTrackState {
        active: true,
        shape: Some([fh, fw, 2]),
        problem: None,
        layer: flayer,
    };
    let (cw, ch) = (160_usize, 160_usize);
    let mut crgb = vec![40_u8; cw * ch * 3];
    for y in 16..16 + fh {
        for x in 16..16 + fw {
            let sdr: u8 = if x - 16 >= fw / 2 { 220 } else { 80 };
            let idx = (y * cw + x) * 3;
            crgb[idx] = sdr;
            crgb[idx + 1] = sdr;
            crgb[idx + 2] = sdr;
        }
    }
    let flayout = r#"{"width":160,"height":160,"photo":[16,16],"bg":[40,40,40],"items":[{"k":"photo","x":16,"y":16,"w":128,"h":128}],"bgfx":null}"#;
    let payload = encode_jpeg(&crgb, cw as u32, ch as u32, 92, "{}").unwrap();
    let params = Params::default();
    let fmat = frame_track(&fstate, flayout).unwrap();
    let fplaced = frame_placement(fstate, flayout).unwrap();
    let want_done = finish(
        &payload, &fmat, "jpeg", &crgb, cw as u64, ch as u64, &params,
    )
    .unwrap();
    let got_done = finish_placed(
        &payload, fplaced, "jpeg", &crgb, cw as u64, ch as u64, &params,
    )
    .unwrap();
    assert_eq!(got_done, want_done);
}
