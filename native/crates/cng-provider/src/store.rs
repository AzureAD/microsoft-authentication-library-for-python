use std::ffi::{CStr, c_char, c_int, c_void};
use std::ptr::{null, null_mut};

use cng_core::CngKey;
use openssl_provider_sys::{
    OSSL_FUNC_STORE_CLOSE, OSSL_FUNC_STORE_EOF, OSSL_FUNC_STORE_EXPORT_OBJECT,
    OSSL_FUNC_STORE_LOAD, OSSL_FUNC_STORE_OPEN, OSSL_FUNC_STORE_SET_CTX_PARAMS,
    OSSL_FUNC_STORE_SETTABLE_CTX_PARAMS, OSSL_OBJECT_PKEY, OSSL_PARAM, OSSL_PARAM_INTEGER,
    OSSL_PARAM_UTF8_STRING, OSSL_PASSPHRASE_CALLBACK, OSSL_STORE_INFO_PKEY, OsslCallback,
    OsslDispatch, param_definition, param_end, param_int, param_octet_string, param_utf8_string,
};
use zeroize::{Zeroize, Zeroizing};

use crate::allocation;
use crate::error::{self, CNG_FAILURE, INVALID_PARAMETER, INVALID_REFERENCE, INVALID_URI};
use crate::key::{KEY_MAGIC, KeyMaterial, ProviderKey, export_public, free_provider_key};
use crate::params;
use crate::uri::{StoreUriError, parse_store_uri};

struct StaticParams<const N: usize>([OSSL_PARAM; N]);
// SAFETY: these arrays contain only immutable static key pointers and null data pointers.
unsafe impl<const N: usize> Sync for StaticParams<N> {}

static SETTABLE_PARAMS: StaticParams<3> = StaticParams([
    param_definition(c"expect".as_ptr(), OSSL_PARAM_INTEGER),
    param_definition(c"properties".as_ptr(), OSSL_PARAM_UTF8_STRING),
    param_end(),
]);

struct StoreContext {
    reference: OwnedProviderKeyReference,
    expected_type: c_int,
    loaded: bool,
    description: Zeroizing<Vec<u8>>,
}

#[repr(transparent)]
struct OwnedProviderKeyReference(*mut ProviderKey);

impl OwnedProviderKeyReference {
    const fn new(reference: *mut ProviderKey) -> Self {
        Self(reference)
    }

    const fn is_null(&self) -> bool {
        self.0.is_null()
    }
}

impl Drop for OwnedProviderKeyReference {
    fn drop(&mut self) {
        if !self.0.is_null() {
            // SAFETY: this wrapper is the sole owner until KEYMGMT_LOAD nulls its pointer slot.
            unsafe { free_provider_key(self.0) };
            self.0 = null_mut();
        }
    }
}

impl Drop for StoreContext {
    fn drop(&mut self) {
        self.description.zeroize();
    }
}

unsafe extern "C" fn store_open(provider_context: *mut c_void, uri: *const c_char) -> *mut c_void {
    ffi_guard!(null_mut(), {
        if uri.is_null() {
            error::raise(INVALID_URI, "a CNG STORE URI is required");
            return null_mut();
        }
        // SAFETY: OpenSSL supplies a NUL-terminated URI string.
        let uri = unsafe { CStr::from_ptr(uri) };
        let Ok(uri_text) = uri.to_str() else {
            error::raise(INVALID_URI, "the CNG STORE URI is not valid UTF-8");
            return null_mut();
        };
        let parsed = match parse_store_uri(uri_text) {
            Ok(parsed) => parsed,
            Err(StoreUriError::Invalid) => {
                error::raise(
                    INVALID_URI,
                    "expected cng://?key=<url-encoded>&provider=<url-encoded>",
                );
                return null_mut();
            }
            Err(StoreUriError::Memory) => {
                error::raise_memory_failure();
                return null_mut();
            }
        };
        let key = match CngKey::open(&parsed.provider_name, &parsed.key_name) {
            Ok(key) => key,
            Err(cng_error) => {
                error::raise_cng(CNG_FAILURE, &cng_error);
                return null_mut();
            }
        };
        let Some(description) = allocation::try_copy_bytes_with_nul(uri.to_bytes()) else {
            error::raise_memory_failure();
            return null_mut();
        };
        let description = Zeroizing::new(description);
        let reference = ProviderKey::new(provider_context, KeyMaterial::Private(key));
        if reference.is_null() {
            return null_mut();
        }
        let Some(context) = allocation::try_box(StoreContext {
            reference: OwnedProviderKeyReference::new(reference),
            expected_type: 0,
            loaded: false,
            description,
        }) else {
            error::raise_memory_failure();
            return null_mut();
        };
        Box::into_raw(context).cast()
    })
}

