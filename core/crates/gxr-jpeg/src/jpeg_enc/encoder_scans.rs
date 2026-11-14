use std::fs::File;
use std::io::BufWriter;
use std::path::Path;

use super::encoder::Encoder;
use crate::jpeg_enc::EncodingError;
use crate::jpeg_enc::fdct::fdct;
use crate::jpeg_enc::huffman::HuffmanTable;
use crate::jpeg_enc::image_buffer::ImageBuffer;
use crate::jpeg_enc::marker::Marker;
use crate::jpeg_enc::quantization::QuantizationTable;
use crate::jpeg_enc::writer::{JfifWrite, ZIGZAG};

impl<W: JfifWrite> Encoder<W> {
    /// Encode all components with one scan
    ///
    /// This is only valid for sampling factors of 1 and 2
    pub(super) fn encode_image_interleaved<I: ImageBuffer, OP: Operations>(
        &mut self,
        image: I,
        q_tables: &[QuantizationTable; 2],
    ) -> Result<(), EncodingError> {
        self.write_frame_header(&image, q_tables)?;
        self.writer
            .write_scan_header(&self.components.iter().collect::<Vec<_>>(), None)?;

        let (max_h_sampling, max_v_sampling) = self.get_max_sampling_size();

        let width = image.width();
        let height = image.height();

        let num_cols = ceil_div(usize::from(width), 8 * max_h_sampling);
        let num_rows = ceil_div(usize::from(height), 8 * max_v_sampling);

        let buffer_width = num_cols * 8 * max_h_sampling;
        let buffer_size = buffer_width * 8 * max_v_sampling;

        let mut row: [Vec<_>; 4] = self.init_rows(buffer_size);

        let mut prev_dc = [0i16; 4];

        let restart_interval = self.restart_interval.unwrap_or(0);
        let mut restarts = 0;
        let mut restarts_to_go = restart_interval;

        for block_y in 0..num_rows {
            for r in &mut row {
                r.clear();
            }

            for y in 0..(8 * max_v_sampling) {
                let y = y + block_y * 8 * max_v_sampling;
                let y = (y.min(height as usize - 1)) as u16;

                image.fill_buffers(y, &mut row);

                for _ in usize::from(width)..buffer_width {
                    for channel in &mut row {
                        if !channel.is_empty() {
                            channel.push(channel[channel.len() - 1]);
                        }
                    }
                }
            }

            for block_x in 0..num_cols {
                if restart_interval > 0 && restarts_to_go == 0 {
                    self.writer.finalize_bit_buffer()?;
                    self.writer
                        .write_marker(Marker::RST((restarts % 8) as u8))?;

                    prev_dc[0] = 0;
                    prev_dc[1] = 0;
                    prev_dc[2] = 0;
                    prev_dc[3] = 0;
                }

                for (i, component) in self.components.iter().enumerate() {
                    for v_offset in 0..component.vertical_sampling_factor as usize {
                        for h_offset in 0..component.horizontal_sampling_factor as usize {
                            let mut block = get_block(
                                &row[i],
                                block_x * 8 * max_h_sampling + (h_offset * 8),
                                v_offset * 8,
                                max_h_sampling / component.horizontal_sampling_factor as usize,
                                max_v_sampling / component.vertical_sampling_factor as usize,
                                buffer_width,
                            );

                            OP::fdct(&mut block);

                            let mut q_block = [0i16; 64];

                            OP::quantize_block(
                                &block,
                                &mut q_block,
                                &q_tables[component.quantization_table as usize],
                            );

                            self.writer.write_block(
                                &q_block,
                                prev_dc[i],
                                &self.huffman_tables[component.dc_huffman_table as usize].0,
                                &self.huffman_tables[component.ac_huffman_table as usize].1,
                            )?;

                            prev_dc[i] = q_block[0];
                        }
                    }
                }

                if restart_interval > 0 {
                    if restarts_to_go == 0 {
                        restarts_to_go = restart_interval;
                        restarts += 1;
                        restarts &= 7;
                    }
                    restarts_to_go -= 1;
                }
            }
        }

        self.writer.finalize_bit_buffer()?;

        Ok(())
    }

