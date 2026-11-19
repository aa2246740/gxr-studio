//! Port of `gxr_studio/camdata/catalog_names.py`: frozen catalog names, camera order, and position resolution.
//!
//! Contains only the public names and camera order already present in `src/gxr_studio/camdata/catalog_names.py`.

pub const CAMERA_UNITS: [(&str, &str); 3] =
    [("28", "qlaunch"), ("50", "jlaunch"), ("MT", "l08firm")];

pub const UNIT_LABELS: [(&str, &str); 3] = [("28", "28mm"), ("50", "50mm"), ("MT", "MOUNT")];

pub const UNIT_BITS: [(&str, u8); 3] = [("50", 1), ("28", 2), ("MT", 4)];

/// Library generation for each frozen catalog identity; g178/g181/g182/g185/g250 share 178.
pub const CATALOG_GENERATIONS: [(&str, u32); 4] = [
    ("d25d2f0a68", 176),
    ("6a3a5a3c02", 178),
    ("6adf847602", 178),
    ("0ccc86c19b", 178),
];

/// Base slots 1..8 in the library RV list.
pub const BASE_NAMES: [&str; 8] = [
    "GRPos", "GRNeg", "HC BW", "Std", "K64", "P160", "K25", "Gold",
];

const G178_RECORDS: [&str; 19] = [
    "F-CC",
    "F-CN",
    "Provia100F",
    "M9 STD",
    "L-Classic",
    "L-Contemp",
    "Leica F1",
    "K64",
    "K25",
    "Portra160",
    "Gold200",
    "Ektachrm100",
    "HNCS HiCon",
    "HNCS LowSat",
    "Teal Orange",
    "Y2K Plady",
    "Agfa LeBox",
    "Acros",
    "Tri-X 400",
];

const KUBIK_LOOKS: [&str; 3] = ["K-0815", "K-PACIFIC", "K-KINO"];

/// Shared descriptor table order for `d25d2f0a68` (g176): `pos = effect + 4`, 1-based `effect`, unit masks 1/2/4.
pub const G176_DESCRIPTORS: [(&str, u8); 29] = [
    ("Acros", 1),
    ("Acros", 2),
    ("Acros", 4),
    ("ARRI 709", 7),
    ("M9 STD", 1),
    ("M9 STD", 2),
    ("M9 STD", 4),
    ("instax mini", 7),
    ("HNCS HiCon", 1),
    ("HNCS HiCon", 2),
    ("HNCS HiCon", 4),
    ("HNCS LowSat", 3),
    ("HNCS LowSat", 4),
    ("Tri-X 400", 7),
    ("Ektachrm 100", 1),
    ("Ektachrm 100", 2),
    ("Ektachrm 100", 4),
    ("Provia 100F", 3),
    ("Provia 100F", 4),
    ("Portra 800", 3),
    ("Portra 800", 4),
    ("Vision3 500T", 1),
    ("Vision3 500T", 4),
    ("F-CC", 5),
    ("F-CC", 2),
    ("F-CN", 3),
    ("F-CN", 4),
    ("Leica F1", 7),
    ("Teal Orange", 7),
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CatalogTarget {
    pub kind: &'static str, // "builtin" | "record"
    pub slot: Option<u8>,
    pub effect: Option<usize>,
    pub name: &'static str,
}

pub fn camera_unit(unit: &str) -> Option<&'static str> {
    CAMERA_UNITS
        .iter()
        .find_map(|&(code, name)| (code == unit).then_some(name))
}

pub fn unit_label(unit: &str) -> Option<&'static str> {
    UNIT_LABELS
        .iter()
        .find_map(|&(code, label)| (code == unit).then_some(label))
}

pub fn unit_bit(unit: &str) -> Option<u8> {
    UNIT_BITS
        .iter()
        .find_map(|&(code, bit)| (code == unit).then_some(bit))
}

pub fn catalog_generation(identity: &str) -> Option<u32> {
    CATALOG_GENERATIONS
        .iter()
        .find_map(|&(id, generation)| (id == identity).then_some(generation))
}

