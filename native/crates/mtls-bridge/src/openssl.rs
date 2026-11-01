use std::ffi::{CStr, CString};
use std::fs::File;
use std::io::Read;
use std::os::windows::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::ptr::{null, null_mut};

use openssl_provider_sys::{
    BIO_free, BIO_new_mem_buf, ERR_clear_error, ERR_error_string_n, ERR_peek_last_error, EVP_PKEY,
    EVP_PKEY_free, OSSL_LIB_CTX, OSSL_LIB_CTX_free, OSSL_LIB_CTX_new, OSSL_PROVIDER,
    OSSL_PROVIDER_add_builtin, OSSL_PROVIDER_load, OSSL_PROVIDER_unload, OSSL_STORE_INFO_PKEY,
    OSSL_STORE_INFO_free, OSSL_STORE_INFO_get_type, OSSL_STORE_INFO_get1_PKEY, OSSL_STORE_close,
    OSSL_STORE_eof, OSSL_STORE_error, OSSL_STORE_expect, OSSL_STORE_load, OSSL_STORE_open_ex,
    OsslProviderInitFn, PEM_read_bio_X509, PEM_read_bio_X509_AUX, SSL, SSL_CTX,
    SSL_CTX_add1_chain_cert, SSL_CTX_check_private_key, SSL_CTX_free, SSL_CTX_get_cert_store,
    SSL_CTX_new_ex, SSL_CTX_set_max_proto_version, SSL_CTX_set_min_proto_version,
    SSL_CTX_set_options, SSL_CTX_set_verify, SSL_CTX_set_verify_depth, SSL_CTX_use_PrivateKey,
    SSL_CTX_use_certificate, SSL_VERIFY_PEER, TLS_client_method, TLS1_2_VERSION, X509,
    X509_STORE_add_cert, X509_free, X509_new_ex,
};
use windows_sys::Win32::Foundation::{FreeLibrary, GetLastError, HMODULE};
use windows_sys::Win32::System::LibraryLoader::{
    GetProcAddress, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR, LOAD_LIBRARY_SEARCH_SYSTEM32, LoadLibraryExW,
};

use crate::abi::{BridgeFailure, ErrorCode};
use crate::http::ValidatedRequest;

const MAX_PEM_BYTES: usize = 4 * 1024 * 1024;
const CERTIFICATE_BEGIN: &[u8] = b"-----BEGIN CERTIFICATE-----";
const CERTIFICATE_END: &[u8] = b"-----END CERTIFICATE-----";
const SSL_OP_IGNORE_UNEXPECTED_EOF: u64 = 1 << 7;
const SSL_OP_NO_RENEGOTIATION: u64 = 1 << 30;

pub struct OpenSslState {
    libctx: *mut OSSL_LIB_CTX,
    default_provider: *mut OSSL_PROVIDER,
    cng_provider: *mut OSSL_PROVIDER,
    ssl_ctx: *mut SSL_CTX,
    pub ssl: *mut SSL,
    private_key: *mut EVP_PKEY,
    certificate: *mut X509,
    module: HMODULE,
}