    /// Encode components with one scan per component
    pub(super) fn encode_image_sequential<I: ImageBuffer, OP: Operations>(
        &mut self,
        image: I,
        q_tables: &[QuantizationTable; 2],
    ) -> Result<(), EncodingError> {
        let blocks = self.encode_blocks::<_, OP>(&image, q_tables);

        if self.optimize_huffman_table {
            self.optimize_huffman_table(&blocks);
        }

        self.write_frame_header(&image, q_tables)?;

        for (i, component) in self.components.iter().enumerate() {
            let restart_interval = self.restart_interval.unwrap_or(0);
            let mut restarts = 0;
            let mut restarts_to_go = restart_interval;

            self.writer.write_scan_header(&[component], None)?;

            let mut prev_dc = 0;

            for block in &blocks[i] {
                if restart_interval > 0 && restarts_to_go == 0 {
                    self.writer.finalize_bit_buffer()?;
                    self.writer
                        .write_marker(Marker::RST((restarts % 8) as u8))?;

                    prev_dc = 0;
                }

                self.writer.write_block(
                    block,
                    prev_dc,
                    &self.huffman_tables[component.dc_huffman_table as usize].0,
                    &self.huffman_tables[component.ac_huffman_table as usize].1,
                )?;

                prev_dc = block[0];

                if restart_interval > 0 {
                    if restarts_to_go == 0 {
                        restarts_to_go = restart_interval;
                        restarts += 1;
                        restarts &= 7;
                    }
                    restarts_to_go -= 1;
                }
            }

            self.writer.finalize_bit_buffer()?;
        }

        Ok(())
    }

    /// Encode image in progressive mode
    ///
    /// This only support spectral selection for now
    pub(super) fn encode_image_progressive<I: ImageBuffer, OP: Operations>(
        &mut self,
        image: I,
        scans: u8,
        q_tables: &[QuantizationTable; 2],
    ) -> Result<(), EncodingError> {
        let blocks = self.encode_blocks::<_, OP>(&image, q_tables);

        if self.optimize_huffman_table {
            self.optimize_huffman_table(&blocks);
        }

        self.write_frame_header(&image, q_tables)?;

        // Phase 1: DC Scan
        //          Only the DC coefficients can be transfer in the first component scans
        for (i, component) in self.components.iter().enumerate() {
            self.writer.write_scan_header(&[component], Some((0, 0)))?;

            let restart_interval = self.restart_interval.unwrap_or(0);
            let mut restarts = 0;
            let mut restarts_to_go = restart_interval;

            let mut prev_dc = 0;

            for block in &blocks[i] {
                if restart_interval > 0 && restarts_to_go == 0 {
                    self.writer.finalize_bit_buffer()?;
                    self.writer
                        .write_marker(Marker::RST((restarts % 8) as u8))?;

                    prev_dc = 0;
                }

                self.writer.write_dc(
                    block[0],
                    prev_dc,
                    &self.huffman_tables[component.dc_huffman_table as usize].0,
                )?;

                prev_dc = block[0];

                if restart_interval > 0 {
                    if restarts_to_go == 0 {
                        restarts_to_go = restart_interval;
                        restarts += 1;
                        restarts &= 7;
                    }
                    restarts_to_go -= 1;
                }
            }

            self.writer.finalize_bit_buffer()?;
        }

        // Phase 2: AC scans
        let scans = scans as usize - 1;

        let values_per_scan = 64 / scans;

        for scan in 0..scans {
            let start = (scan * values_per_scan).max(1);
            let end = if scan == scans - 1 {
                // ensure last scan is always transfers the remaining coefficients
                64
            } else {
                (scan + 1) * values_per_scan
            };

            for (i, component) in self.components.iter().enumerate() {
                let restart_interval = self.restart_interval.unwrap_or(0);
                let mut restarts = 0;
                let mut restarts_to_go = restart_interval;

                self.writer
                    .write_scan_header(&[component], Some((start as u8, end as u8 - 1)))?;

                for block in &blocks[i] {
                    if restart_interval > 0 && restarts_to_go == 0 {
                        self.writer.finalize_bit_buffer()?;
                        self.writer
                            .write_marker(Marker::RST((restarts % 8) as u8))?;
                    }

                    self.writer.write_ac_block(
                        block,
                        start,
                        end,
                        &self.huffman_tables[component.ac_huffman_table as usize].1,
                    )?;

                    if restart_interval > 0 {
                        if restarts_to_go == 0 {
                            restarts_to_go = restart_interval;
                            restarts += 1;
                            restarts &= 7;
                        }
                        restarts_to_go -= 1;
                    }
                }

                self.writer.finalize_bit_buffer()?;
            }
        }

        Ok(())
    }

