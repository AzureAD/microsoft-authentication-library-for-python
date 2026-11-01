use std::alloc::{Layout, alloc};
use std::ptr::NonNull;

#[cfg(test)]
thread_local! {
    static BOX_ALLOCATION_FAILURE_COUNTDOWN: std::cell::Cell<Option<usize>> =
        const { std::cell::Cell::new(None) };
    static STRING_ALLOCATION_FAILURE_COUNTDOWN: std::cell::Cell<Option<usize>> =
        const { std::cell::Cell::new(None) };
}

pub fn try_box<T>(value: T) -> Option<Box<T>> {
    #[cfg(test)]
    if BOX_ALLOCATION_FAILURE_COUNTDOWN.with(|countdown| match countdown.get() {
        Some(0) => {
            countdown.set(None);
            true
        }
        Some(remaining) => {
            countdown.set(Some(remaining - 1));
            false
        }
        None => false,
    }) {
        return None;
    }

    let layout = Layout::new::<T>();
    let pointer = if layout.size() == 0 {
        NonNull::<T>::dangling().as_ptr()
    } else {
        // SAFETY: `layout` describes one `T`; a null result is handled below.
        unsafe { alloc(layout).cast::<T>() }
    };
    if pointer.is_null() {
        return None;
    }
    // SAFETY: `pointer` is aligned and allocated for one `T` (or is the valid dangling pointer
    // for a zero-sized `T`). Ownership is immediately transferred to the returned Box.
    unsafe {
        pointer.write(value);
        Some(Box::from_raw(pointer))
    }
}

pub fn try_copy_bytes(value: &[u8]) -> Option<Vec<u8>> {
    let mut copy = Vec::new();
    copy.try_reserve_exact(value.len()).ok()?;
    copy.extend_from_slice(value);
    Some(copy)
}

pub fn try_copy_bytes_with_nul(value: &[u8]) -> Option<Vec<u8>> {
    let required = value.len().checked_add(1)?;
    let mut copy = Vec::new();
    copy.try_reserve_exact(required).ok()?;
    copy.extend_from_slice(value);
    copy.push(0);
    Some(copy)
}

pub fn try_zeroed_bytes(length: usize) -> Option<Vec<u8>> {
    let mut bytes = Vec::new();
    bytes.try_reserve_exact(length).ok()?;
    bytes.resize(length, 0);
    Some(bytes)
}

pub fn try_copy_str(value: &str) -> Option<String> {
    #[cfg(test)]
    if STRING_ALLOCATION_FAILURE_COUNTDOWN.with(|countdown| match countdown.get() {
        Some(0) => {
            countdown.set(None);
            true
        }
        Some(remaining) => {
            countdown.set(Some(remaining - 1));
            false
        }
        None => false,
    }) {
        return None;
    }

    let mut copy = String::new();
    copy.try_reserve_exact(value.len()).ok()?;
    copy.push_str(value);
    Some(copy)
}

#[cfg(test)]
pub fn fail_box_allocation_after(successful_allocations: usize) {
    BOX_ALLOCATION_FAILURE_COUNTDOWN.with(|countdown| {
        countdown.set(Some(successful_allocations));
    });
}

#[cfg(test)]
pub fn fail_string_allocation_after(successful_allocations: usize) {
    STRING_ALLOCATION_FAILURE_COUNTDOWN.with(|countdown| {
        countdown.set(Some(successful_allocations));
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct DropCounter<'a>(&'a AtomicUsize);

    impl Drop for DropCounter<'_> {
        fn drop(&mut self) {
            self.0.fetch_add(1, Ordering::Relaxed);
        }
    }

    #[test]
    fn failed_box_allocation_drops_the_input_once() {
        let drops = AtomicUsize::new(0);
        fail_box_allocation_after(0);

        assert!(try_box(DropCounter(&drops)).is_none());
        assert_eq!(drops.load(Ordering::Relaxed), 1);
    }
}
