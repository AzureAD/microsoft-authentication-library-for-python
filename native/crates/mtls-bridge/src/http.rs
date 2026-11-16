use std::ffi::c_char;
use std::fmt;
use std::net::IpAddr;
use std::path::Path;

use zeroize::Zeroizing;

use crate::abi::{
    BridgeFailure, ErrorCode, MAX_BODY_BYTES, MAX_HEADER_BYTES, MAX_HEADER_NAME_BYTES,
    MAX_HEADER_VALUE_BYTES, MAX_HEADERS, MAX_RESPONSE_BYTES, MicrosoftIdentityNativeRequest,
};

const DEFAULT_TIMEOUT_MS: u32 = 10_000;
const MAX_TIMEOUT_MS: u32 = 120_000;
const MAX_HOST: usize = 253;
const MAX_METHOD: usize = 8;
const MAX_TARGET: usize = 2_048;
const MAX_GENERAL_HEADER_VALUE: usize = 1_024;
const MAX_REQUEST_BYTES: usize = MAX_HEADER_BYTES + MAX_BODY_BYTES;
const INVALID_CORE_MESSAGE: &str =
    "A provider, CNG URI, certificate path, host, port, method, or relative target is invalid.";
const INVALID_HEADER_MESSAGE: &str = "Only bounded Accept, Authorization, Content-Type, User-Agent, X-Request-ID, client-request-id, x-ms-client-request-id, and x-ms-tokenboundauth headers are accepted.";

pub struct Header {
    pub name: String,
    pub value: Zeroizing<String>,
}

impl fmt::Debug for Header {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Header")
            .field("name", &self.name)
            .field("value", &"[REDACTED]")
            .finish()
    }
}

pub struct ValidatedRequest {
    pub provider_module_path: Zeroizing<String>,
    pub store_uri: Zeroizing<String>,
    pub client_certificate_path: Zeroizing<String>,
    pub ca_bundle_path: Zeroizing<String>,
    pub host: String,
    pub port: u16,
    pub method: String,
    pub target: String,
    pub headers: Vec<Header>,
    pub body: Zeroizing<Vec<u8>>,
    pub connect_timeout_ms: u32,
    pub io_timeout_ms: u32,
}

impl fmt::Debug for ValidatedRequest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ValidatedRequest")
            .field("provider_module_path", &"[REDACTED]")
            .field("store_uri", &"[REDACTED]")
            .field("client_certificate_path", &"[REDACTED]")
            .field("ca_bundle_path", &"[REDACTED]")
            .field("host", &self.host)
            .field("port", &self.port)
            .field("method", &self.method)
            .field("target", &self.target)
            .field("headers", &self.headers)
            .field("body", &"[REDACTED]")
            .field("connect_timeout_ms", &self.connect_timeout_ms)
            .field("io_timeout_ms", &self.io_timeout_ms)
            .finish()
    }
}