    fn encode_blocks<I: ImageBuffer, OP: Operations>(
        &mut self,
        image: &I,
        q_tables: &[QuantizationTable; 2],
    ) -> [Vec<[i16; 64]>; 4] {
        let width = image.width();
        let height = image.height();

        let (max_h_sampling, max_v_sampling) = self.get_max_sampling_size();

        let num_cols = ceil_div(usize::from(width), 8 * max_h_sampling) * max_h_sampling;
        let num_rows = ceil_div(usize::from(height), 8 * max_v_sampling) * max_v_sampling;

        debug_assert!(num_cols > 0);
        debug_assert!(num_rows > 0);

        let buffer_width = num_cols * 8;
        let buffer_size = num_cols * num_rows * 64;

        let mut row: [Vec<_>; 4] = self.init_rows(buffer_size);

        for y in 0..num_rows * 8 {
            let y = (y.min(usize::from(height) - 1)) as u16;

            image.fill_buffers(y, &mut row);

            for _ in usize::from(width)..num_cols * 8 {
                for channel in &mut row {
                    if !channel.is_empty() {
                        channel.push(channel[channel.len() - 1]);
                    }
                }
            }
        }

        let num_cols = ceil_div(usize::from(width), 8);
        let num_rows = ceil_div(usize::from(height), 8);

        debug_assert!(num_cols > 0);
        debug_assert!(num_rows > 0);

        let mut blocks: [Vec<_>; 4] = self.init_block_buffers(buffer_size / 64);

        for (i, component) in self.components.iter().enumerate() {
            let h_scale = max_h_sampling / component.horizontal_sampling_factor as usize;
            let v_scale = max_v_sampling / component.vertical_sampling_factor as usize;

            let cols = ceil_div(num_cols, h_scale);
            let rows = ceil_div(num_rows, v_scale);

            debug_assert!(cols > 0);
            debug_assert!(rows > 0);

            for block_y in 0..rows {
                for block_x in 0..cols {
                    let mut block = get_block(
                        &row[i],
                        block_x * 8 * h_scale,
                        block_y * 8 * v_scale,
                        h_scale,
                        v_scale,
                        buffer_width,
                    );

                    OP::fdct(&mut block);

                    let mut q_block = [0i16; 64];

                    OP::quantize_block(
                        &block,
                        &mut q_block,
                        &q_tables[component.quantization_table as usize],
                    );

                    blocks[i].push(q_block);
                }
            }
        }
        blocks
    }

    fn init_block_buffers(&mut self, buffer_size: usize) -> [Vec<[i16; 64]>; 4] {
        // To simplify the code and to give the compiler more infos to optimize stuff we always initialize 4 components
        // Resource overhead should be minimal because an empty Vec doesn't allocate

        match self.components.len() {
            1 => [
                Vec::with_capacity(buffer_size),
                Vec::new(),
                Vec::new(),
                Vec::new(),
            ],
            3 => [
                Vec::with_capacity(buffer_size),
                Vec::with_capacity(buffer_size),
                Vec::with_capacity(buffer_size),
                Vec::new(),
            ],
            4 => [
                Vec::with_capacity(buffer_size),
                Vec::with_capacity(buffer_size),
                Vec::with_capacity(buffer_size),
                Vec::with_capacity(buffer_size),
            ],
            len => unreachable!("Unsupported component length: {}", len),
        }
    }