impl OpenSslState {
    pub fn initialize(request: &ValidatedRequest) -> Result<Self, BridgeFailure> {
        let mut state = Self {
            libctx: null_mut(),
            default_provider: null_mut(),
            cng_provider: null_mut(),
            ssl_ctx: null_mut(),
            ssl: null_mut(),
            private_key: null_mut(),
            certificate: null_mut(),
            module: null_mut(),
        };
        let (provider_path, module_name) = normalize_provider_path(&request.provider_module_path)?;
        // SAFETY: creates an owned private OpenSSL library context.
        state.libctx = unsafe { OSSL_LIB_CTX_new() };
        if state.libctx.is_null() {
            return Err(openssl_failure(
                ErrorCode::Provider,
                "Creating the private OpenSSL context",
            ));
        }

        let provider_wide = try_wide_path(&provider_path)?;
        // SAFETY: the canonical UTF-16 path is NUL-terminated and retained for the call.
        state.module = unsafe {
            LoadLibraryExW(
                provider_wide.as_ptr(),
                null_mut(),
                LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32,
            )
        };
        if state.module.is_null() {
            return Err(BridgeFailure::system(
                ErrorCode::Provider,
                // SAFETY: GetLastError has no preconditions.
                unsafe { GetLastError() } as i32,
                "Loading the provider module by its UTF-8 path failed.",
            ));
        }
        // SAFETY: the module is live and the exported symbol name is NUL-terminated.
        let entry = unsafe { GetProcAddress(state.module, c"OSSL_provider_init".as_ptr().cast()) };
        let Some(entry) = entry else {
            return Err(BridgeFailure::system(
                ErrorCode::Provider,
                // SAFETY: GetLastError has no preconditions.
                unsafe { GetLastError() } as i32,
                "The provider module has no OSSL_provider_init export.",
            ));
        };
        // SAFETY: `GetProcAddress` returned the provider's required OSSL_provider_init export. On
        // Windows x64, extern "system" and extern "C" use the same ABI and function-pointer size.
        let init: OsslProviderInitFn = unsafe { std::mem::transmute(entry) };
        // SAFETY: the private libctx and module-name C string remain live; OpenSSL records the
        // process-lifetime function address while `state.module` is retained.
        if unsafe { OSSL_PROVIDER_add_builtin(state.libctx, module_name.as_ptr(), Some(init)) } != 1
        {
            return Err(openssl_failure(
                ErrorCode::Provider,
                "Registering the provider module",
            ));
        }
        // SAFETY: the private libctx is live and names are static/NUL-terminated.
        state.default_provider = unsafe { OSSL_PROVIDER_load(state.libctx, c"default".as_ptr()) };
        // SAFETY: same invariant, with the registered module name retained locally for the call.
        state.cng_provider = unsafe { OSSL_PROVIDER_load(state.libctx, module_name.as_ptr()) };
        if state.default_provider.is_null() || state.cng_provider.is_null() {
            return Err(openssl_failure(
                ErrorCode::Provider,
                "Loading the OpenSSL providers",
            ));
        }

        state.private_key = load_store_key(state.libctx, &request.store_uri)?;
        // SAFETY: the private libctx and TLS client method are live.
        state.ssl_ctx = unsafe { SSL_CTX_new_ex(state.libctx, null(), TLS_client_method()) };
        if state.ssl_ctx.is_null()
            // SAFETY: SSL_CTX is live and the control has no pointer argument.
            || !unsafe { SSL_CTX_set_min_proto_version(state.ssl_ctx, TLS1_2_VERSION) }
            // SAFETY: Key Vault's token-bound client-certificate path requires TLS 1.2.
            || unsafe { SSL_CTX_set_max_proto_version(state.ssl_ctx, TLS1_2_VERSION) } != 1
        {
            return Err(openssl_failure(
                ErrorCode::Certificate,
                "Creating the TLS client context",
            ));
        }
        // HTTP framing is enforced independently, so an omitted close_notify
        // cannot truncate a response that the bridge accepts as complete.
        // SAFETY: SSL_CTX is live and the option is supported by OpenSSL 3.
        unsafe {
            SSL_CTX_set_options(
                state.ssl_ctx,
                SSL_OP_IGNORE_UNEXPECTED_EOF | SSL_OP_NO_RENEGOTIATION,
            );
        }
        configure_ca_bundle(state.libctx, state.ssl_ctx, &request.ca_bundle_path)?;
        state.certificate = configure_client_certificate(
            state.libctx,
            state.ssl_ctx,
            &request.client_certificate_path,
        )?;
        // SAFETY: the SSL_CTX and provider-backed EVP_PKEY are live.
        if unsafe { SSL_CTX_use_PrivateKey(state.ssl_ctx, state.private_key) } != 1
            // SAFETY: the SSL_CTX has both its certificate and key configured.
            || unsafe { SSL_CTX_check_private_key(state.ssl_ctx) } != 1
        {
            return Err(openssl_failure(
                ErrorCode::Certificate,
                "Configuring the client certificate and key",
            ));
        }
        // SAFETY: the SSL_CTX is live; callback null requests standard verification.
        unsafe {
            SSL_CTX_set_verify(state.ssl_ctx, SSL_VERIFY_PEER, None);
            SSL_CTX_set_verify_depth(state.ssl_ctx, 6);
        }
        Ok(state)
    }

