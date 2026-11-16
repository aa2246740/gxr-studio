use super::ftcalc::{ft_div_fix, ft_mul_fix, ft_pix_ceil, ft_pix_floor};
use super::glyf::{FT_CURVE_TAG_ON, FT_OUTLINE_OVERLAP, FontFace, SizeMetrics};

fn push_u16(out: &mut Vec<u8>, v: u16) {
    out.extend_from_slice(&v.to_be_bytes());
}

fn push_i16(out: &mut Vec<u8>, v: i16) {
    out.extend_from_slice(&v.to_be_bytes());
}

fn push_u32(out: &mut Vec<u8>, v: u32) {
    out.extend_from_slice(&v.to_be_bytes());
}

fn pack_sfnt(tables: &[(&str, Vec<u8>)]) -> Vec<u8> {
    let mut out = Vec::new();
    push_u32(&mut out, 0x0001_0000);
    push_u16(&mut out, tables.len() as u16);
    out.extend_from_slice(&[0; 6]);
    let mut at = 12 + 16 * tables.len();
    let mut body = Vec::new();
    for (tag, blob) in tables {
        out.extend_from_slice(tag.as_bytes());
        push_u32(&mut out, 0);
        push_u32(&mut out, at as u32);
        push_u32(&mut out, blob.len() as u32);
        body.extend_from_slice(blob);
        at += blob.len();
    }
    out.extend_from_slice(&body);
    out
}

fn build_cmap4_single(cp: u16, gid: u16) -> Vec<u8> {
    let mut sub = Vec::new();
    push_u16(&mut sub, 4); // format
    push_u16(&mut sub, 32); // length
    push_u16(&mut sub, 0); // lang
    push_u16(&mut sub, 4); // segCountX2 (2 segs)
    push_u16(&mut sub, 4);
    push_u16(&mut sub, 1);
    push_u16(&mut sub, 0);
    push_u16(&mut sub, cp);
    push_u16(&mut sub, 0xFFFF);
    push_u16(&mut sub, 0);
    push_u16(&mut sub, cp);
    push_u16(&mut sub, 0xFFFF);
    let delta = gid.wrapping_sub(cp) as i16;
    push_i16(&mut sub, delta);
    push_i16(&mut sub, 1);
    push_u16(&mut sub, 0);
    push_u16(&mut sub, 0);

    let mut cmap = Vec::new();
    push_u16(&mut cmap, 0);
    push_u16(&mut cmap, 1);
    push_u16(&mut cmap, 3);
    push_u16(&mut cmap, 1);
    push_u32(&mut cmap, 12);
    cmap.extend_from_slice(&sub);
    cmap
}

fn build_simple_glyph(x_min: i16, x_max: i16, pts: &[(i16, i16)], overlap: bool) -> Vec<u8> {
    let mut g = Vec::new();
    push_i16(&mut g, 1); // 1 contour
    push_i16(&mut g, x_min);
    push_i16(&mut g, 0);
    push_i16(&mut g, x_max);
    push_i16(&mut g, 500);
    push_u16(&mut g, (pts.len() - 1) as u16); // endPtsOfContours
    push_u16(&mut g, 0); // instructionLength
    for (idx, _) in pts.iter().enumerate() {
        let f = if idx == 0 && overlap { 0x41 } else { 0x01 };
        g.push(f);
    }
    let mut prev_x = 0_i16;
    for &(x, _) in pts {
        push_i16(&mut g, x - prev_x);
        prev_x = x;
    }
    let mut prev_y = 0_i16;
    for &(_, y) in pts {
        push_i16(&mut g, y - prev_y);
        prev_y = y;
    }
    if g.len() % 2 != 0 {
        g.push(0);
    }
    g
}

