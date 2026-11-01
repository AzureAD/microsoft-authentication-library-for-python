#![cfg_attr(not(windows), allow(dead_code))]

#[cfg(not(windows))]
compile_error!("cng-core requires Windows");

use std::alloc::{Layout, alloc, dealloc};
use std::ffi::c_void;
use std::fmt::{self, Write};
use std::marker::PhantomData;
use std::ops::Deref;
use std::ptr::NonNull;
use std::ptr::{null, null_mut};
use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering, fence};

use windows_sys::Win32::Foundation::{
    CloseHandle, ERROR_ALREADY_EXISTS, ERROR_FILE_NOT_FOUND, ERROR_INSUFFICIENT_BUFFER,
    ERROR_PATH_NOT_FOUND, GetLastError, HANDLE, NTE_BAD_KEYSET, NTE_EXISTS, NTE_NO_MORE_ITEMS,
    NTE_NOT_FOUND, WAIT_ABANDONED, WAIT_OBJECT_0, WAIT_TIMEOUT,
};
use windows_sys::Win32::Globalization::{LCMAP_UPPERCASE, LCMapStringEx, LOCALE_NAME_INVARIANT};
use windows_sys::Win32::Security::Cryptography::{
    BCRYPT_ALG_HANDLE, BCRYPT_HASH_HANDLE, BCRYPT_HASH_LENGTH, BCRYPT_OBJECT_LENGTH,
    BCRYPT_PKCS1_PADDING_INFO, BCRYPT_PSS_PADDING_INFO, BCRYPT_RSAPRIVATE_BLOB,
    BCRYPT_RSAPUBLIC_BLOB, BCRYPT_RSAPUBLIC_MAGIC, BCRYPT_SHA256_ALGORITHM,
    BCRYPT_SHA384_ALGORITHM, BCRYPT_SHA512_ALGORITHM, BCRYPT_USE_SYSTEM_PREFERRED_RNG,
    BCryptCloseAlgorithmProvider, BCryptCreateHash, BCryptDestroyHash, BCryptDuplicateHash,
    BCryptFinishHash, BCryptGenRandom, BCryptGetProperty, BCryptHashData,
    BCryptOpenAlgorithmProvider, NCRYPT_ALLOW_ALL_USAGES, NCRYPT_ALLOW_SIGNING_FLAG,
    NCRYPT_EXPORT_POLICY_PROPERTY, NCRYPT_KEY_HANDLE, NCRYPT_KEY_USAGE_PROPERTY,
    NCRYPT_LENGTH_PROPERTY, NCRYPT_PAD_PKCS1_FLAG, NCRYPT_PAD_PSS_FLAG, NCRYPT_PROV_HANDLE,
    NCRYPT_RSA_ALGORITHM, NCRYPT_SILENT_FLAG, NCryptCreatePersistedKey, NCryptDeleteKey,
    NCryptEnumKeys, NCryptExportKey, NCryptFinalizeKey, NCryptFreeBuffer, NCryptFreeObject,
    NCryptGetProperty, NCryptKeyName, NCryptOpenKey, NCryptOpenStorageProvider, NCryptSetProperty,
    NCryptSignHash,
};
use windows_sys::Win32::Security::{GetTokenInformation, TOKEN_QUERY, TOKEN_USER, TokenUser};
use windows_sys::Win32::System::Threading::{
    AddSIDToBoundaryDescriptor, ClosePrivateNamespace, CreateBoundaryDescriptorW, CreateMutexW,
    CreatePrivateNamespaceW, DeleteBoundaryDescriptor, GetCurrentProcess, OpenPrivateNamespaceW,
    OpenProcessToken, ReleaseMutex, WaitForSingleObject,
};
use windows_sys::core::PCWSTR;
use zeroize::{Zeroize, Zeroizing};

pub const DEFAULT_PROVIDER_NAME: &str = "Microsoft Software Key Storage Provider";
pub const TEST_KEY_PREFIXES: &[&str] = &[
    "openssl-cng-provider-test-",
    "openssl-cng-integration-",
    "openssl-cng-rust-test-",
];
const CREATE_RACE_OPEN_ATTEMPTS: u32 = 128;
const NCRYPT_ALLOW_KEY_ATTESTATION_FLAG: u32 = 0x0000_0010;
const MIN_RSA_BITS: u32 = 2_048;
const MAX_RSA_BITS: u32 = 16_384;
const MAX_PUBLIC_BLOB_BYTES: u32 = 4_096;
const CREATE_RACE_MAX_DELAY_MS: u64 = 25;
const KEY_CREATE_MUTEX_WAIT_MS: u32 = 10_000;
const KEY_CREATE_MUTEX_PREFIX: &[u8] = b"openssl-cng-provider-key-create-";
const KEY_CREATE_MUTEX_NAME_LEN: usize = KEY_CREATE_MUTEX_PREFIX.len() + 64 + 1;
const KEY_CREATE_MUTEX_HASH_CHUNK_UNITS: usize = 64;
const KEY_CREATE_NAMESPACE_ALIAS: &str = "openssl-cng-provider-v1";
const KEY_CREATE_NAMESPACE_BOUNDARY: &str = "openssl-cng-provider-user-boundary-v1";
const KEY_CREATE_NAMESPACE_OPEN_ATTEMPTS: u32 = 8;
static KEY_CREATE_NAMESPACE: Mutex<KeyCreationNamespaceState> =
    Mutex::new(KeyCreationNamespaceState {
        handle: 0,
        references: 0,
    });

const ERROR_DETAIL_LENGTH: usize = 80;

#[derive(Clone, Eq, PartialEq)]
struct ErrorDetail {
    bytes: [u8; ERROR_DETAIL_LENGTH],
    length: usize,
}

impl ErrorDetail {
    const fn new() -> Self {
        Self {
            bytes: [0; ERROR_DETAIL_LENGTH],
            length: 0,
        }
    }

    fn as_str(&self) -> &str {
        // SAFETY: `write_str` copies only complete UTF-8 characters from valid Rust strings.
        unsafe { std::str::from_utf8_unchecked(&self.bytes[..self.length]) }
    }
}

impl Write for ErrorDetail {
    fn write_str(&mut self, value: &str) -> fmt::Result {
        for character in value.chars() {
            let mut encoded = [0_u8; 4];
            let encoded = character.encode_utf8(&mut encoded).as_bytes();
            let Some(end) = self.length.checked_add(encoded.len()) else {
                break;
            };
            if end > self.bytes.len() {
                break;
            }
            self.bytes[self.length..end].copy_from_slice(encoded);
            self.length = end;
        }
        Ok(())
    }
}

#[derive(Clone, Eq, PartialEq)]
pub struct CngError {
    operation: &'static str,
    status: i32,
    detail: ErrorDetail,
    memory_failure: bool,
}

impl fmt::Debug for CngError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let detail = (self.detail.length != 0).then(|| self.detail.as_str());
        formatter
            .debug_struct("CngError")
            .field("operation", &self.operation)
            .field("status", &self.status)
            .field("detail", &detail)
            .field("memory_failure", &self.memory_failure)
            .finish()
    }
}

impl CngError {
    #[must_use]
    pub const fn status(&self) -> i32 {
        self.status
    }

    fn from_status(operation: &'static str, status: i32) -> Self {
        Self {
            operation,
            status,
            detail: ErrorDetail::new(),
            memory_failure: false,
        }
    }

    fn detail(operation: &'static str, detail: impl fmt::Display) -> Self {
        let mut buffer = ErrorDetail::new();
        let _ = write!(&mut buffer, "{detail}");
        Self {
            operation,
            status: 0,
            detail: buffer,
            memory_failure: false,
        }
    }

    fn with_operation(mut self, operation: &'static str) -> Self {
        self.operation = operation;
        self
    }

    #[must_use]
    pub const fn memory_failure(operation: &'static str) -> Self {
        Self {
            operation,
            status: 0,
            detail: ErrorDetail::new(),
            memory_failure: true,
        }
    }

    #[must_use]
    pub const fn is_memory_failure(&self) -> bool {
        self.memory_failure
    }
}

impl fmt::Display for CngError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.memory_failure {
            write!(formatter, "{}: memory allocation failed", self.operation)
        } else if self.detail.length != 0 {
            write!(formatter, "{}: {}", self.operation, self.detail.as_str())
        } else {
            write!(
                formatter,
                "{} failed with status 0x{:08X}",
                self.operation, self.status as u32
            )
        }
    }
}

impl std::error::Error for CngError {}

struct Shared<T> {
    references: AtomicUsize,
    value: T,
}

struct FallibleArc<T> {
    pointer: NonNull<Shared<T>>,
    marker: PhantomData<Shared<T>>,
}

impl<T> FallibleArc<T> {
    fn try_new(value: T) -> Result<Self, T> {
        let layout = Layout::new::<Shared<T>>();
        // SAFETY: `layout` describes one `Shared<T>` and a null allocation is handled below.
        let pointer = unsafe { alloc(layout).cast::<Shared<T>>() };
        let Some(pointer) = NonNull::new(pointer) else {
            return Err(value);
        };
        // SAFETY: `pointer` is aligned and allocated for one `Shared<T>`.
        unsafe {
            pointer.as_ptr().write(Shared {
                references: AtomicUsize::new(1),
                value,
            });
        }
        Ok(Self {
            pointer,
            marker: PhantomData,
        })
    }
}

impl<T> Clone for FallibleArc<T> {
    fn clone(&self) -> Self {
        // SAFETY: a live `FallibleArc` keeps the shared allocation initialized.
        let references = unsafe { &self.pointer.as_ref().references };
        let previous = references.fetch_add(1, Ordering::Relaxed);
        if previous > isize::MAX as usize {
            std::process::abort();
        }
        Self {
            pointer: self.pointer,
            marker: PhantomData,
        }
    }
}

impl<T> Deref for FallibleArc<T> {
    type Target = T;

    fn deref(&self) -> &Self::Target {
        // SAFETY: every live `FallibleArc` owns one reference to the initialized allocation.
        unsafe { &self.pointer.as_ref().value }
    }
}

impl<T> Drop for FallibleArc<T> {
    fn drop(&mut self) {
        // SAFETY: a live `FallibleArc` keeps the shared allocation initialized.
        let references = unsafe { &self.pointer.as_ref().references };
        if references.fetch_sub(1, Ordering::Release) != 1 {
            return;
        }
        fence(Ordering::Acquire);
        // SAFETY: this was the final reference, so no other thread can access the value. The
        // allocation uses the same layout passed to `alloc`.
        unsafe {
            std::ptr::drop_in_place(self.pointer.as_ptr());
            dealloc(self.pointer.as_ptr().cast(), Layout::new::<Shared<T>>());
        }
    }
}

// SAFETY: shared immutable access requires `T: Sync`, and final destruction may occur on any
// thread only when `T: Send`.
unsafe impl<T: Send + Sync> Send for FallibleArc<T> {}
// SAFETY: shared immutable access is safe when `T: Sync`; ownership can cross threads when
// `T: Send`.
unsafe impl<T: Send + Sync> Sync for FallibleArc<T> {}

