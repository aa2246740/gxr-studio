//! Port of gxr_studio/camdata/engine_bindings.py: registered engine/link identities.

use gxr_raw::CoreError;
use gxr_raw::sha256::sha256_hex;

/// Port of gxr_studio/camdata/engine_bindings.py:7 EngineDescription.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EngineDescription<'a> {
    pub engine_sha256: &'a str,
    pub entry: u32,
    pub bss_bytes: u32,
    pub relocations: &'a [(u32, u32)],
    pub linked_data_end: usize,
}

const G156_ENGINES: &[EngineDescription<'static>] = &[
    EngineDescription {
        engine_sha256: "e406f24535118960ec63a835c918659c87a53be0b279e8687d0f836cae17d654",
        entry: 61984,
        bss_bytes: 9284,
        relocations: &[(65840, 56136)],
        linked_data_end: 88684,
    },
    EngineDescription {
        engine_sha256: "688427df58d334a375b125f1b5574ac6f0854962f5123859e47e5030c0052bf0",
        entry: 64892,
        bss_bytes: 9296,
        relocations: &[(68748, 59044)],
        linked_data_end: 90532,
    },
];

const G158_ENGINES: &[EngineDescription<'static>] = &[
    EngineDescription {
        engine_sha256: "86c9eb3a42d5d43f8c8fefa07b574f2ba723f750256d31472d762fbd0885528c",
        entry: 63832,
        bss_bytes: 4360,
        relocations: &[(67484, 57984)],
        linked_data_end: 92484,
    },
    EngineDescription {
        engine_sha256: "441531dc1900ae5c3cf2e62092c5c6dc88a1f326b7cb74f8506159245fd2b34e",
        entry: 66056,
        bss_bytes: 4360,
        relocations: &[(69716, 60208)],
        linked_data_end: 94952,
    },
];

const G176_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "099b71fd8a1f4e9418c8fdde0b3ebc38c035e0d28f0877b4ee4cc52981c1cc76",
    entry: 70732,
    bss_bytes: 4248,
    relocations: &[(74492, 70700)],
    linked_data_end: 74664,
}];

const G178_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "1fd9fb0de74e535cef5dd91c939c2bcac09772cf038b97955e25baa68d2b4c85",
    entry: 71756,
    bss_bytes: 23772,
    relocations: &[(75552, 71724)],
    linked_data_end: 75724,
}];

const G181_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "28013529e2291f3854622dbc46ebefce10098689aa000cb0f512351838f2102f",
    entry: 72308,
    bss_bytes: 4252,
    relocations: &[(76076, 65220)],
    linked_data_end: 76248,
}];

const G182_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "2391f3a55c036888750dfde9ca2d28748fe3fb4a85edd3ee0385be17f7803cc9",
    entry: 72480,
    bss_bytes: 4252,
    relocations: &[(76264, 62800)],
    linked_data_end: 76436,
}];

const ALL_BOUND: &[&[EngineDescription<'static>]] = &[
    G156_ENGINES,
    G158_ENGINES,
    G176_ENGINES,
    G178_ENGINES,
    G181_ENGINES,
    G182_ENGINES,
];

fn bound_engines(generation: u32) -> Option<&'static [EngineDescription<'static>]> {
    match generation {
        156 => Some(G156_ENGINES),
        158 => Some(G158_ENGINES),
        176 => Some(G176_ENGINES),
        178 => Some(G178_ENGINES),
        181 => Some(G181_ENGINES),
        182 => Some(G182_ENGINES),
        _ => None,
    }
}

/// Port of gxr_studio/camdata/datcodec.py:210 _check_engine_binding.
pub fn check_engine_binding(
    generation: u32,
    engine: &[u8],
    entry: u32,
    bss: u32,
    relocations: &[(u32, u32)],
    data_end: usize,
) -> Result<(), CoreError> {
    let sha = sha256_hex(engine);
    let desc = EngineDescription {
        engine_sha256: &sha,
        entry,
        bss_bytes: bss,
        relocations,
        linked_data_end: data_end,
    };
    match bound_engines(generation) {
        None => {
            let known_other = ALL_BOUND
                .iter()
                .flat_map(|group| group.iter())
                .any(|e| e.engine_sha256 == desc.engine_sha256);
            if !known_other {
                return Ok(());
            }
        }
        Some(registered) => {
            if registered.contains(&desc) {
                return Ok(());
            }
        }
    }
    Err(CoreError::new(
        "dat_error",
        format!("{generation} 引擎或链接布局未登记"),
    ))
}
