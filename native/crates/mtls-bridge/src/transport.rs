use std::ffi::{CString, c_void};
use std::net::IpAddr;
use std::ptr::{null, null_mut};
use std::time::{Duration, Instant};

use openssl_provider_sys::{
    SSL, SSL_ERROR_SYSCALL, SSL_ERROR_WANT_READ, SSL_ERROR_WANT_WRITE, SSL_ERROR_ZERO_RETURN,
    SSL_connect, SSL_get_error, SSL_get_verify_result, SSL_get0_param, SSL_new, SSL_read_ex,
    SSL_set_fd, SSL_set_tlsext_host_name, SSL_set1_host, SSL_shutdown, SSL_write_ex,
    X509_CHECK_FLAG_NO_PARTIAL_WILDCARDS, X509_V_OK, X509_VERIFY_PARAM_set_hostflags,
    X509_VERIFY_PARAM_set1_ip_asc,
};
use windows_sys::Win32::Foundation::{GetLastError, HANDLE, HMODULE, WAIT_OBJECT_0, WAIT_TIMEOUT};
use windows_sys::Win32::Networking::WinSock::{
    ADDRINFOEXW, AF_UNSPEC, FD_SET, FIONBIO, FreeAddrInfoExW, GetAddrInfoExCancel,
    GetAddrInfoExOverlappedResult, GetAddrInfoExW, INVALID_SOCKET, IPPROTO_TCP, NS_DNS, SO_ERROR,
    SOCK_STREAM, SOCKET, SOCKET_ERROR, SOL_SOCKET, TIMEVAL, WSA_FLAG_NO_HANDLE_INHERIT,
    WSA_INVALID_EVENT, WSA_IO_PENDING, WSACleanup, WSACloseEvent, WSACreateEvent, WSADATA,
    WSAETIMEDOUT, WSAEWOULDBLOCK, WSAGetLastError, WSASocketW, WSAStartup, closesocket, connect,
    getsockopt, ioctlsocket, select,
};
use windows_sys::Win32::System::IO::OVERLAPPED;
use windows_sys::Win32::System::LibraryLoader::{
    GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS, GET_MODULE_HANDLE_EX_FLAG_PIN, GetModuleHandleExW,
};
use windows_sys::Win32::System::Threading::{INFINITE, WaitForSingleObject};
use zeroize::Zeroizing;

use crate::abi::{BridgeFailure, ErrorCode, MAX_HEADER_BYTES, MAX_RESPONSE_BYTES};
use crate::http::{ValidatedRequest, find_bytes, inspect_http_response};
use crate::openssl::{OpenSslState, openssl_failure};

const MAX_INFORMATIONAL_RESPONSES: usize = 16;

pub struct Winsock;

impl Winsock {
    pub fn startup() -> Result<Self, BridgeFailure> {
        // SAFETY: WSADATA is plain output storage initialized by WSAStartup.
        let mut data: WSADATA = unsafe { std::mem::zeroed() };
        // SAFETY: the output pointer is valid and version 2.2 is requested.
        let result = unsafe { WSAStartup(0x0202, &raw mut data) };
        if result != 0 {
            Err(BridgeFailure::system_formatted(
                ErrorCode::Connect,
                result,
                format_args!("WSAStartup failed with error {result}."),
            ))
        } else {
            Ok(Self)
        }
    }
}

impl Drop for Winsock {
    fn drop(&mut self) {
        // SAFETY: each successful Winsock::startup owns one matching cleanup.
        unsafe {
            let _ = WSACleanup();
        }
    }
}

#[derive(Clone, Copy)]
pub struct Deadline {
    expires: Instant,
}

impl Deadline {
    pub fn new(timeout_ms: u32) -> Self {
        Self {
            expires: Instant::now() + Duration::from_millis(u64::from(timeout_ms)),
        }
    }

    fn remaining_ms(self) -> Option<u32> {
        let remaining = self.expires.checked_duration_since(Instant::now())?;
        if remaining.is_zero() {
            return None;
        }
        Some(
            remaining
                .as_millis()
                .clamp(1, u128::from(u32::MAX))
                .try_into()
                .unwrap_or(u32::MAX),
        )
    }

    fn ensure(self, code: ErrorCode, operation: &str) -> Result<(), BridgeFailure> {
        if self.remaining_ms().is_some() {
            Ok(())
        } else {
            Err(BridgeFailure::system_formatted(
                code,
                WSAETIMEDOUT,
                format_args!("{operation} exceeded the total I/O deadline."),
            ))
        }
    }
}

pub struct Socket(SOCKET);

impl Socket {
    pub const fn raw(&self) -> SOCKET {
        self.0
    }
}

impl Drop for Socket {
    fn drop(&mut self) {
        if self.0 != INVALID_SOCKET {
            // SAFETY: this wrapper owns the connected socket.
            unsafe {
                let _ = closesocket(self.0);
            }
        }
    }
}

struct DnsQuery {
    overlapped: OVERLAPPED,
    cancel_handle: HANDLE,
    hints: ADDRINFOEXW,
    addresses: *mut ADDRINFOEXW,
    host: Vec<u16>,
    port: [u16; 6],
    winsock_reference: bool,
}

// SAFETY: once submitted, the one-element query vector has a stable address and is touched only
// by the owning thread after the completion event is signaled.
unsafe impl Send for DnsQuery {}