fn try_zeroed_bytes(
    length: usize,
    operation: &'static str,
) -> Result<Zeroizing<Vec<u8>>, CngError> {
    let mut bytes = Vec::new();
    bytes
        .try_reserve_exact(length)
        .map_err(|_| CngError::memory_failure(operation))?;
    bytes.resize(length, 0);
    Ok(Zeroizing::new(bytes))
}

fn try_copy_bytes(value: &[u8], operation: &'static str) -> Result<Vec<u8>, CngError> {
    let mut copy = Vec::new();
    copy.try_reserve_exact(value.len())
        .map_err(|_| CngError::memory_failure(operation))?;
    copy.extend_from_slice(value);
    Ok(copy)
}

fn try_copy_str(value: &str, operation: &'static str) -> Result<String, CngError> {
    let mut copy = String::new();
    copy.try_reserve_exact(value.len())
        .map_err(|_| CngError::memory_failure(operation))?;
    copy.push_str(value);
    Ok(copy)
}

fn wide(value: &str, operation: &'static str) -> Result<Vec<u16>, CngError> {
    if value.is_empty() || value.contains('\0') {
        return Err(CngError::detail(
            operation,
            "a non-empty name without NUL characters is required",
        ));
    }
    let length = value
        .encode_utf16()
        .count()
        .checked_add(1)
        .ok_or_else(|| CngError::memory_failure(operation))?;
    let mut wide = Vec::new();
    wide.try_reserve_exact(length)
        .map_err(|_| CngError::memory_failure(operation))?;
    wide.extend(value.encode_utf16());
    wide.push(0);
    Ok(wide)
}

fn security_bits(bits: u32) -> u32 {
    match bits {
        15_360.. => 256,
        7_680.. => 192,
        3_072.. => 128,
        2_048.. => 112,
        1_024.. => 80,
        _ => 0,
    }
}

fn missing_key(status: i32) -> bool {
    status == NTE_BAD_KEYSET || status == NTE_NOT_FOUND
}

struct KeyCreationNamespaceState {
    handle: usize,
    references: usize,
}

struct KeyCreationNamespace;

impl KeyCreationNamespace {
    fn open_handle() -> Result<HANDLE, CngError> {
        let boundary_name = wide(
            KEY_CREATE_NAMESPACE_BOUNDARY,
            "CNG key creation private namespace",
        )?;
        let alias = wide(
            KEY_CREATE_NAMESPACE_ALIAS,
            "CNG key creation private namespace",
        )?;
        // SAFETY: the boundary name is live and NUL-terminated.
        let mut boundary = unsafe { CreateBoundaryDescriptorW(boundary_name.as_ptr(), 0) };
        if boundary.is_null() {
            // SAFETY: GetLastError immediately follows the failed boundary creation.
            return Err(CngError::from_status(
                "CreateBoundaryDescriptorW(CNG key creation)",
                unsafe { GetLastError() } as i32,
            ));
        }
        let result = (|| {
            add_current_user_boundary_sid(&mut boundary)?;
            for attempt in 0..KEY_CREATE_NAMESPACE_OPEN_ATTEMPTS {
                // SAFETY: boundary and alias are live; null attributes request the token's
                // default security descriptor within the current-user boundary.
                let namespace =
                    unsafe { CreatePrivateNamespaceW(null(), boundary.cast(), alias.as_ptr()) };
                if !namespace.is_null() {
                    return Ok(namespace);
                }
                // SAFETY: GetLastError immediately follows CreatePrivateNamespaceW.
                let status = unsafe { GetLastError() };
                if status != ERROR_ALREADY_EXISTS {
                    return Err(CngError::from_status(
                        "CreatePrivateNamespaceW(CNG key creation)",
                        status as i32,
                    ));
                }
                // SAFETY: boundary and alias are the same current-user namespace identity.
                let namespace = unsafe { OpenPrivateNamespaceW(boundary.cast(), alias.as_ptr()) };
                if !namespace.is_null() {
                    return Ok(namespace);
                }
                // SAFETY: GetLastError immediately follows OpenPrivateNamespaceW.
                let status = unsafe { GetLastError() };
                if !matches!(status, ERROR_FILE_NOT_FOUND | ERROR_PATH_NOT_FOUND)
                    || attempt + 1 == KEY_CREATE_NAMESPACE_OPEN_ATTEMPTS
                {
                    return Err(CngError::from_status(
                        "OpenPrivateNamespaceW(CNG key creation)",
                        status as i32,
                    ));
                }
                std::thread::yield_now();
            }
            unreachable!("private namespace retry loop always returns")
        })();
        // SAFETY: boundary is owned by this function and no longer needed after namespace open.
        unsafe { DeleteBoundaryDescriptor(boundary) };
        result
    }

    fn acquire() -> Result<Self, CngError> {
        let mut state = KEY_CREATE_NAMESPACE
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let references = state.references.checked_add(1).ok_or_else(|| {
            CngError::detail(
                "CNG key creation private namespace",
                "too many concurrent namespace users",
            )
        })?;
        if state.references == 0 {
            if state.handle != 0 {
                return Err(CngError::detail(
                    "CNG key creation private namespace",
                    "the process-local namespace state is inconsistent",
                ));
            }
            state.handle = Self::open_handle()? as usize;
        } else if state.handle == 0 {
            return Err(CngError::detail(
                "CNG key creation private namespace",
                "the process-local namespace handle is unavailable",
            ));
        }
        state.references = references;
        Ok(Self)
    }
}

impl Drop for KeyCreationNamespace {
    fn drop(&mut self) {
        let mut state = KEY_CREATE_NAMESPACE
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if state.references == 0 {
            return;
        }
        state.references -= 1;
        if state.references == 0 {
            let handle = std::mem::take(&mut state.handle);
            if handle != 0 {
                // Keep the manager locked until the alias is unregistered so another local
                // acquisition cannot race the final close and fail with ERROR_DUP_NAME.
                // SAFETY: the final process-local namespace lease owns this handle. Flags zero
                // close only this handle and do not destroy the namespace while another process
                // uses it.
                let _ = unsafe { ClosePrivateNamespace(handle as HANDLE, 0) };
            }
        }
    }
}

fn add_current_user_boundary_sid(boundary: &mut HANDLE) -> Result<(), CngError> {
    let mut token = null_mut();
    // SAFETY: current process pseudo-handle is valid and token output storage is writable.
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw mut token) } == 0 {
        // SAFETY: GetLastError immediately follows OpenProcessToken.
        return Err(CngError::from_status(
            "OpenProcessToken(CNG key creation)",
            unsafe { GetLastError() } as i32,
        ));
    }
    let result = (|| {
        let mut length = 0_u32;
        // SAFETY: the first call intentionally requests the required TokenUser buffer length.
        let sizing =
            unsafe { GetTokenInformation(token, TokenUser, null_mut(), 0, &raw mut length) };
        // SAFETY: GetLastError immediately follows the sizing query when it fails.
        let sizing_error = unsafe { GetLastError() };
        if sizing != 0 || sizing_error != ERROR_INSUFFICIENT_BUFFER || length == 0 {
            return Err(CngError::from_status(
                "GetTokenInformation(CNG key creation size)",
                sizing_error as i32,
            ));
        }
        let length = usize::try_from(length)
            .map_err(|_| CngError::memory_failure("CNG key creation token information"))?;
        let units = length
            .checked_add(size_of::<usize>() - 1)
            .map(|value| value / size_of::<usize>())
            .ok_or_else(|| CngError::memory_failure("CNG key creation token information"))?;
        let mut information = Vec::<usize>::new();
        information
            .try_reserve_exact(units)
            .map_err(|_| CngError::memory_failure("CNG key creation token information"))?;
        information.resize(units, 0);
        let mut written = 0_u32;
        // SAFETY: the aligned buffer is writable for at least `length` bytes.
        if unsafe {
            GetTokenInformation(
                token,
                TokenUser,
                information.as_mut_ptr().cast(),
                u32::try_from(length).unwrap_or(u32::MAX),
                &raw mut written,
            )
        } == 0
        {
            // SAFETY: GetLastError immediately follows GetTokenInformation.
            return Err(CngError::from_status(
                "GetTokenInformation(CNG key creation)",
                unsafe { GetLastError() } as i32,
            ));
        }
        if usize::try_from(written).ok() != Some(length) {
            return Err(CngError::detail(
                "GetTokenInformation(CNG key creation)",
                "Windows returned an inconsistent TokenUser length",
            ));
        }
        // SAFETY: successful TokenUser output starts with an aligned TOKEN_USER structure.
        let user = unsafe { &*information.as_ptr().cast::<TOKEN_USER>() };
        // SAFETY: boundary is live and TokenUser contains a live SID in `information`.
        if unsafe { AddSIDToBoundaryDescriptor(boundary, user.User.Sid) } == 0 {
            // SAFETY: GetLastError immediately follows AddSIDToBoundaryDescriptor.
            return Err(CngError::from_status(
                "AddSIDToBoundaryDescriptor(CNG key creation)",
                unsafe { GetLastError() } as i32,
            ));
        }
        Ok(())
    })();
    // SAFETY: token is owned by this function.
    unsafe {
        let _ = CloseHandle(token);
    }
    result
}

fn private_mutex_name(name: &[u16]) -> Result<Vec<u16>, CngError> {
    let alias = wide(
        KEY_CREATE_NAMESPACE_ALIAS,
        "CNG key creation private mutex name",
    )?;
    let length = alias
        .len()
        .checked_add(name.len())
        .ok_or_else(|| CngError::memory_failure("CNG key creation private mutex name"))?;
    let mut qualified = Vec::new();
    qualified
        .try_reserve_exact(length)
        .map_err(|_| CngError::memory_failure("CNG key creation private mutex name"))?;
    qualified.extend_from_slice(&alias[..alias.len() - 1]);
    qualified.push(u16::from(b'\\'));
    qualified.extend_from_slice(name);
    Ok(qualified)
}

struct KeyCreationMutexGuard {
    handle: HANDLE,
    acquired: bool,
    namespace: Option<KeyCreationNamespace>,
}

