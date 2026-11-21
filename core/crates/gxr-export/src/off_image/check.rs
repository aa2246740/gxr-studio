//! 关机图段重封装与 DAT 260（OFF_IMG_STRICT）严格校验。

use gxr_raw::{CoreError, Json};

pub const WIDTH: u32 = 640;
pub const HEIGHT: u32 = 480;
pub const MAX_BYTES: usize = 32768;
pub const HEADER_BYTES: usize = 595;
pub const DRI_VALUE: u16 = 40;
pub const RESTARTS: usize = 59;

const DC0_BITS: [u8; 16] = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0];
const DC1_BITS: [u8; 16] = [0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0];
const DC_VALS: [u8; 12] = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11];
const AC0_BITS: [u8; 16] = [0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D];
const AC0_VALS: [u8; 162] = [
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06, 0x13, 0x51, 0x61, 0x07,
    0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08, 0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0,
    0x24, 0x33, 0x62, 0x72, 0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49,
    0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69,
    0x6A, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
    0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7,
    0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5,
    0xC6, 0xC7, 0xC8, 0xC9, 0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
    0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8,
    0xF9, 0xFA,
];
const AC1_BITS: [u8; 16] = [0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77];
const AC1_VALS: [u8; 162] = [
    0x00, 0x01, 0x02, 0x03, 0x11, 0x04, 0x05, 0x21, 0x31, 0x06, 0x12, 0x41, 0x51, 0x07, 0x61, 0x71,
    0x13, 0x22, 0x32, 0x81, 0x08, 0x14, 0x42, 0x91, 0xA1, 0xB1, 0xC1, 0x09, 0x23, 0x33, 0x52, 0xF0,
    0x15, 0x62, 0x72, 0xD1, 0x0A, 0x16, 0x24, 0x34, 0xE1, 0x25, 0xF1, 0x17, 0x18, 0x19, 0x1A, 0x26,
    0x27, 0x28, 0x29, 0x2A, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48,
    0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68,
    0x69, 0x6A, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7A, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
    0x88, 0x89, 0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3, 0xA4, 0xA5,
    0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3,
    0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA,
    0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8,
    0xF9, 0xFA,
];

/// 标准 Annex K.3 霍夫曼表（固定顺序 DC0、DC1、AC0、AC1）。
pub const ANNEX_K: [(u8, &[u8; 16], &[u8]); 4] = [
    (0x00, &DC0_BITS, &DC_VALS),
    (0x01, &DC1_BITS, &DC_VALS),
    (0x10, &AC0_BITS, &AC0_VALS),
    (0x11, &AC1_BITS, &AC1_VALS),
];

const EXPECTED_DHT_ORDER: [u8; 4] = [0x00, 0x01, 0x10, 0x11];
const EXPECTED_SOF0_BODY: [u8; 15] = [
    8, 0x01, 0xE0, 0x02, 0x80, 3, 1, 0x21, 0, 2, 0x11, 1, 3, 0x11, 1,
];
const EXPECTED_SOS_BODY: [u8; 10] = [3, 1, 0x00, 2, 0x11, 3, 0x11, 0, 63, 0];

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CheckIssue {
    pub code: &'static str,
    pub message: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OffImageCheck {
    pub ok: bool,
    pub dat_state: u8,
    pub errors: Vec<CheckIssue>,
}

impl OffImageCheck {
    pub fn errors_json(&self) -> Json {
        Json::Array(
            self.errors
                .iter()
                .map(|e| {
                    Json::object([
                        ("code", Json::str(e.code)),
                        ("message", Json::str(&e.message)),
                    ])
                })
                .collect(),
        )
    }

    pub fn to_json(&self) -> Json {
        Json::object([
            ("ok", Json::Bool(self.ok)),
            ("dat_state", Json::Int(i64::from(self.dat_state))),
            ("errors", self.errors_json()),
        ])
    }
}

fn push_segment(out: &mut Vec<u8>, marker: u8, body: &[u8]) {
    out.push(0xFF);
    out.push(marker);
    let len = (body.len() + 2) as u16;
    out.extend_from_slice(&len.to_be_bytes());
    out.extend_from_slice(body);
}

/// 合法关机图 `[136, 595)` 的固定字节：SOF0、DHT、DRI、SOS。
pub fn fixed_tail() -> Vec<u8> {
    let mut out = Vec::with_capacity(HEADER_BYTES - 136);
    push_segment(&mut out, 0xC0, &EXPECTED_SOF0_BODY);
    let mut dht = Vec::with_capacity(416);
    for &(tc, bits, vals) in &ANNEX_K {
        dht.push(tc);
        dht.extend_from_slice(bits);
        dht.extend_from_slice(vals);
    }
    push_segment(&mut out, 0xC4, &dht);
    push_segment(&mut out, 0xDD, &DRI_VALUE.to_be_bytes());
    push_segment(&mut out, 0xDA, &EXPECTED_SOS_BODY);
    out
}

/// IEEE CRC-32（与固件 `OFF_HDR_CRC` 一致）。
pub fn header_crc() -> u32 {
    let tail = fixed_tail();
    let mut crc = 0xFFFF_FFFF_u32;
    for &byte in &tail {
        crc ^= u32::from(byte);
        for _ in 0..8 {
            if (crc & 1) != 0 {
                crc = (crc >> 1) ^ 0xEDB8_8320;
            } else {
                crc >>= 1;
            }
        }
    }
    !crc
}

struct Bits<'a> {
    data: &'a [u8],
    pos: usize,
    end: usize,
    acc: u8,
    n: u8,
}

