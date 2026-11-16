#![allow(
    hidden_glob_reexports,
    non_camel_case_types,
    non_snake_case,
    non_upper_case_globals
)]

use std::ffi::{c_char, c_int, c_long, c_uint, c_ulong, c_void};
use std::ptr::{null, null_mut};

pub use openssl_sys::*;
use static_assertions::const_assert_eq;

pub enum OsslCoreHandle {}
pub enum OsslStoreCtx {}
pub enum OsslStoreInfo {}
pub enum UiMethod {}
pub enum EvpSignature {}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct OsslDispatch {
    pub function_id: c_int,
    pub function: *const c_void,
}

// SAFETY: dispatch tables are immutable process-lifetime arrays of function addresses.
unsafe impl Sync for OsslDispatch {}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct OsslItem {
    pub id: c_uint,
    pub ptr: *mut c_void,
}

// SAFETY: reason-string tables are immutable process-lifetime arrays.
unsafe impl Sync for OsslItem {}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct OsslAlgorithm {
    pub algorithm_names: *const c_char,
    pub property_definition: *const c_char,
    pub implementation: *const OsslDispatch,
    pub algorithm_description: *const c_char,
}

// SAFETY: algorithm tables point only at immutable process-lifetime strings and dispatch arrays.
unsafe impl Sync for OsslAlgorithm {}

pub type OsslCallback =
    Option<unsafe extern "C" fn(params: *const OSSL_PARAM, argument: *mut c_void) -> c_int>;
pub type OsslProviderInitFn = unsafe extern "C" fn(
    handle: *const OsslCoreHandle,
    input: *const OsslDispatch,
    output: *mut *const OsslDispatch,
    provider_context: *mut *mut c_void,
) -> c_int;

const_assert_eq!(size_of::<OsslDispatch>(), 16);
const_assert_eq!(align_of::<OsslDispatch>(), 8);
const_assert_eq!(size_of::<OsslItem>(), 16);
const_assert_eq!(size_of::<OsslAlgorithm>(), 32);
const_assert_eq!(size_of::<OSSL_PARAM>(), 40);

pub const OSSL_PARAM_INTEGER: c_uint = 1;
pub const OSSL_PARAM_UNSIGNED_INTEGER: c_uint = 2;
pub const OSSL_PARAM_UTF8_STRING: c_uint = 4;
pub const OSSL_PARAM_OCTET_STRING: c_uint = 5;
pub const OSSL_PARAM_UTF8_PTR: c_uint = 6;

pub const OSSL_OP_KEYMGMT: c_int = 10;
pub const OSSL_OP_SIGNATURE: c_int = 12;
pub const OSSL_OP_STORE: c_int = 22;

pub const OSSL_KEYMGMT_SELECT_PRIVATE_KEY: c_int = 0x01;
pub const OSSL_KEYMGMT_SELECT_PUBLIC_KEY: c_int = 0x02;
pub const OSSL_KEYMGMT_SELECT_KEYPAIR: c_int =
    OSSL_KEYMGMT_SELECT_PRIVATE_KEY | OSSL_KEYMGMT_SELECT_PUBLIC_KEY;
pub const OSSL_KEYMGMT_VALIDATE_FULL_CHECK: c_int = 0;
pub const OSSL_KEYMGMT_VALIDATE_QUICK_CHECK: c_int = 1;

pub const OSSL_OBJECT_PKEY: c_int = 2;
pub const OSSL_STORE_INFO_PUBKEY: c_int = 3;
pub const OSSL_STORE_INFO_PKEY: c_int = 4;

pub const OSSL_FUNC_CORE_NEW_ERROR: c_int = 5;
pub const OSSL_FUNC_CORE_SET_ERROR_DEBUG: c_int = 6;
pub const OSSL_FUNC_CORE_VSET_ERROR: c_int = 7;