fn descriptors_for(identity: &str) -> Option<Vec<(&'static str, u8)>> {
    match identity {
        "d25d2f0a68" => Some(G176_DESCRIPTORS.to_vec()),
        "6a3a5a3c02" | "6adf847602" => {
            let mut out = Vec::with_capacity(G178_RECORDS.len() * 3);
            for &name in &G178_RECORDS {
                for mask in [1u8, 2, 4] {
                    out.push((name, mask));
                }
            }
            Some(out)
        }
        "0ccc86c19b" => {
            let mut out = Vec::with_capacity((G178_RECORDS.len() + KUBIK_LOOKS.len()) * 3);
            for &name in &G178_RECORDS {
                for mask in [1u8, 2, 4] {
                    out.push((name, mask));
                }
            }
            for &name in &KUBIK_LOOKS {
                for mask in [1u8, 2, 4] {
                    out.push((name, mask));
                }
            }
            Some(out)
        }
        _ => None,
    }
}

/// Port of `gxr_studio/camdata/catalog_names.py:115 resolve_descriptors`.
///
/// Camera rule `pos = effect ? effect + 4 : slot`: 1–4 lookup base slots; >=5 first tries
/// `effect = pos - 4` with unit mask check; otherwise 5–8 fall back to base slots; else `None`.
pub fn resolve_descriptors(
    descriptors: &[(&'static str, u8)],
    unit: &str,
    position: i64,
) -> Option<CatalogTarget> {
    let bit = unit_bit(unit)?;
    if position < 1 {
        return None;
    }
    if position <= 4 {
        let slot = position as u8;
        return Some(CatalogTarget {
            kind: "builtin",
            slot: Some(slot),
            effect: None,
            name: BASE_NAMES[(slot - 1) as usize],
        });
    }
    let effect = usize::try_from(position - 4).ok()?;
    if effect >= 1
        && effect <= descriptors.len()
        && let Some(&(name, mask)) = descriptors.get(effect - 1)
        && (mask & bit) != 0
    {
        return Some(CatalogTarget {
            kind: "record",
            slot: None,
            effect: Some(effect),
            name,
        });
    }
    if position <= 8 {
        let slot = position as u8;
        return Some(CatalogTarget {
            kind: "builtin",
            slot: Some(slot),
            effect: None,
            name: BASE_NAMES[(slot - 1) as usize],
        });
    }
    None
}

/// Port of `gxr_studio/camdata/catalog_names.py:132 resolve_catalog_position`.
pub fn resolve_catalog_position(
    identity: &str,
    unit: &str,
    position: i64,
) -> Option<CatalogTarget> {
    let descriptors = descriptors_for(identity)?;
    resolve_descriptors(&descriptors, unit, position)
}

/// Port of `gxr_studio/camdata/catalog_names.py:139 recipe_name`.
pub fn recipe_name(identity: &str, unit: &str, position: i64) -> Option<&'static str> {
    resolve_catalog_position(identity, unit, position).map(|t| t.name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn covers_every_identity_in_catalog_generations() {
        // Every identity in CATALOG_GENERATIONS resolves builtins 1..4 for all units and rejects pos <= 0 or unknown unit.
        for &(id, expected_gen) in &CATALOG_GENERATIONS {
            assert_eq!(catalog_generation(id), Some(expected_gen));
            for &(unit, _) in &CAMERA_UNITS {
                assert_eq!(resolve_catalog_position(id, unit, 0), None);
                assert_eq!(resolve_catalog_position(id, unit, -1), None);
                for slot in 1..=4i64 {
                    assert_eq!(
                        resolve_catalog_position(id, unit, slot),
                        Some(CatalogTarget {
                            kind: "builtin",
                            slot: Some(slot as u8),
                            effect: None,
                            name: BASE_NAMES[(slot - 1) as usize],
                        })
                    );
                }
            }
            assert_eq!(resolve_catalog_position(id, "99", 1), None);
        }
        assert_eq!(catalog_generation("deadbeef00"), None);
        assert_eq!(resolve_catalog_position("deadbeef00", "50", 1), None);
    }

    #[test]
    fn g176_record_and_base_fallback_positions() {
        let id = "d25d2f0a68";
        // pos 5 -> effect 1 ("Acros", mask 1 = 50mm only): 50mm hits record, 28mm/MT fall back to base slot 5 ("K64").
        assert_eq!(
            resolve_catalog_position(id, "50", 5),
            Some(CatalogTarget {
                kind: "record",
                slot: None,
                effect: Some(1),
                name: "Acros",
            })
        );
        assert_eq!(
            resolve_catalog_position(id, "28", 5),
            Some(CatalogTarget {
                kind: "builtin",
                slot: Some(5),
                effect: None,
                name: "K64",
            })
        );
        assert_eq!(
            resolve_catalog_position(id, "MT", 5),
            Some(CatalogTarget {
                kind: "builtin",
                slot: Some(5),
                effect: None,
                name: "K64",
            })
        );
        // pos 8 -> effect 4 ("ARRI 709", mask 7 = all units): all units hit record.
        for unit in ["50", "28", "MT"] {
            assert_eq!(recipe_name(id, unit, 8), Some("ARRI 709"));
        }
        // pos 26 -> effect 22 ("Vision3 500T", mask 1 = 50mm): 50mm hits, 28mm gets None (>8 so no base fallback).
        assert_eq!(recipe_name(id, "50", 26), Some("Vision3 500T"));
        assert_eq!(recipe_name(id, "28", 26), None);
        // Last descriptor pos 33 -> effect 29 ("Teal Orange", mask 7); pos 34 -> None.
        assert_eq!(recipe_name(id, "MT", 33), Some("Teal Orange"));
        assert_eq!(recipe_name(id, "MT", 34), None);
    }

    #[test]
    fn g178_and_r6_and_kubik_positions() {
        // Both g178 identities (6a3a5a3c02 and 6adf847602) have identical 57-entry record tables.
        for id in ["6a3a5a3c02", "6adf847602"] {
            // F-CC is record index 0 -> effects 1 (50), 2 (28), 3 (MT) -> pos 5, 6, 7.
            assert_eq!(recipe_name(id, "50", 5), Some("F-CC"));
            assert_eq!(recipe_name(id, "28", 6), Some("F-CC"));
            assert_eq!(recipe_name(id, "MT", 7), Some("F-CC"));
            // Cross-unit on pos 5 falls back to base slot 5 ("K64") because pos <= 8.
            assert_eq!(
                resolve_catalog_position(id, "28", 5),
                Some(CatalogTarget {
                    kind: "builtin",
                    slot: Some(5),
                    effect: None,
                    name: "K64",
                })
            );
            // Teal Orange is G178_RECORDS[14] -> effects 43 (50), 44 (28), 45 (MT) -> pos 47, 48, 49.
            assert_eq!(recipe_name(id, "50", 47), Some("Teal Orange"));
            assert_eq!(recipe_name(id, "28", 48), Some("Teal Orange"));
            assert_eq!(recipe_name(id, "MT", 49), Some("Teal Orange"));
            // Cross-unit beyond pos 8 yields None.
            assert_eq!(recipe_name(id, "28", 47), None);
            // End of g178 table: Tri-X 400 MT at effect 57 (pos 61); pos 62 is None.
            assert_eq!(recipe_name(id, "MT", 61), Some("Tri-X 400"));
            assert_eq!(recipe_name(id, "50", 62), None);
        }

        // KUBIK (0ccc86c19b) extends g178 with K-0815 (62..64), K-PACIFIC (65..67), K-KINO (68..70).
        let kubik = "0ccc86c19b";
        assert_eq!(recipe_name(kubik, "50", 62), Some("K-0815"));
        assert_eq!(recipe_name(kubik, "28", 63), Some("K-0815"));
        assert_eq!(recipe_name(kubik, "MT", 64), Some("K-0815"));
        assert_eq!(recipe_name(kubik, "50", 65), Some("K-PACIFIC"));
        assert_eq!(recipe_name(kubik, "28", 66), Some("K-PACIFIC"));
        assert_eq!(recipe_name(kubik, "MT", 67), Some("K-PACIFIC"));
        assert_eq!(recipe_name(kubik, "50", 68), Some("K-KINO"));
        assert_eq!(recipe_name(kubik, "28", 69), Some("K-KINO"));
        assert_eq!(recipe_name(kubik, "MT", 70), Some("K-KINO"));
        assert_eq!(recipe_name(kubik, "50", 71), None);
    }
}
