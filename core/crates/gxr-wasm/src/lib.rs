//! wasm-bindgen bindings of the GXR core (see docs/mobile.md).
#![forbid(unsafe_code)]

use wasm_bindgen::prelude::wasm_bindgen;

/// Core version, the same string `gxr-cli version` prints.
#[wasm_bindgen]
pub fn version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}