impl Drop for DnsQuery {
    fn drop(&mut self) {
        // SAFETY: completed address results and event handles are owned by this query.
        unsafe {
            if !self.addresses.is_null() {
                FreeAddrInfoExW(self.addresses);
            }
            let event = self.overlapped.hEvent as isize;
            if event != WSA_INVALID_EVENT && event != 0 {
                let _ = WSACloseEvent(event);
            }
            if self.winsock_reference {
                let _ = WSACleanup();
            }
        }
    }
}

pub fn connect_tcp(
    request: &ValidatedRequest,
    deadline: Deadline,
) -> Result<Socket, BridgeFailure> {
    let host = try_utf16_z(&request.host)?;
    let mut port = [0_u16; 6];
    write_decimal_utf16(request.port, &mut port);
    let mut query_storage = Vec::new();
    query_storage
        .try_reserve_exact(1)
        .map_err(|_| BridgeFailure::memory())?;
    let event = {
        // SAFETY: WSACreateEvent has no preconditions after Winsock startup.
        unsafe { WSACreateEvent() }
    };
    if event == WSA_INVALID_EVENT {
        return Err(BridgeFailure::system(
            ErrorCode::Dns,
            // SAFETY: WSAGetLastError has no preconditions.
            unsafe { WSAGetLastError() },
            "Unable to create the name-resolution event.",
        ));
    }
    // Keep an independent Winsock reference with the asynchronous query so timeout cancellation
    // cleanup remains valid after the calling request releases its own Winsock reference.
    // SAFETY: WSADATA is plain output storage.
    let mut dns_winsock: WSADATA = unsafe { std::mem::zeroed() };
    // SAFETY: requests an independent Winsock 2.2 reference for the query lifetime.
    let dns_startup = unsafe { WSAStartup(0x0202, &raw mut dns_winsock) };
    if dns_startup != 0 {
        // SAFETY: the event was created above and is still owned locally.
        unsafe {
            let _ = WSACloseEvent(event);
        }
        return Err(BridgeFailure::system(
            ErrorCode::Dns,
            dns_startup,
            "Unable to retain Winsock for name resolution.",
        ));
    }
    query_storage.push(DnsQuery {
        overlapped: OVERLAPPED {
            hEvent: event as usize as HANDLE,
            ..Default::default()
        },
        cancel_handle: null_mut(),
        hints: ADDRINFOEXW {
            ai_family: i32::from(AF_UNSPEC),
            ai_socktype: SOCK_STREAM,
            ai_protocol: IPPROTO_TCP,
            ..Default::default()
        },
        addresses: null_mut(),
        host,
        port,
        winsock_reference: true,
    });
    let query = &mut query_storage[0];
    let Some(remaining) = deadline.remaining_ms() else {
        return Err(BridgeFailure::system(
            ErrorCode::Dns,
            WSAETIMEDOUT,
            "Name resolution exceeded the connection deadline.",
        ));
    };
    // SAFETY: the reserved one-element vector has a stable address; all input strings and output
    // fields remain live until completion or asynchronous cancellation cleanup.
    let mut result = unsafe {
        GetAddrInfoExW(
            query.host.as_ptr(),
            query.port.as_ptr(),
            NS_DNS,
            null(),
            &raw const query.hints,
            &raw mut query.addresses,
            null(),
            &raw const query.overlapped,
            None,
            &raw mut query.cancel_handle,
        )
    };
    if result == WSA_IO_PENDING {
        // SAFETY: the completion event belongs to this pending query.
        let wait = unsafe { WaitForSingleObject(query.overlapped.hEvent, remaining) };
        if wait == WAIT_TIMEOUT {
            // SAFETY: the cancel handle belongs to the pending lookup.
            unsafe {
                let _ = GetAddrInfoExCancel(&raw const query.cancel_handle);
            }
            retain_cancelled_query(query_storage);
            return Err(BridgeFailure::system(
                ErrorCode::Dns,
                WSAETIMEDOUT,
                "Name resolution exceeded the connection deadline.",
            ));
        }
        if wait == WAIT_OBJECT_0 {
            // SAFETY: the completion event is signaled and the overlapped structure remains live.
            result = unsafe { GetAddrInfoExOverlappedResult(&raw const query.overlapped) };
        } else {
            // SAFETY: GetLastError has no preconditions.
            let wait_error = unsafe { GetLastError() } as i32;
            // SAFETY: the cancel handle belongs to the pending lookup.
            unsafe {
                let _ = GetAddrInfoExCancel(&raw const query.cancel_handle);
            }
            retain_cancelled_query(query_storage);
            return Err(BridgeFailure::system_formatted(
                ErrorCode::Dns,
                wait_error,
                format_args!("Waiting for name resolution failed with error {wait_error}."),
            ));
        }
    }
    if deadline.remaining_ms().is_none() {
        return Err(BridgeFailure::system(
            ErrorCode::Dns,
            WSAETIMEDOUT,
            "Name resolution exceeded the connection deadline.",
        ));
    }
    if result != 0 {
        return if result == WSAETIMEDOUT {
            Err(BridgeFailure::system(
                ErrorCode::Dns,
                result,
                "Name resolution exceeded the connection deadline.",
            ))
        } else {
            Err(BridgeFailure::system_formatted(
                ErrorCode::Dns,
                result,
                format_args!("Name resolution failed with Winsock error {result}."),
            ))
        };
    }

    let mut current = query.addresses;
    let mut last_error = 0;
    while !current.is_null() {
        if deadline.remaining_ms().is_none() {
            last_error = WSAETIMEDOUT;
            break;
        }
        // SAFETY: current points into the completed linked address list owned by query.
        let address = unsafe { &*current };
        // SAFETY: address family/type/protocol came from Winsock.
        let socket_handle = unsafe {
            WSASocketW(
                address.ai_family,
                address.ai_socktype,
                address.ai_protocol,
                null(),
                0,
                WSA_FLAG_NO_HANDLE_INHERIT,
            )
        };
        if socket_handle == INVALID_SOCKET {
            // SAFETY: WSAGetLastError has no preconditions.
            last_error = unsafe { WSAGetLastError() };
            current = address.ai_next;
            continue;
        }
        let mut nonblocking = 1_u32;
        // SAFETY: socket is live and nonblocking points to writable u32 storage.
        if unsafe { ioctlsocket(socket_handle, FIONBIO, &raw mut nonblocking) } != 0 {
            // SAFETY: WSAGetLastError has no preconditions.
            last_error = unsafe { WSAGetLastError() };
            // SAFETY: socket is owned locally on this failed attempt.
            unsafe {
                let _ = closesocket(socket_handle);
            }
            current = address.ai_next;
            continue;
        }
        // SAFETY: Winsock supplied a valid socket address and length.
        let mut connect_result = unsafe {
            connect(
                socket_handle,
                address.ai_addr,
                address.ai_addrlen.min(i32::MAX as usize) as i32,
            )
        };
        if connect_result == SOCKET_ERROR
            // SAFETY: WSAGetLastError has no preconditions.
            && unsafe { WSAGetLastError() } == WSAEWOULDBLOCK
        {
            match wait_connect(socket_handle, deadline) {
                Ok(()) => connect_result = 0,
                Err(error) => last_error = error,
            }
        }
        if connect_result == 0 {
            if deadline.remaining_ms().is_some() {
                return Ok(Socket(socket_handle));
            }
            last_error = WSAETIMEDOUT;
        }
        if last_error == 0 {
            // SAFETY: WSAGetLastError has no preconditions.
            last_error = unsafe { WSAGetLastError() };
        }
        // SAFETY: socket is owned locally on this failed attempt.
        unsafe {
            let _ = closesocket(socket_handle);
        }
        current = address.ai_next;
    }
    if last_error == WSAETIMEDOUT {
        Err(BridgeFailure::system(
            ErrorCode::Connect,
            last_error,
            "The TCP connection exceeded its total deadline.",
        ))
    } else {
        Err(BridgeFailure::system_formatted(
            ErrorCode::Connect,
            last_error,
            format_args!("The TCP connection failed with Winsock error {last_error}."),
        ))
    }
}