unsafe extern "C" fn store_settable_ctx_params(
    _provider_context: *mut c_void,
) -> *const OSSL_PARAM {
    ffi_guard!(null(), { SETTABLE_PARAMS.0.as_ptr() })
}

unsafe extern "C" fn store_set_ctx_params(
    loader_context: *mut c_void,
    input: *const OSSL_PARAM,
) -> c_int {
    ffi_guard!(0, {
        if loader_context.is_null() {
            return 0;
        }
        if input.is_null() {
            return 1;
        }
        // SAFETY: OpenSSL passes a live STORE context.
        let store = unsafe { &mut *loader_context.cast::<StoreContext>() };
        // SAFETY: input is a terminated OpenSSL parameter array.
        let parameter = unsafe { params::locate(input, c"expect") };
        if !parameter.is_null() {
            // SAFETY: the located parameter remains live with input.
            let Some(expected) = (unsafe { params::get_int(parameter) }) else {
                error::raise(INVALID_PARAMETER, "the STORE expectation is invalid");
                return 0;
            };
            if expected != 0 && expected != OSSL_STORE_INFO_PKEY {
                error::raise(
                    INVALID_PARAMETER,
                    "the CNG store supports only private-key (PKEY) expectations",
                );
                return 0;
            }
            store.expected_type = expected;
        }
        // SAFETY: input is a terminated OpenSSL parameter array.
        let parameter = unsafe { params::locate(input, c"properties") };
        if !parameter.is_null() {
            // SAFETY: the located parameter remains live with input.
            match unsafe { params::get_utf8_owned(parameter, 256) } {
                Ok(_) => {}
                Err(params::Utf8OwnedError::Memory) => {
                    error::raise_memory_failure();
                    return 0;
                }
                Err(params::Utf8OwnedError::Invalid) => {
                    error::raise(
                        INVALID_PARAMETER,
                        "the STORE property query is invalid or too long",
                    );
                    return 0;
                }
            }
        }
        1
    })
}

unsafe extern "C" fn store_load(
    loader_context: *mut c_void,
    object_callback: OsslCallback,
    callback_argument: *mut c_void,
    _passphrase_callback: OSSL_PASSPHRASE_CALLBACK,
    _passphrase_argument: *mut c_void,
) -> c_int {
    ffi_guard!(0, {
        let Some(object_callback) = object_callback else {
            return 0;
        };
        if loader_context.is_null() {
            return 0;
        }
        // SAFETY: OpenSSL passes a live STORE context.
        let store = unsafe { &mut *loader_context.cast::<StoreContext>() };
        if store.loaded || store.reference.is_null() {
            return 0;
        }
        store.loaded = true;
        let mut object_type = OSSL_OBJECT_PKEY;
        let mut data_type = *b"RSA\0";
        let description_length = store.description.len().saturating_sub(1);
        let params = [
            param_int(c"type".as_ptr(), &raw mut object_type),
            param_utf8_string(c"data-type".as_ptr(), data_type.as_mut_ptr().cast(), 0),
            param_octet_string(
                c"reference".as_ptr(),
                (&raw mut store.reference).cast(),
                size_of::<*mut ProviderKey>(),
            ),
            param_utf8_string(
                c"desc".as_ptr(),
                store.description.as_mut_ptr().cast(),
                description_length,
            ),
            param_end(),
        ];
        // SAFETY: the parameter array and all referenced stack/STORE data remain live for this
        // synchronous callback.
        unsafe { object_callback(params.as_ptr(), callback_argument) }
    })
}

