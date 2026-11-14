//! Every allocation in `fft/*` goes through here, so it is fallible and refused with the
//! shared `size` error (`array is too big; ...`) on native and wasm32 alike.
//!
//! Unit tests can make the n-th allocation on the current thread fail (`fail_nth`), the way
//! Sol's r2 driver made one allocation of the global allocator return null; they then check
//! that every such failure comes back as that `CoreError` instead of aborting.

use gxr_raw::CoreError;

use crate::size;

#[cfg(test)]
thread_local! {
    static FAIL_AT: core::cell::Cell<usize> = const { core::cell::Cell::new(0) };
    static SEEN: core::cell::Cell<usize> = const { core::cell::Cell::new(0) };
}

/// Make the `nth` (1-based) allocation from now on fail; 0 turns injection off. Returns the
/// number of allocations seen since the previous call.
#[cfg(test)]
pub fn fail_nth(nth: usize) -> usize {
    FAIL_AT.with(|f| f.set(nth));
    SEEN.with(|s| s.replace(0))
}

fn injected() -> Result<(), CoreError> {
    #[cfg(test)]
    {
        let seen = SEEN.with(|s| {
            s.set(s.get() + 1);
            s.get()
        });
        if FAIL_AT.with(|f| f.get()) == seen {
            return Err(size::too_big());
        }
    }
    Ok(())
}

/// An empty vector with room for `n` elements.
pub fn with_capacity<T>(n: usize) -> Result<Vec<T>, CoreError> {
    injected()?;
    size::with_capacity(n)
}

/// `n` copies of `value`.
pub fn filled<T: Clone>(n: usize, value: T) -> Result<Vec<T>, CoreError> {
    injected()?;
    size::filled(n, value)
}
