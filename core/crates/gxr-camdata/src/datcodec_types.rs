//! Types, constants, and generation profiles for `datcodec`.

use std::collections::BTreeMap;

use gxr_raw::CoreError;

pub const NAMES: [&str; 3] = ["jlaunch", "qlaunch", "l08firm"];
pub const KINDS: [usize; 10] = [0, 1, 2, 3, 0, 1, 1, 1, 0, 1];
pub const OFFSETS: [usize; 10] = [20, 82, 115, 216, 284, 346, 379, 412, 456, 518];
pub const ROW_BYTES: [usize; 10] = [62, 33, 96, 56, 62, 33, 33, 33, 62, 33];
pub const POOL_LENGTHS: [usize; 4] = [62, 33, 96, 56];
pub const POOL_LIMITS: [usize; 4] = [144, 240, 48, 48];
pub const CRC_FIELDS: [(usize, usize, usize); 9] = [
    (20, 62, 8),
    (82, 33, 12),
    (115, 96, 16),
    (216, 56, 212),
    (284, 62, 272),
    (346, 33, 276),
    (379, 66, 280),
    (456, 62, 448),
    (518, 33, 452),
];
pub const PIN_CRC32: [u32; 3] = [0xF09F_E7B4, 0xFB3F_BCB2, 0x95F3_DCA9];
pub const STOCK_ENTRY_CRC: [u32; 3] = [0x0D2B_0EA9, 0x341D_DBEE, 0x4686_301B];
pub const ILAUNCH3_SHA256: &str =
    "da1980e9d6f3996ede4953b8311cf0ce2abeb4bb300b6ee60a38f38e29a3cdf7";
pub const ENTRY_BYTES: usize = 552;
pub const HEADER: usize = 256;
pub const MAX_RAM: usize = 114_688;
pub const DESCRIPTOR_BYTES: usize = 20;

pub const GXUI_MAGIC: &[u8; 8] = b"GXUI124\0";
pub const LEGACY_BYTES: usize = 1408;
pub const CONFIG_BYTES: usize = 48;
pub const USER_ENTRY_BYTES: usize = 24;
pub const USER_CAPACITY: usize = 32;
pub const USER_OFFSET: usize = 64 + 24 * CONFIG_BYTES;
pub const EXT_BYTES: usize = USER_OFFSET + USER_CAPACITY * USER_ENTRY_BYTES;
pub const LEGACY_IDS: [usize; 4] = [1, 3, 4, 2];
pub const KV2_BLOCK_BYTES: usize = 17_384;
pub const KV2_BLOCK_SHA256: &str =
    "43405f6c5f63050505e958882dcd260f2a9e1f707d3b407860041787964992f9";
pub const KV3_BLOCK_BYTES: usize = 7_904;
pub const KV3_BLOCK_SHA256: &str =
    "e91c8e0aa7b8dfefc4b662438acd3ca0a927a97099dc534c24f1dda32352e64c";

/// Port of gxr_studio/camdata/intake.py:28 MAX_DAT_BYTES.
pub const MAX_DAT_BYTES: usize = 98_304;

pub const SLOTS: [u8; 8] = [1, 2, 3, 4, 5, 6, 7, 8];

pub fn dat_error(msg: impl Into<String>) -> CoreError {
    CoreError::new("dat_error", msg)
}

pub fn unsupported_error(msg: impl Into<String>) -> CoreError {
    CoreError::new("unsupported_generation", msg)
}

/// Port of gxr_studio/camdata/datcodec.py:67 Profile.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Profile {
    pub generation: u32,
    pub max_file: usize,
    pub kv2_block: bool,
    pub looks: bool,
    pub max_engine: usize,
    pub flat_list: bool,
    pub mount_knobs_from_50mm: bool,
    pub library_generation: Option<u32>,
}

impl Profile {
    const fn simple(generation: u32, max_file: usize, kv2_block: bool) -> Self {
        Self {
            generation,
            max_file,
            kv2_block,
            looks: false,
            max_engine: 65536,
            flat_list: false,
            mount_knobs_from_50mm: false,
            library_generation: None,
        }
    }

    /// Port of gxr_studio/camdata/datcodec.py:78 Profile.bank_generation.
    pub const fn bank_generation(&self) -> u32 {
        if self.generation == 156 {
            155
        } else {
            self.generation
        }
    }

    /// Port of gxr_studio/camdata/datcodec.py:83 Profile.knob_bytes.
    pub const fn knob_bytes(&self) -> usize {
        if self.looks {
            KV3_BLOCK_BYTES
        } else if self.kv2_block {
            KV2_BLOCK_BYTES
        } else {
            0
        }
    }

    /// Port of gxr_studio/camdata/datcodec.py:87 Profile.knob_sha256.
    pub const fn knob_sha256(&self) -> &'static str {
        if self.looks {
            KV3_BLOCK_SHA256
        } else {
            KV2_BLOCK_SHA256
        }
    }
}

