//! `geom-*` commands. Owner: wave-1 unit geometry.

use crate::common::{Failure, Output};

pub fn run(command: &str, args: &[String]) -> Result<Output, Failure> {
    let _ = args;
    Err(Failure::Usage(format!("unknown command {command:?}")))
}
