use std::ffi::{c_char, c_int, c_uchar};
use std::fmt::{self, Write};

use static_assertions::const_assert_eq;

pub const ABI_VERSION: u32 = 1;
pub const MAX_HEADERS: usize = 16;
pub const MAX_HEADER_BYTES: usize = 16_384;
pub const MAX_HEADER_NAME_BYTES: usize = 64;
pub const MAX_HEADER_VALUE_BYTES: usize = 12_288;
pub const MAX_BODY_BYTES: usize = 1_048_576;
pub const MAX_RESPONSE_BYTES: usize = 1_048_576;

#[repr(i32)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ErrorCode {
    None = 0,
    InvalidArgument = 1,
    Limit = 2,
    Provider = 3,
    Store = 4,
    Certificate = 5,
    Dns = 6,
    Connect = 7,
    Tls = 8,
    Io = 9,
    Http = 10,
    Memory = 11,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct MicrosoftIdentityNativeHeader {
    pub name: *const c_char,
    pub value: *const c_char,
}

#[repr(C)]
pub struct MicrosoftIdentityNativeRequest {
    pub struct_size: u32,
    pub abi_version: u32,
    pub provider_module_path: *const c_char,
    pub store_uri: *const c_char,
    pub client_certificate_path: *const c_char,
    pub ca_bundle_path: *const c_char,
    pub host: *const c_char,
    pub port: u16,
    pub method: *const c_char,
    pub target: *const c_char,
    pub headers: *const MicrosoftIdentityNativeHeader,
    pub header_count: usize,
    pub body: *const c_uchar,
    pub body_size: usize,
    pub connect_timeout_ms: u32,
    pub io_timeout_ms: u32,
}

#[repr(C)]
pub struct MicrosoftIdentityNativeResponse {
    pub http_status: c_int,
    pub headers: *mut c_char,
    pub headers_length: usize,
    pub body: *mut c_uchar,
    pub body_length: usize,
}

impl MicrosoftIdentityNativeResponse {
    pub const fn empty() -> Self {
        Self {
            http_status: 0,
            headers: std::ptr::null_mut(),
            headers_length: 0,
            body: std::ptr::null_mut(),
            body_length: 0,
        }
    }
}

#[repr(C)]
pub struct MicrosoftIdentityNativeError {
    pub code: ErrorCode,
    pub system_error: c_int,
    pub openssl_error: u32,
    pub message: [c_char; 512],
}

impl MicrosoftIdentityNativeError {
    pub const fn empty() -> Self {
        Self {
            code: ErrorCode::None,
            system_error: 0,
            openssl_error: 0,
            message: [0; 512],
        }
    }
}

const_assert_eq!(size_of::<MicrosoftIdentityNativeHeader>(), 16);
const_assert_eq!(size_of::<MicrosoftIdentityNativeRequest>(), 112);
const_assert_eq!(size_of::<MicrosoftIdentityNativeResponse>(), 40);
const_assert_eq!(size_of::<MicrosoftIdentityNativeError>(), 524);

#[derive(Clone)]
pub struct FailureMessage {
    bytes: [u8; 511],
    length: usize,
}

impl FailureMessage {
    fn from_text(value: &str) -> Self {
        let mut output = Self {
            bytes: [0; 511],
            length: 0,
        };
        let _ = output.write_str(value);
        output
    }

    fn from_arguments(arguments: fmt::Arguments<'_>) -> Self {
        let mut output = Self {
            bytes: [0; 511],
            length: 0,
        };
        let _ = fmt::write(&mut output, arguments);
        output
    }

    pub fn as_bytes(&self) -> &[u8] {
        &self.bytes[..self.length]
    }
}

impl Write for FailureMessage {
    fn write_str(&mut self, value: &str) -> fmt::Result {
        let available = self.bytes.len() - self.length;
        let mut copied = value.len().min(available);
        while copied != 0 && !value.is_char_boundary(copied) {
            copied -= 1;
        }
        self.bytes[self.length..self.length + copied].copy_from_slice(&value.as_bytes()[..copied]);
        self.length += copied;
        Ok(())
    }
}

impl fmt::Debug for FailureMessage {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        std::str::from_utf8(self.as_bytes())
            .unwrap_or("<invalid failure message>")
            .fmt(formatter)
    }
}

#[derive(Clone, Debug)]
pub struct BridgeFailure {
    pub code: ErrorCode,
    pub system_error: i32,
    pub openssl_error: u32,
    pub message: FailureMessage,
}

impl BridgeFailure {
    pub fn new(code: ErrorCode, message: &str) -> Self {
        Self {
            code,
            system_error: 0,
            openssl_error: 0,
            message: FailureMessage::from_text(message),
        }
    }

    pub fn formatted(code: ErrorCode, message: fmt::Arguments<'_>) -> Self {
        Self {
            code,
            system_error: 0,
            openssl_error: 0,
            message: FailureMessage::from_arguments(message),
        }
    }

    pub fn system(code: ErrorCode, system_error: i32, message: &str) -> Self {
        Self {
            code,
            system_error,
            openssl_error: 0,
            message: FailureMessage::from_text(message),
        }
    }

    pub fn system_formatted(
        code: ErrorCode,
        system_error: i32,
        message: fmt::Arguments<'_>,
    ) -> Self {
        Self {
            code,
            system_error,
            openssl_error: 0,
            message: FailureMessage::from_arguments(message),
        }
    }

    pub fn openssl_formatted(
        code: ErrorCode,
        openssl_error: u32,
        message: fmt::Arguments<'_>,
    ) -> Self {
        Self {
            code,
            system_error: 0,
            openssl_error,
            message: FailureMessage::from_arguments(message),
        }
    }

    pub fn memory() -> Self {
        Self::new(
            ErrorCode::Memory,
            "Unable to allocate memory for the native mTLS request or response.",
        )
    }
}

pub unsafe fn clear_error(error: *mut MicrosoftIdentityNativeError) {
    if !error.is_null() {
        // SAFETY: the caller supplied writable ABI error storage.
        unsafe { error.write(MicrosoftIdentityNativeError::empty()) };
    }
}

pub unsafe fn write_error(error: *mut MicrosoftIdentityNativeError, failure: &BridgeFailure) {
    if error.is_null() {
        return;
    }
    let mut output = MicrosoftIdentityNativeError::empty();
    output.code = failure.code;
    output.system_error = failure.system_error;
    output.openssl_error = failure.openssl_error;
    let bytes = failure.message.as_bytes();
    let length = bytes.len().min(output.message.len() - 1);
    for (destination, source) in output.message[..length].iter_mut().zip(&bytes[..length]) {
        *destination = *source as c_char;
    }
    // SAFETY: the caller supplied writable ABI error storage.
    unsafe { error.write(output) };
}