fn retain_cancelled_query(mut query_storage: Vec<DnsQuery>) {
    if !pin_current_module_for_dns_cleanup() {
        debug_assert!(cancelled_cleanup_is_unload_safe(false, false));
        // The cancelled query remains OS-owned. Leaking it is unload-safe; running Rust cleanup
        // code from an unpinned DLL would not be.
        std::mem::forget(query_storage);
        return;
    }
    debug_assert!(cancelled_cleanup_is_unload_safe(true, true));
    let raw = query_storage.as_mut_ptr() as usize;
    let length = query_storage.len();
    let capacity = query_storage.capacity();
    std::mem::forget(query_storage);
    let spawned = std::thread::Builder::new().spawn(move || {
        // SAFETY: these parts came from the forgotten one-element query vector and this is
        // the sole reconstruction.
        let query_storage = unsafe { Vec::from_raw_parts(raw as *mut DnsQuery, length, capacity) };
        let query = &query_storage[0];
        // SAFETY: the event remains live in query until completion.
        let completed = unsafe { WaitForSingleObject(query.overlapped.hEvent, INFINITE) };
        if completed == WAIT_OBJECT_0 {
            // SAFETY: the completion event is signaled and the overlapped structure remains live.
            unsafe {
                let _ = GetAddrInfoExOverlappedResult(&raw const query.overlapped);
            }
            drop(query_storage);
        } else {
            std::mem::forget(query_storage);
        }
    });
    if spawned.is_err() {
        // The raw query and extra Winsock reference are intentionally retained rather than
        // freeing memory still referenced by an in-flight system call. The DLL is already pinned,
        // so either disposition is unload-safe.
    }
}

const fn cancelled_cleanup_is_unload_safe(module_pinned: bool, detached_thread: bool) -> bool {
    !detached_thread || module_pinned
}

fn pin_current_module_for_dns_cleanup() -> bool {
    let mut module: HMODULE = null_mut();
    // SAFETY: FROM_ADDRESS interprets this function address as an address inside the containing
    // module. PIN makes the rare timeout cleanup thread safe until process termination.
    unsafe {
        GetModuleHandleExW(
            GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
            (pin_current_module_for_dns_cleanup as *const ()).cast::<u16>(),
            &raw mut module,
        ) != 0
            && !module.is_null()
    }
}

fn wait_connect(socket_handle: SOCKET, deadline: Deadline) -> Result<(), i32> {
    let Some(remaining) = deadline.remaining_ms() else {
        return Err(WSAETIMEDOUT);
    };
    let mut write_set = fd_set(socket_handle);
    let mut error_set = fd_set(socket_handle);
    let timeout = timeval(remaining);
    // SAFETY: all fd sets and timeout storage remain live for select.
    let selected = unsafe {
        select(
            0,
            null_mut(),
            &raw mut write_set,
            &raw mut error_set,
            &raw const timeout,
        )
    };
    if deadline.remaining_ms().is_none() {
        return Err(WSAETIMEDOUT);
    }
    if selected == 0 {
        return Err(WSAETIMEDOUT);
    }
    if selected == SOCKET_ERROR || error_set.fd_count != 0 {
        return Err(socket_error(socket_handle));
    }
    if write_set.fd_count == 0 {
        return Err(WSAETIMEDOUT);
    }
    let error = socket_error(socket_handle);
    if error == 0 { Ok(()) } else { Err(error) }
}