impl ValidatedRequest {
    pub unsafe fn from_abi(
        request: *const MicrosoftIdentityNativeRequest,
    ) -> Result<Self, BridgeFailure> {
        if request.is_null() {
            return Err(BridgeFailure::new(
                ErrorCode::InvalidArgument,
                "The request ABI version or structure size is invalid.",
            ));
        }
        // SAFETY: reading the first two u32 fields requires only the documented ABI prefix.
        let struct_size = unsafe { request.cast::<u32>().read_unaligned() };
        // SAFETY: the ABI prefix contains a second u32 immediately after struct_size.
        let abi_version = unsafe { request.cast::<u32>().add(1).read_unaligned() };
        if struct_size < size_of::<MicrosoftIdentityNativeRequest>() as u32
            || abi_version != super::abi::ABI_VERSION
        {
            return Err(BridgeFailure::new(
                ErrorCode::InvalidArgument,
                "The request ABI version or structure size is invalid.",
            ));
        }
        // SAFETY: struct_size now establishes that the complete version-1 structure is readable.
        let request = unsafe { &*request };

        // SAFETY: each ABI string is caller-owned and validated with a bounded scan.
        let provider_module_path = Zeroizing::new(unsafe {
            read_utf8(request.provider_module_path, 32_767, INVALID_CORE_MESSAGE)
        }?);
        // SAFETY: same bounded caller-owned string invariant.
        let store_uri =
            Zeroizing::new(unsafe { read_utf8(request.store_uri, 2_047, INVALID_CORE_MESSAGE) }?);
        // SAFETY: same bounded caller-owned string invariant.
        let client_certificate_path = Zeroizing::new(unsafe {
            read_utf8(
                request.client_certificate_path,
                32_767,
                INVALID_CORE_MESSAGE,
            )
        }?);
        // SAFETY: same bounded caller-owned string invariant.
        let ca_bundle_path = Zeroizing::new(unsafe {
            read_utf8(request.ca_bundle_path, 32_767, INVALID_CORE_MESSAGE)
        }?);
        // SAFETY: same bounded caller-owned string invariant.
        let host = unsafe { read_utf8(request.host, MAX_HOST, INVALID_CORE_MESSAGE) }?;
        // SAFETY: same bounded caller-owned string invariant.
        let method = unsafe { read_utf8(request.method, MAX_METHOD, INVALID_CORE_MESSAGE) }?;
        // SAFETY: same bounded caller-owned string invariant.
        let target = unsafe { read_utf8(request.target, MAX_TARGET, INVALID_CORE_MESSAGE) }?;

        if !Path::new(provider_module_path.as_str()).is_absolute()
            || !Path::new(client_certificate_path.as_str()).is_absolute()
            || !Path::new(ca_bundle_path.as_str()).is_absolute()
        {
            return Err(BridgeFailure::new(
                ErrorCode::InvalidArgument,
                "Provider, client-certificate, and CA-bundle paths must be absolute.",
            ));
        }

        if !store_uri
            .get(..7)
            .is_some_and(|prefix| prefix.eq_ignore_ascii_case("cng://?"))
            || !host_is_valid(&host)
            || request.port == 0
            || !method_is_allowed(&method)
            || !target_is_valid(&target)
        {
            return Err(invalid_core());
        }
        if request.header_count > MAX_HEADERS
            || (request.header_count != 0 && request.headers.is_null())
            || request.body_size > MAX_BODY_BYTES
            || (request.body_size != 0 && request.body.is_null())
        {
            return Err(BridgeFailure::new(
                ErrorCode::Limit,
                "The request exceeds the header or body limit.",
            ));
        }

        let mut headers = Vec::new();
        headers
            .try_reserve_exact(request.header_count)
            .map_err(|_| BridgeFailure::memory())?;
        if request.header_count != 0 {
            // SAFETY: the count was bounded and the pointer is non-null by the preceding checks.
            let input =
                unsafe { std::slice::from_raw_parts(request.headers, request.header_count) };
            for header in input {
                // SAFETY: header strings are caller-owned and scanned within ABI limits.
                let name = unsafe {
                    read_utf8(header.name, MAX_HEADER_NAME_BYTES, INVALID_HEADER_MESSAGE)
                }?;
                // SAFETY: same bounded caller-owned string invariant.
                let value = Zeroizing::new(unsafe {
                    read_utf8(header.value, MAX_HEADER_VALUE_BYTES, INVALID_HEADER_MESSAGE)
                }?);
                if !header_name_is_allowed(&name)
                    || !header_value_is_valid(&name, &value)
                    || headers
                        .iter()
                        .any(|existing: &Header| existing.name.eq_ignore_ascii_case(&name))
                {
                    return Err(BridgeFailure::new(
                        ErrorCode::InvalidArgument,
                        INVALID_HEADER_MESSAGE,
                    ));
                }
                headers.push(Header { name, value });
            }
        }
        let body = Zeroizing::new(if request.body_size == 0 {
            Vec::new()
        } else {
            // SAFETY: the body pointer is non-null and the size is bounded by one MiB.
            try_copy_bytes(unsafe { std::slice::from_raw_parts(request.body, request.body_size) })?
        });
        let connect_timeout_ms = if request.connect_timeout_ms == 0 {
            DEFAULT_TIMEOUT_MS
        } else {
            request.connect_timeout_ms
        };
        let io_timeout_ms = if request.io_timeout_ms == 0 {
            DEFAULT_TIMEOUT_MS
        } else {
            request.io_timeout_ms
        };
        if connect_timeout_ms > MAX_TIMEOUT_MS || io_timeout_ms > MAX_TIMEOUT_MS {
            return Err(BridgeFailure::new(
                ErrorCode::Limit,
                "Connection and I/O timeouts must not exceed 120 seconds.",
            ));
        }

        Ok(Self {
            provider_module_path,
            store_uri,
            client_certificate_path,
            ca_bundle_path,
            host,
            port: request.port,
            method,
            target,
            headers,
            body,
            connect_timeout_ms,
            io_timeout_ms,
        })
    }
}

fn invalid_core() -> BridgeFailure {
    BridgeFailure::new(ErrorCode::InvalidArgument, INVALID_CORE_MESSAGE)
}

pub(crate) unsafe fn read_utf8(
    pointer: *const c_char,
    maximum: usize,
    invalid_message: &str,
) -> Result<String, BridgeFailure> {
    if pointer.is_null() {
        return Err(BridgeFailure::new(
            ErrorCode::InvalidArgument,
            invalid_message,
        ));
    }
    let mut length = 0;
    while length <= maximum {
        // SAFETY: the ABI requires a readable NUL-terminated string; the bounded scan prevents
        // reading beyond the documented maximum plus terminator.
        if unsafe { *pointer.add(length) } == 0 {
            break;
        }
        length += 1;
    }
    if length == 0 || length > maximum {
        return Err(BridgeFailure::new(
            ErrorCode::InvalidArgument,
            invalid_message,
        ));
    }
    // SAFETY: the bounded scan established exactly `length` initialized bytes before the NUL.
    let bytes = unsafe { std::slice::from_raw_parts(pointer.cast::<u8>(), length) };
    let value = std::str::from_utf8(bytes)
        .map_err(|_| BridgeFailure::new(ErrorCode::InvalidArgument, invalid_message))?;
    try_copy_string(value)
}