    pub fn ssl_context(&self) -> *mut SSL_CTX {
        self.ssl_ctx
    }
}

impl Drop for OpenSslState {
    fn drop(&mut self) {
        // SAFETY: each non-null handle is owned by this state and released once in dependency
        // order. The thread error queue is cleared before provider-owned code is unloaded.
        unsafe {
            if !self.ssl.is_null() {
                openssl_provider_sys::SSL_free(self.ssl);
            }
            if !self.ssl_ctx.is_null() {
                SSL_CTX_free(self.ssl_ctx);
            }
            if !self.certificate.is_null() {
                X509_free(self.certificate);
            }
            if !self.private_key.is_null() {
                EVP_PKEY_free(self.private_key);
            }
            ERR_clear_error();
            if !self.cng_provider.is_null() {
                let _ = OSSL_PROVIDER_unload(self.cng_provider);
            }
            if !self.default_provider.is_null() {
                let _ = OSSL_PROVIDER_unload(self.default_provider);
            }
            if !self.libctx.is_null() {
                OSSL_LIB_CTX_free(self.libctx);
            }
            ERR_clear_error();
            if !self.module.is_null() {
                let _ = FreeLibrary(self.module);
            }
        }
    }
}

fn normalize_provider_path(input: &str) -> Result<(PathBuf, CString), BridgeFailure> {
    let path = Path::new(input);
    let full_path = path.canonicalize().map_err(|error| {
        BridgeFailure::system(
            ErrorCode::Provider,
            error.raw_os_error().unwrap_or_default(),
            "The provider module path cannot be normalized.",
        )
    })?;
    if !full_path.is_file() {
        return Err(BridgeFailure::new(
            ErrorCode::Provider,
            "The provider module path is not a file.",
        ));
    }
    let filename = full_path
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| {
            BridgeFailure::new(
                ErrorCode::Provider,
                "The provider module name is not valid UTF-8.",
            )
        })?;
    let module_name = filename
        .len()
        .checked_sub(4)
        .and_then(|offset| filename.get(offset..).map(|suffix| (offset, suffix)))
        .filter(|(_, suffix)| suffix.eq_ignore_ascii_case(".dll"))
        .map_or(filename, |(offset, _)| &filename[..offset]);
    if module_name.is_empty() || module_name.len() >= 256 {
        return Err(BridgeFailure::new(
            ErrorCode::Provider,
            "The provider module name is too long.",
        ));
    }
    if module_name.as_bytes().contains(&0) {
        return Err(BridgeFailure::new(
            ErrorCode::Provider,
            "The provider module name contains a NUL character.",
        ));
    }
    let module_name = try_cstring(module_name)?;
    Ok((full_path, module_name))
}

