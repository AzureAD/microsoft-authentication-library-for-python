// Bridge failures use an inline, allocation-free ABI-sized message buffer.
#![allow(clippy::result_large_err)]

use std::ffi::c_void;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::ptr::{copy_nonoverlapping, null_mut};

use openssl_provider_sys::ERR_clear_error;
use zeroize::Zeroize;

mod abi;
mod http;
mod openssl;
mod transport;

pub use abi::{
    MicrosoftIdentityNativeError, MicrosoftIdentityNativeHeader, MicrosoftIdentityNativeRequest,
    MicrosoftIdentityNativeResponse,
};

use abi::{BridgeFailure, ErrorCode, clear_error, write_error};
use http::{ParsedResponse, ValidatedRequest, build_http_request, parse_http_response};
use openssl::OpenSslState;
use transport::{
    Deadline, Winsock, connect_tcp, establish_tls, ssl_expect_clean_end, ssl_read_response,
    ssl_shutdown_deadline, ssl_write_all,
};

#[unsafe(no_mangle)]
pub extern "C" fn microsoft_identity_native_abi_version() -> u32 {
    catch_unwind(|| abi::ABI_VERSION).unwrap_or(abi::ABI_VERSION)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn microsoft_identity_native_request(
    request: *const MicrosoftIdentityNativeRequest,
    response: *mut MicrosoftIdentityNativeResponse,
    error: *mut MicrosoftIdentityNativeError,
) -> i32 {
    // SAFETY: error is optional ABI output storage.
    unsafe { clear_error(error) };
    if response.is_null() {
        // SAFETY: error is optional ABI output storage.
        unsafe {
            write_error(
                error,
                &BridgeFailure::new(
                    ErrorCode::InvalidArgument,
                    "A response structure is required.",
                ),
            );
        }
        return 0;
    }
    // SAFETY: the non-null response pointer is caller-provided writable ABI storage.
    unsafe { response.write(MicrosoftIdentityNativeResponse::empty()) };
    // SAFETY: clears only the current thread's OpenSSL error queue.
    unsafe { ERR_clear_error() };

    let result = catch_unwind(AssertUnwindSafe(|| {
        // SAFETY: validation performs bounded reads of the caller-owned ABI structure.
        let request = unsafe { ValidatedRequest::from_abi(request) }?;
        perform_request(&request)
    }));
    match result {
        Ok(Ok(parsed)) => {
            let allocation = allocate_response(&parsed);
            match allocation {
                Ok(output) => {
                    // SAFETY: response points to writable ABI output storage.
                    unsafe { response.write(output) };
                    1
                }
                Err(failure) => {
                    // SAFETY: error is optional ABI output storage.
                    unsafe { write_error(error, &failure) };
                    0
                }
            }
        }
        Ok(Err(failure)) => {
            // SAFETY: error is optional ABI output storage.
            unsafe { write_error(error, &failure) };
            0
        }
        Err(_) => {
            // SAFETY: error is optional ABI output storage.
            unsafe {
                write_error(
                    error,
                    &BridgeFailure::new(
                        ErrorCode::Io,
                        "A Rust panic was contained at the native bridge boundary.",
                    ),
                );
            }
            0
        }
    }
}

fn perform_request(request: &ValidatedRequest) -> Result<ParsedResponse, BridgeFailure> {
    let http_request = build_http_request(request)?;
    let mut openssl = OpenSslState::initialize(request)?;
    let _winsock = Winsock::startup()?;
    let connect_deadline = Deadline::new(request.connect_timeout_ms);
    let socket = connect_tcp(request, connect_deadline)?;
    let io_deadline = Deadline::new(request.io_timeout_ms);
    establish_tls(&mut openssl, &socket, request, io_deadline)?;
    ssl_write_all(openssl.ssl, socket.raw(), &http_request, io_deadline)?;
    let raw_response = ssl_read_response(openssl.ssl, socket.raw(), &request.method, io_deadline)?;
    let parsed = parse_http_response(&raw_response, &request.method)?;
    ssl_expect_clean_end(openssl.ssl, socket.raw(), io_deadline)?;
    ssl_shutdown_deadline(openssl.ssl, socket.raw(), io_deadline)?;
    Ok(parsed)
}

fn allocate_response(
    parsed: &ParsedResponse,
) -> Result<MicrosoftIdentityNativeResponse, BridgeFailure> {
    let header_size = parsed.headers.len().checked_add(1).ok_or_else(|| {
        BridgeFailure::new(
            ErrorCode::Memory,
            "Unable to allocate the parsed HTTP response.",
        )
    })?;
    // SAFETY: malloc returns suitably aligned storage or null; header_size is nonzero.
    let headers = unsafe { libc::malloc(header_size) }.cast::<i8>();
    if headers.is_null() {
        return Err(BridgeFailure::new(
            ErrorCode::Memory,
            "Unable to allocate the parsed HTTP response.",
        ));
    }
    // SAFETY: `headers` is writable for header_size bytes.
    unsafe {
        copy_nonoverlapping(
            parsed.headers.as_ptr(),
            headers.cast::<u8>(),
            parsed.headers.len(),
        );
        *headers.add(parsed.headers.len()) = 0;
    }
    let body = if parsed.body.is_empty() {
        null_mut()
    } else {
        // SAFETY: malloc returns writable storage or null for the requested nonzero length.
        unsafe { libc::malloc(parsed.body.len()) }.cast::<u8>()
    };
    if !parsed.body.is_empty() && body.is_null() {
        // SAFETY: headers was allocated above for exactly header_size writable bytes.
        unsafe {
            wipe_bytes(headers.cast(), header_size);
            libc::free(headers.cast::<c_void>());
        }
        return Err(BridgeFailure::new(
            ErrorCode::Memory,
            "Unable to allocate the parsed HTTP response.",
        ));
    }
    if !body.is_null() {
        // SAFETY: body is writable for parsed.body.len() bytes.
        unsafe { copy_nonoverlapping(parsed.body.as_ptr(), body, parsed.body.len()) };
    }
    Ok(MicrosoftIdentityNativeResponse {
        http_status: parsed.status,
        headers,
        headers_length: parsed.headers.len(),
        body,
        body_length: parsed.body.len(),
    })
}

unsafe fn wipe_bytes(pointer: *mut u8, length: usize) {
    if !pointer.is_null() && length != 0 {
        // SAFETY: callers provide a writable allocation of at least `length` bytes.
        unsafe { std::slice::from_raw_parts_mut(pointer, length) }.zeroize();
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn microsoft_identity_native_response_free(
    response: *mut MicrosoftIdentityNativeResponse,
) {
    let _ = catch_unwind(AssertUnwindSafe(|| {
        if response.is_null() {
            return;
        }
        // SAFETY: response is caller-provided ABI storage initialized by this bridge on success.
        let response = unsafe { &mut *response };
        if !response.headers.is_null() {
            if response.headers_length <= abi::MAX_HEADER_BYTES + 2 {
                // SAFETY: successful bridge allocations include the trailing NUL byte.
                unsafe {
                    wipe_bytes(response.headers.cast(), response.headers_length + 1);
                }
            }
            // SAFETY: successful response headers are allocated with libc::malloc.
            unsafe { libc::free(response.headers.cast::<c_void>()) };
        }
        if !response.body.is_null() {
            if response.body_length <= abi::MAX_RESPONSE_BYTES {
                // SAFETY: successful body allocations contain body_length writable bytes.
                unsafe { wipe_bytes(response.body, response.body_length) };
            }
            // SAFETY: successful response bodies are allocated with libc::malloc.
            unsafe { libc::free(response.body.cast::<c_void>()) };
        }
        *response = MicrosoftIdentityNativeResponse::empty();
    }));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plaintext_allocation_wipe_clears_every_byte() {
        let mut plaintext = b"intermediate response plaintext".to_vec();
        // SAFETY: the vector owns a writable allocation covering its current length.
        unsafe { wipe_bytes(plaintext.as_mut_ptr(), plaintext.len()) };
        assert!(plaintext.iter().all(|byte| *byte == 0));
    }
}