impl<'a> Bits<'a> {
    fn new(data: &'a [u8], pos: usize, end: usize) -> Self {
        Self {
            data,
            pos,
            end,
            acc: 0,
            n: 0,
        }
    }

    fn bit(&mut self) -> i32 {
        if self.n == 0 {
            if self.pos >= self.end {
                return -1;
            }
            let c = self.data[self.pos];
            if c == 0xFF {
                if self.pos + 1 >= self.end || self.data[self.pos + 1] != 0 {
                    return -1;
                }
                self.pos += 1;
            }
            self.pos += 1;
            self.acc = c;
            self.n = 8;
        }
        self.n -= 1;
        i32::from((self.acc >> self.n) & 1)
    }

    fn bits(&mut self, k: u8) -> i32 {
        let mut v = 0_i32;
        for _ in 0..k {
            let x = self.bit();
            if x < 0 {
                return -1;
            }
            v = (v << 1) | x;
        }
        v
    }

    fn sym(&mut self, table: &[u8]) -> i32 {
        let mut code = 0_i32;
        let mut first = 0_i32;
        let mut idx = 0_usize;
        for &cnt_u8 in &table[..16] {
            let x = self.bit();
            if x < 0 {
                return -1;
            }
            code |= x;
            let cnt = i32::from(cnt_u8);
            if code - first < cnt {
                let offset = 16 + idx + (code - first) as usize;
                return i32::from(*table.get(offset).unwrap_or(&0));
            }
            idx += usize::from(cnt_u8);
            first = (first + cnt) << 1;
            code <<= 1;
        }
        -1
    }
}

fn walk_entropy(data: &[u8], end: usize) -> bool {
    if data.len() < HEADER_BYTES || end < HEADER_BYTES {
        return false;
    }
    let dc = [&data[160..205], &data[189..234]];
    let ac = [&data[218..413], &data[397..575]];
    let mut b = Bits::new(data, HEADER_BYTES, end);
    for iv in 0..=RESTARTS {
        b.n = 0;
        for m in 0..160_usize {
            let comp = (m & 3) >> 1;
            let s = b.sym(dc[comp]);
            if !(0..=16).contains(&s) || b.bits(s as u8) < 0 {
                return false;
            }
            let mut k = 1_usize;
            while k < 64 {
                let rs = b.sym(ac[comp]);
                if rs < 0 {
                    return false;
                }
                let low = (rs & 15) as u8;
                if low == 0 {
                    if rs != 0xF0 {
                        break;
                    }
                    k += 16;
                    continue;
                }
                k += (rs >> 4) as usize;
                if b.bits(low) < 0 {
                    return false;
                }
                k += 1;
            }
            if k > 64 {
                return false;
            }
        }
        if b.n > 0 {
            let mask = (1_u8 << b.n) - 1;
            if (b.acc & mask) != mask {
                return false;
            }
        }
        if iv < RESTARTS {
            if b.pos + 1 >= b.end
                || data[b.pos] != 0xFF
                || data[b.pos + 1] != (0xD0 | (iv as u8 & 7))
            {
                return false;
            }
            b.pos += 2;
        }
    }
    b.pos == b.end
}

type ParsedSegments<'a> = (Vec<(u8, &'a [u8])>, Option<usize>, Option<String>);

