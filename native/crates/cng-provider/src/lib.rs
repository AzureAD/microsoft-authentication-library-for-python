use std::ffi::{CStr, c_int, c_void};
use std::ptr::{null, null_mut};

use openssl_provider_sys::{
    OSSL_FUNC_PROVIDER_GET_PARAMS, OSSL_FUNC_PROVIDER_GET_REASON_STRINGS,
    OSSL_FUNC_PROVIDER_GETTABLE_PARAMS, OSSL_FUNC_PROVIDER_QUERY_OPERATION,
    OSSL_FUNC_PROVIDER_TEARDOWN, OSSL_OP_KEYMGMT, OSSL_OP_SIGNATURE, OSSL_OP_STORE, OSSL_PARAM,
    OSSL_PARAM_UNSIGNED_INTEGER, OSSL_PARAM_UTF8_PTR, OsslAlgorithm, OsslCoreHandle, OsslDispatch,
    OsslItem, param_definition, param_end,
};

macro_rules! ffi_guard {
    ($default:expr, $body:block) => {{
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| $body)).unwrap_or_else(|_| {
            crate::error::raise(
                crate::error::INVALID_PARAMETER,
                "a Rust panic was contained at the provider FFI boundary",
            );
            $default
        })
    }};
}

macro_rules! ffi_guard_void {
    ($body:block) => {{
        let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| $body)).map_err(|_| {
            crate::error::raise(
                crate::error::INVALID_PARAMETER,
                "a Rust panic was contained at the provider FFI boundary",
            );
        });
    }};
}

mod allocation;
mod error;
mod key;
mod keymgmt;
mod params;
mod signature;
mod store;
mod uri;

struct StaticParams<const N: usize>([OSSL_PARAM; N]);
// SAFETY: these arrays contain only immutable static key pointers and null data pointers.
unsafe impl<const N: usize> Sync for StaticParams<N> {}

struct ProviderContext {
    _handle: *const OsslCoreHandle,
}

const PROVIDER_VERSION_BYTES: &[u8] = concat!(env!("CARGO_PKG_VERSION"), "\0").as_bytes();

fn provider_version() -> &'static CStr {
    // SAFETY: Cargo package versions are valid UTF-8 without interior NUL bytes, and the
    // compile-time concatenation appends exactly one terminating NUL.
    unsafe { CStr::from_bytes_with_nul_unchecked(PROVIDER_VERSION_BYTES) }
}

static PROVIDER_GETTABLE_PARAMS: StaticParams<5> = StaticParams([
    param_definition(c"name".as_ptr(), OSSL_PARAM_UTF8_PTR),
    param_definition(c"version".as_ptr(), OSSL_PARAM_UTF8_PTR),
    param_definition(c"buildinfo".as_ptr(), OSSL_PARAM_UTF8_PTR),
    param_definition(c"status".as_ptr(), OSSL_PARAM_UNSIGNED_INTEGER),
    param_end(),
]);

unsafe extern "C" fn provider_gettable_params(_context: *mut c_void) -> *const OSSL_PARAM {
    ffi_guard!(null(), { PROVIDER_GETTABLE_PARAMS.0.as_ptr() })
}

unsafe extern "C" fn provider_get_params(_context: *mut c_void, output: *mut OSSL_PARAM) -> c_int {
    ffi_guard!(0, {
        if output.is_null() {
            return 0;
        }
        // SAFETY: OpenSSL passes a mutable terminated parameter array.
        let parameter = unsafe { params::locate_mut(output, c"name") };
        if !parameter.is_null()
            // SAFETY: the output parameter is live and the returned string is static.
            && !unsafe {
                params::set_utf8_static(parameter, c"Windows CNG RSA provider (Rust)")
            }
        {
            return 0;
        }
        // SAFETY: same output-array invariant.
        let parameter = unsafe { params::locate_mut(output, c"version") };
        if !parameter.is_null()
            // SAFETY: the output parameter is live and the returned string is static.
            && !unsafe { params::set_utf8_static(parameter, provider_version()) }
        {
            return 0;
        }
        // SAFETY: same output-array invariant.
        let parameter = unsafe { params::locate_mut(output, c"buildinfo") };
        if !parameter.is_null()
            // SAFETY: the output parameter is live and the returned string is static.
            && !unsafe {
                params::set_utf8_static(parameter, c"Rust OpenSSL 3 provider using NCrypt")
            }
        {
            return 0;
        }
        // SAFETY: same output-array invariant.
        let parameter = unsafe { params::locate_mut(output, c"status") };
        let status_set = parameter.is_null() || {
            // SAFETY: the located output parameter remains live and writable.
            unsafe { params::set_int(parameter, 1, true) }
        };
        if !status_set {
            return 0;
        }
        1
    })
}

static KEYMGMT_ALGORITHMS: [OsslAlgorithm; 2] = [
    OsslAlgorithm {
        algorithm_names: c"RSA:rsaEncryption".as_ptr(),
        property_definition: c"provider=microsoft_identity_native_provider,cng.provider=yes"
            .as_ptr(),
        implementation: keymgmt::KEYMGMT_FUNCTIONS.as_ptr(),
        algorithm_description: c"Windows CNG persisted RSA key management".as_ptr(),
    },
    OsslAlgorithm {
        algorithm_names: null(),
        property_definition: null(),
        implementation: null(),
        algorithm_description: null(),
    },
];