fn socket_error(socket_handle: SOCKET) -> i32 {
    let mut error = 0_i32;
    let mut size = size_of::<i32>() as i32;
    // SAFETY: socket is live and the output pointers are writable.
    if unsafe {
        getsockopt(
            socket_handle,
            SOL_SOCKET,
            SO_ERROR,
            (&raw mut error).cast::<u8>(),
            &raw mut size,
        )
    } == 0
    {
        error
    } else {
        // SAFETY: WSAGetLastError has no preconditions.
        unsafe { WSAGetLastError() }
    }
}

fn fd_set(socket_handle: SOCKET) -> FD_SET {
    let mut fd_array = [0; 64];
    fd_array[0] = socket_handle;
    FD_SET {
        fd_count: 1,
        fd_array,
    }
}

fn timeval(milliseconds: u32) -> TIMEVAL {
    TIMEVAL {
        tv_sec: (milliseconds / 1_000) as i32,
        tv_usec: ((milliseconds % 1_000) * 1_000) as i32,
    }
}

fn try_utf16_z(value: &str) -> Result<Vec<u16>, BridgeFailure> {
    let length = value
        .encode_utf16()
        .count()
        .checked_add(1)
        .ok_or_else(BridgeFailure::memory)?;
    let mut output = Vec::new();
    output
        .try_reserve_exact(length)
        .map_err(|_| BridgeFailure::memory())?;
    output.extend(value.encode_utf16());
    output.push(0);
    Ok(output)
}

fn try_cstring(value: &str) -> Result<CString, BridgeFailure> {
    let length = value
        .len()
        .checked_add(1)
        .ok_or_else(BridgeFailure::memory)?;
    let mut bytes = Vec::new();
    bytes
        .try_reserve_exact(length)
        .map_err(|_| BridgeFailure::memory())?;
    bytes.extend_from_slice(value.as_bytes());
    bytes.push(0);
    // SAFETY: callers reject interior NUL bytes and this helper appends exactly one terminator.
    Ok(unsafe { CString::from_vec_with_nul_unchecked(bytes) })
}

fn write_decimal_utf16(mut value: u16, output: &mut [u16; 6]) {
    let mut reversed = [0_u16; 5];
    let mut length = 0;
    loop {
        reversed[length] = u16::from(b'0') + value % 10;
        length += 1;
        value /= 10;
        if value == 0 {
            break;
        }
    }
    for (destination, source) in output[..length]
        .iter_mut()
        .zip(reversed[..length].iter().rev())
    {
        *destination = *source;
    }
}

pub fn establish_tls(
    state: &mut OpenSslState,
    socket: &Socket,
    request: &ValidatedRequest,
    deadline: Deadline,
) -> Result<(), BridgeFailure> {
    // SAFETY: state owns a live SSL_CTX.
    state.ssl = unsafe { SSL_new(state.ssl_context()) };
    if state.ssl.is_null() {
        return Err(openssl_failure(
            ErrorCode::Tls,
            "Establishing the verified TLS connection",
        ));
    }
    configure_peer_name(state.ssl, &request.host)?;
    // SAFETY: SSL and socket are live. This mirrors OpenSSL's Windows socket descriptor ABI.
    if unsafe { SSL_set_fd(state.ssl, socket.raw() as i32) } != 1 {
        return Err(openssl_failure(
            ErrorCode::Tls,
            "Associating the TLS connection socket",
        ));
    }
    ssl_connect_deadline(state.ssl, socket.raw(), deadline)?;
    // SAFETY: SSL handshake completed.
    let verification = unsafe { SSL_get_verify_result(state.ssl) };
    if verification != X509_V_OK {
        return Err(BridgeFailure::formatted(
            ErrorCode::Tls,
            format_args!("The server certificate verification result was {verification}."),
        ));
    }
    Ok(())
}

fn configure_peer_name(ssl: *mut SSL, host: &str) -> Result<(), BridgeFailure> {
    if host.as_bytes().contains(&0) {
        return Err(BridgeFailure::new(
            ErrorCode::Tls,
            "The peer host contains a NUL character.",
        ));
    }
    let host = try_cstring(host)?;
    if host.to_bytes().is_empty() {
        return Err(BridgeFailure::new(
            ErrorCode::Tls,
            "The peer host is empty.",
        ));
    }
    if host
        .to_str()
        .ok()
        .and_then(|value| value.parse::<IpAddr>().ok())
        .is_some()
    {
        // SAFETY: SSL and its verification parameter are live; host is NUL-terminated.
        if unsafe { X509_VERIFY_PARAM_set1_ip_asc(SSL_get0_param(ssl), host.as_ptr()) } != 1 {
            return Err(openssl_failure(
                ErrorCode::Tls,
                "Configuring IP certificate validation",
            ));
        }
    } else {
        // SAFETY: SSL is live and host is NUL-terminated for the immediate SNI call.
        if !unsafe { SSL_set_tlsext_host_name(ssl, host.as_ptr()) } {
            return Err(openssl_failure(
                ErrorCode::Tls,
                "Configuring hostname validation",
            ));
        }
        // SAFETY: SSL and its verification parameter are live. The flag is installed before
        // SSL_set1_host so partial-label wildcards are never accepted for this connection.
        let parameter = unsafe { SSL_get0_param(ssl) };
        if parameter.is_null() {
            return Err(openssl_failure(
                ErrorCode::Tls,
                "Configuring hostname validation",
            ));
        }
        // SAFETY: parameter is the live verification parameter owned by SSL.
        unsafe {
            X509_VERIFY_PARAM_set_hostflags(parameter, X509_CHECK_FLAG_NO_PARTIAL_WILDCARDS);
        }
        // SAFETY: SSL is live and host is NUL-terminated for the immediate validation call.
        if unsafe { SSL_set1_host(ssl, host.as_ptr()) } != 1 {
            return Err(openssl_failure(
                ErrorCode::Tls,
                "Configuring hostname validation",
            ));
        }
    }
    Ok(())
}