impl KeyCreationMutexGuard {
    fn acquire(name: &[u16]) -> Result<Self, CngError> {
        let namespace = KeyCreationNamespace::acquire()?;
        let qualified_name = private_mutex_name(name)?;
        // SAFETY: the qualified name is live and NUL-terminated; private namespace security is
        // constrained by the current-user boundary.
        let handle = unsafe { CreateMutexW(null(), 0, qualified_name.as_ptr()) };
        if handle.is_null() {
            // SAFETY: GetLastError has no preconditions and immediately follows CreateMutexW.
            return Err(CngError::from_status(
                "CreateMutexW(CNG key creation)",
                unsafe { GetLastError() } as i32,
            ));
        }

        let mut guard = Self {
            handle,
            acquired: false,
            namespace: Some(namespace),
        };
        // SAFETY: `handle` is a live mutex handle owned by `guard`.
        let wait = unsafe { WaitForSingleObject(handle, KEY_CREATE_MUTEX_WAIT_MS) };
        match wait {
            WAIT_OBJECT_0 | WAIT_ABANDONED => {
                guard.acquired = true;
                Ok(guard)
            }
            WAIT_TIMEOUT => Err(CngError::detail(
                "CNG key creation mutex wait",
                format_args!("timed out after {KEY_CREATE_MUTEX_WAIT_MS} ms"),
            )),
            _ => {
                // SAFETY: GetLastError has no preconditions and immediately follows a failed wait.
                Err(CngError::from_status(
                    "WaitForSingleObject(CNG key creation mutex)",
                    unsafe { GetLastError() } as i32,
                ))
            }
        }
    }
}

impl Drop for KeyCreationMutexGuard {
    fn drop(&mut self) {
        if self.acquired {
            // SAFETY: this process owns the mutex after a successful or abandoned wait.
            let _ = unsafe { ReleaseMutex(self.handle) };
        }
        if !self.handle.is_null() {
            // SAFETY: `handle` is owned by this guard and closed exactly once.
            let _ = unsafe { CloseHandle(self.handle) };
            self.handle = null_mut();
        }
        drop(self.namespace.take());
    }
}

fn windows_ordinal_uppercase(value: &[u16]) -> Result<Vec<u16>, CngError> {
    let source_length = i32::try_from(value.len()).map_err(|_| {
        CngError::detail(
            "CNG key creation mutex name",
            "a provider or key name exceeds the Windows casing limit",
        )
    })?;
    // SAFETY: the nonempty source slice is readable for `source_length` UTF-16 code units. The
    // null destination with zero length requests the exact output size.
    let required = unsafe {
        LCMapStringEx(
            LOCALE_NAME_INVARIANT,
            LCMAP_UPPERCASE,
            value.as_ptr(),
            source_length,
            null_mut(),
            0,
            null(),
            null(),
            0,
        )
    };
    if required == 0 {
        // SAFETY: GetLastError has no preconditions and immediately follows LCMapStringEx.
        return Err(CngError::from_status(
            "CNG key creation mutex name",
            unsafe { GetLastError() } as i32,
        ));
    }
    let required = usize::try_from(required).map_err(|_| {
        CngError::detail(
            "CNG key creation mutex name",
            "Windows returned an invalid uppercase mapping length",
        )
    })?;
    let mut uppercase = Vec::new();
    uppercase
        .try_reserve_exact(required)
        .map_err(|_| CngError::memory_failure("CNG key creation mutex name"))?;
    uppercase.resize(required, 0);
    // SAFETY: the source remains readable and `uppercase` is writable for exactly `required`
    // UTF-16 code units, as returned by the sizing call above.
    let written = unsafe {
        LCMapStringEx(
            LOCALE_NAME_INVARIANT,
            LCMAP_UPPERCASE,
            value.as_ptr(),
            source_length,
            uppercase.as_mut_ptr(),
            i32::try_from(required).map_err(|_| {
                CngError::detail(
                    "CNG key creation mutex name",
                    "Windows returned an invalid uppercase mapping length",
                )
            })?,
            null(),
            null(),
            0,
        )
    };
    if written == 0 {
        // SAFETY: GetLastError has no preconditions and immediately follows LCMapStringEx.
        return Err(CngError::from_status(
            "CNG key creation mutex name",
            unsafe { GetLastError() } as i32,
        ));
    }
    if usize::try_from(written).ok() != Some(required) {
        return Err(CngError::detail(
            "CNG key creation mutex name",
            "Windows changed the uppercase mapping length between calls",
        ));
    }
    Ok(uppercase)
}

fn hash_mutex_name_field(
    hash: &mut HashState,
    field: &[u8],
    value: &[u16],
) -> Result<(), CngError> {
    let uppercase = windows_ordinal_uppercase(value)?;
    let length = u64::try_from(uppercase.len()).map_err(|_| {
        CngError::detail(
            "CNG key creation mutex name",
            "a canonical provider or key name is too long",
        )
    })?;
    hash.update(field)?;
    hash.update(&length.to_le_bytes())?;

    let mut encoded = [0_u8; KEY_CREATE_MUTEX_HASH_CHUNK_UNITS * 2];
    for chunk in uppercase.chunks(KEY_CREATE_MUTEX_HASH_CHUNK_UNITS) {
        for (target, unit) in encoded.chunks_exact_mut(2).zip(chunk.iter().copied()) {
            target.copy_from_slice(&unit.to_le_bytes());
        }
        hash.update(&encoded[..chunk.len() * 2])?;
    }
    Ok(())
}

fn key_creation_mutex_name(
    provider_name: &[u16],
    key_name: &[u16],
) -> Result<[u16; KEY_CREATE_MUTEX_NAME_LEN], CngError> {
    let digest = (|| {
        let mut hash = HashState::new(Digest::Sha256)?;
        hash.update(b"openssl-cng-provider:key-create-mutex:v2\0")?;
        hash_mutex_name_field(&mut hash, b"provider\0", provider_name)?;
        hash_mutex_name_field(&mut hash, b"key\0", key_name)?;
        hash.finish_clone()
    })()
    .map_err(|error| error.with_operation("CNG key creation mutex name"))?;

    let mut name = [0_u16; KEY_CREATE_MUTEX_NAME_LEN];
    for (target, value) in name.iter_mut().zip(KEY_CREATE_MUTEX_PREFIX.iter().copied()) {
        *target = u16::from(value);
    }
    let mut offset = KEY_CREATE_MUTEX_PREFIX.len();
    for value in digest.iter().copied() {
        const HEX: &[u8; 16] = b"0123456789abcdef";
        name[offset] = u16::from(HEX[usize::from(value >> 4)]);
        name[offset + 1] = u16::from(HEX[usize::from(value & 0x0f)]);
        offset += 2;
    }
    debug_assert_eq!(offset + 1, name.len());
    Ok(name)
}

fn read_u32_property(
    key: NCRYPT_KEY_HANDLE,
    property: PCWSTR,
    operation: &'static str,
) -> Result<u32, CngError> {
    let mut value = 0_u32;
    let mut written = 0_u32;
    // SAFETY: `value` is writable for four bytes, `written` is valid, and `key` is held alive
    // by the caller for the duration of the property query.
    let status = unsafe {
        NCryptGetProperty(
            key,
            property,
            (&raw mut value).cast(),
            size_of::<u32>() as u32,
            &raw mut written,
            0,
        )
    };
    if status != 0 {
        return Err(CngError::from_status(operation, status));
    }
    if written as usize != size_of::<u32>() {
        return Err(CngError::detail(
            operation,
            "CNG returned an unexpected property size",
        ));
    }
    Ok(value)
}

fn read_le_u32(bytes: &[u8], offset: usize) -> Option<u32> {
    let value: [u8; 4] = bytes.get(offset..offset + 4)?.try_into().ok()?;
    Some(u32::from_le_bytes(value))
}

struct KeyInner {
    provider: NCRYPT_PROV_HANDLE,
    key: NCRYPT_KEY_HANDLE,
    provider_name: Zeroizing<String>,
    key_name: Zeroizing<String>,
    bits: u32,
    security_bits: u32,
    modulus: Vec<u8>,
    exponent: Vec<u8>,
    sign_lock: Mutex<()>,
}

// SAFETY: NCrypt handles are process-local opaque handles. All signing calls using `key` are
// serialized by `sign_lock`; the remaining operations are immutable metadata reads. Drop runs
// exactly once through Arc ownership.
unsafe impl Send for KeyInner {}
// SAFETY: The same synchronization and immutable-access invariant used for `Send` makes shared
// references safe across threads.
unsafe impl Sync for KeyInner {}

impl Drop for KeyInner {
    fn drop(&mut self) {
        if self.key != 0 {
            // SAFETY: `key` is an owned NCrypt handle and is released exactly once here.
            let _ = unsafe { NCryptFreeObject(self.key) };
        }
        if self.provider != 0 {
            // SAFETY: `provider` is an owned NCrypt handle and is released exactly once here.
            let _ = unsafe { NCryptFreeObject(self.provider) };
        }
        self.modulus.zeroize();
        self.exponent.zeroize();
    }
}

#[derive(Clone)]
pub struct CngKey(FallibleArc<KeyInner>);

impl fmt::Debug for CngKey {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CngKey")
            .field("provider_name", &self.provider_name())
            .field("key_name", &self.key_name())
            .field("bits", &self.bits())
            .finish_non_exhaustive()
    }
}

impl CngKey {
    pub fn open(provider_name: &str, key_name: &str) -> Result<Self, CngError> {
        Self::open_internal(provider_name, key_name, None)
    }

    pub fn open_or_create(
        provider_name: &str,
        key_name: &str,
        bits: u32,
    ) -> Result<Self, CngError> {
        if !(MIN_RSA_BITS..=MAX_RSA_BITS).contains(&bits) {
            return Err(CngError::detail(
                "CngKey::open_or_create",
                format_args!("RSA keys must be between {MIN_RSA_BITS} and {MAX_RSA_BITS} bits"),
            ));
        }
        Self::open_internal(provider_name, key_name, Some(bits))
    }