pub const OSSL_FUNC_PROVIDER_TEARDOWN: c_int = 1024;
pub const OSSL_FUNC_PROVIDER_GETTABLE_PARAMS: c_int = 1025;
pub const OSSL_FUNC_PROVIDER_GET_PARAMS: c_int = 1026;
pub const OSSL_FUNC_PROVIDER_QUERY_OPERATION: c_int = 1027;
pub const OSSL_FUNC_PROVIDER_GET_REASON_STRINGS: c_int = 1029;

pub const OSSL_FUNC_KEYMGMT_NEW: c_int = 1;
pub const OSSL_FUNC_KEYMGMT_LOAD: c_int = 8;
pub const OSSL_FUNC_KEYMGMT_FREE: c_int = 10;
pub const OSSL_FUNC_KEYMGMT_GET_PARAMS: c_int = 11;
pub const OSSL_FUNC_KEYMGMT_GETTABLE_PARAMS: c_int = 12;
pub const OSSL_FUNC_KEYMGMT_QUERY_OPERATION_NAME: c_int = 20;
pub const OSSL_FUNC_KEYMGMT_HAS: c_int = 21;
pub const OSSL_FUNC_KEYMGMT_VALIDATE: c_int = 22;
pub const OSSL_FUNC_KEYMGMT_MATCH: c_int = 23;
pub const OSSL_FUNC_KEYMGMT_IMPORT: c_int = 40;
pub const OSSL_FUNC_KEYMGMT_IMPORT_TYPES: c_int = 41;
pub const OSSL_FUNC_KEYMGMT_EXPORT: c_int = 42;
pub const OSSL_FUNC_KEYMGMT_EXPORT_TYPES: c_int = 43;
pub const OSSL_FUNC_KEYMGMT_DUP: c_int = 44;

pub const OSSL_FUNC_SIGNATURE_NEWCTX: c_int = 1;
pub const OSSL_FUNC_SIGNATURE_DIGEST_SIGN_INIT: c_int = 8;
pub const OSSL_FUNC_SIGNATURE_DIGEST_SIGN_UPDATE: c_int = 9;
pub const OSSL_FUNC_SIGNATURE_DIGEST_SIGN_FINAL: c_int = 10;
pub const OSSL_FUNC_SIGNATURE_DIGEST_SIGN: c_int = 11;
pub const OSSL_FUNC_SIGNATURE_FREECTX: c_int = 16;
pub const OSSL_FUNC_SIGNATURE_DUPCTX: c_int = 17;
pub const OSSL_FUNC_SIGNATURE_GET_CTX_PARAMS: c_int = 18;
pub const OSSL_FUNC_SIGNATURE_GETTABLE_CTX_PARAMS: c_int = 19;
pub const OSSL_FUNC_SIGNATURE_SET_CTX_PARAMS: c_int = 20;
pub const OSSL_FUNC_SIGNATURE_SETTABLE_CTX_PARAMS: c_int = 21;

pub const OSSL_FUNC_STORE_OPEN: c_int = 1;
pub const OSSL_FUNC_STORE_SETTABLE_CTX_PARAMS: c_int = 3;
pub const OSSL_FUNC_STORE_SET_CTX_PARAMS: c_int = 4;
pub const OSSL_FUNC_STORE_LOAD: c_int = 5;
pub const OSSL_FUNC_STORE_EOF: c_int = 6;
pub const OSSL_FUNC_STORE_CLOSE: c_int = 7;
pub const OSSL_FUNC_STORE_EXPORT_OBJECT: c_int = 8;

pub const RSA_PKCS1_PADDING: c_int = 1;
pub const RSA_PKCS1_PSS_PADDING: c_int = 6;
pub const RSA_PSS_SALTLEN_DIGEST: c_int = -1;
pub const RSA_PSS_SALTLEN_AUTO: c_int = -2;
pub const RSA_PSS_SALTLEN_MAX: c_int = -3;
pub const RSA_PSS_SALTLEN_AUTO_DIGEST_MAX: c_int = -4;

pub const X509_CHECK_FLAG_NO_PARTIAL_WILDCARDS: c_uint = 0x4;
pub const MBSTRING_ASC: c_int = 0x1001;

pub const ERR_LIB_USER: c_int = 128;