fn ssl_connect_deadline(
    ssl: *mut SSL,
    socket: SOCKET,
    deadline: Deadline,
) -> Result<(), BridgeFailure> {
    loop {
        deadline.ensure(ErrorCode::Tls, "Establishing the verified TLS connection")?;
        // A synchronous provider/KSP operation invoked inside OpenSSL cannot be preempted. The
        // total deadline is therefore enforced immediately before and after the OpenSSL call.
        // SAFETY: SSL is live and associated with a nonblocking socket.
        let result = unsafe { SSL_connect(ssl) };
        deadline.ensure(ErrorCode::Tls, "Establishing the verified TLS connection")?;
        if result == 1 {
            return Ok(());
        }
        ssl_wait_for_retry(
            ssl,
            result,
            socket,
            deadline,
            ErrorCode::Tls,
            "Establishing the verified TLS connection",
        )?;
    }
}

pub fn ssl_write_all(
    ssl: *mut SSL,
    socket: SOCKET,
    mut data: &[u8],
    deadline: Deadline,
) -> Result<(), BridgeFailure> {
    while !data.is_empty() {
        deadline.ensure(ErrorCode::Io, "Writing the HTTPS request")?;
        let mut written = 0_usize;
        // SAFETY: SSL is live and data is readable for its full length.
        let result = unsafe {
            SSL_write_ex(
                ssl,
                data.as_ptr().cast::<c_void>(),
                data.len(),
                &raw mut written,
            )
        };
        deadline.ensure(ErrorCode::Io, "Writing the HTTPS request")?;
        if result != 1 {
            ssl_wait_for_retry(
                ssl,
                result,
                socket,
                deadline,
                ErrorCode::Io,
                "Writing the HTTPS request",
            )?;
            continue;
        }
        if written == 0 {
            return Err(BridgeFailure::new(
                ErrorCode::Io,
                "Writing the HTTPS request made no progress.",
            ));
        }
        data = &data[written..];
    }
    Ok(())
}

pub fn ssl_read_response(
    ssl: *mut SSL,
    socket: SOCKET,
    method: &str,
    deadline: Deadline,
) -> Result<Zeroizing<Vec<u8>>, BridgeFailure> {
    let mut bytes = Vec::new();
    // Reserve the full bound once so response plaintext is never copied through a reallocation
    // that cannot be zeroized after the allocator releases the old buffer.
    bytes
        .try_reserve_exact(MAX_RESPONSE_BYTES)
        .map_err(|_| BridgeFailure::memory())?;
    let mut buffer = Zeroizing::new(bytes);
    let mut progress = ResponseProgress::default();
    loop {
        if progress.is_complete(buffer.len()) {
            return Ok(buffer);
        }
        let remaining_wire_bytes = progress.remaining_wire_bytes()?;
        let mut available = (buffer.capacity() - buffer.len()).min(remaining_wire_bytes);
        if progress.framing_known {
            available = available.min(progress.expected_length - buffer.len());
        }
        let old_len = buffer.len();
        // SAFETY: spare capacity is writable; set_len occurs only for the initialized bytes
        // reported by OpenSSL.
        let destination = unsafe { buffer.as_mut_ptr().add(old_len) };
        let mut received = 0_usize;
        deadline.ensure(ErrorCode::Io, "Reading the HTTPS response")?;
        // SAFETY: SSL is live and destination has `available` writable spare bytes.
        let result = unsafe {
            SSL_read_ex(
                ssl,
                destination.cast::<c_void>(),
                available,
                &raw mut received,
            )
        };
        deadline.ensure(ErrorCode::Io, "Reading the HTTPS response")?;
        if result == 1 {
            if received == 0 {
                return Err(BridgeFailure::new(
                    ErrorCode::Io,
                    "Reading the HTTPS response made no progress.",
                ));
            }
            // SAFETY: OpenSSL initialized exactly `received` bytes in spare capacity.
            unsafe { buffer.set_len(old_len + received) };
            progress.record_received(&mut buffer, method, received)?;
            continue;
        }
        ssl_wait_for_retry(
            ssl,
            result,
            socket,
            deadline,
            ErrorCode::Io,
            "Reading the HTTPS response",
        )?;
    }
}

#[derive(Debug, Default)]
struct ResponseProgress {
    expected_length: usize,
    framing_known: bool,
    wire_bytes: usize,
    informational_responses: usize,
}

impl ResponseProgress {
    fn is_complete(&self, buffer_length: usize) -> bool {
        self.framing_known && buffer_length == self.expected_length
    }

    fn remaining_wire_bytes(&self) -> Result<usize, BridgeFailure> {
        MAX_RESPONSE_BYTES
            .checked_sub(self.wire_bytes)
            .filter(|remaining| *remaining != 0)
            .ok_or_else(|| {
                BridgeFailure::new(ErrorCode::Limit, "The HTTPS response exceeds 1 MiB.")
            })
    }

