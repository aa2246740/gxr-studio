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
    pub xdata_sha256: Option<&'a str>,
}

const G156_ENGINES: &[EngineDescription<'static>] = &[
    EngineDescription {
        engine_sha256: "e406f24535118960ec63a835c918659c87a53be0b279e8687d0f836cae17d654",
        entry: 61984,
        bss_bytes: 9284,
        relocations: &[(65840, 56136)],
        linked_data_end: 88684,
        xdata_sha256: None,
    },
    EngineDescription {
        engine_sha256: "688427df58d334a375b125f1b5574ac6f0854962f5123859e47e5030c0052bf0",
        entry: 64892,
        bss_bytes: 9296,
        relocations: &[(68748, 59044)],
        linked_data_end: 90532,
        xdata_sha256: None,
    },
];

const G158_ENGINES: &[EngineDescription<'static>] = &[
    EngineDescription {
        engine_sha256: "86c9eb3a42d5d43f8c8fefa07b574f2ba723f750256d31472d762fbd0885528c",
        entry: 63832,
        bss_bytes: 4360,
        relocations: &[(67484, 57984)],
        linked_data_end: 92484,
        xdata_sha256: None,
    },
    EngineDescription {
        engine_sha256: "441531dc1900ae5c3cf2e62092c5c6dc88a1f326b7cb74f8506159245fd2b34e",
        entry: 66056,
        bss_bytes: 4360,
        relocations: &[(69716, 60208)],
        linked_data_end: 94952,
        xdata_sha256: None,
    },
];

const G176_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "099b71fd8a1f4e9418c8fdde0b3ebc38c035e0d28f0877b4ee4cc52981c1cc76",
    entry: 70732,
    bss_bytes: 4248,
    relocations: &[(74492, 70700)],
    linked_data_end: 74664,
    xdata_sha256: None,
}];

const G178_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "1fd9fb0de74e535cef5dd91c939c2bcac09772cf038b97955e25baa68d2b4c85",
    entry: 71756,
    bss_bytes: 23772,
    relocations: &[(75552, 71724)],
    linked_data_end: 75724,
    xdata_sha256: None,
}];

const G181_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "28013529e2291f3854622dbc46ebefce10098689aa000cb0f512351838f2102f",
    entry: 72308,
    bss_bytes: 4252,
    relocations: &[(76076, 65220)],
    linked_data_end: 76248,
    xdata_sha256: None,
}];

const G182_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "2391f3a55c036888750dfde9ca2d28748fe3fb4a85edd3ee0385be17f7803cc9",
    entry: 72480,
    bss_bytes: 4252,
    relocations: &[(76264, 62800)],
    linked_data_end: 76436,
    xdata_sha256: None,
}];

const G185_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "7c1ccba52638de97711602ad25c404504a2f618c0cddea01d7fa28e26d4b0ef7",
    entry: 74760,
    bss_bytes: 8468,
    relocations: &[(78756, 64892)],
    linked_data_end: 78928,
    xdata_sha256: None,
}];

const G250_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "0f35d588356501e528639f2d2b90ec948ff40f6e97debc3f7192be9b3a9e77e8",
    entry: 79052,
    bss_bytes: 8608,
    relocations: &[(86352, 65340)],
    linked_data_end: 90440,
    xdata_sha256: Some("22f939465361925ea31bdb19e7f016b6d374bdb2f70b6e35449375e8df00d1a0"),
}];

const G253_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "25f66879e735ccb886c50cf772a7f762c17f720eec534db79f941e17ee831bdb",
    entry: 79528,
    bss_bytes: 8616,
    relocations: &[(86828, 65472)],
    linked_data_end: 90932,
    xdata_sha256: Some("ec5a9de229d25c7b8fc580768d9154e4803d29ad343857f9e253dd94bae4e697"),
}];

const G256_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "55a289e0f8c5ae56761a9de8d12c1ecc293191c93284a2c6b8089f02a3b69964",
    entry: 79756,
    bss_bytes: 8648,
    relocations: &[(87224, 65740)],
    linked_data_end: 91292,
    xdata_sha256: Some("1324689f9a6e22cbe27f5d1bf3d78c0092fcb64305e7dcf1537cc9da8488906c"),
}];

