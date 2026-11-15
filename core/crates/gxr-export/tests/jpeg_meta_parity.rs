//! Integration tests for `gxr_export::meta::tiff_exif::open_image` Pillow 12.2.0 JPEG edge cases.

use gxr_export::meta::tiff_exif::open_image;

fn seg(marker: u8, payload: &[u8]) -> Vec<u8> {
    let len = u16::try_from(payload.len() + 2).unwrap();
    let mut out = vec![0xFF, marker];
    out.extend_from_slice(&len.to_be_bytes());
    out.extend_from_slice(payload);
    out
}

fn sof_seg(marker: u8, bits: u8, h: u16, w: u16, tail: &[u8]) -> Vec<u8> {
    let mut p = vec![bits];
    p.extend_from_slice(&h.to_be_bytes());
    p.extend_from_slice(&w.to_be_bytes());
    p.push(1);
    p.extend_from_slice(tail);
    seg(marker, &p)
}

fn wrap_jpg(body: &[u8]) -> Vec<u8> {
    let mut out = vec![0xFF, 0xD8];
    out.extend_from_slice(body);
    out.extend_from_slice(b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xff\xd9");
    out
}

#[test]
fn jpeg_header_edge_cases_match_pillow() {
    let ok_sof = sof_seg(0xC0, 8, 4, 4, &[1, 0x11, 0]);

    // All 14 SOF markers including DHP (0xDE): bad tail % 3 != 0 fails; valid 3-byte record succeeds
    let all_sofs: &[u8] = &[
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF, 0xDE,
    ];
    for &m in all_sofs {
        assert!(open_image(&wrap_jpg(&sof_seg(m, 8, 4, 4, &[1]))).is_err());
        assert!(open_image(&wrap_jpg(&sof_seg(m, 8, 4, 4, &[1, 0x11, 0]))).is_ok());
    }

    // APP0 JFIF / APP14 Adobe / APP2 ICC_PROFILE\0
    assert!(
        open_image(&wrap_jpg(
            &[seg(0xE0, b"JFIF\x00\x01"), ok_sof.clone()].concat()
        ))
        .is_err()
    );
    assert!(
        open_image(&wrap_jpg(
            &[seg(0xEE, b"Adobe\x00"), ok_sof.clone()].concat()
        ))
        .is_err()
    );
    assert!(
        open_image(&wrap_jpg(
            &[seg(0xE2, b"ICC_PROFILE\x00"), ok_sof.clone()].concat()
        ))
        .is_err()
    );
    assert!(
        open_image(&wrap_jpg(
            &[ok_sof.clone(), seg(0xE2, b"ICC_PROFILE\x00")].concat()
        ))
        .is_ok()
    );

    // APP13 Photoshop 3.0\0
    let ps_miss_nl = [b"Photoshop 3.0\x00".as_slice(), b"8BIM\x03\xed"].concat();
    assert!(
        open_image(&wrap_jpg(
            &[seg(0xED, &ps_miss_nl), ok_sof.clone()].concat()
        ))
        .is_err()
    );
    let ps_trunc_sz = [
        b"Photoshop 3.0\x00".as_slice(),
        b"8BIM\x03\xed\x00\x00\x00\x01",
    ]
    .concat();
    assert!(
        open_image(&wrap_jpg(
            &[seg(0xED, &ps_trunc_sz), ok_sof.clone()].concat()
        ))
        .is_ok()
    );

    // APP2 MPF\0: short 0xB002 rejects; malformed TIFF falls back
    let mut mpf_short = b"MPF\x00II*\x00\x08\x00\x00\x00".to_vec();
    mpf_short.extend_from_slice(&2u16.to_le_bytes());
    mpf_short.extend_from_slice(&[0x01, 0xB0, 4, 0, 1, 0, 0, 0, 1, 0, 0, 0]);
    mpf_short.extend_from_slice(&[0x02, 0xB0, 7, 0, 1, 0, 0, 0, 0, 0, 0, 0]);
    mpf_short.extend_from_slice(&0u32.to_le_bytes());
    assert!(open_image(&wrap_jpg(&[seg(0xE2, &mpf_short), ok_sof.clone()].concat())).is_err());
    assert!(
        open_image(&wrap_jpg(
            &[seg(0xE2, b"MPF\x00BAD"), ok_sof.clone()].concat()
        ))
        .is_ok()
    );

    // Zero-width/height SOF alone rejects; followed by valid SOF succeeds
    let zero_w = sof_seg(0xC0, 8, 4, 0, &[1, 0x11, 0]);
    assert!(open_image(&wrap_jpg(&zero_w)).is_err());
    assert!(open_image(&wrap_jpg(&[zero_w, ok_sof.clone()].concat())).is_ok());

    // DQT invalid length rejects; 0xFFC8 / 0xFFF0 and SOS len < 2 succeed
    assert!(open_image(&wrap_jpg(&[seg(0xDB, &[0, 0]), ok_sof.clone()].concat())).is_err());
    assert!(
        open_image(&wrap_jpg(
            &[vec![0xFF, 0xC8, 0xFF, 0xF0], ok_sof.clone()].concat()
        ))
        .is_ok()
    );
    let mut sos_short = vec![0xFF, 0xD8];
    sos_short.extend_from_slice(&ok_sof);
    sos_short.extend_from_slice(b"\xff\xda\x00\x00\xff\xd9");
    assert!(open_image(&sos_short).is_ok());
}

fn pack_classic_mpf(entries: &[(u16, u16, u32, &[u8])]) -> Vec<u8> {
    let mut out = b"MPF\x00II*\x00".to_vec();
    out.extend_from_slice(&8u32.to_le_bytes());
    out.extend_from_slice(&u16::try_from(entries.len()).unwrap().to_le_bytes());
    let mut cursor = 8 + 2 + entries.len() * 12 + 4;
    let mut extra = Vec::new();
    for &(tag, typ, cnt, val) in entries {
        out.extend_from_slice(&tag.to_le_bytes());
        out.extend_from_slice(&typ.to_le_bytes());
        out.extend_from_slice(&cnt.to_le_bytes());
        if val.len() <= 4 {
            let mut v = [0u8; 4];
            v[..val.len()].copy_from_slice(val);
            out.extend_from_slice(&v);
        } else {
            out.extend_from_slice(&u32::try_from(cursor).unwrap().to_le_bytes());
            extra.extend_from_slice(val);
            cursor += val.len();
        }
    }
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&extra);
    out
}