fn make_test_ttf(head_flags: u16, os2_sel: Option<u16>) -> Vec<u8> {
    let mut head = vec![0_u8; 54];
    head[16..18].copy_from_slice(&head_flags.to_be_bytes());
    head[18..20].copy_from_slice(&1000_u16.to_be_bytes());
    let mut hhea = vec![0_u8; 36];
    hhea[4..6].copy_from_slice(&800_i16.to_be_bytes());
    hhea[6..8].copy_from_slice(&(-200_i16).to_be_bytes());
    hhea[34..36].copy_from_slice(&2_u16.to_be_bytes());
    let mut maxp = vec![0_u8; 6];
    maxp[4..6].copy_from_slice(&3_u16.to_be_bytes());

    let mut hmtx = Vec::new();
    push_u16(&mut hmtx, 500);
    push_i16(&mut hmtx, 0);
    push_u16(&mut hmtx, 600);
    push_i16(&mut hmtx, 50);
    push_i16(&mut hmtx, 100); // gid 2 lsb

    let g1 = build_simple_glyph(100, 400, &[(100, 0), (400, 0), (250, 500)], true);
    let mut g2 = Vec::new();
    push_i16(&mut g2, -1); // composite
    push_i16(&mut g2, 100);
    push_i16(&mut g2, 0);
    push_i16(&mut g2, 400);
    push_i16(&mut g2, 500);
    // flags: ARGS_ARE_XY_VALUES (0x02) | ROUND_XY_TO_GRID (0x04) | OVERLAP_COMPOUND (0x0400)
    push_u16(&mut g2, 0x0406);
    push_u16(&mut g2, 1); // subglyph 1
    g2.push(10_u8); // dx = 10
    g2.push(20_u8); // dy = 20

    let mut glyf = Vec::new();
    let mut loca = Vec::new();
    push_u16(&mut loca, 0); // gid 0 empty
    push_u16(&mut loca, 0);
    glyf.extend_from_slice(&g1);
    push_u16(&mut loca, (glyf.len() / 2) as u16);
    glyf.extend_from_slice(&g2);
    push_u16(&mut loca, (glyf.len() / 2) as u16);

    let mut tables = vec![
        ("head", head),
        ("hhea", hhea),
        ("maxp", maxp),
        ("hmtx", hmtx),
        ("loca", loca),
        ("glyf", glyf),
        ("cmap", build_cmap4_single(65, 1)),
    ];
    if let Some(sel) = os2_sel {
        let mut os2 = vec![0_u8; 78];
        os2[62..64].copy_from_slice(&sel.to_be_bytes());
        os2[68..70].copy_from_slice(&900_i16.to_be_bytes());
        os2[70..72].copy_from_slice(&(-150_i16).to_be_bytes());
        tables.push(("OS/2", os2));
    }
    pack_sfnt(&tables)
}

#[test]
fn integer_ppem_vs_fractional_size_metrics() {
    let ttf_int = make_test_ttf(8, None);
    let face_int = FontFace::parse(&ttf_int).expect("parse int ppem");
    // 26.6 width/height = 20.5 px = 1312; bit 3 rounds ppem to 21 and scales from 21*64.
    let m_int = SizeMetrics::from_26_6(&face_int, 1312, 1312).expect("metrics");
    assert_eq!(m_int.x_ppem, 21);
    assert_eq!(m_int.root_x_scale, ft_div_fix(1312, 1000));
    assert_eq!(m_int.hinted_x_scale, ft_div_fix(21 << 6, 1000));
    assert_eq!(
        m_int.root_ascender,
        ft_pix_ceil(ft_mul_fix(800, m_int.root_y_scale))
    );
    assert_eq!(
        m_int.root_descender,
        ft_pix_floor(ft_mul_fix(-200, m_int.root_y_scale))
    );

    let ttf_frac = make_test_ttf(0, Some(128));
    let face_frac = FontFace::parse(&ttf_frac).expect("parse os2");
    assert_eq!(face_frac.ascender, 900);
    assert_eq!(face_frac.descender, -150);
    let m_frac = SizeMetrics::from_26_6(&face_frac, 1312, 1312).expect("metrics");
    assert_eq!(m_frac.hinted_x_scale, m_frac.root_x_scale);
}

#[test]
fn simple_and_composite_glyph_loading() {
    let ttf = make_test_ttf(8, None);
    let face = FontFace::parse(&ttf).expect("parse");
    let m = SizeMetrics::from_pixel_size(&face, 32).expect("metrics");
    let g1 = face.load_char(&m, 'A').expect("load A");
    assert_eq!(g1.outline.points.len(), 3);
    assert_eq!(g1.outline.contours, vec![2]);
    assert_eq!(g1.outline.tags, vec![FT_CURVE_TAG_ON; 3]);
    assert_ne!(g1.outline.flags & FT_OUTLINE_OVERLAP, 0);
    // lsb = 50 -> scaled pp1 = ft_mul_fix(x_min - lsb, scale) is subtracted from scaled x
    let g1_pp1 = ft_mul_fix(100 - 50, m.hinted_x_scale);
    assert_eq!(
        g1.outline.points[0].x,
        ft_mul_fix(100, m.hinted_x_scale) - g1_pp1
    );

    let g2 = face.load_glyph(&m, 2).expect("load composite");
    assert_eq!(g2.outline.points.len(), 3);
    assert_ne!(g2.outline.flags & FT_OUTLINE_OVERLAP, 0);
    // Subglyph dx=10 is NOT rounded in v40, dy=20 IS rounded to pixel grid.
    let dx = ft_mul_fix(10, m.hinted_x_scale);
    let dy = (ft_mul_fix(20, m.hinted_y_scale) + 32) & !63;
    let g2_pp1 = ft_mul_fix(100 - 100, m.hinted_x_scale);
    assert_eq!(
        g2.outline.points[0].x,
        ft_mul_fix(100, m.hinted_x_scale) + dx - g2_pp1
    );
    assert_eq!(g2.outline.points[0].y, g1.outline.points[0].y + dy);
}