unsafe extern "C" fn store_eof(loader_context: *mut c_void) -> c_int {
    ffi_guard!(1, {
        if loader_context.is_null() {
            1
        } else {
            // SAFETY: OpenSSL passes a live STORE context.
            i32::from(unsafe { (*loader_context.cast::<StoreContext>()).loaded })
        }
    })
}

unsafe extern "C" fn store_close(loader_context: *mut c_void) -> c_int {
    ffi_guard!(0, {
        if !loader_context.is_null() {
            // SAFETY: OpenSSL closes the Box allocation from `store_open` exactly once.
            drop(unsafe { Box::from_raw(loader_context.cast::<StoreContext>()) });
        }
        1
    })
}

unsafe extern "C" fn store_export_object(
    loader_context: *mut c_void,
    object_reference: *const c_void,
    reference_size: usize,
    export_callback: OsslCallback,
    callback_argument: *mut c_void,
) -> c_int {
    ffi_guard!(0, {
        if loader_context.is_null()
            || object_reference.is_null()
            || reference_size != size_of::<*mut ProviderKey>()
        {
            error::raise(
                INVALID_REFERENCE,
                "the CNG STORE object reference is invalid",
            );
            return 0;
        }
        // SAFETY: STORE object references are pointer-sized slots populated by this provider.
        let key = unsafe { *object_reference.cast::<*mut ProviderKey>() };
        if key.is_null()
            // SAFETY: a non-null pointer is a provider-owned key retained through the callback.
            || unsafe { (*key).magic } != KEY_MAGIC
        {
            error::raise(
                INVALID_REFERENCE,
                "the CNG STORE object reference is no longer available",
            );
            return 0;
        }
        // SAFETY: the key and export callback remain live for this synchronous call.
        i32::from(unsafe { export_public(&*key, export_callback, callback_argument) })
    })
}

pub static STORE_FUNCTIONS: [OsslDispatch; 8] = [
    OsslDispatch {
        function_id: OSSL_FUNC_STORE_OPEN,
        function: store_open as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_STORE_SETTABLE_CTX_PARAMS,
        function: store_settable_ctx_params as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_STORE_SET_CTX_PARAMS,
        function: store_set_ctx_params as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_STORE_LOAD,
        function: store_load as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_STORE_EOF,
        function: store_eof as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_STORE_CLOSE,
        function: store_close as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_STORE_EXPORT_OBJECT,
        function: store_export_object as *const c_void,
    },
    OsslDispatch {
        function_id: 0,
        function: null(),
    },
];

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::CString;

    use cng_core::{DEFAULT_PROVIDER_NAME, delete_test_key, random_bytes};

    fn owned_name() -> String {
        let mut random = [0_u8; 16];
        random_bytes(&mut random).expect("random STORE test key suffix");
        let suffix = random
            .iter()
            .map(|value| format!("{value:02x}"))
            .collect::<String>();
        random.zeroize();
        format!(
            "openssl-cng-rust-test-store-{}-{suffix}",
            std::process::id()
        )
    }

    #[test]
    fn store_context_allocation_failure_has_one_reference_owner() {
        let name = owned_name();
        let key =
            CngKey::open_or_create(DEFAULT_PROVIDER_NAME, &name, 2_048).expect("create test key");
        drop(key);
        let uri = CString::new(format!("cng://?key={name}")).expect("STORE URI");

        allocation::fail_box_allocation_after(1);
        // SAFETY: the URI is NUL-terminated and the null provider context is opaque to STORE.
        let context = unsafe { store_open(null_mut(), uri.as_ptr()) };
        if !context.is_null() {
            // SAFETY: a non-null result is a live STORE context owned by this test.
            let _ = unsafe { store_close(context) };
        }

        delete_test_key(DEFAULT_PROVIDER_NAME, &name).expect("delete STORE test key");
        assert!(
            context.is_null(),
            "fault injection must fail StoreContext allocation"
        );
    }
}