    fn record_received(
        &mut self,
        buffer: &mut Vec<u8>,
        method: &str,
        received: usize,
    ) -> Result<(), BridgeFailure> {
        self.wire_bytes = self
            .wire_bytes
            .checked_add(received)
            .filter(|total| *total <= MAX_RESPONSE_BYTES)
            .ok_or_else(|| {
                BridgeFailure::new(ErrorCode::Limit, "The HTTPS response exceeds 1 MiB.")
            })?;
        while !self.framing_known && find_bytes(buffer, b"\r\n\r\n").is_some() {
            let framing = inspect_http_response(buffer, method)?;
            if framing.status / 100 == 1 && framing.status != 101 {
                if self.informational_responses == MAX_INFORMATIONAL_RESPONSES {
                    return Err(BridgeFailure::new(
                        ErrorCode::Limit,
                        "The HTTPS response contains too many informational responses.",
                    ));
                }
                self.informational_responses += 1;
                buffer.drain(..framing.expected_length);
                continue;
            }
            let informational_bytes = self
                .wire_bytes
                .checked_sub(buffer.len())
                .expect("drained response bytes cannot exceed received bytes");
            if framing.expected_length > MAX_RESPONSE_BYTES.saturating_sub(informational_bytes) {
                return Err(BridgeFailure::new(
                    ErrorCode::Limit,
                    "The HTTPS response exceeds 1 MiB.",
                ));
            }
            self.expected_length = framing.expected_length;
            self.framing_known = true;
            if buffer.len() > self.expected_length {
                return Err(BridgeFailure::new(
                    ErrorCode::Http,
                    "The HTTP response contains bytes beyond its declared framing.",
                ));
            }
        }
        if !self.framing_known && buffer.len() > MAX_HEADER_BYTES + 3 {
            return Err(BridgeFailure::new(
                ErrorCode::Http,
                "The HTTP response header is too large.",
            ));
        }
        Ok(())
    }
}

pub fn ssl_expect_clean_end(
    ssl: *mut SSL,
    socket: SOCKET,
    deadline: Deadline,
) -> Result<(), BridgeFailure> {
    let mut unexpected = Zeroizing::new([0_u8; 1]);
    loop {
        let mut received = 0_usize;
        deadline.ensure(ErrorCode::Io, "Waiting for the TLS response shutdown")?;
        // SAFETY: SSL is live and unexpected is writable for one byte.
        let result = unsafe {
            SSL_read_ex(
                ssl,
                unexpected.as_mut_ptr().cast(),
                unexpected.len(),
                &raw mut received,
            )
        };
        deadline.ensure(ErrorCode::Io, "Waiting for the TLS response shutdown")?;
        if result == 1 {
            return Err(BridgeFailure::new(
                ErrorCode::Http,
                "The HTTP response contains bytes beyond its declared framing.",
            ));
        }
        // SAFETY: SSL is live and result came from SSL_read_ex.
        let error = unsafe { SSL_get_error(ssl, result) };
        match error {
            SSL_ERROR_ZERO_RETURN => return Ok(()),
            SSL_ERROR_WANT_READ => wait_for_socket(
                socket,
                true,
                deadline,
                ErrorCode::Io,
                "Waiting for the TLS response shutdown",
            )?,
            SSL_ERROR_WANT_WRITE => wait_for_socket(
                socket,
                false,
                deadline,
                ErrorCode::Io,
                "Waiting for the TLS response shutdown",
            )?,
            _ => {
                return ssl_wait_for_retry(
                    ssl,
                    result,
                    socket,
                    deadline,
                    ErrorCode::Io,
                    "Waiting for the TLS response shutdown",
                );
            }
        }
    }
}

pub fn ssl_shutdown_deadline(
    ssl: *mut SSL,
    socket: SOCKET,
    deadline: Deadline,
) -> Result<(), BridgeFailure> {
    let mut sent_close_notify = false;
    loop {
        deadline.ensure(ErrorCode::Io, "Shutting down the TLS connection")?;
        // SAFETY: SSL is live.
        let result = unsafe { SSL_shutdown(ssl) };
        deadline.ensure(ErrorCode::Io, "Shutting down the TLS connection")?;
        if result == 1 {
            return Ok(());
        }
        if result == 0 && !sent_close_notify {
            sent_close_notify = true;
            continue;
        }
        ssl_wait_for_retry(
            ssl,
            result,
            socket,
            deadline,
            ErrorCode::Io,
            "Shutting down the TLS connection",
        )?;
    }
}

fn ssl_wait_for_retry(
    ssl: *mut SSL,
    ssl_result: i32,
    socket: SOCKET,
    deadline: Deadline,
    code: ErrorCode,
    operation: &str,
) -> Result<(), BridgeFailure> {
    // SAFETY: SSL is live and ssl_result came from an SSL operation.
    match unsafe { SSL_get_error(ssl, ssl_result) } {
        SSL_ERROR_WANT_READ => wait_for_socket(socket, true, deadline, code, operation),
        SSL_ERROR_WANT_WRITE => wait_for_socket(socket, false, deadline, code, operation),
        SSL_ERROR_ZERO_RETURN => Err(BridgeFailure::formatted(
            code,
            format_args!("{operation} ended with a TLS close before completion."),
        )),
        SSL_ERROR_SYSCALL => {
            let system_error = if ssl_result == 0 {
                0
            } else {
                // SAFETY: WSAGetLastError has no preconditions.
                unsafe { WSAGetLastError() }
            };
            if system_error == 0 {
                Err(BridgeFailure::system_formatted(
                    code,
                    system_error,
                    format_args!("{operation} encountered an unexpected TLS EOF."),
                ))
            } else {
                Err(BridgeFailure::system_formatted(
                    code,
                    system_error,
                    format_args!("{operation} failed with Winsock error {system_error}."),
                ))
            }
        }
        _ => Err(openssl_failure(code, operation)),
    }
}