const G257_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "d3065103ca9165c1534eae872318f62dedf385ec08080040622b7c192121edf6",
    entry: 79656,
    bss_bytes: 8644,
    relocations: &[(87124, 65640)],
    linked_data_end: 91192,
    xdata_sha256: Some("1324689f9a6e22cbe27f5d1bf3d78c0092fcb64305e7dcf1537cc9da8488906c"),
}];

const G258_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "1767ab7769f23c0f72b3d4c647ff00e35805e10b3d4d881ec1b6e5b1b4bd4845",
    entry: 79708,
    bss_bytes: 8644,
    relocations: &[(87236, 65692)],
    linked_data_end: 91304,
    xdata_sha256: Some("1324689f9a6e22cbe27f5d1bf3d78c0092fcb64305e7dcf1537cc9da8488906c"),
}];

const G259_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "7282b5c9397fa0620fd1a46b422bf8a6c583e9a3a68c4f429133bb352dedf82e",
    entry: 80608,
    bss_bytes: 8680,
    relocations: &[(88136, 66564)],
    linked_data_end: 92328,
    xdata_sha256: Some("ba83e5b8e2a18795146a0ddfed632d8a295b438039a99393fdb05bf9c057827a"),
}];

const G260_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "9cd51001bd44675994a1c663205cb916c1413f158de2df31c64744cee7fab682",
    entry: 81304,
    bss_bytes: 8680,
    relocations: &[(88832, 67260)],
    linked_data_end: 93024,
    xdata_sha256: Some("ba83e5b8e2a18795146a0ddfed632d8a295b438039a99393fdb05bf9c057827a"),
}];

const G262_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "df8559ecd05777ad24906e1fb0671fbf7013283664e6b4c6b04c13ac47e0d201",
    entry: 81620,
    bss_bytes: 9160,
    relocations: &[(89188, 67576)],
    linked_data_end: 93380,
    xdata_sha256: Some("ba83e5b8e2a18795146a0ddfed632d8a295b438039a99393fdb05bf9c057827a"),
}];

const G266_ENGINES: &[EngineDescription<'static>] = &[EngineDescription {
    engine_sha256: "8fae9c79c895852d9090831c584255879dd5b6eb48473c0ebc4e1a03391693e9",
    entry: 85764,
    bss_bytes: 9164,
    relocations: &[(93696, 102789), (93716, 71708)],
    linked_data_end: 97980,
    xdata_sha256: Some("105c98b490b86cf2cbb20e461925f3a2531f7e395c6e0f6f9254e16df45b1342"),
}];

const ALL_BOUND: &[&[EngineDescription<'static>]] = &[
    G156_ENGINES,
    G158_ENGINES,
    G176_ENGINES,
    G178_ENGINES,
    G181_ENGINES,
    G182_ENGINES,
    G185_ENGINES,
    G250_ENGINES,
    G253_ENGINES,
    G256_ENGINES,
    G257_ENGINES,
    G258_ENGINES,
    G259_ENGINES,
    G260_ENGINES,
    G262_ENGINES,
    G266_ENGINES,
];

fn bound_engines(generation: u32) -> Option<&'static [EngineDescription<'static>]> {
    match generation {
        156 => Some(G156_ENGINES),
        158 => Some(G158_ENGINES),
        176 => Some(G176_ENGINES),
        178 => Some(G178_ENGINES),
        181 => Some(G181_ENGINES),
        182 => Some(G182_ENGINES),
        185 => Some(G185_ENGINES),
        250 => Some(G250_ENGINES),
        253 => Some(G253_ENGINES),
        256 => Some(G256_ENGINES),
        257 => Some(G257_ENGINES),
        258 => Some(G258_ENGINES),
        259 => Some(G259_ENGINES),
        260 => Some(G260_ENGINES),
        262 => Some(G262_ENGINES),
        266 => Some(G266_ENGINES),
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
    xdata: &[u8],
) -> Result<(), CoreError> {
    let sha = sha256_hex(engine);
    let xdata_sha;
    let xdata_sha256 = if xdata.is_empty() {
        None
    } else {
        xdata_sha = sha256_hex(xdata);
        Some(xdata_sha.as_str())
    };
    let desc = EngineDescription {
        engine_sha256: &sha,
        entry,
        bss_bytes: bss,
        relocations,
        linked_data_end: data_end,
        xdata_sha256,
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