    // Create new huffman tables optimized for this image
    fn optimize_huffman_table(&mut self, blocks: &[Vec<[i16; 64]>; 4]) {
        // TODO: Find out if it's possible to reuse some code from the writer

        let max_tables = self.components.len().min(2) as u8;

        for table in 0..max_tables {
            let mut dc_freq = [0u32; 257];
            dc_freq[256] = 1;
            let mut ac_freq = [0u32; 257];
            ac_freq[256] = 1;

            let mut had_ac = false;
            let mut had_dc = false;

            for (i, component) in self.components.iter().enumerate() {
                if component.dc_huffman_table == table {
                    had_dc = true;

                    let mut prev_dc = 0;

                    debug_assert!(!blocks[i].is_empty());

                    for block in &blocks[i] {
                        let value = block[0];
                        let diff = value - prev_dc;
                        let num_bits = get_num_bits(diff);

                        dc_freq[num_bits as usize] += 1;

                        prev_dc = value;
                    }
                }

                if component.ac_huffman_table == table {
                    had_ac = true;

                    if let Some(scans) = self.progressive_scans {
                        let scans = scans as usize - 1;

                        let values_per_scan = 64 / scans;

                        for scan in 0..scans {
                            let start = (scan * values_per_scan).max(1);
                            let end = if scan == scans - 1 {
                                // Due to rounding we might need to transfer more than values_per_scan values in the last scan
                                64
                            } else {
                                (scan + 1) * values_per_scan
                            };

                            debug_assert!(!blocks[i].is_empty());

                            for block in &blocks[i] {
                                let mut zero_run = 0;

                                for &value in &block[start..end] {
                                    if value == 0 {
                                        zero_run += 1;
                                    } else {
                                        while zero_run > 15 {
                                            ac_freq[0xF0] += 1;
                                            zero_run -= 16;
                                        }
                                        let num_bits = get_num_bits(value);
                                        let symbol = (zero_run << 4) | num_bits;

                                        ac_freq[symbol as usize] += 1;

                                        zero_run = 0;
                                    }
                                }

                                if zero_run > 0 {
                                    ac_freq[0] += 1;
                                }
                            }
                        }
                    } else {
                        for block in &blocks[i] {
                            let mut zero_run = 0;

                            for &value in &block[1..] {
                                if value == 0 {
                                    zero_run += 1;
                                } else {
                                    while zero_run > 15 {
                                        ac_freq[0xF0] += 1;
                                        zero_run -= 16;
                                    }
                                    let num_bits = get_num_bits(value);
                                    let symbol = (zero_run << 4) | num_bits;

                                    ac_freq[symbol as usize] += 1;

                                    zero_run = 0;
                                }
                            }

                            if zero_run > 0 {
                                ac_freq[0] += 1;
                            }
                        }
                    }
                }
            }

            assert!(had_dc, "Missing DC data for table {}", table);
            assert!(had_ac, "Missing AC data for table {}", table);

            self.huffman_tables[table as usize] = (
                HuffmanTable::new_optimized(dc_freq),
                HuffmanTable::new_optimized(ac_freq),
            );
        }
    }
}

impl Encoder<BufWriter<File>> {
    /// Create a new decoder that writes into a file
    ///
    /// See [new](Encoder::new) for further information.
    ///
    /// # Errors
    ///
    /// Returns an `IoError(std::io::Error)` if the file can't be created
    pub fn new_file<P: AsRef<Path>>(
        path: P,
        quality: u8,
    ) -> Result<Encoder<BufWriter<File>>, EncodingError> {
        let file = File::create(path)?;
        let buf = BufWriter::new(file);
        Ok(Self::new(buf, quality))
    }
}