    fn open_internal(
        provider_name: &str,
        key_name: &str,
        create_bits: Option<u32>,
    ) -> Result<Self, CngError> {
        let provider_name = if provider_name.is_empty() {
            DEFAULT_PROVIDER_NAME
        } else {
            provider_name
        };
        let provider_wide = wide(provider_name, "provider name conversion")?;
        let key_wide = wide(key_name, "key name conversion")?;
        let create_request = create_bits
            .map(|bits| {
                key_creation_mutex_name(
                    &provider_wide[..provider_wide.len() - 1],
                    &key_wide[..key_wide.len() - 1],
                )
                .map(|mutex_name| (bits, mutex_name))
            })
            .transpose()?;
        let mut provider = 0;
        let mut key = 0;
        let mut created = false;
        let mut delete_created_on_failure = false;
        let mut create_guard = None;

        // SAFETY: output handle storage and both NUL-terminated UTF-16 strings are valid.
        let status =
            unsafe { NCryptOpenStorageProvider(&raw mut provider, provider_wide.as_ptr(), 0) };
        if status != 0 {
            return Err(CngError::from_status("NCryptOpenStorageProvider", status));
        }

        // SAFETY: `provider` is live and the output/key-name pointers are valid.
        let mut status = unsafe {
            NCryptOpenKey(
                provider,
                &raw mut key,
                key_wide.as_ptr(),
                0,
                NCRYPT_SILENT_FLAG,
            )
        };
        if missing_key(status) {
            let Some((bits, mutex_name)) = create_request.as_ref() else {
                // SAFETY: `provider` is owned by this function and has not been freed.
                let _ = unsafe { NCryptFreeObject(provider) };
                return Err(CngError::detail(
                    "NCryptOpenKey",
                    "the named CNG key does not exist",
                ));
            };
            create_guard = Some(match KeyCreationMutexGuard::acquire(mutex_name) {
                Ok(guard) => guard,
                Err(error) => {
                    // SAFETY: `provider` is owned by this function and has not been freed.
                    let _ = unsafe { NCryptFreeObject(provider) };
                    return Err(error);
                }
            });
            // Another caller may have finalized the key while this process waited.
            // SAFETY: provider, output storage, and the NUL-terminated key name remain live.
            status = unsafe {
                NCryptOpenKey(
                    provider,
                    &raw mut key,
                    key_wide.as_ptr(),
                    0,
                    NCRYPT_SILENT_FLAG,
                )
            };
            if missing_key(status) {
                // SAFETY: `provider` and UTF-16 key name are live, and `key` is writable.
                status = unsafe {
                    NCryptCreatePersistedKey(
                        provider,
                        &raw mut key,
                        NCRYPT_RSA_ALGORITHM,
                        key_wide.as_ptr(),
                        0,
                        0,
                    )
                };
                if status == NTE_EXISTS {
                    if key != 0 {
                        // SAFETY: a failed concurrent create may still return a losing handle. It
                        // was not created by this caller and must be released, never deleted.
                        let _ = unsafe { NCryptFreeObject(key) };
                        key = 0;
                    }
                    status = open_after_create_race(provider, &key_wide, &mut key);
                } else if status == 0 {
                    created = true;
                    delete_created_on_failure = true;
                    let usage = NCRYPT_ALLOW_SIGNING_FLAG;
                    let export_policy = 0_u32;
                    // SAFETY: each property pointer references a four-byte value for the duration
                    // of the call, and `key` is a valid unfinalized key handle.
                    status = unsafe {
                        NCryptSetProperty(
                            key,
                            NCRYPT_LENGTH_PROPERTY,
                            (&raw const *bits).cast(),
                            size_of::<u32>() as u32,
                            0,
                        )
                    };
                    if status == 0 {
                        // SAFETY: same property-buffer and live-handle invariant as above.
                        status = unsafe {
                            NCryptSetProperty(
                                key,
                                NCRYPT_KEY_USAGE_PROPERTY,
                                (&raw const usage).cast(),
                                size_of::<u32>() as u32,
                                0,
                            )
                        };
                    }
                    if status == 0 {
                        // SAFETY: same property-buffer and live-handle invariant as above.
                        status = unsafe {
                            NCryptSetProperty(
                                key,
                                NCRYPT_EXPORT_POLICY_PROPERTY,
                                (&raw const export_policy).cast(),
                                size_of::<u32>() as u32,
                                0,
                            )
                        };
                    }
                    if status == 0 {
                        // SAFETY: `key` is a valid unfinalized persisted key.
                        status = unsafe { NCryptFinalizeKey(key, NCRYPT_SILENT_FLAG) };
                    }
                    if status == 0 {
                        // Once finalized, another caller can open this persisted key. Never delete
                        // it during later local setup failure.
                        delete_created_on_failure = false;
                    }
                    if status == NTE_EXISTS {
                        // Another caller finalized the same name first. This handle is the losing
                        // unfinalized state: release it without deleting the winner, then open that
                        // finalized key with bounded retry/backoff.
                        if key != 0 {
                            // SAFETY: the unfinalized losing handle is owned by this caller.
                            let _ = unsafe { NCryptFreeObject(key) };
                            key = 0;
                        }
                        created = false;
                        delete_created_on_failure = false;
                        status = open_after_create_race(provider, &key_wide, &mut key);
                    }
                }
            }
        }

        if status != 0 {
            if delete_created_on_failure && key != 0 {
                // SAFETY: only this caller's unfinalized key is eligible for deletion.
                if unsafe { NCryptDeleteKey(key, 0) } == 0 {
                    key = 0;
                }
            }
            if key != 0 {
                // SAFETY: the key handle is owned and has not otherwise been released.
                let _ = unsafe { NCryptFreeObject(key) };
            }
            // SAFETY: the provider handle is owned and has not otherwise been released.
            let _ = unsafe { NCryptFreeObject(provider) };
            return Err(CngError::from_status(
                if created {
                    "CNG key creation/finalization"
                } else {
                    "NCryptOpenKey"
                },
                status,
            ));
        }
        let result = Self::finish_open(provider, key, provider_name, key_name, create_bits);
        if result.is_err() {
            if delete_created_on_failure && key != 0 {
                // SAFETY: only this caller's unfinalized key is eligible for deletion.
                if unsafe { NCryptDeleteKey(key, 0) } == 0 {
                    key = 0;
                }
            }
            if key != 0 {
                // SAFETY: the handle remains owned here.
                let _ = unsafe { NCryptFreeObject(key) };
            }
            // SAFETY: the provider handle remains owned here.
            let _ = unsafe { NCryptFreeObject(provider) };
        }
        drop(create_guard);
        result
    }

    fn finish_open(
        provider: NCRYPT_PROV_HANDLE,
        key: NCRYPT_KEY_HANDLE,
        provider_name: &str,
        key_name: &str,
        requested_bits: Option<u32>,
    ) -> Result<Self, CngError> {
        let export_policy = read_u32_property(
            key,
            NCRYPT_EXPORT_POLICY_PROPERTY,
            "NCryptGetProperty(Export Policy)",
        )?;
        if export_policy != 0 {
            return Err(CngError::detail(
                "CngKey::open",
                format_args!(
                    "the persisted key has export policy 0x{export_policy:08X}; zero is required"
                ),
            ));
        }

        let usage = read_u32_property(
            key,
            NCRYPT_KEY_USAGE_PROPERTY,
            "NCryptGetProperty(Key Usage)",
        )?;
        let allowed_usage = NCRYPT_ALLOW_SIGNING_FLAG | NCRYPT_ALLOW_KEY_ATTESTATION_FLAG;
        if usage == NCRYPT_ALLOW_ALL_USAGES
            || usage & NCRYPT_ALLOW_SIGNING_FLAG == 0
            || usage & !allowed_usage != 0
        {
            return Err(CngError::detail(
                "CngKey::open",
                format_args!(
                    "the persisted key usage 0x{usage:08X} is not restricted to signing and optional attestation"
                ),
            ));
        }

        let (bits, exponent, modulus) = export_public_blob(key)?;
        let key_security_bits = security_bits(bits);
        if !(MIN_RSA_BITS..=MAX_RSA_BITS).contains(&bits) || key_security_bits < 112 {
            return Err(CngError::detail(
                "CngKey::open",
                format_args!("the persisted RSA key is too small ({bits} bits)"),
            ));
        }
        if requested_bits.is_some_and(|expected| expected != bits) {
            return Err(CngError::detail(
                "CngKey::open_or_create",
                format_args!(
                    "the persisted key is {bits} bits, not the requested {} bits",
                    requested_bits.unwrap_or_default()
                ),
            ));
        }

        let provider_name = Zeroizing::new(try_copy_str(
            provider_name,
            "CngKey provider name retention",
        )?);
        let key_name = Zeroizing::new(try_copy_str(key_name, "CngKey key name retention")?);
        let inner = KeyInner {
            provider,
            key,
            provider_name,
            key_name,
            bits,
            security_bits: key_security_bits,
            modulus,
            exponent,
            sign_lock: Mutex::new(()),
        };
        match FallibleArc::try_new(inner) {
            Ok(inner) => Ok(Self(inner)),
            Err(mut inner) => {
                // The caller still owns and releases the handles when `finish_open` fails.
                inner.key = 0;
                inner.provider = 0;
                drop(inner);
                Err(CngError::memory_failure("CngKey shared ownership"))
            }
        }
    }

    #[must_use]
    pub fn provider_name(&self) -> &str {
        self.0.provider_name.as_str()
    }

    #[must_use]
    pub fn key_name(&self) -> &str {
        self.0.key_name.as_str()
    }

    #[must_use]
    pub fn bits(&self) -> u32 {
        self.0.bits
    }

    #[must_use]
    pub fn security_bits(&self) -> u32 {
        self.0.security_bits
    }

    #[must_use]
    pub fn max_signature_size(&self) -> usize {
        self.0.modulus.len()
    }

    #[must_use]
    pub fn public_modulus(&self) -> &[u8] {
        &self.0.modulus
    }

    #[must_use]
    pub fn public_exponent(&self) -> &[u8] {
        &self.0.exponent
    }

    pub fn sign_digest(
        &self,
        digest: Digest,
        padding: RsaPadding,
        hash: &[u8],
        output: Option<&mut [u8]>,
    ) -> Result<usize, CngError> {
        if hash.len() != digest.output_len() {
            return Err(CngError::detail(
                "NCryptSignHash",
                "the digest length does not match the selected algorithm",
            ));
        }
        let mut required = 0_u32;
        let (padding_info, flags): (*const c_void, u32);
        let pkcs1;
        let pss;
        match padding {
            RsaPadding::Pkcs1 => {
                pkcs1 = BCRYPT_PKCS1_PADDING_INFO {
                    pszAlgId: digest.bcrypt_name(),
                };
                padding_info = (&raw const pkcs1).cast();
                flags = NCRYPT_PAD_PKCS1_FLAG;
            }
            RsaPadding::Pss { salt_len } => {
                pss = BCRYPT_PSS_PADDING_INFO {
                    pszAlgId: digest.bcrypt_name(),
                    cbSalt: salt_len,
                };
                padding_info = (&raw const pss).cast();
                flags = NCRYPT_PAD_PSS_FLAG;
            }
        }

        let _guard = self
            .0
            .sign_lock
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        // SAFETY: the key and padding structure remain live under the mutex, the digest slice is
        // readable, and output is intentionally null for the size query.
        let status = unsafe {
            NCryptSignHash(
                self.0.key,
                padding_info,
                hash.as_ptr(),
                hash.len() as u32,
                null_mut(),
                0,
                &raw mut required,
                flags | NCRYPT_SILENT_FLAG,
            )
        };
        if status != 0 {
            return Err(CngError::from_status("NCryptSignHash(size)", status));
        }
        let expected = self.max_signature_size();
        if required as usize != expected {
            return Err(CngError::detail(
                "NCryptSignHash(size)",
                format_args!(
                    "the KSP reported a {required}-byte signature for a {expected}-byte RSA modulus"
                ),
            ));
        }

        let Some(output) = output else {
            return Ok(expected);
        };
        if output.len() < required as usize || output.len() > u32::MAX as usize {
            return Err(CngError::detail(
                "NCryptSignHash",
                format_args!(
                    "signature buffer is {} bytes; {required} bytes are required",
                    output.len()
                ),
            ));
        }
        let mut written = 0_u32;
        // SAFETY: all input objects remain live under the mutex, and `output` is writable for the
        // declared size.
        let status = unsafe {
            NCryptSignHash(
                self.0.key,
                padding_info,
                hash.as_ptr(),
                hash.len() as u32,
                output.as_mut_ptr(),
                output.len() as u32,
                &raw mut written,
                flags | NCRYPT_SILENT_FLAG,
            )
        };
        if status != 0 {
            output.zeroize();
            return Err(CngError::from_status("NCryptSignHash", status));
        }
        if written as usize != expected || written as usize > output.len() {
            output.zeroize();
            return Err(CngError::detail(
                "NCryptSignHash",
                format_args!(
                    "the KSP wrote an inconsistent {written}-byte signature; {expected} bytes were expected"
                ),
            ));
        }
        Ok(expected)
    }
}