fn load_store_key(libctx: *mut OSSL_LIB_CTX, uri: &str) -> Result<*mut EVP_PKEY, BridgeFailure> {
    if uri.as_bytes().contains(&0) {
        return Err(BridgeFailure::new(
            ErrorCode::Store,
            "The CNG STORE URI contains a NUL character.",
        ));
    }
    let uri = try_cstring(uri)?;
    // SAFETY: all C strings and the private libctx are live; optional callbacks/params are null.
    let store = unsafe {
        OSSL_STORE_open_ex(
            uri.as_ptr(),
            libctx,
            c"provider=microsoft_identity_native_provider".as_ptr(),
            null(),
            null_mut(),
            null(),
            None,
            null_mut(),
        )
    };
    if store.is_null()
        // SAFETY: store is checked before this call.
        || unsafe { OSSL_STORE_expect(store, OSSL_STORE_INFO_PKEY) } != 1
    {
        if !store.is_null() {
            // SAFETY: store is owned by this function.
            unsafe {
                let _ = OSSL_STORE_close(store);
            }
        }
        return Err(openssl_failure(
            ErrorCode::Store,
            "Opening the CNG STORE URI",
        ));
    }

    let mut key = null_mut();
    loop {
        // SAFETY: store remains live until the end of this function.
        if unsafe { OSSL_STORE_eof(store) } != 0 {
            break;
        }
        // SAFETY: store remains live and returns an owned info object or null.
        let info = unsafe { OSSL_STORE_load(store) };
        if info.is_null() {
            // SAFETY: store remains live.
            if unsafe { OSSL_STORE_error(store) } != 0 {
                // SAFETY: store is owned here.
                unsafe {
                    let _ = OSSL_STORE_close(store);
                }
                return Err(openssl_failure(
                    ErrorCode::Store,
                    "Loading the CNG STORE key",
                ));
            }
            break;
        }
        // SAFETY: info is a live OSSL_STORE_INFO.
        if unsafe { OSSL_STORE_INFO_get_type(info) } == OSSL_STORE_INFO_PKEY {
            // SAFETY: info is live; get1 returns an independently referenced EVP_PKEY.
            key = unsafe { OSSL_STORE_INFO_get1_PKEY(info) };
            // SAFETY: info is owned here.
            unsafe { OSSL_STORE_INFO_free(info) };
            break;
        }
        // SAFETY: info is owned here.
        unsafe { OSSL_STORE_INFO_free(info) };
    }
    // SAFETY: store is owned here.
    unsafe {
        let _ = OSSL_STORE_close(store);
    }
    if key.is_null() {
        Err(BridgeFailure::new(
            ErrorCode::Store,
            "The CNG STORE URI returned no private key.",
        ))
    } else {
        Ok(key)
    }
}

fn read_pem_file(path: &str, description: &str) -> Result<Vec<u8>, BridgeFailure> {
    let mut file = File::open(path).map_err(|error| {
        BridgeFailure::system_formatted(
            ErrorCode::Certificate,
            error.raw_os_error().unwrap_or_default(),
            format_args!("Unable to open the {description}."),
        )
    })?;
    let size = file.metadata().map_err(|error| {
        BridgeFailure::system_formatted(
            ErrorCode::Certificate,
            error.raw_os_error().unwrap_or_default(),
            format_args!("Unable to inspect the {description}."),
        )
    })?;
    let Ok(size) = usize::try_from(size.len()) else {
        return Err(BridgeFailure::formatted(
            ErrorCode::Certificate,
            format_args!("The {description} must be between 1 byte and 4 MiB."),
        ));
    };
    if size == 0 || size > MAX_PEM_BYTES {
        return Err(BridgeFailure::formatted(
            ErrorCode::Certificate,
            format_args!("The {description} must be between 1 byte and 4 MiB."),
        ));
    }
    let mut contents = Vec::new();
    contents
        .try_reserve_exact(size)
        .map_err(|_| BridgeFailure::memory())?;
    contents.resize(size, 0);
    file.read_exact(&mut contents).map_err(|error| {
        BridgeFailure::system_formatted(
            ErrorCode::Certificate,
            error.raw_os_error().unwrap_or_default(),
            format_args!("Unable to read the {description}."),
        )
    })?;
    let mut extra = [0_u8; 1];
    if file.read(&mut extra).map_err(|error| {
        BridgeFailure::system_formatted(
            ErrorCode::Certificate,
            error.raw_os_error().unwrap_or_default(),
            format_args!("Unable to read the {description}."),
        )
    })? != 0
    {
        return Err(BridgeFailure::formatted(
            ErrorCode::Certificate,
            format_args!("The {description} changed while it was being read."),
        ));
    }
    Ok(contents)
}