fn get_block(
    data: &[u8],
    start_x: usize,
    start_y: usize,
    col_stride: usize,
    row_stride: usize,
    width: usize,
) -> [i16; 64] {
    let mut block = [0i16; 64];

    for y in 0..8 {
        for x in 0..8 {
            let ix = start_x + (x * col_stride);
            let iy = start_y + (y * row_stride);

            block[y * 8 + x] = (data[iy * width + ix] as i16) - 128;
        }
    }

    block
}

pub(super) fn ceil_div(value: usize, div: usize) -> usize {
    value / div + usize::from(value % div != 0)
}

fn get_num_bits(mut value: i16) -> u8 {
    if value < 0 {
        value = -value;
    }

    let mut num_bits = 0;

    while value > 0 {
        num_bits += 1;
        value >>= 1;
    }

    num_bits
}

pub(super) trait Operations {
    #[inline(always)]
    fn fdct(data: &mut [i16; 64]) {
        fdct(data);
    }

    #[inline(always)]
    fn quantize_block(block: &[i16; 64], q_block: &mut [i16; 64], table: &QuantizationTable) {
        for i in 0..64 {
            let z = ZIGZAG[i] as usize;
            q_block[i] = table.quantize(block[z], z);
        }
    }
}

pub(super) struct DefaultOperations;

impl Operations for DefaultOperations {}

#[cfg(test)]
mod tests {
    use std::vec;

    use super::get_num_bits;
    use crate::jpeg_enc::encoder::{Encoder, SamplingFactor};
    use crate::jpeg_enc::writer::get_code;

    #[test]
    fn test_get_num_bits() {
        let min_max = 2i16.pow(13);

        for value in -min_max..=min_max {
            let num_bits1 = get_num_bits(value);
            let (num_bits2, _) = get_code(value);

            assert_eq!(
                num_bits1, num_bits2,
                "Difference in num bits for value {}: {} vs {}",
                value, num_bits1, num_bits2
            );
        }
    }

    #[test]
    fn sampling_factors() {
        assert_eq!(SamplingFactor::F_1_1.get_sampling_factors(), (1, 1));
        assert_eq!(SamplingFactor::F_2_1.get_sampling_factors(), (2, 1));
        assert_eq!(SamplingFactor::F_1_2.get_sampling_factors(), (1, 2));
        assert_eq!(SamplingFactor::F_2_2.get_sampling_factors(), (2, 2));
        assert_eq!(SamplingFactor::F_4_1.get_sampling_factors(), (4, 1));
        assert_eq!(SamplingFactor::F_4_2.get_sampling_factors(), (4, 2));
        assert_eq!(SamplingFactor::F_1_4.get_sampling_factors(), (1, 4));
        assert_eq!(SamplingFactor::F_2_4.get_sampling_factors(), (2, 4));

        assert_eq!(SamplingFactor::R_4_4_4.get_sampling_factors(), (1, 1));
        assert_eq!(SamplingFactor::R_4_4_0.get_sampling_factors(), (1, 2));
        assert_eq!(SamplingFactor::R_4_4_1.get_sampling_factors(), (1, 4));
        assert_eq!(SamplingFactor::R_4_2_2.get_sampling_factors(), (2, 1));
        assert_eq!(SamplingFactor::R_4_2_0.get_sampling_factors(), (2, 2));
        assert_eq!(SamplingFactor::R_4_2_1.get_sampling_factors(), (2, 4));
        assert_eq!(SamplingFactor::R_4_1_1.get_sampling_factors(), (4, 1));
        assert_eq!(SamplingFactor::R_4_1_0.get_sampling_factors(), (4, 2));
    }

    #[test]
    fn test_set_progressive() {
        let mut encoder = Encoder::new(vec![], 100);
        encoder.set_progressive(true);
        assert_eq!(encoder.progressive_scans(), Some(4));

        encoder.set_progressive(false);
        assert_eq!(encoder.progressive_scans(), None);
    }
}
