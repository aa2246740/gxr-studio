//! `lut-*` and `scene-*` commands. Owner: wave-1 unit lut.

use crate::common::{Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    let _ = args;
    Err(Failure::Usage(format!("unknown command {command:?}")))
}