#[must_use]
pub const fn param_end() -> OSSL_PARAM {
    OSSL_PARAM {
        key: null(),
        data_type: 0,
        data: null_mut(),
        data_size: 0,
        return_size: 0,
    }
}

#[must_use]
pub const fn param_definition(key: *const c_char, data_type: c_uint) -> OSSL_PARAM {
    OSSL_PARAM {
        key,
        data_type,
        data: null_mut(),
        data_size: 0,
        return_size: 0,
    }
}

#[must_use]
pub const fn param_int(key: *const c_char, value: *mut c_int) -> OSSL_PARAM {
    OSSL_PARAM {
        key,
        data_type: OSSL_PARAM_INTEGER,
        data: value.cast(),
        data_size: size_of::<c_int>(),
        return_size: usize::MAX,
    }
}

#[must_use]
pub const fn param_utf8_string(
    key: *const c_char,
    value: *mut c_char,
    length: usize,
) -> OSSL_PARAM {
    OSSL_PARAM {
        key,
        data_type: OSSL_PARAM_UTF8_STRING,
        data: value.cast(),
        data_size: length,
        return_size: usize::MAX,
    }
}

#[must_use]
pub const fn param_octet_string(
    key: *const c_char,
    value: *mut c_void,
    length: usize,
) -> OSSL_PARAM {
    OSSL_PARAM {
        key,
        data_type: OSSL_PARAM_OCTET_STRING,
        data: value,
        data_size: length,
        return_size: usize::MAX,
    }
}

unsafe extern "C" {
    pub fn OSSL_PROVIDER_add_builtin(
        context: *mut OSSL_LIB_CTX,
        name: *const c_char,
        init: Option<OsslProviderInitFn>,
    ) -> c_int;

    pub fn OSSL_STORE_open_ex(
        uri: *const c_char,
        context: *mut OSSL_LIB_CTX,
        properties: *const c_char,
        ui_method: *const UiMethod,
        ui_data: *mut c_void,
        params: *const OSSL_PARAM,
        post_process: Option<
            unsafe extern "C" fn(info: *mut OsslStoreInfo, data: *mut c_void) -> *mut OsslStoreInfo,
        >,
        post_process_data: *mut c_void,
    ) -> *mut OsslStoreCtx;
    pub fn OSSL_STORE_expect(context: *mut OsslStoreCtx, expected_type: c_int) -> c_int;
    pub fn OSSL_STORE_load(context: *mut OsslStoreCtx) -> *mut OsslStoreInfo;
    pub fn OSSL_STORE_eof(context: *mut OsslStoreCtx) -> c_int;
    pub fn OSSL_STORE_error(context: *mut OsslStoreCtx) -> c_int;
    pub fn OSSL_STORE_close(context: *mut OsslStoreCtx) -> c_int;
    pub fn OSSL_STORE_INFO_get_type(info: *const OsslStoreInfo) -> c_int;
    pub fn OSSL_STORE_INFO_get1_PKEY(info: *const OsslStoreInfo) -> *mut EVP_PKEY;
    pub fn OSSL_STORE_INFO_free(info: *mut OsslStoreInfo);

    pub fn SSL_CTX_new_ex(
        context: *mut OSSL_LIB_CTX,
        properties: *const c_char,
        method: *const SSL_METHOD,
    ) -> *mut SSL_CTX;
    pub fn SSL_set_fd(ssl: *mut SSL, descriptor: c_int) -> c_int;
    pub fn SSL_set1_host(ssl: *mut SSL, host: *const c_char) -> c_int;

    pub fn BIO_free(bio: *mut BIO) -> c_int;
    pub fn PEM_read_bio_X509_AUX(
        bio: *mut BIO,
        output: *mut *mut X509,
        callback: openssl_sys::pem_password_cb,
        user_data: *mut c_void,
    ) -> *mut X509;
    pub fn X509_new_ex(context: *mut OSSL_LIB_CTX, properties: *const c_char) -> *mut X509;
    pub fn X509_get_subject_name(certificate: *const X509) -> *mut X509_NAME;
    pub fn X509_NAME_add_entry_by_txt(
        name: *mut X509_NAME,
        field: *const c_char,
        field_type: c_int,
        bytes: *const u8,
        length: c_int,
        location: c_int,
        set: c_int,
    ) -> c_int;
    pub fn X509_VERIFY_PARAM_set_hostflags(parameter: *mut X509_VERIFY_PARAM, flags: c_uint);
    pub fn X509_VERIFY_PARAM_get_hostflags(parameter: *const X509_VERIFY_PARAM) -> c_uint;
    pub fn X509_VERIFY_PARAM_set1_ip_asc(
        parameter: *mut X509_VERIFY_PARAM,
        address: *const c_char,
    ) -> c_int;
    pub fn X509_VERIFY_PARAM_get0_host(
        parameter: *mut X509_VERIFY_PARAM,
        index: c_int,
    ) -> *mut c_char;
    pub fn X509_check_host(
        certificate: *mut X509,
        name: *const c_char,
        name_length: usize,
        flags: c_uint,
        peer_name: *mut *mut c_char,
    ) -> c_int;
    pub fn EVP_set_default_properties(
        context: *mut OSSL_LIB_CTX,
        properties: *const c_char,
    ) -> c_int;
    pub fn EVP_SIGNATURE_fetch(
        context: *mut OSSL_LIB_CTX,
        algorithm: *const c_char,
        properties: *const c_char,
    ) -> *mut EvpSignature;
    pub fn EVP_SIGNATURE_gettable_ctx_params(signature: *const EvpSignature) -> *const OSSL_PARAM;
    pub fn EVP_SIGNATURE_settable_ctx_params(signature: *const EvpSignature) -> *const OSSL_PARAM;
    pub fn EVP_SIGNATURE_free(signature: *mut EvpSignature);
    pub fn ERR_error_string_n(error: c_ulong, buffer: *mut c_char, length: usize);
    pub fn OSSL_PARAM_set_BN(parameter: *mut OSSL_PARAM, value: *const BIGNUM) -> c_int;
}