fn pack_bigtiff_mpf(endian_be: bool, entries: &[(u16, u16, u64, &[u8])]) -> Vec<u8> {
    let mut out = if endian_be {
        b"MPF\x00MM\x00+\x00\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00\x10".to_vec()
    } else {
        b"MPF\x00II+\x00\x08\x00\x00\x00\x10\x00\x00\x00\x00\x00\x00\x00".to_vec()
    };
    let push_u16 = |buf: &mut Vec<u8>, v: u16| {
        buf.extend_from_slice(&if endian_be {
            v.to_be_bytes()
        } else {
            v.to_le_bytes()
        });
    };
    let push_u64 = |buf: &mut Vec<u8>, v: u64| {
        buf.extend_from_slice(&if endian_be {
            v.to_be_bytes()
        } else {
            v.to_le_bytes()
        });
    };
    push_u64(&mut out, u64::try_from(entries.len()).unwrap());
    let mut cursor = 16 + 8 + entries.len() * 20 + 8;
    let mut extra = Vec::new();
    for &(tag, typ, cnt, val) in entries {
        push_u16(&mut out, tag);
        push_u16(&mut out, typ);
        push_u64(&mut out, cnt);
        if val.len() <= 8 {
            let mut v = [0u8; 8];
            v[..val.len()].copy_from_slice(val);
            out.extend_from_slice(&v);
        } else {
            push_u64(&mut out, u64::try_from(cursor).unwrap());
            extra.extend_from_slice(val);
            cursor += val.len();
        }
    }
    push_u64(&mut out, 0);
    out.extend_from_slice(&extra);
    out
}

#[test]
fn sol_r3_jpeg_header_regressions_match_pillow() {
    let ok_sof = sof_seg(0xC0, 8, 384, 576, &[1, 0x11, 0]);
    let bomb_sof = sof_seg(0xC0, 8, 10_000, 20_000, &[1, 0x11, 0]);
    let mut q2 = Vec::new();
    q2.extend_from_slice(&1u32.to_le_bytes());
    q2.extend_from_slice(&2u32.to_le_bytes());

    // P2-01: 0xFF01 TEM rejects before and after SOF; pre-SOS 0xFFD9 EOI keeps scanning.
    assert!(open_image(&wrap_jpg(&[vec![0xFF, 0x01], ok_sof.clone()].concat())).is_err());
    assert!(open_image(&wrap_jpg(&[ok_sof.clone(), vec![0xFF, 0x01]].concat())).is_err());
    assert!(open_image(&wrap_jpg(&[vec![0xFF, 0xD9], ok_sof.clone()].concat())).is_ok());
    assert!(open_image(&wrap_jpg(&[ok_sof.clone(), vec![0xFF, 0xD9]].concat())).is_ok());

    // P2-02: 0xB001 count=2 unwraps first integer (length=1), rejecting short 0xB002 types 1 & 7.
    for typ in [1u16, 7u16] {
        for count in [1usize, 15usize] {
            let zeroes = vec![0u8; count];
            let mpf =
                pack_classic_mpf(&[(0xB001, 4, 2, &q2), (0xB002, typ, count as u32, &zeroes)]);
            let err =
                open_image(&wrap_jpg(&[seg(0xE2, &mpf), ok_sof.clone()].concat())).unwrap_err();
            assert!(
                err.starts_with("UnidentifiedImageError:"),
                "typ={typ} count={count}: {err}"
            );
        }
    }
    let valid_16 = pack_classic_mpf(&[(0xB001, 4, 2, &q2), (0xB002, 7, 16, &[0u8; 16])]);
    assert!(open_image(&wrap_jpg(&[seg(0xE2, &valid_16), ok_sof.clone()].concat())).is_ok());

    // P2-03: BigTIFF MPF falls back cleanly to base JPEG (matching _getmp 8-byte header slice).
    for endian_be in [false, true] {
        let two = if endian_be {
            2u64.to_be_bytes()
        } else {
            2u64.to_le_bytes()
        };
        let big_mpf = pack_bigtiff_mpf(endian_be, &[(0xB001, 4, 1, &two), (0xB002, 7, 1, &[0])]);
        assert!(open_image(&wrap_jpg(&[seg(0xE2, &big_mpf), ok_sof.clone()].concat())).is_ok());
    }

    // P2-04: Short classic MPF fails via fallback BEFORE decompression bomb check.
    let classic_short =
        pack_classic_mpf(&[(0xB001, 4, 1, &2u32.to_le_bytes()), (0xB002, 7, 1, &[0])]);
    let err = open_image(&wrap_jpg(&[seg(0xE2, &classic_short), bomb_sof].concat())).unwrap_err();
    assert!(err.starts_with("UnidentifiedImageError:"), "{err}");
}