fn configure_client_certificate(
    libctx: *mut OSSL_LIB_CTX,
    ssl_ctx: *mut SSL_CTX,
    path: &str,
) -> Result<*mut X509, BridgeFailure> {
    let contents = read_pem_file(path, "client certificate PEM file")?;
    validate_certificate_pem(&contents, "client certificate PEM file")?;
    let mut cursor = 0;
    let leaf = next_certificate_pem_block(&contents, &mut cursor, "client certificate PEM file")?
        .expect("validated client certificate contains a leaf");
    let certificate = parse_x509_block(libctx, leaf, true, "Loading the client certificate")?;
    if
    // SAFETY: both the SSL_CTX and certificate are live when non-null.
    unsafe { SSL_CTX_use_certificate(ssl_ctx, certificate) } != 1 {
        // SAFETY: the local certificate reference is owned here.
        unsafe { X509_free(certificate) };
        return Err(openssl_failure(
            ErrorCode::Certificate,
            "Loading the client certificate",
        ));
    }

    while let Some(block) =
        next_certificate_pem_block(&contents, &mut cursor, "client certificate PEM file")?
    {
        let chain =
            match parse_x509_block(libctx, block, false, "Loading the client certificate chain") {
                Ok(chain) => chain,
                Err(error) => {
                    // SAFETY: the local leaf reference is owned here.
                    unsafe { X509_free(certificate) };
                    return Err(error);
                }
            };
        // SAFETY: SSL_CTX and chain are live; add1 increments the chain certificate reference.
        let installed = unsafe { SSL_CTX_add1_chain_cert(ssl_ctx, chain) };
        // SAFETY: local chain reference is owned here after add1.
        unsafe { X509_free(chain) };
        if !installed {
            // SAFETY: the local leaf reference is owned here.
            unsafe { X509_free(certificate) };
            return Err(openssl_failure(
                ErrorCode::Certificate,
                "Installing the client certificate chain",
            ));
        }
    }
    Ok(certificate)
}

fn configure_ca_bundle(
    libctx: *mut OSSL_LIB_CTX,
    ssl_ctx: *mut SSL_CTX,
    path: &str,
) -> Result<(), BridgeFailure> {
    let contents = read_pem_file(path, "CA bundle PEM file")?;
    validate_certificate_pem(&contents, "CA bundle PEM file")?;
    // SAFETY: SSL_CTX is live.
    let store = unsafe { SSL_CTX_get_cert_store(ssl_ctx) };
    if store.is_null() {
        return Err(openssl_failure(
            ErrorCode::Certificate,
            "Opening the CA bundle",
        ));
    }
    let mut cursor = 0;
    while let Some(block) =
        next_certificate_pem_block(&contents, &mut cursor, "CA bundle PEM file")?
    {
        let certificate = parse_x509_block(libctx, block, true, "Loading the CA bundle")?;
        // SAFETY: store and certificate are live; X509_STORE_add_cert retains the certificate.
        let installed = unsafe { X509_STORE_add_cert(store, certificate) };
        // SAFETY: local certificate reference is owned here.
        unsafe { X509_free(certificate) };
        if installed != 1 {
            return Err(openssl_failure(
                ErrorCode::Certificate,
                "Installing the CA bundle",
            ));
        }
    }
    Ok(())
}

fn validate_certificate_pem(contents: &[u8], description: &str) -> Result<usize, BridgeFailure> {
    let mut cursor = 0;
    let mut count = 0;
    while next_certificate_pem_block(contents, &mut cursor, description)?.is_some() {
        count += 1;
    }
    if count == 0 {
        return Err(BridgeFailure::formatted(
            ErrorCode::Certificate,
            format_args!("The {description} contains no complete CERTIFICATE PEM block."),
        ));
    }
    Ok(count)
}