fn method_is_allowed(method: &str) -> bool {
    matches!(
        method,
        "GET" | "POST" | "PUT" | "DELETE" | "PATCH" | "HEAD" | "OPTIONS"
    )
}

fn host_is_valid(host: &str) -> bool {
    if host.parse::<IpAddr>().is_ok() {
        return true;
    }
    let bytes = host.as_bytes();
    if bytes.is_empty()
        || bytes.len() > MAX_HOST
        || matches!(bytes.first(), Some(b'.' | b'-'))
        || matches!(bytes.last(), Some(b'.' | b'-'))
    {
        return false;
    }
    for (index, value) in bytes.iter().copied().enumerate() {
        if !value.is_ascii_alphanumeric() && value != b'.' && value != b'-' {
            return false;
        }
        if value == b'.'
            && ((index > 0 && matches!(bytes[index - 1], b'.' | b'-'))
                || (index + 1 < bytes.len() && bytes[index + 1] == b'-'))
        {
            return false;
        }
    }
    true
}

fn target_is_valid(target: &str) -> bool {
    let bytes = target.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= MAX_TARGET
        && bytes[0] == b'/'
        && !target.contains("://")
        && !target.contains('#')
        && bytes
            .iter()
            .all(|value| (0x21..=0x7e).contains(value) && *value != b'\\')
}

fn header_name_is_allowed(name: &str) -> bool {
    [
        "Accept",
        "Authorization",
        "Content-Type",
        "User-Agent",
        "X-Request-ID",
        "client-request-id",
        "x-ms-client-request-id",
        "x-ms-tokenboundauth",
    ]
    .iter()
    .any(|allowed| name.eq_ignore_ascii_case(allowed))
}

fn header_value_is_valid(name: &str, value: &str) -> bool {
    if value.is_empty()
        || value.len() > MAX_HEADER_VALUE_BYTES
        || !value
            .as_bytes()
            .iter()
            .all(|byte| (0x20..=0x7e).contains(byte))
    {
        return false;
    }
    if name.eq_ignore_ascii_case("Authorization") {
        let Some(credential) = value.get("mtls_pop ".len()..) else {
            return false;
        };
        return value[.."mtls_pop ".len()].eq_ignore_ascii_case("mtls_pop ")
            && !credential.is_empty()
            && credential
                .as_bytes()
                .iter()
                .all(|byte| (0x21..=0x7e).contains(byte));
    }
    if name.eq_ignore_ascii_case("x-ms-tokenboundauth") {
        return value.eq_ignore_ascii_case("true");
    }
    value.len() <= MAX_GENERAL_HEADER_VALUE
}

pub fn build_http_request(request: &ValidatedRequest) -> Result<Zeroizing<Vec<u8>>, BridgeFailure> {
    let is_ipv6 = request.host.parse::<std::net::Ipv6Addr>().is_ok();
    let mut port_storage = [0_u8; 5];
    let port = decimal_bytes(usize::from(request.port), &mut port_storage);
    let mut length_storage = [0_u8; 20];
    let content_length = decimal_bytes(request.body.len(), &mut length_storage);
    let mut header_length = request
        .method
        .len()
        .checked_add(1)
        .and_then(|value| value.checked_add(request.target.len()))
        .and_then(|value| value.checked_add(b" HTTP/1.1\r\nHost: ".len()))
        .and_then(|value| value.checked_add(request.host.len()))
        .and_then(|value| value.checked_add(if is_ipv6 { b"[]".len() } else { 0 }))
        .and_then(|value| {
            value.checked_add(if request.port == 443 {
                b"\r\n".len()
            } else {
                1 + port.len() + b"\r\n".len()
            })
        })
        .and_then(|value| value.checked_add(b"Connection: close\r\nContent-Length: ".len()))
        .and_then(|value| value.checked_add(content_length.len()))
        .and_then(|value| value.checked_add(b"\r\n".len()))
        .ok_or_else(BridgeFailure::memory)?;
    for header in &request.headers {
        header_length = header_length
            .checked_add(header.name.len())
            .and_then(|value| value.checked_add(b": ".len()))
            .and_then(|value| value.checked_add(header.value.len()))
            .and_then(|value| value.checked_add(b"\r\n".len()))
            .ok_or_else(BridgeFailure::memory)?;
    }
    if header_length > MAX_HEADER_BYTES {
        return Err(BridgeFailure::new(
            ErrorCode::Limit,
            "The generated HTTP request headers are too large.",
        ));
    }
    let total_length = header_length
        .checked_add(b"\r\n".len())
        .and_then(|value| value.checked_add(request.body.len()))
        .ok_or_else(BridgeFailure::memory)?;
    if total_length > MAX_REQUEST_BYTES {
        return Err(BridgeFailure::new(
            ErrorCode::Limit,
            "The generated HTTP request exceeds its limit.",
        ));
    }
    let mut output = Vec::new();
    output
        .try_reserve_exact(total_length)
        .map_err(|_| BridgeFailure::memory())?;
    output.extend_from_slice(request.method.as_bytes());
    output.push(b' ');
    output.extend_from_slice(request.target.as_bytes());
    output.extend_from_slice(b" HTTP/1.1\r\nHost: ");
    if is_ipv6 {
        output.push(b'[');
    }
    output.extend_from_slice(request.host.as_bytes());
    if is_ipv6 {
        output.push(b']');
    }
    if request.port != 443 {
        output.push(b':');
        output.extend_from_slice(port);
    }
    output.extend_from_slice(b"\r\nConnection: close\r\nContent-Length: ");
    output.extend_from_slice(content_length);
    output.extend_from_slice(b"\r\n");
    for header in &request.headers {
        output.extend_from_slice(header.name.as_bytes());
        output.extend_from_slice(b": ");
        output.extend_from_slice(header.value.as_bytes());
        output.extend_from_slice(b"\r\n");
    }
    output.extend_from_slice(b"\r\n");
    output.extend_from_slice(&request.body);
    Ok(Zeroizing::new(output))
}

