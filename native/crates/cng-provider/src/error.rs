use std::ffi::CStr;
use std::fmt::{self, Write};

use cng_core::CngError;
use openssl_provider_sys::{ERR_LIB_USER, ERR_new, ERR_set_error};

pub const INVALID_PARAMETER: i32 = 1;
pub const CNG_FAILURE: i32 = 2;
pub const MEMORY_FAILURE: i32 = 3;
pub const KEY_NOT_LOADED: i32 = 4;
pub const UNSUPPORTED_DIGEST: i32 = 5;
pub const UNSUPPORTED_PADDING: i32 = 6;
pub const PRIVATE_EXPORT_DISABLED: i32 = 7;
pub const SIGNATURE_FAILURE: i32 = 8;
pub const INVALID_URI: i32 = 9;
pub const INVALID_REFERENCE: i32 = 10;
pub const KEY_VALIDATION_FAILURE: i32 = 11;

const ERROR_BUFFER_LENGTH: usize = 512;

struct ErrorBuffer {
    bytes: [u8; ERROR_BUFFER_LENGTH],
    length: usize,
}

impl ErrorBuffer {
    const fn new() -> Self {
        Self {
            bytes: [0; ERROR_BUFFER_LENGTH],
            length: 0,
        }
    }

    fn push(&mut self, byte: u8) {
        if self.length + 1 < self.bytes.len() {
            self.bytes[self.length] = byte;
            self.length += 1;
        }
    }

    fn as_c_str(&self) -> &CStr {
        // SAFETY: the buffer is zero-initialized, `push` never writes NUL, and always leaves the
        // byte following the written message as the terminating NUL.
        unsafe { CStr::from_bytes_with_nul_unchecked(&self.bytes[..=self.length]) }
    }
}

impl Write for ErrorBuffer {
    fn write_str(&mut self, value: &str) -> fmt::Result {
        for byte in value.bytes() {
            if byte == 0 {
                self.push(b'\\');
                self.push(b'0');
            } else {
                self.push(byte);
            }
        }
        Ok(())
    }
}

fn raise_cstr(reason: i32, message: &CStr) {
    // SAFETY: OpenSSL copies the formatted message during this call.
    unsafe {
        ERR_new();
        ERR_set_error(ERR_LIB_USER, reason, c"%s".as_ptr(), message.as_ptr());
    }
}

pub fn raise(reason: i32, message: impl fmt::Display) {
    if reason == MEMORY_FAILURE {
        raise_memory_failure();
        return;
    }
    let mut buffer = ErrorBuffer::new();
    let _ = write!(&mut buffer, "{message}");
    raise_cstr(reason, buffer.as_c_str());
}

pub fn raise_memory_failure() {
    raise_cstr(MEMORY_FAILURE, c"memory allocation failed");
}

const fn cng_reason(reason: i32, error: &CngError) -> i32 {
    if error.is_memory_failure() {
        MEMORY_FAILURE
    } else {
        reason
    }
}

pub fn raise_cng(reason: i32, error: &CngError) {
    if cng_reason(reason, error) == MEMORY_FAILURE {
        raise_memory_failure();
    } else {
        raise(reason, error);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cng_memory_errors_map_to_the_static_memory_reason() {
        let error = CngError::memory_failure("test allocation");
        assert_eq!(cng_reason(CNG_FAILURE, &error), MEMORY_FAILURE);
    }
}