pub const PROFILES: &[Profile] = &[
    Profile::simple(127, 65536, false),
    Profile::simple(129, 98304, true),
    Profile::simple(131, 98304, true),
    Profile::simple(135, 98304, true),
    Profile::simple(137, 98304, true),
    Profile::simple(138, 98304, true),
    Profile::simple(150, 98304, true),
    Profile {
        looks: true,
        ..Profile::simple(152, 98304, true)
    },
    Profile {
        looks: true,
        ..Profile::simple(153, 98304, true)
    },
    Profile {
        looks: true,
        flat_list: true,
        mount_knobs_from_50mm: true,
        ..Profile::simple(154, 98304, true)
    },
    Profile {
        looks: true,
        max_engine: 69632,
        flat_list: true,
        mount_knobs_from_50mm: true,
        ..Profile::simple(155, 98304, true)
    },
    Profile {
        looks: true,
        max_engine: 69632,
        flat_list: true,
        mount_knobs_from_50mm: true,
        ..Profile::simple(156, 98304, true)
    },
    Profile {
        looks: true,
        max_engine: 69632,
        flat_list: true,
        mount_knobs_from_50mm: true,
        ..Profile::simple(158, 98304, true)
    },
    Profile {
        max_engine: 81920,
        mount_knobs_from_50mm: true,
        library_generation: Some(176),
        ..Profile::simple(176, 98304, true)
    },
    Profile {
        max_engine: 81920,
        mount_knobs_from_50mm: true,
        library_generation: Some(178),
        ..Profile::simple(178, 98304, true)
    },
    Profile {
        max_engine: 81920,
        mount_knobs_from_50mm: true,
        library_generation: Some(178),
        ..Profile::simple(181, 98304, true)
    },
    Profile {
        max_engine: 81920,
        mount_knobs_from_50mm: true,
        library_generation: Some(178),
        ..Profile::simple(182, 98304, true)
    },
];

pub fn profile(generation: u32) -> Option<&'static Profile> {
    PROFILES.iter().find(|p| p.generation == generation)
}

/// Port of gxr_studio/camdata/datcodec.py:145 identity.
pub fn identity(record: &[u8; 64]) -> [u8; 49] {
    let mut out = [0u8; 49];
    out[0] = record[2];
    out[1..33].copy_from_slice(&record[4..36]);
    out[33] = record[3];
    out[34..49].copy_from_slice(&record[36..51]);
    out
}

/// Port of gxr_studio/camdata/datcodec.py:150 Module.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Module {
    pub name: &'static str,
    pub map: Vec<u8>,
    pub records: Vec<[u8; 64]>,
    pub entries: BTreeMap<u8, [u8; ENTRY_BYTES]>,
    pub refs: [u8; 16],
    pub ranges: [[[i8; 3]; 8]; 16],
    pub pin: [u8; 32],
    pub crc_pin: u32,
}

/// Port of gxr_studio/camdata/datcodec.py:161 Look.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Look {
    pub name: String,
    pub base: u8,
    pub unit_mask: u8,
    pub entry: [u8; ENTRY_BYTES],
}

/// Port of gxr_studio/camdata/datcodec.py:169 FullRecipe.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FullRecipe {
    pub name: String,
    pub base: u8,
    pub unit_mask: u8,
    pub entry: [u8; ENTRY_BYTES],
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UserEntry {
    pub base: u8,
    pub levels: [i8; 11],
}

/// Port of gxr_studio/camdata/datcodec.py:177 Dat.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Dat {
    pub generation: u32,
    pub flags: u32,
    pub entry: u32,
    pub bss: u32,
    pub resident_id: [u8; 32],
    pub abi_id: [u8; 16],
    pub engine: Vec<u8>,
    pub relocations: Vec<(u32, u32)>,
    pub modules: Vec<Module>,
    pub ids: [u32; 8],
    pub ext_config: Vec<u8>,
    pub users: Vec<UserEntry>,
    pub kv2_block: Option<Vec<u8>>,
    pub effect_area: Vec<u8>,
    pub looks: Vec<Look>,
    pub user_names: Vec<String>,
    pub recipes: Vec<FullRecipe>,
    pub shell_header: Vec<u8>,
}

impl Dat {
    /// Port of gxr_studio/camdata/datcodec.py:200 Dat.entry_of (slot 1..=16).
    pub fn entry_of(&self, unit: usize, slot: u8) -> Result<&[u8; ENTRY_BYTES], CoreError> {
        let m = self
            .modules
            .get(unit)
            .ok_or_else(|| dat_error("单元索引越界"))?;
        let slot_idx = usize::from(slot.checked_sub(1).ok_or_else(|| dat_error("槽位越界"))?);
        let owner = *m.refs.get(slot_idx).ok_or_else(|| dat_error("槽位越界"))?;
        m.entries
            .get(&owner)
            .ok_or_else(|| dat_error("描述符所有者缺失"))
    }
}