fn parse_header_segments(data: &[u8]) -> ParsedSegments<'_> {
    let mut segs = Vec::new();
    let mut pos = 2_usize;
    let n = data.len();
    while pos < n {
        if data[pos] != 0xFF {
            return (segs, None, Some(format!("偏移 {pos} 处缺少段标记")));
        }
        while pos < n && data[pos] == 0xFF {
            pos += 1;
        }
        if pos >= n {
            return (segs, None, Some("文件在段标记处截断".into()));
        }
        let marker = data[pos];
        pos += 1;
        if marker == 0xD9 {
            return (segs, None, Some("遇到 EOI 之前没有找到 SOS".into()));
        }
        if pos + 2 > n {
            return (segs, None, Some(format!("段 FF{marker:02X} 缺少长度字段")));
        }
        let length = usize::from(u16::from_be_bytes([data[pos], data[pos + 1]]));
        if length < 2 || pos + length > n {
            return (segs, None, Some(format!("段 FF{marker:02X} 长度越界")));
        }
        let body = &data[pos + 2..pos + length];
        segs.push((marker, body));
        pos += length;
        if marker == 0xDA {
            return (segs, Some(pos), None);
        }
    }
    (segs, None, Some("缺少扫描头 SOS".into()))
}

fn push_issue(issues: &mut Vec<CheckIssue>, code: &'static str, msg: impl Into<String>) {
    issues.push(CheckIssue {
        code,
        message: msg.into(),
    });
}

fn diagnose_dqt(dqts: &[&[u8]], issues: &mut Vec<CheckIssue>) {
    if dqts.is_empty() {
        push_issue(issues, "missing_dqt", "缺少量化表段 DQT。");
    } else if dqts.len() > 1 {
        push_issue(
            issues,
            "multiple_dqt",
            format!(
                "存在 {} 个 DQT 段，必须合并为 1 个 DQT 段（表 0 和表 1）。",
                dqts.len()
            ),
        );
    } else if dqts[0].len() != 130 || dqts[0][0] != 0 || dqts[0][65] != 1 {
        push_issue(
            issues,
            "bad_dqt",
            "DQT 段格式不对（需要 130 B，包含 8 位量化表 0 和 1）。",
        );
    }
}

fn diagnose_sof(segs: &[(u8, &[u8])], issues: &mut Vec<CheckIssue>) {
    let sof0s: Vec<&[u8]> = segs
        .iter()
        .filter(|(m, _)| *m == 0xC0)
        .map(|(_, b)| *b)
        .collect();
    let other = segs.iter().any(|(m, _)| matches!(*m, 0xC1..=0xC3));
    if other || sof0s.is_empty() {
        push_issue(
            issues,
            "missing_sof0",
            "缺少基线帧头 SOF0（仅支持基线 JPEG）。",
        );
        return;
    }
    if sof0s.len() > 1 || sof0s[0].len() < 6 {
        push_issue(issues, "bad_sof0", "SOF0 帧头不完整。");
        return;
    }
    let body = sof0s[0];
    let precision = body[0];
    let height = u32::from(u16::from_be_bytes([body[1], body[2]]));
    let width = u32::from(u16::from_be_bytes([body[3], body[4]]));
    let ncomp = body[5];
    if width != WIDTH || height != HEIGHT {
        push_issue(
            issues,
            "bad_dimensions",
            format!("尺寸为 {width}×{height}，必须为 {WIDTH}×{HEIGHT}。"),
        );
    }
    if ncomp != 3 || body.len() != 15 {
        push_issue(
            issues,
            "bad_subsampling",
            "必须为 3 分量 4:2:2 采样（Y 2×1，Cb/Cr 1×1）。",
        );
        return;
    }
    let samplings = (body[7], body[10], body[13]);
    if samplings != (0x21, 0x11, 0x11) {
        if samplings == (0x22, 0x11, 0x11) {
            push_issue(
                issues,
                "bad_subsampling",
                "色度采样为 4:2:0，必须为 4:2:2（Y 2×1，Cb/Cr 1×1）。",
            );
        } else {
            push_issue(
                issues,
                "bad_subsampling",
                "色度采样不是 4:2:2（Y 2×1，Cb/Cr 1×1）。",
            );
        }
    } else if precision != 8 || body != EXPECTED_SOF0_BODY {
        push_issue(issues, "bad_sof0", "SOF0 帧头参数不符合要求。");
    }
}