fn next_certificate_pem_block<'a>(
    contents: &'a [u8],
    cursor: &mut usize,
    description: &str,
) -> Result<Option<&'a [u8]>, BridgeFailure> {
    while *cursor < contents.len() && is_pem_whitespace(contents[*cursor]) {
        *cursor += 1;
    }
    if *cursor == contents.len() {
        return Ok(None);
    }
    let start = *cursor;
    if !contents[start..].starts_with(CERTIFICATE_BEGIN) {
        return Err(malformed_pem(description));
    }
    let body_start = start + CERTIFICATE_BEGIN.len();
    let Some(relative_end) = crate::http::find_bytes(&contents[body_start..], CERTIFICATE_END)
    else {
        return Err(malformed_pem(description));
    };
    let end = body_start + relative_end + CERTIFICATE_END.len();
    *cursor = end;
    Ok(Some(&contents[start..end]))
}

fn is_pem_whitespace(value: u8) -> bool {
    matches!(value, b' ' | b'\t' | b'\r' | b'\n')
}

fn malformed_pem(description: &str) -> BridgeFailure {
    BridgeFailure::formatted(
        ErrorCode::Certificate,
        format_args!(
            "The {description} must contain only whitespace and complete CERTIFICATE PEM blocks."
        ),
    )
}

fn parse_x509_block(
    libctx: *mut OSSL_LIB_CTX,
    block: &[u8],
    auxiliary: bool,
    operation: &str,
) -> Result<*mut X509, BridgeFailure> {
    // SAFETY: the block remains live for the complete BIO lifetime.
    let bio = unsafe { BIO_new_mem_buf(block.as_ptr().cast(), block.len() as i32) };
    if bio.is_null() {
        return Err(openssl_failure(ErrorCode::Certificate, operation));
    }
    // SAFETY: creates an owned empty X509 bound to the bridge's private library context.
    let mut certificate = unsafe { X509_new_ex(libctx, null()) };
    if certificate.is_null() {
        // SAFETY: bio is owned here.
        unsafe {
            let _ = BIO_free(bio);
        }
        return Err(openssl_failure(ErrorCode::Certificate, operation));
    }
    // SAFETY: clears only the current thread's OpenSSL error queue before parsing this block.
    unsafe { ERR_clear_error() };
    // SAFETY: bio and the preallocated private-context X509 are live; optional callbacks are null.
    let parsed = unsafe {
        if auxiliary {
            PEM_read_bio_X509_AUX(bio, &raw mut certificate, None, null_mut())
        } else {
            PEM_read_bio_X509(bio, &raw mut certificate, None, null_mut())
        }
    };
    if parsed.is_null() {
        let failure = openssl_failure(ErrorCode::Certificate, operation);
        // SAFETY: on failure, any non-null value retained in the output slot is owned here.
        unsafe {
            if !certificate.is_null() {
                X509_free(certificate);
            }
            let _ = BIO_free(bio);
        }
        return Err(failure);
    }
    debug_assert_eq!(parsed, certificate);
    // SAFETY: bio is owned here; the parsed X509 is returned to the caller.
    unsafe {
        let _ = BIO_free(bio);
    }
    Ok(parsed)
}

pub fn openssl_failure(code: ErrorCode, operation: &str) -> BridgeFailure {
    // SAFETY: OpenSSL error queue access has no preconditions.
    let openssl_error = unsafe { ERR_peek_last_error() } as u32;
    let mut detail = [0_i8; 256];
    if openssl_error != 0 {
        // SAFETY: detail is writable and the error code came from OpenSSL.
        unsafe { ERR_error_string_n(openssl_error, detail.as_mut_ptr(), detail.len()) };
    }
    let detail = if detail[0] == 0 {
        None
    } else {
        // SAFETY: ERR_error_string_n always NUL-terminates a non-empty destination.
        unsafe { CStr::from_ptr(detail.as_ptr()) }.to_str().ok()
    };
    match detail {
        Some(detail) => BridgeFailure::openssl_formatted(
            code,
            openssl_error,
            format_args!("{operation} failed: {detail}."),
        ),
        None => BridgeFailure::openssl_formatted(
            code,
            openssl_error,
            format_args!("{operation} failed."),
        ),
    }
}

