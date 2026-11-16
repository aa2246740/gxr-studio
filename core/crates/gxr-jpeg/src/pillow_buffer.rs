//! When Pillow 12.2.0 can't write an `optimize=True` JPEG.
//!
//! `JpegImagePlugin._save` sizes one output buffer for the whole file, because with
//! `optimize_coding` libjpeg writes every table and the whole scan inside `jpeg_finish_compress`,
//! where its destination can't suspend:
//! `bufsize = max(MAXBLOCK, k*w*h + len(exif) + 5 + len(extra) + 1, 4*w)` (`ImageFile._save`),
//! with `k = 2` at quality >= 95, else 1, and `extra` the APP2 ICC markers. `JpegEncode.c` gets a
//! fresh buffer of that size on every `encode(bufsize)` call, and returns between its states:
//! SOI and APP0 (`jpeg_start_compress`), APP1 EXIF, the ICC markers (copied, possibly across
//! calls), then the scanlines only when at least 1024 bytes are free, and `jpeg_finish_compress`
//! only when at least 100 are. If the finish output doesn't fit in what is left, the destination
//! fills (free reaches 0) and libjpeg stops with `JERR_CANT_SUSPEND` ("Suspension not allowed
//! here"); `_finish` catches it and tries the reduced EXIF, then gives up with `encode_failed`.
//!
//! Measured on Pillow 12.2.0 (round-6 `mobile-jpeg-optimize/scripts/bufprobe*.py`): the finish
//! output `F` fits only when `F < free` (300x400 q95: F = 239980 fits, 239981 = free fails), and
//! the scanline step starts a new buffer with 1022 bytes free but not with 1024.

const MAXBLOCK: u64 = 65536;
/// SOI (2) and the JFIF APP0 segment (18).
const SOI_APP0: u64 = 20;

/// Whether Pillow fits the file: `exif` and `extra` are the APP1 payload and the APP2 segment
/// bytes (markers included), `finish` the bytes from the first DQT to EOI. All sizes come from a
/// file this crate just wrote, so they are far below `u64` limits; the sums still saturate.
pub(crate) fn pillow_fits(
    width: u32,
    height: u32,
    quality: i32,
    exif: u64,
    extra: u64,
    finish: u64,
) -> bool {
    let k: u64 = if quality >= 95 { 2 } else { 1 };
    let mut want = k
        .saturating_mul(u64::from(width))
        .saturating_mul(u64::from(height));
    if exif > 0 {
        want = want.saturating_add(exif + 5);
    }
    if extra > 0 {
        want = want.saturating_add(extra + 1);
    }
    let size = MAXBLOCK.max(want).max(u64::from(width).saturating_mul(4));

    // Call 1: `jpeg_start_compress` writes SOI and APP0.
    let mut used = SOI_APP0;
    // State 2: the EXIF marker needs `len + 5` free, else the next call.
    if exif > 0 {
        if exif + 5 > size - used {
            used = 0;
        }
        used += exif + 4;
    }
    // State 3: the ICC markers, copied in pieces across calls.
    let mut left = extra;
    loop {
        let n = left.min(size.saturating_sub(used));
        used += n;
        left -= n;
        if left == 0 {
            break;
        }
        used = 0;
    }
    // State 5: scanlines need 1024 free; state 6: `jpeg_finish_compress` needs 100.
    if size - used < 1024 {
        used = 0;
    }
    if size - used < 100 {
        used = 0;
    }
    finish < size - used
}

#[cfg(test)]
mod tests {
    use super::*;

    const ICC: u64 = 606;

    #[test]
    fn measured_boundaries() {
        // (h, w, q, exif, free after the headers) from bufprobe.py, without EXIF.
        assert!(pillow_fits(400, 300, 95, 0, ICC, 239_980));
        assert!(!pillow_fits(400, 300, 95, 0, ICC, 239_981));
        assert!(pillow_fits(256, 256, 92, 0, ICC, 65_516));
        assert!(!pillow_fits(256, 256, 92, 0, ICC, 65_517));
        // MAXBLOCK regime: 65536 - 626 = 64910.
        assert!(pillow_fits(160, 160, 100, 0, ICC, 64_909));
        assert!(!pillow_fits(160, 160, 100, 0, ICC, 64_910));
        // With a 20034-byte EXIF: 71846 - 20664 = 51182.
        assert!(pillow_fits(160, 160, 100, 20_034, ICC, 51_181));
        assert!(!pillow_fits(160, 160, 100, 20_034, ICC, 51_182));
    }

    #[test]
    fn small_room_starts_a_new_buffer() {
        // 18x18 q100, F = 1878: 1022 free -> fresh 65536-byte buffer; 1024 free -> fails.
        let exif_for = |free: u64| MAXBLOCK - free - 24 - ICC;
        assert!(pillow_fits(18, 18, 100, exif_for(1022), ICC, 1878));
        assert!(!pillow_fits(18, 18, 100, exif_for(1024), ICC, 1878));
    }

    #[test]
    fn icc_split_across_calls() {
        // A 1x1 picture with an EXIF so large the ICC markers don't fit after it.
        let exif = MAXBLOCK - 24 - 100;
        assert!(pillow_fits(1, 1, 50, exif, ICC, 600));
        // Huge sizes saturate instead of overflowing.
        assert!(pillow_fits(u32::MAX, u32::MAX, 95, u64::MAX / 2, ICC, 10));
    }
}