fn diagnose_dht(dhts: &[&[u8]], issues: &mut Vec<CheckIssue>) {
    if dhts.is_empty() {
        push_issue(issues, "missing_dht", "缺少霍夫曼表段 DHT。");
        return;
    }
    if dhts.len() > 1 {
        push_issue(
            issues,
            "multiple_dht",
            format!("存在 {} 个 DHT 段，必须合并为 1 个 DHT 段。", dhts.len()),
        );
        return;
    }
    let mut rem = dhts[0];
    let mut parsed: Vec<(u8, &[u8], &[u8])> = Vec::new();
    while !rem.is_empty() {
        if rem.len() < 17 {
            push_issue(issues, "bad_dht", "DHT 段截断。");
            return;
        }
        let tc = rem[0];
        let bits = &rem[1..17];
        let count: usize = bits.iter().map(|&x| usize::from(x)).sum();
        let size = 17 + count;
        if rem.len() < size {
            push_issue(issues, "bad_dht", "DHT 表项长度越界。");
            return;
        }
        parsed.push((tc, bits, &rem[17..size]));
        rem = &rem[size..];
    }
    let order: Vec<u8> = parsed.iter().map(|(tc, _, _)| *tc).collect();
    if order.as_slice() != EXPECTED_DHT_ORDER {
        push_issue(
            issues,
            "bad_dht_order",
            "DHT 表顺序错误（必须为 DC0、DC1、AC0、AC1）。",
        );
        return;
    }
    let exact = parsed
        .iter()
        .zip(ANNEX_K.iter())
        .all(|((tc, b, v), (etc, eb, ev))| *tc == *etc && *b == eb.as_slice() && *v == *ev);
    if !exact {
        push_issue(issues, "bad_dht", "DHT 不是标准 Annex K.3 霍夫曼表。");
    }
}

fn diagnose_dri_sos_order(segs: &[(u8, &[u8])], issues: &mut Vec<CheckIssue>) {
    let dris: Vec<&[u8]> = segs
        .iter()
        .filter(|(m, _)| *m == 0xDD)
        .map(|(_, b)| *b)
        .collect();
    if dris.is_empty() {
        push_issue(
            issues,
            "missing_dri",
            format!("缺少重启间隔段 DRI（必须为 {DRI_VALUE}）。"),
        );
    } else if dris.len() > 1 || dris[0].len() != 2 {
        push_issue(
            issues,
            "bad_dri",
            format!("DRI 段格式不对（必须为 1 个且值为 {DRI_VALUE}）。"),
        );
    } else {
        let val = u16::from_be_bytes([dris[0][0], dris[0][1]]);
        if val != DRI_VALUE {
            push_issue(
                issues,
                "bad_dri",
                format!("DRI 重启间隔为 {val}，必须为 {DRI_VALUE}。"),
            );
        }
    }

    let soss: Vec<&[u8]> = segs
        .iter()
        .filter(|(m, _)| *m == 0xDA)
        .map(|(_, b)| *b)
        .collect();
    if soss.is_empty() || soss[0] != EXPECTED_SOS_BODY {
        push_issue(issues, "bad_sos", "SOS 扫描头不符合标准 3 分量基线布局。");
    }

    let markers: Vec<u8> = segs.iter().map(|(m, _)| *m).collect();
    if issues.is_empty() && markers.as_slice() != [0xDB, 0xC0, 0xC4, 0xDD, 0xDA] {
        push_issue(
            issues,
            "bad_segment_order",
            "段顺序不符合要求（必须依次为 SOI、DQT、SOF0、DHT、DRI、SOS）。",
        );
    }
}