#[derive(Eq, PartialEq)]
pub struct ParsedResponse {
    pub status: i32,
    pub headers: Zeroizing<Vec<u8>>,
    pub body: Zeroizing<Vec<u8>>,
}

impl fmt::Debug for ParsedResponse {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ParsedResponse")
            .field("status", &self.status)
            .field("headers_length", &self.headers.len())
            .field("body_length", &self.body.len())
            .finish()
    }
}

#[derive(Clone, Copy, Debug)]
pub struct ResponseFraming {
    pub expected_length: usize,
    pub status: i32,
}

pub fn inspect_http_response(raw: &[u8], method: &str) -> Result<ResponseFraming, BridgeFailure> {
    let header_end = find_bytes(raw, b"\r\n\r\n").ok_or_else(|| {
        BridgeFailure::new(
            ErrorCode::Http,
            "The HTTP response header is missing or too large.",
        )
    })?;
    if header_end > MAX_HEADER_BYTES {
        return Err(BridgeFailure::new(
            ErrorCode::Http,
            "The HTTP response header is missing or too large.",
        ));
    }
    let status_end = find_bytes(&raw[..header_end + 2], b"\r\n")
        .ok_or_else(|| BridgeFailure::new(ErrorCode::Http, "The HTTP status line is malformed."))?;
    let status_line = &raw[..status_end];
    if status_line.len() < 12
        || (!status_line.starts_with(b"HTTP/1.1 ") && !status_line.starts_with(b"HTTP/1.0 "))
        || !(b'1'..=b'5').contains(&status_line[9])
        || !status_line[10].is_ascii_digit()
        || !status_line[11].is_ascii_digit()
        || (status_line.len() > 12 && status_line[12] != b' ')
    {
        return Err(BridgeFailure::new(
            ErrorCode::Http,
            "The HTTP status line is unsupported.",
        ));
    }
    let status = i32::from(status_line[9] - b'0') * 100
        + i32::from(status_line[10] - b'0') * 10
        + i32::from(status_line[11] - b'0');
    let headers_start = status_end + 2;
    let headers = &raw[headers_start..header_end + 2];
    let content_length = parse_content_length(headers)?;
    let header_bytes = header_end + 4;
    let expected_length = if response_has_no_body(method, status) {
        header_bytes
    } else {
        let Some(content_length) = content_length else {
            return Err(BridgeFailure::new(
                ErrorCode::Http,
                "A body-bearing HTTP response requires Content-Length.",
            ));
        };
        if content_length > MAX_RESPONSE_BYTES.saturating_sub(header_bytes) {
            return Err(BridgeFailure::new(
                ErrorCode::Limit,
                "The HTTPS response exceeds 1 MiB.",
            ));
        }
        header_bytes + content_length
    };
    Ok(ResponseFraming {
        expected_length,
        status,
    })
}