fn open_after_create_race(
    provider: NCRYPT_PROV_HANDLE,
    key_name: &[u16],
    key: &mut NCRYPT_KEY_HANDLE,
) -> i32 {
    for attempt in 0..CREATE_RACE_OPEN_ATTEMPTS {
        if attempt < 4 {
            std::thread::yield_now();
        } else {
            let shift = (attempt - 4).min(5);
            let delay = (1_u64 << shift).min(CREATE_RACE_MAX_DELAY_MS);
            std::thread::sleep(std::time::Duration::from_millis(delay));
        }
        if *key != 0 {
            // SAFETY: any handle returned by a failed prior open attempt remains caller-owned.
            let _ = unsafe { NCryptFreeObject(*key) };
            *key = 0;
        }
        // SAFETY: provider, output storage, and the NUL-terminated key name remain live.
        let status =
            unsafe { NCryptOpenKey(provider, key, key_name.as_ptr(), 0, NCRYPT_SILENT_FLAG) };
        if status == 0 || !missing_key(status) || attempt + 1 == CREATE_RACE_OPEN_ATTEMPTS {
            return status;
        }
    }
    NTE_EXISTS
}

fn export_public_blob(key: NCRYPT_KEY_HANDLE) -> Result<(u32, Vec<u8>, Vec<u8>), CngError> {
    let mut required = 0_u32;
    // SAFETY: `key` is live; all optional input/output buffers are null for the size query.
    let status = unsafe {
        NCryptExportKey(
            key,
            0,
            BCRYPT_RSAPUBLIC_BLOB,
            null(),
            null_mut(),
            0,
            &raw mut required,
            NCRYPT_SILENT_FLAG,
        )
    };
    if status != 0 {
        return Err(CngError::from_status(
            "NCryptExportKey(public size)",
            status,
        ));
    }
    if !(24..=MAX_PUBLIC_BLOB_BYTES).contains(&required) {
        return Err(CngError::detail(
            "NCryptExportKey(public size)",
            format_args!("CNG reported an invalid {required}-byte RSA public blob"),
        ));
    }
    let mut blob = try_zeroed_bytes(required as usize, "NCryptExportKey(public buffer)")?;
    let mut written = 0_u32;
    // SAFETY: `blob` is writable for `required` bytes and the key remains live.
    let status = unsafe {
        NCryptExportKey(
            key,
            0,
            BCRYPT_RSAPUBLIC_BLOB,
            null(),
            blob.as_mut_ptr(),
            required,
            &raw mut written,
            NCRYPT_SILENT_FLAG,
        )
    };
    if status != 0 {
        return Err(CngError::from_status("NCryptExportKey(public)", status));
    }
    if written != required || blob.len() < 24 {
        return Err(CngError::detail(
            "NCryptExportKey(public)",
            "CNG returned a truncated RSA public blob",
        ));
    }

    let magic = read_le_u32(&blob, 0);
    let bits = read_le_u32(&blob, 4);
    let exponent_len = read_le_u32(&blob, 8);
    let modulus_len = read_le_u32(&blob, 12);
    let (Some(magic), Some(bits), Some(exponent_len), Some(modulus_len)) =
        (magic, bits, exponent_len, modulus_len)
    else {
        return Err(CngError::detail(
            "NCryptExportKey(public)",
            "CNG returned an invalid RSA public blob header",
        ));
    };
    let expected = 24_usize
        .checked_add(exponent_len as usize)
        .and_then(|length| length.checked_add(modulus_len as usize));
    if magic != BCRYPT_RSAPUBLIC_MAGIC
        || !(MIN_RSA_BITS..=MAX_RSA_BITS).contains(&bits)
        || exponent_len == 0
        || exponent_len > 8
        || modulus_len == 0
        || modulus_len != bits.div_ceil(8)
        || expected != Some(blob.len())
    {
        return Err(CngError::detail(
            "NCryptExportKey(public)",
            "CNG returned an invalid RSA public blob",
        ));
    }
    let exponent_end = 24 + exponent_len as usize;
    let mut exponent = try_copy_bytes(&blob[24..exponent_end], "NCryptExportKey(public exponent)")?;
    let exponent_value = exponent
        .iter()
        .fold(0_u64, |value, byte| (value << 8) | u64::from(*byte));
    if exponent_value < 3 || exponent_value & 1 == 0 {
        exponent.zeroize();
        return Err(CngError::detail(
            "NCryptExportKey(public)",
            "CNG returned an invalid RSA public exponent",
        ));
    }
    let modulus = match try_copy_bytes(&blob[exponent_end..], "NCryptExportKey(public modulus)") {
        Ok(modulus) => modulus,
        Err(error) => {
            exponent.zeroize();
            return Err(error);
        }
    };
    Ok((bits, exponent, modulus))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RsaPadding {
    Pkcs1,
    Pss { salt_len: u32 },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Digest {
    Sha256,
    Sha384,
    Sha512,
}

impl Digest {
    #[must_use]
    pub const fn canonical_name(self) -> &'static str {
        match self {
            Self::Sha256 => "SHA256",
            Self::Sha384 => "SHA384",
            Self::Sha512 => "SHA512",
        }
    }

    #[must_use]
    pub const fn output_len(self) -> usize {
        match self {
            Self::Sha256 => 32,
            Self::Sha384 => 48,
            Self::Sha512 => 64,
        }
    }

    fn bcrypt_name(self) -> PCWSTR {
        match self {
            Self::Sha256 => BCRYPT_SHA256_ALGORITHM,
            Self::Sha384 => BCRYPT_SHA384_ALGORITHM,
            Self::Sha512 => BCRYPT_SHA512_ALGORITHM,
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        if value.eq_ignore_ascii_case("SHA256")
            || value.eq_ignore_ascii_case("SHA2-256")
            || value.eq_ignore_ascii_case("SHA-256")
        {
            Some(Self::Sha256)
        } else if value.eq_ignore_ascii_case("SHA384")
            || value.eq_ignore_ascii_case("SHA2-384")
            || value.eq_ignore_ascii_case("SHA-384")
        {
            Some(Self::Sha384)
        } else if value.eq_ignore_ascii_case("SHA512")
            || value.eq_ignore_ascii_case("SHA2-512")
            || value.eq_ignore_ascii_case("SHA-512")
        {
            Some(Self::Sha512)
        } else {
            None
        }
    }
}

struct HashAlgorithm {
    handle: BCRYPT_ALG_HANDLE,
    object_len: u32,
}

// SAFETY: algorithm provider handles are immutable after opening and BCrypt permits them to be
// shared between independent hash objects.
unsafe impl Send for HashAlgorithm {}
// SAFETY: all mutable state belongs to individual hash handles; the shared algorithm handle is
// queried only during construction.
unsafe impl Sync for HashAlgorithm {}

impl Drop for HashAlgorithm {
    fn drop(&mut self) {
        if !self.handle.is_null() {
            // SAFETY: `handle` is owned and released exactly once.
            let _ = unsafe { BCryptCloseAlgorithmProvider(self.handle, 0) };
        }
    }
}

pub struct HashState {
    algorithm: FallibleArc<HashAlgorithm>,
    handle: BCRYPT_HASH_HANDLE,
    object: Zeroizing<Vec<u8>>,
    digest: Digest,
}

impl HashState {
    pub fn new(digest: Digest) -> Result<Self, CngError> {
        let mut handle = null_mut();
        // SAFETY: output storage is valid and the algorithm name is a static NUL-terminated string.
        let status = unsafe {
            BCryptOpenAlgorithmProvider(&raw mut handle, digest.bcrypt_name(), null(), 0)
        };
        if status < 0 {
            return Err(CngError::from_status("BCryptOpenAlgorithmProvider", status));
        }
        let mut algorithm = HashAlgorithm {
            handle,
            object_len: 0,
        };
        let object_len =
            bcrypt_u32_property(algorithm.handle, BCRYPT_OBJECT_LENGTH, "ObjectLength")?;
        let hash_len =
            bcrypt_u32_property(algorithm.handle, BCRYPT_HASH_LENGTH, "HashDigestLength")?;
        if hash_len as usize != digest.output_len() {
            return Err(CngError::detail(
                "BCryptGetProperty(HashDigestLength)",
                "BCrypt returned an unexpected digest length",
            ));
        }
        algorithm.object_len = object_len;
        let algorithm = FallibleArc::try_new(algorithm).map_err(|algorithm| {
            drop(algorithm);
            CngError::memory_failure("BCrypt algorithm shared ownership")
        })?;
        let mut object = try_zeroed_bytes(object_len as usize, "BCryptCreateHash object buffer")?;
        let mut hash = null_mut();
        // SAFETY: the algorithm handle is live, `object` is writable for the supplied size, and
        // the hash output pointer is valid.
        let status = unsafe {
            BCryptCreateHash(
                algorithm.handle,
                &raw mut hash,
                object.as_mut_ptr(),
                object_len,
                null(),
                0,
                0,
            )
        };
        if status < 0 {
            if !hash.is_null() {
                // SAFETY: a failed creation that returned a handle leaves that handle owned here.
                let _ = unsafe { BCryptDestroyHash(hash) };
            }
            return Err(CngError::from_status("BCryptCreateHash", status));
        }
        Ok(Self {
            algorithm,
            handle: hash,
            object,
            digest,
        })
    }

    #[must_use]
    pub const fn digest(&self) -> Digest {
        self.digest
    }

    pub fn update(&mut self, mut data: &[u8]) -> Result<(), CngError> {
        while !data.is_empty() {
            let chunk_len = data.len().min(u32::MAX as usize);
            // SAFETY: the hash handle is live and the chunk is readable for `chunk_len` bytes.
            let status = unsafe { BCryptHashData(self.handle, data.as_ptr(), chunk_len as u32, 0) };
            if status < 0 {
                return Err(CngError::from_status("BCryptHashData", status));
            }
            data = &data[chunk_len..];
        }
        Ok(())
    }

    pub fn finish_clone(&self) -> Result<Zeroizing<Vec<u8>>, CngError> {
        let duplicate = self.try_clone()?;
        let mut output =
            try_zeroed_bytes(self.digest.output_len(), "BCryptFinishHash output buffer")?;
        // SAFETY: the duplicated hash handle is live and `output` is writable for its full size.
        let status = unsafe {
            BCryptFinishHash(
                duplicate.handle,
                output.as_mut_ptr(),
                output.len() as u32,
                0,
            )
        };
        if status < 0 {
            return Err(CngError::from_status("BCryptFinishHash", status));
        }
        Ok(output)
    }

    pub fn hash_message(digest: Digest, message: &[u8]) -> Result<Zeroizing<Vec<u8>>, CngError> {
        let mut state = Self::new(digest)?;
        state.update(message)?;
        state.finish_clone()
    }

    pub fn try_clone(&self) -> Result<Self, CngError> {
        let mut object = try_zeroed_bytes(
            self.algorithm.object_len as usize,
            "BCryptDuplicateHash object buffer",
        )?;
        let mut handle = null_mut();
        // SAFETY: the source hash and shared algorithm remain live, while `object` and the output
        // handle pointer are writable for the declared sizes.
        let status = unsafe {
            BCryptDuplicateHash(
                self.handle,
                &raw mut handle,
                object.as_mut_ptr(),
                self.algorithm.object_len,
                0,
            )
        };
        if status < 0 {
            if !handle.is_null() {
                // SAFETY: a failed duplication that returned a handle leaves that handle owned
                // here.
                let _ = unsafe { BCryptDestroyHash(handle) };
            }
            return Err(CngError::from_status("BCryptDuplicateHash", status));
        }
        Ok(Self {
            algorithm: self.algorithm.clone(),
            handle,
            object,
            digest: self.digest,
        })
    }
}

impl Drop for HashState {
    fn drop(&mut self) {
        if !self.handle.is_null() {
            // SAFETY: `handle` is owned by this state and released exactly once.
            let _ = unsafe { BCryptDestroyHash(self.handle) };
        }
        self.object.zeroize();
    }
}

fn bcrypt_u32_property(
    handle: BCRYPT_ALG_HANDLE,
    property: PCWSTR,
    property_name: &'static str,
) -> Result<u32, CngError> {
    let mut value = 0_u32;
    let mut written = 0_u32;
    // SAFETY: the algorithm handle is live and both output pointers are writable.
    let status = unsafe {
        BCryptGetProperty(
            handle,
            property,
            (&raw mut value).cast(),
            size_of::<u32>() as u32,
            &raw mut written,
            0,
        )
    };
    if status < 0 {
        return Err(CngError::from_status("BCryptGetProperty", status));
    }
    if written as usize != size_of::<u32>() {
        return Err(CngError::detail(
            "BCryptGetProperty",
            format_args!("{property_name} had an unexpected size"),
        ));
    }
    Ok(value)
}

pub fn random_bytes(output: &mut [u8]) -> Result<(), CngError> {
    if output.len() > u32::MAX as usize {
        return Err(CngError::detail(
            "BCryptGenRandom",
            "requested random buffer is too large",
        ));
    }
    // SAFETY: `output` is writable for the supplied length; the system-preferred RNG accepts a
    // null algorithm handle.
    let status = unsafe {
        BCryptGenRandom(
            null_mut(),
            output.as_mut_ptr(),
            output.len() as u32,
            BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        )
    };
    if status < 0 {
        Err(CngError::from_status("BCryptGenRandom", status))
    } else {
        Ok(())
    }
}

pub fn private_export_is_blocked(provider_name: &str, key_name: &str) -> Result<(), CngError> {
    let key = CngKey::open(provider_name, key_name)?;
    let mut size = 0_u32;
    // SAFETY: the key is held live, and null output requests only a private-blob size query.
    let status = unsafe {
        NCryptExportKey(
            key.0.key,
            0,
            BCRYPT_RSAPRIVATE_BLOB,
            null(),
            null_mut(),
            0,
            &raw mut size,
            NCRYPT_SILENT_FLAG,
        )
    };
    if status == 0 {
        Err(CngError::detail(
            "NCryptExportKey(private)",
            "CNG unexpectedly allowed a private-key export size query",
        ))
    } else {
        Ok(())
    }
}

pub fn delete_test_key(provider_name: &str, key_name: &str) -> Result<(), CngError> {
    if !TEST_KEY_PREFIXES
        .iter()
        .any(|prefix| key_name.starts_with(prefix))
    {
        return Err(CngError::detail(
            "delete_test_key",
            "refusing to delete a key without a recognized test-owned prefix",
        ));
    }

    let provider_name = if provider_name.is_empty() {
        DEFAULT_PROVIDER_NAME
    } else {
        provider_name
    };
    let provider_wide = wide(provider_name, "provider name conversion")?;
    let key_wide = wide(key_name, "key name conversion")?;
    let mut provider = 0;
    let mut key = 0;
    // SAFETY: output storage and UTF-16 input strings are valid.
    let status = unsafe { NCryptOpenStorageProvider(&raw mut provider, provider_wide.as_ptr(), 0) };
    if status != 0 {
        return Err(CngError::from_status("NCryptOpenStorageProvider", status));
    }
    // SAFETY: the provider is live and output/key-name pointers are valid.
    let status = unsafe {
        NCryptOpenKey(
            provider,
            &raw mut key,
            key_wide.as_ptr(),
            0,
            NCRYPT_SILENT_FLAG,
        )
    };
    if missing_key(status) {
        // SAFETY: the provider handle is owned here.
        let _ = unsafe { NCryptFreeObject(provider) };
        return Ok(());
    }
    if status != 0 {
        // SAFETY: the provider handle is owned here.
        let _ = unsafe { NCryptFreeObject(provider) };
        return Err(CngError::from_status("NCryptOpenKey(delete)", status));
    }
    // SAFETY: this function accepts only explicitly test-owned names and owns the opened handle.
    let delete_status = unsafe { NCryptDeleteKey(key, NCRYPT_SILENT_FLAG) };
    if delete_status == 0 {
        key = 0;
    }
    if key != 0 {
        // SAFETY: failed deletion leaves the opened key handle owned here.
        let _ = unsafe { NCryptFreeObject(key) };
    }
    // SAFETY: the provider handle is owned here.
    let _ = unsafe { NCryptFreeObject(provider) };
    if delete_status != 0 {
        Err(CngError::from_status("NCryptDeleteKey", delete_status))
    } else {
        Ok(())
    }
}

fn try_utf16_lossy(value: &[u16], operation: &'static str) -> Result<String, CngError> {
    let capacity = value
        .len()
        .checked_mul(3)
        .ok_or_else(|| CngError::memory_failure(operation))?;
    let mut output = String::new();
    output
        .try_reserve_exact(capacity)
        .map_err(|_| CngError::memory_failure(operation))?;
    for character in char::decode_utf16(value.iter().copied()) {
        output.push(character.unwrap_or(char::REPLACEMENT_CHARACTER));
    }
    Ok(output)
}

pub fn project_test_keys(provider_name: &str) -> Result<Vec<String>, CngError> {
    let provider_name = if provider_name.is_empty() {
        DEFAULT_PROVIDER_NAME
    } else {
        provider_name
    };
    let provider_wide = wide(provider_name, "provider name conversion")?;
    let mut provider = 0;
    // SAFETY: output storage and the NUL-terminated provider name are valid.
    let status = unsafe { NCryptOpenStorageProvider(&raw mut provider, provider_wide.as_ptr(), 0) };
    if status != 0 {
        return Err(CngError::from_status("NCryptOpenStorageProvider", status));
    }
    let mut enumeration_state: *mut c_void = null_mut();
    let mut matches = Vec::new();
    let result = loop {
        let mut key_name: *mut NCryptKeyName = null_mut();
        // SAFETY: provider is live; both output slots remain writable for the call and enumeration
        // state is preserved between calls as required by NCrypt.
        let status = unsafe {
            NCryptEnumKeys(
                provider,
                null(),
                &raw mut key_name,
                &raw mut enumeration_state,
                0,
            )
        };
        if status == NTE_NO_MORE_ITEMS {
            break Ok(matches);
        }
        if status != 0 {
            break Err(CngError::from_status("NCryptEnumKeys", status));
        }
        let mut item_result = Ok(());
        if !key_name.is_null() {
            // SAFETY: NCrypt returned a live NCryptKeyName allocation with a NUL-terminated name.
            let name_pointer = unsafe { (*key_name).pszName };
            if !name_pointer.is_null() {
                let mut length = 0_usize;
                // SAFETY: NCrypt key names are NUL-terminated; the defensive bound prevents an
                // unbounded scan if a provider violates that contract.
                while length < 32_768 && unsafe { *name_pointer.add(length) } != 0 {
                    length += 1;
                }
                if length < 32_768 {
                    // SAFETY: the bounded scan established `length` initialized UTF-16 code units.
                    let units = unsafe { std::slice::from_raw_parts(name_pointer, length) };
                    match try_utf16_lossy(units, "NCryptEnumKeys key name") {
                        Ok(name)
                            if TEST_KEY_PREFIXES
                                .iter()
                                .any(|prefix| name.starts_with(prefix)) =>
                        {
                            if matches.try_reserve(1).is_err() {
                                item_result =
                                    Err(CngError::memory_failure("NCryptEnumKeys result list"));
                            } else {
                                matches.push(name);
                            }
                        }
                        Ok(_) => {}
                        Err(error) => item_result = Err(error),
                    }
                }
            }
            // SAFETY: key_name is the allocation returned by this NCryptEnumKeys iteration.
            unsafe {
                let _ = NCryptFreeBuffer(key_name.cast());
            }
        }
        if let Err(error) = item_result {
            break Err(error);
        }
    };
    if !enumeration_state.is_null() {
        // SAFETY: enumeration_state is the NCrypt-owned state returned by NCryptEnumKeys.
        unsafe {
            let _ = NCryptFreeBuffer(enumeration_state);
        }
    }
    // SAFETY: provider is owned by this function.
    unsafe {
        let _ = NCryptFreeObject(provider);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Read as IoRead, Write as IoWrite};
    use std::process::{Child, ChildStdout, Command, Stdio};
    use std::sync::Arc;
    use std::time::{Duration, Instant};
    use windows_sys::Win32::Foundation::ERROR_INVALID_HANDLE;
    use windows_sys::Win32::System::Threading::{CreateEventW, GetProcessHandleCount};

    const CASE_VARIANT_MUTEX_CHILD: &str = "OPENSSL_CNG_CASE_VARIANT_MUTEX_CHILD";
    const CASE_VARIANT_MUTEX_PROVIDER: &str = "OPENSSL_CNG_CASE_VARIANT_MUTEX_PROVIDER";
    const CASE_VARIANT_MUTEX_KEY: &str = "OPENSSL_CNG_CASE_VARIANT_MUTEX_KEY";
    const CASE_VARIANT_MUTEX_HOLDER_READY: &str = "case-variant-mutex-holder-ready";
    const CASE_VARIANT_MUTEX_CONTENDER_READY: &str = "case-variant-mutex-contender-ready";
    const HANDLE_LIFECYCLE_CHILD: &str = "OPENSSL_CNG_HANDLE_LIFECYCLE_CHILD";
    const NAMESPACE_HANDOFF_CHILD: &str = "OPENSSL_CNG_NAMESPACE_HANDOFF_CHILD";

    #[test]
    fn memory_errors_use_the_allocation_free_marker() {
        let error = try_zeroed_bytes(usize::MAX, "test allocation").expect_err("capacity overflow");
        assert!(error.is_memory_failure());
        assert_eq!(error.status(), 0);
        assert_eq!(
            error.to_string(),
            "test allocation: memory allocation failed"
        );
        assert!(error.clone().is_memory_failure());
    }

    #[test]
    fn inline_error_details_preserve_dynamic_status_text() {
        let error = CngError::detail(
            "test operation",
            format_args!("buffer {} requires {}", 12, 34),
        );
        assert!(!error.is_memory_failure());
        assert_eq!(error.to_string(), "test operation: buffer 12 requires 34");
    }

    #[test]
    fn fallible_utf16_conversion_replaces_invalid_surrogates() {
        let converted = try_utf16_lossy(
            &[u16::from(b'A'), 0xD800, u16::from(b'B')],
            "test conversion",
        )
        .expect("conversion");
        assert_eq!(converted, "A\u{FFFD}B");
    }

    fn owned_name() -> String {
        let mut random = [0_u8; 16];
        random_bytes(&mut random).expect("random test key suffix");
        let suffix = random
            .iter()
            .map(|value| format!("{value:02x}"))
            .collect::<String>();
        random.zeroize();
        format!("openssl-cng-rust-test-{}-{suffix}", std::process::id())
    }

    fn mutex_name(
        provider_name: &str,
        key_name: &str,
    ) -> Result<[u16; KEY_CREATE_MUTEX_NAME_LEN], CngError> {
        let provider = wide(provider_name, "test provider name")?;
        let key = wide(key_name, "test key name")?;
        key_creation_mutex_name(&provider[..provider.len() - 1], &key[..key.len() - 1])
    }

    fn process_handle_count() -> u32 {
        let mut count = 0_u32;
        // SAFETY: the current-process pseudo-handle is always valid and count is writable.
        let succeeded = unsafe { GetProcessHandleCount(GetCurrentProcess(), &raw mut count) };
        assert_ne!(succeeded, 0, "query process handle count");
        count
    }

    fn run_isolated_test(test_name: &str, environment: &str) -> bool {
        if std::env::var_os(environment).is_some() {
            return false;
        }
        let executable = std::env::current_exe().expect("locate current test executable");
        let output = Command::new(executable)
            .arg("--exact")
            .arg(test_name)
            .arg("--nocapture")
            .env(environment, "1")
            .output()
            .expect("spawn isolated test process");
        assert!(
            output.status.success(),
            "isolated test {test_name} failed with {}; stdout: {}; stderr: {}",
            output.status,
            String::from_utf8_lossy(&output.stdout).trim(),
            String::from_utf8_lossy(&output.stderr).trim()
        );
        true
    }

    fn legacy_local_mutex_name(name: &[u16]) -> Vec<u16> {
        let mut local = "Local\\".encode_utf16().collect::<Vec<_>>();
        local.extend_from_slice(name);
        local
    }

    fn case_variant_child_configuration() -> Option<(String, String, String)> {
        let mode = std::env::var(CASE_VARIANT_MUTEX_CHILD).ok()?;
        let provider =
            std::env::var(CASE_VARIANT_MUTEX_PROVIDER).expect("case-variant provider name");
        let key = std::env::var(CASE_VARIANT_MUTEX_KEY).expect("case-variant key name");
        Some((mode, provider, key))
    }

    #[test]
    fn case_variant_mutex_holder_process() {
        let Some((mode, provider, key)) = case_variant_child_configuration() else {
            return;
        };
        assert_eq!(mode, "holder");
        let name = mutex_name(&provider, &key).expect("holder mutex name");
        let _guard = KeyCreationMutexGuard::acquire(&name).expect("holder mutex acquisition");
        println!("{CASE_VARIANT_MUTEX_HOLDER_READY}");
        std::io::stdout().flush().expect("flush holder readiness");
        let mut release = [0_u8; 1];
        std::io::stdin()
            .read_exact(&mut release)
            .expect("wait for holder release");
    }

    #[test]
    fn case_variant_mutex_contender_process() {
        let Some((mode, provider, key)) = case_variant_child_configuration() else {
            return;
        };
        assert_eq!(mode, "contender");
        println!("{CASE_VARIANT_MUTEX_CONTENDER_READY}");
        std::io::stdout()
            .flush()
            .expect("flush contender readiness");
        let name = mutex_name(&provider, &key).expect("contender mutex name");
        let _guard = KeyCreationMutexGuard::acquire(&name).expect("contender mutex acquisition");
    }

    fn spawn_case_variant_child(
        test_name: &str,
        mode: &str,
        provider_name: &str,
        key_name: &str,
    ) -> Result<Child, String> {
        let executable = std::env::current_exe()
            .map_err(|error| format!("locate current test executable: {error}"))?;
        Command::new(executable)
            .arg("--exact")
            .arg(test_name)
            .arg("--nocapture")
            .env(CASE_VARIANT_MUTEX_CHILD, mode)
            .env(CASE_VARIANT_MUTEX_PROVIDER, provider_name)
            .env(CASE_VARIANT_MUTEX_KEY, key_name)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|error| format!("spawn {mode} process: {error}"))
    }

    fn read_until_marker(
        child: &mut Child,
        marker: &str,
        label: &str,
    ) -> Result<(BufReader<ChildStdout>, String), String> {
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| format!("{label} stdout is unavailable"))?;
        let mut reader = BufReader::new(stdout);
        let mut output = String::new();
        loop {
            let mut line = String::new();
            let read = reader
                .read_line(&mut line)
                .map_err(|error| format!("read {label} output: {error}"))?;
            if read == 0 {
                let status = child
                    .wait()
                    .map_err(|error| format!("wait for {label}: {error}"))?;
                let mut stderr = String::new();
                if let Some(mut stream) = child.stderr.take() {
                    stream
                        .read_to_string(&mut stderr)
                        .map_err(|error| format!("read {label} stderr: {error}"))?;
                }
                return Err(format!(
                    "{label} exited before readiness with {status}; stdout: {}; stderr: {}",
                    output.trim(),
                    stderr.trim()
                ));
            }
            output.push_str(&line);
            if line.contains(marker) {
                return Ok((reader, output));
            }
        }
    }

    fn release_mutex_holder(child: &mut Child) -> Result<(), String> {
        let mut stdin = child
            .stdin
            .take()
            .ok_or_else(|| "holder stdin is unavailable".to_owned())?;
        stdin
            .write_all(&[1])
            .map_err(|error| format!("release mutex holder: {error}"))
    }

    fn finish_child(
        child: &mut Child,
        mut stdout: BufReader<ChildStdout>,
        mut output: String,
        label: &str,
    ) -> Result<(), String> {
        let status = child
            .wait()
            .map_err(|error| format!("wait for {label}: {error}"))?;
        stdout
            .read_to_string(&mut output)
            .map_err(|error| format!("read remaining {label} output: {error}"))?;
        let mut stderr = String::new();
        if let Some(mut stream) = child.stderr.take() {
            stream
                .read_to_string(&mut stderr)
                .map_err(|error| format!("read {label} stderr: {error}"))?;
        }
        if status.success() {
            Ok(())
        } else {
            Err(format!(
                "{label} failed with {status}; stdout: {}; stderr: {}",
                output.trim(),
                stderr.trim()
            ))
        }
    }

    fn run_case_variant_mutex_test(
        holder_provider: &str,
        holder_key: &str,
        contender_provider: &str,
        contender_key: &str,
    ) -> Result<(), String> {
        let mut holder = spawn_case_variant_child(
            "tests::case_variant_mutex_holder_process",
            "holder",
            holder_provider,
            holder_key,
        )?;
        let (holder_stdout, holder_output) =
            read_until_marker(&mut holder, CASE_VARIANT_MUTEX_HOLDER_READY, "holder")?;

        let mut contender = match spawn_case_variant_child(
            "tests::case_variant_mutex_contender_process",
            "contender",
            contender_provider,
            contender_key,
        ) {
            Ok(child) => child,
            Err(error) => {
                let _ = release_mutex_holder(&mut holder);
                let _ = finish_child(&mut holder, holder_stdout, holder_output, "holder");
                return Err(error);
            }
        };
        let (contender_stdout, contender_output) = match read_until_marker(
            &mut contender,
            CASE_VARIANT_MUTEX_CONTENDER_READY,
            "contender",
        ) {
            Ok(output) => output,
            Err(error) => {
                let release = release_mutex_holder(&mut holder);
                let holder_result =
                    finish_child(&mut holder, holder_stdout, holder_output, "holder");
                release?;
                holder_result?;
                return Err(error);
            }
        };

        std::thread::sleep(Duration::from_millis(250));
        let bypassed = contender
            .try_wait()
            .map_err(|error| format!("poll contender process: {error}"))?
            .is_some();

        let release = release_mutex_holder(&mut holder);
        let holder_result = finish_child(&mut holder, holder_stdout, holder_output, "holder");
        let contender_result = finish_child(
            &mut contender,
            contender_stdout,
            contender_output,
            "contender",
        );
        release?;
        holder_result?;
        contender_result?;
        if bypassed {
            return Err(
                "case-variant contender acquired a different named mutex while holder owned it"
                    .to_owned(),
            );
        }
        Ok(())
    }

    #[test]
    fn digest_aliases_are_strict() {
        assert_eq!(Digest::parse("sha-256"), Some(Digest::Sha256));
        assert_eq!(Digest::parse("SHA2-384"), Some(Digest::Sha384));
        assert_eq!(Digest::parse("sha512"), Some(Digest::Sha512));
        assert_eq!(Digest::parse("SHA1"), None);
    }

    #[test]
    fn hash_state_clone_is_independent() {
        let mut hash = HashState::new(Digest::Sha256).expect("create hash");
        hash.update(b"abc").expect("update hash");
        let mut duplicate = hash.try_clone().expect("duplicate hash");
        hash.update(b"def").expect("finish first input");
        duplicate.update(b"ghi").expect("finish second input");
        assert_ne!(
            hash.finish_clone().expect("first digest").as_slice(),
            duplicate.finish_clone().expect("second digest").as_slice()
        );
    }

    #[test]
    fn persisted_key_lifecycle_and_private_export_policy() {
        let name = owned_name();
        delete_test_key(DEFAULT_PROVIDER_NAME, &name).expect("pre-clean test key");
        let result = (|| {
            let key = CngKey::open_or_create(DEFAULT_PROVIDER_NAME, &name, 2_048)?;
            assert_eq!(key.bits(), 2_048);
            assert_eq!(key.max_signature_size(), 256);
            assert!(!key.public_modulus().is_empty());
            assert!(!key.public_exponent().is_empty());
            let digest = HashState::hash_message(Digest::Sha256, b"cng-core lifecycle")?;
            let required = key.sign_digest(Digest::Sha256, RsaPadding::Pkcs1, &digest, None)?;
            assert_eq!(required, 256);
            let mut signature = vec![0_u8; required];
            let written = key.sign_digest(
                Digest::Sha256,
                RsaPadding::Pss { salt_len: 32 },
                &digest,
                Some(&mut signature),
            )?;
            assert_eq!(written, 256);
            private_export_is_blocked(DEFAULT_PROVIDER_NAME, &name)
        })();
        delete_test_key(DEFAULT_PROVIDER_NAME, &name).expect("delete test key");
        result.expect("CNG lifecycle");
        assert!(CngKey::open(DEFAULT_PROVIDER_NAME, &name).is_err());
    }

    #[test]
    fn concurrent_open_or_create_converges_on_finalized_key() {
        let name = owned_name();
        delete_test_key(DEFAULT_PROVIDER_NAME, &name).expect("pre-clean concurrent test key");
        let result = (|| -> Result<(), CngError> {
            let barrier = Arc::new(std::sync::Barrier::new(6));
            let threads = (0..6)
                .map(|_| {
                    let barrier = Arc::clone(&barrier);
                    let name = name.clone();
                    std::thread::spawn(move || {
                        barrier.wait();
                        CngKey::open_or_create(DEFAULT_PROVIDER_NAME, &name, 2_048)
                    })
                })
                .collect::<Vec<_>>();
            let mut keys = Vec::new();
            for thread in threads {
                keys.push(thread.join().expect("concurrent key thread")?);
            }
            let modulus = keys[0].public_modulus();
            let exponent = keys[0].public_exponent();
            if !keys
                .iter()
                .all(|key| key.public_modulus() == modulus && key.public_exponent() == exponent)
            {
                return Err(CngError::detail(
                    "concurrent open-or-create test",
                    "callers opened different persisted keys",
                ));
            }
            drop(keys);
            Ok(())
        })();
        delete_test_key(DEFAULT_PROVIDER_NAME, &name).expect("delete concurrent test key");
        result.expect("concurrent open-or-create");
    }

    #[test]
    fn key_creation_namespace_scopes_release_process_handles() {
        if run_isolated_test(
            "tests::key_creation_namespace_scopes_release_process_handles",
            HANDLE_LIFECYCLE_CHILD,
        ) {
            return;
        }

        let mutex_name =
            mutex_name(DEFAULT_PROVIDER_NAME, "namespace-handle-lifecycle").expect("mutex name");
        for _ in 0..4 {
            drop(KeyCreationNamespace::acquire().expect("warm up private namespace"));
            drop(KeyCreationMutexGuard::acquire(&mutex_name).expect("warm up private mutex"));
        }
        let baseline = process_handle_count();
        for _ in 0..128 {
            drop(KeyCreationNamespace::acquire().expect("acquire private namespace"));
            drop(KeyCreationMutexGuard::acquire(&mutex_name).expect("acquire private mutex"));
        }
        let after = process_handle_count();
        let state = KEY_CREATE_NAMESPACE
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        assert_eq!(state.references, 0);
        assert_eq!(state.handle, 0);
        assert!(
            after <= baseline.saturating_add(2),
            "private namespace handle count grew from {baseline} to {after}"
        );
    }

    #[test]
    fn concurrent_namespace_handoffs_do_not_race_alias_teardown() {
        const THREADS: usize = 8;
        const ITERATIONS: usize = 256;

        if run_isolated_test(
            "tests::concurrent_namespace_handoffs_do_not_race_alias_teardown",
            NAMESPACE_HANDOFF_CHILD,
        ) {
            return;
        }

        let barrier = Arc::new(std::sync::Barrier::new(THREADS));
        let threads = (0..THREADS)
            .map(|_| {
                let barrier = Arc::clone(&barrier);
                std::thread::spawn(move || -> Result<(), CngError> {
                    barrier.wait();
                    for _ in 0..ITERATIONS {
                        drop(KeyCreationNamespace::acquire()?);
                        std::thread::yield_now();
                    }
                    Ok(())
                })
            })
            .collect::<Vec<_>>();
        for thread in threads {
            thread
                .join()
                .expect("namespace handoff thread")
                .expect("namespace handoff acquisition");
        }
        let state = KEY_CREATE_NAMESPACE
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        assert_eq!(state.references, 0);
        assert_eq!(state.handle, 0);
    }

    #[test]
    fn wrong_type_local_object_cannot_block_private_coordination() {
        let name = owned_name();
        delete_test_key(DEFAULT_PROVIDER_NAME, &name).expect("pre-clean squatting test key");
        let mutex_name = mutex_name(DEFAULT_PROVIDER_NAME, &name).expect("mutex name");
        let local_name = legacy_local_mutex_name(&mutex_name);
        // SAFETY: the generated name is live and NUL-terminated; default event security is used.
        let event = unsafe { CreateEventW(null(), 1, 0, local_name.as_ptr()) };
        assert!(!event.is_null(), "create wrong-type namespace object");
        // SAFETY: opening the squatted Local name as a mutex must fail with the wrong object type.
        let collision = unsafe { CreateMutexW(null(), 0, local_name.as_ptr()) };
        assert!(collision.is_null());
        // SAFETY: GetLastError immediately follows the failed CreateMutexW.
        assert_eq!(unsafe { GetLastError() }, ERROR_INVALID_HANDLE);

        let result = CngKey::open_or_create(DEFAULT_PROVIDER_NAME, &name, 2_048);
        // SAFETY: this test owns the event handle.
        unsafe {
            let _ = CloseHandle(event);
        }
        let key = result.expect("key creation must survive a wrong-type named object");
        assert_eq!(key.bits(), 2_048);
        drop(key);
        delete_test_key(DEFAULT_PROVIDER_NAME, &name).expect("delete squatting test key");
    }

    #[test]
    fn held_local_mutex_uses_private_coordinator_and_converges() {
        let name = owned_name();
        delete_test_key(DEFAULT_PROVIDER_NAME, &name).expect("pre-clean held-mutex test key");
        let mutex_name = mutex_name(DEFAULT_PROVIDER_NAME, &name).expect("mutex name");
        let local_name = legacy_local_mutex_name(&mutex_name);
        // SAFETY: creates and initially owns the deterministic legacy Local mutex.
        let held_mutex = unsafe { CreateMutexW(null(), 1, local_name.as_ptr()) };
        assert!(!held_mutex.is_null(), "hold legacy Local creation mutex");
        let barrier = Arc::new(std::sync::Barrier::new(5));
        let start = Instant::now();
        let threads = (0..4)
            .map(|_| {
                let barrier = Arc::clone(&barrier);
                let name = name.clone();
                std::thread::spawn(move || {
                    barrier.wait();
                    CngKey::open_or_create(DEFAULT_PROVIDER_NAME, &name, 2_048)
                })
            })
            .collect::<Vec<_>>();
        barrier.wait();

        let mut keys = Vec::new();
        for thread in threads {
            keys.push(thread.join().expect("held-mutex key thread"));
        }
        let elapsed = start.elapsed();
        // SAFETY: this thread initially owned the mutex and owns its handle.
        unsafe {
            let _ = ReleaseMutex(held_mutex);
            let _ = CloseHandle(held_mutex);
        }
        let result = (|| -> Result<(), CngError> {
            let keys = keys.into_iter().collect::<Result<Vec<_>, _>>()?;
            let modulus = keys[0].public_modulus().to_vec();
            let exponent = keys[0].public_exponent().to_vec();
            if !keys
                .iter()
                .all(|key| key.public_modulus() == modulus && key.public_exponent() == exponent)
            {
                return Err(CngError::detail(
                    "held-mutex fallback test",
                    "fallback callers opened different persisted keys",
                ));
            }
            drop(keys);
            let reopened = CngKey::open(DEFAULT_PROVIDER_NAME, &name)?;
            if reopened.public_modulus() != modulus || reopened.public_exponent() != exponent {
                return Err(CngError::detail(
                    "held-mutex fallback test",
                    "the finalized persisted key did not converge",
                ));
            }
            Ok(())
        })();
        delete_test_key(DEFAULT_PROVIDER_NAME, &name).expect("delete held-mutex test key");
        assert!(
            elapsed < Duration::from_millis(u64::from(KEY_CREATE_MUTEX_WAIT_MS)),
            "a held Local mutex blocked the private per-user coordinator"
        );
        result.expect("held Local mutex private coordination");
    }

    #[test]
    fn key_creation_mutex_name_is_exact_and_boundary_safe() {
        let name = mutex_name(DEFAULT_PROVIDER_NAME, "key-a").expect("mutex name");
        let repeated = mutex_name(DEFAULT_PROVIDER_NAME, "key-a").expect("repeated mutex name");
        let distinct = mutex_name(DEFAULT_PROVIDER_NAME, "key-b").expect("distinct mutex name");
        let boundary_a = mutex_name("provider-a", "key").expect("boundary A");
        let boundary_b = mutex_name("provider", "a-key").expect("boundary B");
        let composed = mutex_name(DEFAULT_PROVIDER_NAME, "\u{e9}").expect("composed");
        let decomposed = mutex_name(DEFAULT_PROVIDER_NAME, "e\u{301}").expect("decomposed");
        let lowercase = mutex_name("microsoft software key storage provider", "case-\u{e9}")
            .expect("lowercase mutex name");
        let uppercase = mutex_name("MICROSOFT SOFTWARE KEY STORAGE PROVIDER", "CASE-\u{c9}")
            .expect("uppercase mutex name");

        assert_eq!(name, repeated);
        assert_ne!(name, distinct);
        assert_ne!(boundary_a, boundary_b);
        assert_ne!(composed, decomposed);
        assert_eq!(lowercase, uppercase);
        assert_eq!(name.last(), Some(&0));
        assert!(
            name.iter()
                .zip(KEY_CREATE_MUTEX_PREFIX)
                .all(|(wide, byte)| *wide == u16::from(*byte))
        );
    }

    #[test]
    fn case_variant_mutex_serializes_across_processes() {
        let suffix = owned_name();
        let holder_key = format!("{suffix}-case-\u{e9}");
        let contender_key = format!("{}-CASE-\u{c9}", suffix.to_ascii_uppercase());
        run_case_variant_mutex_test(
            "microsoft software key storage provider",
            &holder_key,
            "MICROSOFT SOFTWARE KEY STORAGE PROVIDER",
            &contender_key,
        )
        .expect("case-variant cross-process mutex serialization");
    }

    #[test]
    fn deletion_rejects_non_test_names() {
        assert!(delete_test_key(DEFAULT_PROVIDER_NAME, "production-key").is_err());
    }
}