fn wait_for_socket(
    socket: SOCKET,
    read: bool,
    deadline: Deadline,
    code: ErrorCode,
    operation: &str,
) -> Result<(), BridgeFailure> {
    let Some(remaining) = deadline.remaining_ms() else {
        return Err(BridgeFailure::system_formatted(
            code,
            WSAETIMEDOUT,
            format_args!("{operation} exceeded the total I/O deadline."),
        ));
    };
    let mut read_set = fd_set(socket);
    let mut write_set = fd_set(socket);
    let mut error_set = fd_set(socket);
    let timeout = timeval(remaining);
    // SAFETY: selected fd sets and timeout remain live for the call.
    let selected = unsafe {
        select(
            0,
            if read { &raw mut read_set } else { null_mut() },
            if read { null_mut() } else { &raw mut write_set },
            &raw mut error_set,
            &raw const timeout,
        )
    };
    deadline.ensure(code, operation)?;
    if selected == 0 {
        return Err(BridgeFailure::system_formatted(
            code,
            WSAETIMEDOUT,
            format_args!("{operation} exceeded the total I/O deadline."),
        ));
    }
    if selected == SOCKET_ERROR || error_set.fd_count != 0 {
        let error = socket_error(socket);
        return Err(BridgeFailure::system_formatted(
            code,
            error,
            format_args!("{operation} failed with Winsock error {error}."),
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use openssl_provider_sys::{
        MBSTRING_ASC, OSSL_LIB_CTX, OSSL_LIB_CTX_free, OSSL_LIB_CTX_new, OSSL_PROVIDER,
        OSSL_PROVIDER_load, OSSL_PROVIDER_unload, SSL_CTX_free, SSL_CTX_new_ex, SSL_free,
        TLS_client_method, X509, X509_NAME_add_entry_by_txt, X509_VERIFY_PARAM_get_hostflags,
        X509_VERIFY_PARAM_get0_host, X509_check_host, X509_free, X509_get_subject_name,
        X509_new_ex,
    };

    struct TestSsl {
        libctx: *mut OSSL_LIB_CTX,
        provider: *mut OSSL_PROVIDER,
        context: *mut openssl_provider_sys::SSL_CTX,
        ssl: *mut SSL,
    }

    impl TestSsl {
        fn new() -> Self {
            // SAFETY: creates an owned private OpenSSL library context.
            let libctx = unsafe { OSSL_LIB_CTX_new() };
            assert!(!libctx.is_null());
            // SAFETY: the private context is live and the provider name is static.
            let provider = unsafe { OSSL_PROVIDER_load(libctx, c"default".as_ptr()) };
            assert!(!provider.is_null());
            // SAFETY: the private context and TLS method are live.
            let context = unsafe { SSL_CTX_new_ex(libctx, null(), TLS_client_method()) };
            assert!(!context.is_null());
            // SAFETY: context is live.
            let ssl = unsafe { SSL_new(context) };
            assert!(!ssl.is_null());
            Self {
                libctx,
                provider,
                context,
                ssl,
            }
        }

        fn certificate_with_common_name(&self, common_name: &str) -> *mut X509 {
            // SAFETY: creates an owned X509 associated with this private context.
            let certificate = unsafe { X509_new_ex(self.libctx, null()) };
            assert!(!certificate.is_null());
            // SAFETY: certificate is live.
            let subject = unsafe { X509_get_subject_name(certificate) };
            assert!(!subject.is_null());
            // SAFETY: subject and the ASCII common-name bytes remain live for the call.
            let added = unsafe {
                X509_NAME_add_entry_by_txt(
                    subject,
                    c"CN".as_ptr(),
                    MBSTRING_ASC,
                    common_name.as_ptr(),
                    common_name.len() as i32,
                    -1,
                    0,
                )
            };
            assert_eq!(added, 1);
            certificate
        }
    }

    impl Drop for TestSsl {
        fn drop(&mut self) {
            // SAFETY: each OpenSSL handle is owned by this test fixture.
            unsafe {
                SSL_free(self.ssl);
                SSL_CTX_free(self.context);
                let _ = OSSL_PROVIDER_unload(self.provider);
                OSSL_LIB_CTX_free(self.libctx);
            }
        }
    }

    fn check_host(certificate: *mut X509, host: &str, flags: u32) -> i32 {
        let host = CString::new(host).expect("host CString");
        // SAFETY: certificate and host are live; no peer-name allocation is requested.
        unsafe {
            X509_check_host(
                certificate,
                host.as_ptr(),
                host.as_bytes().len(),
                flags,
                null_mut(),
            )
        }
    }

    fn process_wire_response(
        wire: &[u8],
    ) -> Result<(ResponseProgress, Zeroizing<Vec<u8>>), BridgeFailure> {
        let mut buffer = Zeroizing::new(wire.to_vec());
        let mut progress = ResponseProgress::default();
        progress.record_received(&mut buffer, "GET", wire.len())?;
        Ok((progress, buffer))
    }

    #[test]
    fn detached_dns_cleanup_requires_a_pinned_module() {
        assert!(cancelled_cleanup_is_unload_safe(false, false));
        assert!(cancelled_cleanup_is_unload_safe(true, false));
        assert!(cancelled_cleanup_is_unload_safe(true, true));
        assert!(!cancelled_cleanup_is_unload_safe(false, true));
        assert!(pin_current_module_for_dns_cleanup());
    }

    #[test]
    fn decimal_port_encoding_is_nul_terminated() {
        let mut output = [0_u16; 6];
        write_decimal_utf16(65_535, &mut output);
        assert_eq!(
            &output,
            &[
                u16::from(b'6'),
                u16::from(b'5'),
                u16::from(b'5'),
                u16::from(b'3'),
                u16::from(b'5'),
                0
            ]
        );
    }

    #[test]
    fn dns_peer_names_disable_partial_wildcards_but_keep_openssl_label_policy() {
        let tls = TestSsl::new();
        configure_peer_name(tls.ssl, "www.example.com").expect("configure DNS peer name");
        // SAFETY: SSL owns a live verification parameter.
        let parameter = unsafe { SSL_get0_param(tls.ssl) };
        assert!(!parameter.is_null());
        // SAFETY: parameter is live.
        let flags = unsafe { X509_VERIFY_PARAM_get_hostflags(parameter) };
        assert_ne!(flags & X509_CHECK_FLAG_NO_PARTIAL_WILDCARDS, 0);

        let partial = tls.certificate_with_common_name("w*.example.com");
        assert_eq!(check_host(partial, "www.example.com", 0), 1);
        assert_eq!(check_host(partial, "www.example.com", flags), 0);
        // SAFETY: certificate is owned by this test.
        unsafe { X509_free(partial) };

        let full_label = tls.certificate_with_common_name("*.example.com");
        assert_eq!(check_host(full_label, "www.example.com", flags), 1);
        assert_eq!(check_host(full_label, "deep.www.example.com", flags), 0);
        // SAFETY: certificate is owned by this test.
        unsafe { X509_free(full_label) };
    }

    #[test]
    fn ipv4_peer_names_keep_binary_ip_verification() {
        let tls = TestSsl::new();
        configure_peer_name(tls.ssl, "127.0.0.1").expect("configure IPv4 peer name");
        // SAFETY: SSL owns a live verification parameter.
        let parameter = unsafe { SSL_get0_param(tls.ssl) };
        assert!(!parameter.is_null());
        // SAFETY: parameter is live. IP verification must not be converted into a DNS host entry.
        assert!(unsafe { X509_VERIFY_PARAM_get0_host(parameter, 0) }.is_null());
        // SAFETY: parameter is live.
        let flags = unsafe { X509_VERIFY_PARAM_get_hostflags(parameter) };
        assert_eq!(flags & X509_CHECK_FLAG_NO_PARTIAL_WILDCARDS, 0);
    }

    #[test]
    fn informational_response_count_is_bounded() {
        let mut wire = Vec::new();
        for _ in 0..=MAX_INFORMATIONAL_RESPONSES {
            wire.extend_from_slice(b"HTTP/1.1 100 Continue\r\n\r\n");
        }
        wire.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n");
        assert_eq!(
            process_wire_response(&wire)
                .expect_err("too many informational responses")
                .code,
            ErrorCode::Limit
        );
    }

    #[test]
    fn informational_response_bytes_count_toward_the_total_limit() {
        let mut wire = Vec::new();
        let padding = vec![b'a'; 16_000];
        for _ in 0..MAX_INFORMATIONAL_RESPONSES {
            wire.extend_from_slice(b"HTTP/1.1 100 Continue\r\nX-Pad: ");
            wire.extend_from_slice(&padding);
            wire.extend_from_slice(b"\r\n\r\n");
        }
        wire.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Length: 900000\r\n\r\n");
        assert_eq!(
            process_wire_response(&wire)
                .expect_err("cumulative informational response bytes")
                .code,
            ErrorCode::Limit
        );
    }

    #[test]
    fn cumulative_response_limit_accepts_the_exact_boundary() {
        let informational = b"HTTP/1.1 100 Continue\r\n\r\n";
        let mut body_length = MAX_RESPONSE_BYTES;
        let final_header = loop {
            let header =
                format!("HTTP/1.1 200 OK\r\nContent-Length: {body_length}\r\n\r\n").into_bytes();
            let adjusted = MAX_RESPONSE_BYTES - informational.len() - header.len();
            if adjusted == body_length {
                break header;
            }
            body_length = adjusted;
        };
        let mut wire = Vec::with_capacity(MAX_RESPONSE_BYTES);
        wire.extend_from_slice(informational);
        wire.extend_from_slice(&final_header);
        wire.resize(MAX_RESPONSE_BYTES, b'x');

        let (progress, final_response) =
            process_wire_response(&wire).expect("exact cumulative boundary");
        assert!(progress.is_complete(final_response.len()));
        assert_eq!(progress.wire_bytes, MAX_RESPONSE_BYTES);
        assert_eq!(
            crate::http::parse_http_response(&final_response, "GET")
                .expect("final boundary response")
                .body
                .len(),
            body_length
        );
    }

    #[test]
    fn one_continue_then_final_response_is_returned_normally() {
        let wire = b"HTTP/1.1 100 Continue\r\n\r\n\
HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello";
        let (progress, final_response) =
            process_wire_response(wire).expect("normal informational response");
        assert!(progress.is_complete(final_response.len()));
        assert_eq!(progress.informational_responses, 1);
        let parsed =
            crate::http::parse_http_response(&final_response, "GET").expect("final response");
        assert_eq!(parsed.status, 200);
        assert_eq!(parsed.body.as_slice(), b"hello");
    }
}