fn diagnose_scan_and_eoi(
    data: &[u8],
    scan_start: usize,
    issues: &mut Vec<CheckIssue>,
) -> Option<usize> {
    let n = data.len();
    let mut eoi_pos: Option<usize> = None;
    let mut restarts = 0_usize;
    let mut bad_marker = false;
    let mut i = scan_start;
    while i < n {
        if data[i] == 0xFF {
            let mut j = i + 1;
            while j < n && data[j] == 0xFF {
                j += 1;
            }
            if j >= n {
                push_issue(issues, "missing_eoi", "文件末尾缺少 EOI 标记。");
                return None;
            }
            let code = data[j];
            if code == 0x00 {
                if j > i + 1 {
                    push_issue(
                        issues,
                        "unexpected_marker",
                        format!("偏移 {i} 处出现非法连续 FF 填充字节。"),
                    );
                    bad_marker = true;
                    break;
                }
                i = j + 1;
                continue;
            }
            if code == 0xD9 {
                eoi_pos = Some(j - 1);
                if j + 1 < n {
                    let extra = n - (j + 1);
                    push_issue(
                        issues,
                        "trailing_bytes",
                        format!("EOI 之后有多余的 {extra} 字节。"),
                    );
                }
                break;
            }
            if (0xD0..=0xD7).contains(&code) {
                if j > i + 1 {
                    push_issue(
                        issues,
                        "unexpected_marker",
                        format!("偏移 {i} 处 RST 标记前有非法填充字节。"),
                    );
                    bad_marker = true;
                    break;
                }
                let expected = 0xD0 | (restarts as u8 & 7);
                if code != expected {
                    push_issue(
                        issues,
                        "bad_rst_order",
                        format!(
                            "重启标记顺序错误：第 {} 个应为 RST{}，实际为 RST{}。",
                            restarts + 1,
                            restarts & 7,
                            code & 7
                        ),
                    );
                    bad_marker = true;
                    break;
                }
                restarts += 1;
                i = j + 1;
                continue;
            }
            push_issue(
                issues,
                "unexpected_marker",
                format!("扫描数据中出现意外标记 FF{code:02X}。"),
            );
            bad_marker = true;
            break;
        }
        i += 1;
    }

    if eoi_pos.is_none() && !bad_marker {
        push_issue(issues, "missing_eoi", "文件末尾缺少 EOI 标记。");
        return None;
    }
    if !bad_marker && restarts != RESTARTS {
        push_issue(
            issues,
            "missing_rst",
            format!("重启标记数量为 {restarts}，必须为 {RESTARTS} 个（RST0–7 循环）。"),
        );
    }
    let mut end = eoi_pos?;
    while end > scan_start && data[end - 1] == 0xFF {
        end -= 1;
    }
    Some(end)
}

/// DAT 260（OFF_IMG_STRICT）完整校验。
pub fn check_off_image(data: &[u8]) -> OffImageCheck {
    let mut issues = Vec::new();
    let n = data.len();
    if n == 0 {
        push_issue(&mut issues, "empty", "文件为空。");
        return OffImageCheck {
            ok: false,
            dat_state: 1,
            errors: issues,
        };
    }
    if n > MAX_BYTES {
        push_issue(
            &mut issues,
            "over_max_bytes",
            format!("{n} B 超过上限 {MAX_BYTES} B。"),
        );
    }
    if n < 2 || &data[..2] != b"\xFF\xD8" {
        push_issue(&mut issues, "missing_soi", "缺少 JPEG 起始标记 SOI。");
        let st = if n > MAX_BYTES { 1 } else { 2 };
        return OffImageCheck {
            ok: false,
            dat_state: st,
            errors: issues,
        };
    }

    let (segs, scan_start, parse_err) = parse_header_segments(data);
    for &(m, _) in &segs {
        if (0xE0..=0xEF).contains(&m) {
            push_issue(
                &mut issues,
                "forbidden_segment",
                format!("包含禁止的段 APP{}（不能含 APP/COM/EXIF）。", m - 0xE0),
            );
        } else if m == 0xFE {
            push_issue(
                &mut issues,
                "forbidden_segment",
                "包含禁止的注释段 COM（不能含 APP/COM/EXIF）。",
            );
        }
    }
    let dqts: Vec<&[u8]> = segs
        .iter()
        .filter(|(m, _)| *m == 0xDB)
        .map(|(_, b)| *b)
        .collect();
    let dhts: Vec<&[u8]> = segs
        .iter()
        .filter(|(m, _)| *m == 0xC4)
        .map(|(_, b)| *b)
        .collect();
    diagnose_dqt(&dqts, &mut issues);
    diagnose_sof(&segs, &mut issues);
    diagnose_dht(&dhts, &mut issues);
    diagnose_dri_sos_order(&segs, &mut issues);
    if let Some(msg) = parse_err
        && issues.is_empty()
    {
        push_issue(&mut issues, "bad_header", msg);
    }

    let end = scan_start.and_then(|start| diagnose_scan_and_eoi(data, start, &mut issues));

    if issues.is_empty() {
        let tail = fixed_tail();
        if n < HEADER_BYTES + 2 {
            push_issue(
                &mut issues,
                "too_short",
                format!("文件仅 {n} B，短于固定头长度。"),
            );
        } else if &data[..7] != b"\xFF\xD8\xFF\xDB\x00\x84\x00"
            || data[71] != 1
            || data[136..HEADER_BYTES] != tail
        {
            push_issue(
                &mut issues,
                "bad_header",
                "文件头与相机要求的固定段布局不一致。",
            );
        } else if &data[n - 2..] != b"\xFF\xD9" {
            push_issue(&mut issues, "missing_eoi", "文件末尾缺少 EOI 标记。");
        }
    }

    if issues.is_empty()
        && let Some(e) = end
        && !walk_entropy(data, e)
    {
        push_issue(
            &mut issues,
            "bad_scan_data",
            "扫描数据无法按 60 组 × 40 MCU 严格解码。",
        );
    }

    if issues.is_empty() {
        return OffImageCheck {
            ok: true,
            dat_state: 7,
            errors: issues,
        };
    }
    if n > MAX_BYTES {
        return OffImageCheck {
            ok: false,
            dat_state: 1,
            errors: issues,
        };
    }
    let state = if issues.len() == 1 && issues[0].code == "bad_scan_data" {
        8
    } else {
        2
    };
    OffImageCheck {
        ok: false,
        dat_state: state,
        errors: issues,
    }
}