fn try_wide_path(path: &Path) -> Result<Vec<u16>, BridgeFailure> {
    let length = path
        .as_os_str()
        .encode_wide()
        .count()
        .checked_add(1)
        .ok_or_else(BridgeFailure::memory)?;
    let mut output = Vec::new();
    output
        .try_reserve_exact(length)
        .map_err(|_| BridgeFailure::memory())?;
    output.extend(path.as_os_str().encode_wide());
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

#[cfg(test)]
mod tests {
    use super::*;
    use openssl_provider_sys::{EVP_set_default_properties, X509_get_pubkey};
    use std::sync::Mutex;

    const TEST_CERTIFICATE: &[u8] = b"-----BEGIN CERTIFICATE-----\n\
MIIDSTCCAjGgAwIBAgIBATANBgkqhkiG9w0BAQsFADBGMSAwHgYDVQQKDBdPcGVu\n\
U1NMIENORyBsb2NhbCB0ZXN0czEiMCAGA1UEAwwZT3BlblNTTCBDTkcgTG9jYWwg\n\
VGVzdCBDQTAeFw0yNjA4MTcyMjUyMjFaFw0yNjA4MTgyMjUzMjFaMEYxIDAeBgNV\n\
BAoMF09wZW5TU0wgQ05HIGxvY2FsIHRlc3RzMSIwIAYDVQQDDBlPcGVuU1NMIENO\n\
RyBMb2NhbCBUZXN0IENBMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA\n\
smN1o5KqmX4TPBqJ4D2HnnCDP9ivAbZ4KCQK3Fay1IdjZaCr62FCa/gH1/XsC6Za\n\
N2mrXf5QsZjkWc/zc7bmAOJ+FiZ3v5Jk9hTdWwPsz9lByrhXSGShVwjZjrf+Rf2l\n\
q5+TX8TGaq/pHAYbyMw2PcK6rhpCsmur+cCl7iCRWMxrTdhWPT6IXhx6j27D8az2\n\
9MM4sDi1Cazh/VHel17MsJ6WvORRb/t8/2ZI/SzF1UdlGZyqgpapoSZIEgNN6pSg\n\
vMtkMDiir+jOy3IDBbezi8Cm+320zvQk05SVIc+9JofGwsVgAa7Cf9KFM8Qy6DvW\n\
g6YldC+MRVescNwMF/g8TQIDAQABo0IwQDAPBgNVHRMBAf8EBTADAQH/MA4GA1Ud\n\
DwEB/wQEAwIBBjAdBgNVHQ4EFgQUA4r//P0QtcpZqoHHwFEK14J3u9IwDQYJKoZI\n\
hvcNAQELBQADggEBADPCB63zWvoHQrzzyPVrXV5rdkwDuN8sDFCS6N/nWuumnrZ3\n\
l5jQBbmTLU7GoTA7tPIRAFFH+8V0e1ws0vABew18gNgRQt23QZLMbKg1QJkOfirq\n\
pdq9hHnjNDsS1fcDCKc4joghIRWZIW1FKjQVXlhCX+nQWuSTi/bGPQ4WcRGEhqXE\n\
MQt8uvmOdaPTJUbMV1gy0NqwjypirCAwfZRpz7k5x7RsuftwcpvZpy+HTVIEqAj+\n\
sO1+939usuNfkDTQdpfUTWZY8Fa+1aGR8M+0R4FdW8XcF6lnExf2TDWBEbarnfBT\n\
MkbjF87zqe14SXsBFriIJlRgbWEbFjC5zLG3ceY=\n\
-----END CERTIFICATE-----\n";

    static GLOBAL_PROPERTIES_LOCK: Mutex<()> = Mutex::new(());

    struct PrivateContext {
        libctx: *mut OSSL_LIB_CTX,
        provider: *mut OSSL_PROVIDER,
    }

    impl PrivateContext {
        fn new() -> Self {
            // SAFETY: creates an owned private OpenSSL library context.
            let libctx = unsafe { OSSL_LIB_CTX_new() };
            assert!(!libctx.is_null());
            // SAFETY: the private context is live and the provider name is static.
            let provider = unsafe { OSSL_PROVIDER_load(libctx, c"default".as_ptr()) };
            assert!(!provider.is_null());
            Self { libctx, provider }
        }
    }

    impl Drop for PrivateContext {
        fn drop(&mut self) {
            // SAFETY: both handles are owned by this test context.
            unsafe {
                let _ = OSSL_PROVIDER_unload(self.provider);
                OSSL_LIB_CTX_free(self.libctx);
            }
        }
    }

    struct GlobalPropertiesReset;

    impl Drop for GlobalPropertiesReset {
        fn drop(&mut self) {
            // SAFETY: a null property query restores the global context defaults.
            unsafe {
                let _ = EVP_set_default_properties(null_mut(), null());
            }
        }
    }

    #[test]
    fn strict_pem_validation_rejects_trailing_malformed_data_for_all_inputs() {
        for description in ["client certificate PEM file", "CA bundle PEM file"] {
            let mut incomplete = TEST_CERTIFICATE.to_vec();
            incomplete.extend_from_slice(b"\n-----BEGIN CERTIFICATE-----\n");
            assert_eq!(
                validate_certificate_pem(&incomplete, description)
                    .expect_err("incomplete trailing PEM must fail")
                    .code,
                ErrorCode::Certificate
            );

            let mut garbage = TEST_CERTIFICATE.to_vec();
            garbage.extend_from_slice(b"\nnot-a-certificate");
            assert_eq!(
                validate_certificate_pem(&garbage, description)
                    .expect_err("trailing garbage must fail")
                    .code,
                ErrorCode::Certificate
            );
        }
    }

    #[test]
    fn valid_multi_certificate_pem_is_complete_and_parseable() {
        let context = PrivateContext::new();
        let mut bundle = b" \r\n".to_vec();
        bundle.extend_from_slice(TEST_CERTIFICATE);
        bundle.extend_from_slice(b"\n\t");
        bundle.extend_from_slice(TEST_CERTIFICATE);
        bundle.extend_from_slice(b"\r\n");
        assert_eq!(
            validate_certificate_pem(&bundle, "test certificate bundle")
                .expect("valid multi-certificate PEM"),
            2
        );

        let mut cursor = 0;
        let mut parsed = 0;
        while let Some(block) =
            next_certificate_pem_block(&bundle, &mut cursor, "test certificate bundle")
                .expect("validated PEM iteration")
        {
            let certificate = parse_x509_block(
                context.libctx,
                block,
                parsed == 0,
                "Parsing a private-context certificate",
            )
            .expect("parse complete certificate block");
            // SAFETY: each parsed certificate is owned by this test.
            unsafe { X509_free(certificate) };
            parsed += 1;
        }
        assert_eq!(parsed, 2);
    }

    #[test]
    fn private_libctx_certificate_parsing_ignores_hostile_global_properties() {
        let _lock = GLOBAL_PROPERTIES_LOCK.lock().expect("global property lock");
        let context = PrivateContext::new();
        // SAFETY: installs a deliberately unsatisfiable global provider property query.
        let configured =
            unsafe { EVP_set_default_properties(null_mut(), c"provider=null".as_ptr()) };
        assert_eq!(configured, 1);
        let _reset = GlobalPropertiesReset;

        let certificate = parse_x509_block(
            context.libctx,
            TEST_CERTIFICATE,
            true,
            "Parsing with a hostile global property query",
        )
        .expect("private-context X509 parsing");
        // SAFETY: the certificate is live and retains the private library context.
        let public_key = unsafe { X509_get_pubkey(certificate) };
        assert!(
            !public_key.is_null(),
            "public-key decoding must use the private context"
        );
        // SAFETY: both returned OpenSSL objects are owned by this test.
        unsafe {
            EVP_PKEY_free(public_key);
            X509_free(certificate);
        }
    }
}
