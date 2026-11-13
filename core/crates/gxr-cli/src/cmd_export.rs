//! `export-*` commands. Owner: wave-2 export.

use crate::common::{Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    let _ = args;
    Err(Failure::Usage(format!("unknown command {command:?}")))
}