fn parse_content_length(headers: &[u8]) -> Result<Option<usize>, BridgeFailure> {
    let mut content_length = None;
    let mut cursor = 0;
    while cursor < headers.len() {
        let relative_end = find_bytes(&headers[cursor..], b"\r\n").ok_or_else(|| {
            BridgeFailure::new(
                ErrorCode::Http,
                "The HTTP response contains a malformed header.",
            )
        })?;
        if relative_end == 0 {
            break;
        }
        let line = &headers[cursor..cursor + relative_end];
        let colon = line
            .iter()
            .position(|value| *value == b':')
            .ok_or_else(|| {
                BridgeFailure::new(
                    ErrorCode::Http,
                    "The HTTP response contains a malformed header.",
                )
            })?;
        let name = &line[..colon];
        let value = &line[colon + 1..];
        if name.is_empty()
            || !name.iter().all(|byte| {
                byte.is_ascii_alphanumeric()
                    || matches!(
                        byte,
                        b'!' | b'#'
                            | b'$'
                            | b'%'
                            | b'&'
                            | b'\''
                            | b'*'
                            | b'+'
                            | b'-'
                            | b'.'
                            | b'^'
                            | b'_'
                            | b'`'
                            | b'|'
                            | b'~'
                    )
            })
        {
            return Err(BridgeFailure::new(
                ErrorCode::Http,
                "The HTTP response header name is invalid.",
            ));
        }
        if !value
            .iter()
            .all(|byte| (*byte >= 0x20 || *byte == b'\t') && *byte <= 0x7e)
        {
            return Err(BridgeFailure::new(
                ErrorCode::Http,
                "The HTTP response header value is invalid.",
            ));
        }
        if name.eq_ignore_ascii_case(b"Content-Length") {
            if content_length.is_some() {
                return Err(BridgeFailure::new(
                    ErrorCode::Http,
                    "Duplicate Content-Length headers are rejected.",
                ));
            }
            let start = value
                .iter()
                .position(|byte| !matches!(byte, b' ' | b'\t'))
                .unwrap_or(value.len());
            let end = value
                .iter()
                .rposition(|byte| !matches!(byte, b' ' | b'\t'))
                .map_or(start, |end| end + 1);
            let value = &value[start..end];
            if value.is_empty() || !value.iter().all(u8::is_ascii_digit) {
                return Err(BridgeFailure::new(
                    ErrorCode::Http,
                    "The Content-Length header is invalid.",
                ));
            }
            let mut parsed = 0_usize;
            for digit in value {
                parsed = parsed
                    .checked_mul(10)
                    .and_then(|current| current.checked_add(usize::from(digit - b'0')))
                    .ok_or_else(|| {
                        BridgeFailure::new(ErrorCode::Http, "The Content-Length header is invalid.")
                    })?;
            }
            content_length = Some(parsed);
        } else if name.eq_ignore_ascii_case(b"Transfer-Encoding") {
            return Err(BridgeFailure::new(
                ErrorCode::Http,
                "Transfer-Encoding responses are not accepted.",
            ));
        }
        cursor += relative_end + 2;
    }
    Ok(content_length)
}

pub fn parse_http_response(raw: &[u8], method: &str) -> Result<ParsedResponse, BridgeFailure> {
    let framing = inspect_http_response(raw, method)?;
    if raw.len() != framing.expected_length {
        return Err(BridgeFailure::new(
            ErrorCode::Http,
            "The response body does not match its HTTP framing.",
        ));
    }
    let header_end = find_bytes(raw, b"\r\n\r\n").expect("inspected header delimiter");
    let status_end =
        find_bytes(&raw[..header_end + 2], b"\r\n").expect("inspected status delimiter");
    let headers = Zeroizing::new(try_copy_bytes(&raw[status_end + 2..header_end + 2])?);
    let body = Zeroizing::new(try_copy_bytes(&raw[header_end + 4..])?);
    Ok(ParsedResponse {
        status: framing.status,
        headers,
        body,
    })
}

pub fn response_has_no_body(method: &str, status: i32) -> bool {
    method == "HEAD" || status / 100 == 1 || status == 204 || status == 304
}

pub fn find_bytes(buffer: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() {
        return Some(0);
    }
    buffer
        .windows(needle.len())
        .position(|window| window == needle)
}

fn try_copy_string(value: &str) -> Result<String, BridgeFailure> {
    let mut output = String::new();
    output
        .try_reserve_exact(value.len())
        .map_err(|_| BridgeFailure::memory())?;
    output.push_str(value);
    Ok(output)
}

fn try_copy_bytes(value: &[u8]) -> Result<Vec<u8>, BridgeFailure> {
    let mut output = Vec::new();
    output
        .try_reserve_exact(value.len())
        .map_err(|_| BridgeFailure::memory())?;
    output.extend_from_slice(value);
    Ok(output)
}