/// 固件 B00 J 状态值：7 接受，1 空或超 32 KB，2 布局/标记错，8 熵解码错。
pub fn dat_state(data: &[u8]) -> u8 {
    check_off_image(data).dat_state
}

/// 将标准基线 4:2:2 + DRI 40 JPEG 重封装为相机布局。
pub fn remux_off_image(data: &[u8]) -> Result<Vec<u8>, CoreError> {
    if data.len() < 4 || &data[..2] != b"\xFF\xD8" {
        return Err(CoreError::new("encode_failed", "JPEG 缺少 SOI"));
    }
    let (segs, scan_start, parse_err) = parse_header_segments(data);
    let Some(scan_at) = scan_start else {
        return Err(CoreError::new(
            "encode_failed",
            parse_err.unwrap_or_else(|| "JPEG 缺少 SOS".into()),
        ));
    };

    let mut dqt_body = Vec::new();
    let mut sof0_body: Option<&[u8]> = None;
    let mut dht_concat = Vec::new();
    let mut dri_body: Option<&[u8]> = None;
    let mut sos_body: Option<&[u8]> = None;
    for (m, b) in segs {
        match m {
            0xDB => dqt_body.extend_from_slice(b),
            0xC0 if sof0_body.is_none() => sof0_body = Some(b),
            0xC4 => dht_concat.extend_from_slice(b),
            0xDD if dri_body.is_none() => dri_body = Some(b),
            0xDA if sos_body.is_none() => sos_body = Some(b),
            _ => {}
        }
    }
    let (Some(sof0), Some(dri), Some(sos)) = (sof0_body, dri_body, sos_body) else {
        return Err(CoreError::new("encode_failed", "编码结果缺少必要段"));
    };
    if dqt_body.is_empty() || dht_concat.is_empty() {
        return Err(CoreError::new(
            "encode_failed",
            "编码结果缺少量化表或霍夫曼表",
        ));
    }

    let mut tables: [Option<&[u8]>; 4] = [None; 4];
    let mut rem = dht_concat.as_slice();
    while !rem.is_empty() {
        if rem.len() < 17 {
            return Err(CoreError::new("encode_failed", "DHT 表截断"));
        }
        let count: usize = rem[1..17].iter().map(|&x| usize::from(x)).sum();
        let size = 17 + count;
        if rem.len() < size {
            return Err(CoreError::new("encode_failed", "DHT 表长度越界"));
        }
        if let Some(slot) = EXPECTED_DHT_ORDER.iter().position(|&tc| tc == rem[0]) {
            tables[slot] = Some(&rem[..size]);
        }
        rem = &rem[size..];
    }
    let mut dht_ordered = Vec::with_capacity(416);
    for slot in tables {
        let Some(tbl) = slot else {
            return Err(CoreError::new("encode_failed", "缺少标准霍夫曼表"));
        };
        dht_ordered.extend_from_slice(tbl);
    }

    let scan = &data[scan_at..];
    let mut out = Vec::with_capacity(HEADER_BYTES + scan.len());
    out.extend_from_slice(b"\xFF\xD8");
    push_segment(&mut out, 0xDB, &dqt_body);
    push_segment(&mut out, 0xC0, sof0);
    push_segment(&mut out, 0xC4, &dht_ordered);
    push_segment(&mut out, 0xDD, dri);
    push_segment(&mut out, 0xDA, sos);
    out.extend_from_slice(scan);
    Ok(out)
}