static SIGNATURE_ALGORITHMS: [OsslAlgorithm; 2] = [
    OsslAlgorithm {
        algorithm_names: c"RSA".as_ptr(),
        property_definition: c"provider=microsoft_identity_native_provider,cng.provider=yes"
            .as_ptr(),
        implementation: signature::SIGNATURE_DISPATCH.as_ptr(),
        algorithm_description: c"Windows CNG RSA signatures".as_ptr(),
    },
    OsslAlgorithm {
        algorithm_names: null(),
        property_definition: null(),
        implementation: null(),
        algorithm_description: null(),
    },
];

static STORE_ALGORITHMS: [OsslAlgorithm; 2] = [
    OsslAlgorithm {
        algorithm_names: c"cng".as_ptr(),
        property_definition: c"provider=microsoft_identity_native_provider,cng.provider=yes"
            .as_ptr(),
        implementation: store::STORE_FUNCTIONS.as_ptr(),
        algorithm_description: c"CNG persisted-key STORE loader".as_ptr(),
    },
    OsslAlgorithm {
        algorithm_names: null(),
        property_definition: null(),
        implementation: null(),
        algorithm_description: null(),
    },
];

unsafe extern "C" fn provider_query_operation(
    _context: *mut c_void,
    operation_id: c_int,
    no_cache: *mut c_int,
) -> *const OsslAlgorithm {
    ffi_guard!(null(), {
        if !no_cache.is_null() {
            // SAFETY: OpenSSL supplies writable integer storage when non-null.
            unsafe { *no_cache = 0 };
        }
        match operation_id {
            OSSL_OP_KEYMGMT => KEYMGMT_ALGORITHMS.as_ptr(),
            OSSL_OP_SIGNATURE => SIGNATURE_ALGORITHMS.as_ptr(),
            OSSL_OP_STORE => STORE_ALGORITHMS.as_ptr(),
            _ => null(),
        }
    })
}

static REASON_STRINGS: [OsslItem; 12] = [
    OsslItem {
        id: error::INVALID_PARAMETER as u32,
        ptr: c"invalid parameter".as_ptr().cast_mut().cast(),
    },
    OsslItem {
        id: error::CNG_FAILURE as u32,
        ptr: c"Windows CNG operation failed".as_ptr().cast_mut().cast(),
    },
    OsslItem {
        id: error::MEMORY_FAILURE as u32,
        ptr: c"memory allocation failed".as_ptr().cast_mut().cast(),
    },
    OsslItem {
        id: error::KEY_NOT_LOADED as u32,
        ptr: c"CNG key is not loaded".as_ptr().cast_mut().cast(),
    },
    OsslItem {
        id: error::UNSUPPORTED_DIGEST as u32,
        ptr: c"unsupported digest".as_ptr().cast_mut().cast(),
    },
    OsslItem {
        id: error::UNSUPPORTED_PADDING as u32,
        ptr: c"unsupported RSA padding".as_ptr().cast_mut().cast(),
    },
    OsslItem {
        id: error::PRIVATE_EXPORT_DISABLED as u32,
        ptr: c"private-key export is disabled".as_ptr().cast_mut().cast(),
    },
    OsslItem {
        id: error::SIGNATURE_FAILURE as u32,
        ptr: c"RSA signature operation failed".as_ptr().cast_mut().cast(),
    },
    OsslItem {
        id: error::INVALID_URI as u32,
        ptr: c"invalid CNG STORE URI".as_ptr().cast_mut().cast(),
    },
    OsslItem {
        id: error::INVALID_REFERENCE as u32,
        ptr: c"invalid provider object reference"
            .as_ptr()
            .cast_mut()
            .cast(),
    },
    OsslItem {
        id: error::KEY_VALIDATION_FAILURE as u32,
        ptr: c"CNG key validation failed".as_ptr().cast_mut().cast(),
    },
    OsslItem {
        id: 0,
        ptr: null_mut(),
    },
];

unsafe extern "C" fn provider_get_reason_strings(_context: *mut c_void) -> *const OsslItem {
    ffi_guard!(null(), { REASON_STRINGS.as_ptr() })
}

unsafe extern "C" fn provider_teardown(context: *mut c_void) {
    ffi_guard_void!({
        if !context.is_null() {
            // SAFETY: OpenSSL releases the Box from OSSL_provider_init exactly once after all
            // provider-owned operation objects have been freed.
            drop(unsafe { Box::from_raw(context.cast::<ProviderContext>()) });
        }
    });
}

static PROVIDER_FUNCTIONS: [OsslDispatch; 6] = [
    OsslDispatch {
        function_id: OSSL_FUNC_PROVIDER_TEARDOWN,
        function: provider_teardown as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_PROVIDER_GETTABLE_PARAMS,
        function: provider_gettable_params as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_PROVIDER_GET_PARAMS,
        function: provider_get_params as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_PROVIDER_QUERY_OPERATION,
        function: provider_query_operation as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_PROVIDER_GET_REASON_STRINGS,
        function: provider_get_reason_strings as *const c_void,
    },
    OsslDispatch {
        function_id: 0,
        function: null(),
    },
];

#[unsafe(no_mangle)]
pub unsafe extern "C" fn OSSL_provider_init(
    handle: *const OsslCoreHandle,
    input: *const OsslDispatch,
    output: *mut *const OsslDispatch,
    provider_context: *mut *mut c_void,
) -> c_int {
    ffi_guard!(0, {
        if handle.is_null() || input.is_null() || output.is_null() || provider_context.is_null() {
            return 0;
        }
        let Some(context) = allocation::try_box(ProviderContext { _handle: handle }) else {
            error::raise_memory_failure();
            return 0;
        };
        let context = Box::into_raw(context);
        // SAFETY: both output pointers were validated and are writable for one pointer.
        unsafe {
            *provider_context = context.cast();
            *output = PROVIDER_FUNCTIONS.as_ptr();
        }
        1
    })
}