fn decimal_bytes(mut value: usize, storage: &mut [u8]) -> &[u8] {
    let mut cursor = storage.len();
    loop {
        cursor -= 1;
        storage[cursor] = b'0' + (value % 10) as u8;
        value /= 10;
        if value == 0 {
            return &storage[cursor..];
        }
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::alloc::{GlobalAlloc, Layout, System};
    use std::cell::Cell;

    use crate::abi::{ABI_VERSION, MicrosoftIdentityNativeHeader};

    #[derive(Clone, Copy, Default)]
    struct CopyObservation {
        length: usize,
        pointer: *mut u8,
        freed: bool,
        dirty: bool,
    }

    thread_local! {
        static COPY_OBSERVATION: Cell<CopyObservation> = const { Cell::new(CopyObservation {
            length: 0,
            pointer: std::ptr::null_mut(),
            freed: false,
            dirty: false,
        }) };
    }

    struct ObservedAllocator;

    // SAFETY: allocation/free pairing and layouts are delegated unchanged to System.
    // The thread-local observer is allocation-free and has no destructor. Each fixture
    // selects a copy's unique allocation size and initializes all bytes before freeing it.
    unsafe impl GlobalAlloc for ObservedAllocator {
        unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
            // SAFETY: the caller supplies a valid allocation layout.
            let pointer = unsafe { System.alloc(layout) };
            COPY_OBSERVATION.with(|observation| {
                let mut state = observation.get();
                if state.length != 0 && layout.size() == state.length {
                    state.pointer = pointer;
                    observation.set(state);
                }
            });
            pointer
        }

        unsafe fn dealloc(&self, pointer: *mut u8, layout: Layout) {
            COPY_OBSERVATION.with(|observation| {
                let mut state = observation.get();
                if pointer == state.pointer {
                    // SAFETY: the fixture's watched copy initialized all `length` bytes;
                    // its allocation remains live until System.dealloc below.
                    let bytes = unsafe { std::slice::from_raw_parts(pointer, state.length) };
                    state.dirty = bytes.iter().any(|byte| *byte != 0);
                    state.freed = true;
                    state.pointer = std::ptr::null_mut();
                    observation.set(state);
                }
            });
            // SAFETY: pointer and layout come unchanged from this allocator.
            unsafe { System.dealloc(pointer, layout) };
        }
    }

    #[global_allocator]
    static ALLOCATOR: ObservedAllocator = ObservedAllocator;

    fn abi_request() -> MicrosoftIdentityNativeRequest {
        MicrosoftIdentityNativeRequest {
            struct_size: size_of::<MicrosoftIdentityNativeRequest>() as u32,
            abi_version: ABI_VERSION,
            provider_module_path: c"C:\\provider.dll".as_ptr(),
            store_uri: c"cng://?key=test".as_ptr(),
            client_certificate_path: c"C:\\client.pem".as_ptr(),
            ca_bundle_path: c"C:\\ca.pem".as_ptr(),
            host: c"localhost".as_ptr(),
            port: 443,
            method: c"POST".as_ptr(),
            target: c"/".as_ptr(),
            headers: std::ptr::null(),
            header_count: 0,
            body: std::ptr::null(),
            body_size: 0,
            connect_timeout_ms: 0,
            io_timeout_ms: 0,
        }
    }

    fn assert_failed_copy_is_wiped(
        input: &MicrosoftIdentityNativeRequest,
        length: usize,
        expected_error: ErrorCode,
    ) {
        assert_deallocated_copy_is_wiped(length, || {
            // SAFETY: fixtures keep the complete ABI request and every referenced string,
            // header array and body alive through this immediate validation call.
            let result = unsafe { ValidatedRequest::from_abi(input) };
            assert_eq!(
                result.expect_err("fixture must fail validation").code,
                expected_error
            );
        });
    }

    pub(crate) fn assert_deallocated_copy_is_wiped(length: usize, operation: impl FnOnce()) {
        COPY_OBSERVATION.set(CopyObservation {
            length,
            ..CopyObservation::default()
        });
        operation();
        let observed = COPY_OBSERVATION.replace(CopyObservation::default());
        assert!(observed.freed, "request copy was not observed");
        assert!(!observed.dirty, "request copy was freed without wiping");
    }

    fn success(method: &str, raw: &str, status: i32, body: &[u8]) {
        let parsed = parse_http_response(raw.as_bytes(), method).expect("valid response");
        assert_eq!(parsed.status, status);
        assert_eq!(parsed.body.as_slice(), body);
    }

    fn failure(method: &str, raw: &str) {
        assert_eq!(
            parse_http_response(raw.as_bytes(), method)
                .expect_err("invalid response")
                .code,
            ErrorCode::Http
        );
    }

    #[test]
    fn rejected_headers_wipe_copied_values() {
        let value = std::ffi::CString::new(format!("mtls_pop {}", "t".repeat(71)))
            .expect("synthetic token");
        for name in [c"Authorization", c"Not-Allowed"] {
            let headers = [MicrosoftIdentityNativeHeader {
                name: name.as_ptr(),
                value: value.as_ptr(),
            }; 2];
            let mut input = abi_request();
            input.headers = headers.as_ptr();
            input.header_count = headers.len();
            assert_failed_copy_is_wiped(&input, value.as_bytes().len(), ErrorCode::InvalidArgument);
        }
    }

    #[test]
    fn rejected_timeouts_wipe_copied_body() {
        let body = [b's'; 80];
        for (connect_timeout_ms, io_timeout_ms) in
            [(MAX_TIMEOUT_MS + 1, 0), (0, MAX_TIMEOUT_MS + 1)]
        {
            let mut input = abi_request();
            input.body = body.as_ptr();
            input.body_size = body.len();
            input.connect_timeout_ms = connect_timeout_ms;
            input.io_timeout_ms = io_timeout_ms;
            assert_failed_copy_is_wiped(&input, body.len(), ErrorCode::Limit);
        }
    }

    #[test]
    fn later_validation_failure_wipes_store_uri_copy() {
        let value = std::ffi::CString::new(format!("cng://?{}", "k".repeat(73)))
            .expect("synthetic STORE URI");
        let mut input = abi_request();
        input.store_uri = value.as_ptr();
        input.client_certificate_path = std::ptr::null();
        assert_failed_copy_is_wiped(&input, value.as_bytes().len(), ErrorCode::InvalidArgument);
    }

    #[test]
    fn response_header_names_accept_http_token_punctuation() {
        success(
            "GET",
            "HTTP/1.1 200 OK\r\nX!#$%&'*+-.^_`|~: test\r\nContent-Length: 0\r\n\r\n",
            200,
            b"",
        );
        failure("GET", "HTTP/1.1 200 OK\r\nContent-Length : 0\r\n\r\n");
        failure(
            "GET",
            "HTTP/1.1 200 OK\r\nX Request: test\r\nContent-Length: 0\r\n\r\n",
        );
    }

    #[test]
    fn content_length_accepts_surrounding_ows_but_not_ambiguous_values() {
        success(
            "GET",
            "HTTP/1.1 200 OK\r\nContent-Length: \t5 \t\r\n\r\nhello",
            200,
            b"hello",
        );
        for value in [" \t ", "5 0", "+5", "5,5"] {
            failure(
                "GET",
                &format!("HTTP/1.1 200 OK\r\nContent-Length: {value}\r\n\r\nhello"),
            );
        }
        failure(
            "GET",
            "HTTP/1.1 200 OK\r\nContent-Length: 0 \t\r\nContent-Length:\t0 \r\n\r\n",
        );
    }

    #[test]
    fn ipv6_request_limits_include_host_brackets() {
        for (port, body_length, header_limit) in [
            (443, 0, MAX_HEADER_BYTES),
            (8443, 0, MAX_HEADER_BYTES),
            (443, MAX_BODY_BYTES, MAX_HEADER_BYTES - 2),
            (8443, MAX_BODY_BYTES, MAX_HEADER_BYTES - 2),
        ] {
            let mut request = ValidatedRequest {
                provider_module_path: Zeroizing::new(r"C:\provider.dll".to_owned()),
                store_uri: Zeroizing::new("cng://?key=test".to_owned()),
                client_certificate_path: Zeroizing::new(r"C:\client.pem".to_owned()),
                ca_bundle_path: Zeroizing::new(r"C:\ca.pem".to_owned()),
                host: "::1".to_owned(),
                port,
                method: "POST".to_owned(),
                target: "/".to_owned(),
                headers: [
                    (
                        "Authorization",
                        format!(
                            "mtls_pop {}",
                            "t".repeat(MAX_HEADER_VALUE_BYTES - "mtls_pop ".len())
                        ),
                    ),
                    ("Accept", "a".repeat(MAX_GENERAL_HEADER_VALUE)),
                    ("Content-Type", "c".repeat(MAX_GENERAL_HEADER_VALUE)),
                    ("User-Agent", "u".repeat(MAX_GENERAL_HEADER_VALUE)),
                    ("x-ms-tokenboundauth", "true".to_owned()),
                    ("X-Request-ID", "r".to_owned()),
                ]
                .into_iter()
                .map(|(name, value)| Header {
                    name: name.to_owned(),
                    value: Zeroizing::new(value),
                })
                .collect(),
                body: Zeroizing::new(vec![b'b'; body_length]),
                connect_timeout_ms: DEFAULT_TIMEOUT_MS,
                io_timeout_ms: DEFAULT_TIMEOUT_MS,
            };
            let initial = build_http_request(&request).expect("initial IPv6 request");
            let initial_header_length =
                find_bytes(&initial, b"\r\n\r\n").expect("header delimiter") + 2;
            let padding = request.headers.last_mut().expect("padding header");
            padding
                .value
                .push_str(&"r".repeat(header_limit - initial_header_length));
            assert!(request.headers.iter().all(|header| {
                header_name_is_allowed(&header.name)
                    && header_value_is_valid(&header.name, &header.value)
            }));
            let at_limit = build_http_request(&request).expect("IPv6 request at limit");
            assert_eq!(
                find_bytes(&at_limit, b"\r\n\r\n").expect("header delimiter") + 2,
                header_limit
            );

            request
                .headers
                .last_mut()
                .expect("padding header")
                .value
                .push('r');
            match build_http_request(&request) {
                Err(error) => assert_eq!(error.code, ErrorCode::Limit),
                Ok(_) => panic!("IPv6 request one byte over its wire limit was accepted"),
            }
        }
    }

    #[test]
    fn response_framing_matches_native_contract() {
        success(
            "GET",
            "HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello",
            200,
            b"hello",
        );
        failure("GET", "HTTP/1.1 200 OK\r\nConnection: close\r\n\r\npartial");
        failure("GET", "HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\nshort");
        failure(
            "GET",
            "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
        );
        success(
            "HEAD",
            "HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\n",
            200,
            b"",
        );
        success(
            "GET",
            "HTTP/1.1 103 Early Hints\r\nContent-Length: 99\r\n\r\n",
            103,
            b"",
        );
        success(
            "GET",
            "HTTP/1.1 204 No Content\r\nContent-Length: 12\r\n\r\n",
            204,
            b"",
        );
        success(
            "GET",
            "HTTP/1.1 304 Not Modified\r\nContent-Length: 12\r\n\r\n",
            304,
            b"",
        );
        success(
            "HEAD",
            "HTTP/1.1 200 OK\r\nContent-Length: 1048577\r\n\r\n",
            200,
            b"",
        );
        success(
            "GET",
            "HTTP/1.1 304 Not Modified\r\nContent-Length: 1048577\r\n\r\n",
            304,
            b"",
        );
        failure("HEAD", "HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nbody");
    }

    #[test]
    fn rejects_duplicate_or_ambiguous_framing() {
        failure(
            "GET",
            "HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n",
        );
        failure("GET", "HTTP/1.1 200 OK\r\nContent-Length: +1\r\n\r\nx");
        assert_eq!(
            parse_http_response(b"HTTP/1.1 200 OK\r\nContent-Length: 1048577\r\n\r\n", "GET")
                .expect_err("oversized body-bearing response")
                .code,
            ErrorCode::Limit
        );
    }

    #[test]
    fn parsed_response_plaintext_uses_zeroize_on_drop_storage() {
        fn assert_zeroize_on_drop<T: zeroize::ZeroizeOnDrop>() {}
        assert_zeroize_on_drop::<Zeroizing<Vec<u8>>>();

        let parsed = parse_http_response(
            b"HTTP/1.1 200 OK\r\nX-Secret: header\r\nContent-Length: 6\r\n\r\nsecret",
            "GET",
        )
        .expect("parse response");
        let _: &Zeroizing<Vec<u8>> = &parsed.headers;
        let _: &Zeroizing<Vec<u8>> = &parsed.body;
        assert_eq!(parsed.body.as_slice(), b"secret");
    }

    #[test]
    fn form_post_preserves_correlation_without_authorization() {
        let body = b"client_id=client&grant_type=client_credentials&scope=https%3A%2F%2Fvault.azure.net%2F.default&token_type=mtls_pop";
        let headers = [
            MicrosoftIdentityNativeHeader {
                name: c"Content-Type".as_ptr(),
                value: c"application/x-www-form-urlencoded".as_ptr(),
            },
            MicrosoftIdentityNativeHeader {
                name: c"client-request-id".as_ptr(),
                value: c"token-correlation".as_ptr(),
            },
            MicrosoftIdentityNativeHeader {
                name: c"x-ms-client-request-id".as_ptr(),
                value: c"resource-correlation".as_ptr(),
            },
        ];
        let mut input = abi_request();
        input.target = c"/tenant/oauth2/v2.0/token".as_ptr();
        input.headers = headers.as_ptr();
        input.header_count = headers.len();
        input.body = body.as_ptr();
        input.body_size = body.len();
        // SAFETY: the complete ABI input and all referenced buffers remain live for validation.
        let request =
            unsafe { ValidatedRequest::from_abi(&raw const input) }.expect("bounded form POST");
        let wire = build_http_request(&request).expect("form request");
        let wire = std::str::from_utf8(&wire).expect("ASCII form");
        assert!(wire.starts_with("POST /tenant/oauth2/v2.0/token HTTP/1.1\r\n"));
        assert!(wire.contains("\r\nclient-request-id: token-correlation\r\n"));
        assert!(wire.contains("\r\nx-ms-client-request-id: resource-correlation\r\n"));
        assert!(!wire.contains("\r\nAuthorization:"));
        assert!(wire.ends_with(std::str::from_utf8(body).expect("ASCII body")));
    }

    #[test]
    fn correlation_headers_do_not_expand_other_header_or_value_permissions() {
        for name in ["CLIENT-REQUEST-ID", "X-Ms-Client-Request-Id"] {
            assert!(header_name_is_allowed(name));
            assert!(header_value_is_valid(name, "bounded-correlation"));
            assert!(!header_value_is_valid(name, "safe\r\nInjected: yes"));
            assert!(!header_value_is_valid(
                name,
                &"r".repeat(MAX_GENERAL_HEADER_VALUE + 1)
            ));
        }
        for name in ["Host", "Cookie", "X-Unrelated", "Transfer-Encoding"] {
            assert!(!header_name_is_allowed(name));
        }
        let headers = [
            MicrosoftIdentityNativeHeader {
                name: c"client-request-id".as_ptr(),
                value: c"first".as_ptr(),
            },
            MicrosoftIdentityNativeHeader {
                name: c"Client-Request-Id".as_ptr(),
                value: c"second".as_ptr(),
            },
        ];
        let mut input = abi_request();
        input.headers = headers.as_ptr();
        input.header_count = headers.len();
        // SAFETY: all ABI fields and referenced headers remain live for validation.
        let result = unsafe { ValidatedRequest::from_abi(&raw const input) };
        assert_eq!(
            result.expect_err("duplicate correlation header").code,
            ErrorCode::InvalidArgument
        );
    }

    #[test]
    fn token_bound_headers_are_narrowly_validated_and_redacted() {
        assert!(header_name_is_allowed("Authorization"));
        assert!(header_value_is_valid(
            "Authorization",
            "mtls_pop header.payload.signature"
        ));
        assert!(!header_value_is_valid(
            "Authorization",
            "Bearer header.payload.signature"
        ));
        assert!(!header_value_is_valid(
            "Authorization",
            "mtls_pop token with spaces"
        ));
        assert!(header_value_is_valid("x-ms-tokenboundauth", "true"));
        assert!(!header_value_is_valid("x-ms-tokenboundauth", "false"));

        let header = Header {
            name: "Authorization".to_owned(),
            value: Zeroizing::new("mtls_pop secret".to_owned()),
        };
        let debug = format!("{header:?}");
        assert!(debug.contains("[REDACTED]"));
        assert!(!debug.contains("secret"));
    }
}