#[must_use]
pub unsafe fn SSL_CTX_set_min_proto_version(context: *mut SSL_CTX, version: c_int) -> bool {
    // SAFETY: callers provide a live SSL_CTX; the control takes no pointer argument.
    unsafe {
        openssl_sys::SSL_CTX_ctrl(
            context,
            openssl_sys::SSL_CTRL_SET_MIN_PROTO_VERSION,
            c_long::from(version),
            null_mut(),
        ) == 1
    }
}

#[must_use]
pub unsafe fn SSL_CTX_add1_chain_cert(context: *mut SSL_CTX, certificate: *mut X509) -> bool {
    // SAFETY: callers provide live objects; OpenSSL increments the certificate reference because
    // the `larg` argument is one.
    unsafe {
        openssl_sys::SSL_CTX_ctrl(
            context,
            openssl_sys::SSL_CTRL_CHAIN_CERT,
            1,
            certificate.cast(),
        ) == 1
    }
}

#[must_use]
pub unsafe fn SSL_set_tlsext_host_name(ssl: *mut SSL, host: *const c_char) -> bool {
    // SAFETY: callers provide a live SSL and a NUL-terminated host string retained for the call.
    unsafe {
        openssl_sys::SSL_ctrl(
            ssl,
            openssl_sys::SSL_CTRL_SET_TLSEXT_HOSTNAME,
            0,
            host.cast_mut().cast(),
        ) == 1
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn provider_ffi_layouts_match_openssl_x64() {
        assert_eq!(size_of::<OsslDispatch>(), 16);
        assert_eq!(size_of::<OsslItem>(), 16);
        assert_eq!(size_of::<OsslAlgorithm>(), 32);
        assert_eq!(size_of::<OSSL_PARAM>(), 40);
    }

    #[test]
    fn parameter_constructors_match_header_constants() {
        let end = param_end();
        assert!(end.key.is_null());
        assert_eq!(OSSL_PARAM_UTF8_STRING, 4);
        assert_eq!(OSSL_PARAM_UNSIGNED_INTEGER, 2);
    }
}
