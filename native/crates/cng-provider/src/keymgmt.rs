use std::ffi::{c_int, c_void};
use std::ptr::{null, null_mut};

use cng_core::{CngKey, DEFAULT_PROVIDER_NAME};
use openssl_provider_sys::{
    OSSL_FUNC_KEYMGMT_DUP, OSSL_FUNC_KEYMGMT_EXPORT, OSSL_FUNC_KEYMGMT_EXPORT_TYPES,
    OSSL_FUNC_KEYMGMT_FREE, OSSL_FUNC_KEYMGMT_GET_PARAMS, OSSL_FUNC_KEYMGMT_GETTABLE_PARAMS,
    OSSL_FUNC_KEYMGMT_HAS, OSSL_FUNC_KEYMGMT_IMPORT, OSSL_FUNC_KEYMGMT_IMPORT_TYPES,
    OSSL_FUNC_KEYMGMT_LOAD, OSSL_FUNC_KEYMGMT_MATCH, OSSL_FUNC_KEYMGMT_NEW,
    OSSL_FUNC_KEYMGMT_QUERY_OPERATION_NAME, OSSL_FUNC_KEYMGMT_VALIDATE,
    OSSL_KEYMGMT_SELECT_KEYPAIR, OSSL_KEYMGMT_SELECT_PRIVATE_KEY, OSSL_KEYMGMT_SELECT_PUBLIC_KEY,
    OSSL_KEYMGMT_VALIDATE_FULL_CHECK, OSSL_KEYMGMT_VALIDATE_QUICK_CHECK, OSSL_PARAM,
    OSSL_PARAM_INTEGER, OSSL_PARAM_UNSIGNED_INTEGER, OSSL_PARAM_UTF8_STRING, OsslCallback,
    OsslDispatch, param_definition, param_end,
};

use crate::allocation;
use crate::error::{self, CNG_FAILURE, INVALID_PARAMETER, KEY_NOT_LOADED, PRIVATE_EXPORT_DISABLED};
use crate::key::{
    KEY_MAGIC, KeyMaterial, ProviderKey, export_public, free_provider_key, import_public,
    names_match, set_bn_param,
};
use crate::params;

const RSA_BITS: u32 = 2_048;

struct StaticParams<const N: usize>([OSSL_PARAM; N]);
// SAFETY: these arrays contain only immutable static key pointers and null data pointers.
unsafe impl<const N: usize> Sync for StaticParams<N> {}

static PRIVATE_IMPORT_TYPES: StaticParams<3> = StaticParams([
    param_definition(c"cng-key-name".as_ptr(), OSSL_PARAM_UTF8_STRING),
    param_definition(c"cng-provider-name".as_ptr(), OSSL_PARAM_UTF8_STRING),
    param_end(),
]);
static PUBLIC_IMPORT_TYPES: StaticParams<3> = StaticParams([
    param_definition(c"n".as_ptr(), OSSL_PARAM_UNSIGNED_INTEGER),
    param_definition(c"e".as_ptr(), OSSL_PARAM_UNSIGNED_INTEGER),
    param_end(),
]);
static GETTABLE_PARAMS: StaticParams<9> = StaticParams([
    param_definition(c"bits".as_ptr(), OSSL_PARAM_INTEGER),
    param_definition(c"security-bits".as_ptr(), OSSL_PARAM_INTEGER),
    param_definition(c"max-size".as_ptr(), OSSL_PARAM_INTEGER),
    param_definition(c"default-digest".as_ptr(), OSSL_PARAM_UTF8_STRING),
    param_definition(c"n".as_ptr(), OSSL_PARAM_UNSIGNED_INTEGER),
    param_definition(c"e".as_ptr(), OSSL_PARAM_UNSIGNED_INTEGER),
    param_definition(c"cng-key-name".as_ptr(), OSSL_PARAM_UTF8_STRING),
    param_definition(c"cng-provider-name".as_ptr(), OSSL_PARAM_UTF8_STRING),
    param_end(),
]);
static EXPORT_TYPES: StaticParams<3> = StaticParams([
    param_definition(c"n".as_ptr(), OSSL_PARAM_UNSIGNED_INTEGER),
    param_definition(c"e".as_ptr(), OSSL_PARAM_UNSIGNED_INTEGER),
    param_end(),
]);

unsafe extern "C" fn keymgmt_new(provider_context: *mut c_void) -> *mut c_void {
    ffi_guard!(null_mut(), {
        ProviderKey::new(provider_context, KeyMaterial::Empty).cast()
    })
}

unsafe extern "C" fn keymgmt_load(reference: *const c_void, reference_size: usize) -> *mut c_void {
    ffi_guard!(null_mut(), {
        if reference.is_null() || reference_size != size_of::<*mut ProviderKey>() {
            return null_mut();
        }
        let mutable_reference = reference.cast::<*mut ProviderKey>().cast_mut();
        // SAFETY: STORE supplies a pointer-sized object-reference slot for this provider.
        let key = unsafe { *mutable_reference };
        if key.is_null() {
            return null_mut();
        }
        // SAFETY: the reference is a provider-owned key pointer retained by STORE.
        if unsafe { (*key).magic } != KEY_MAGIC {
            return null_mut();
        }
        // SAFETY: ownership transfers atomically from STORE to OpenSSL by nulling its slot.
        unsafe { *mutable_reference = null_mut() };
        key.cast()
    })
}

unsafe extern "C" fn keymgmt_free(key_data: *mut c_void) {
    ffi_guard_void!({
        // SAFETY: OpenSSL returns provider key allocations exactly once.
        unsafe { free_provider_key(key_data.cast()) };
    });
}

unsafe extern "C" fn keymgmt_dup(key_data: *const c_void, selection: c_int) -> *mut c_void {
    ffi_guard!(null_mut(), {
        if key_data.is_null() || selection & OSSL_KEYMGMT_SELECT_KEYPAIR == 0 {
            return null_mut();
        }
        // SAFETY: OpenSSL passes a live provider key for the duration of this call.
        let source = unsafe { &*key_data.cast::<ProviderKey>() };
        if !source.is_valid() {
            return null_mut();
        }
        let private_key = source
            .private_key()
            .filter(|_| selection & OSSL_KEYMGMT_SELECT_PRIVATE_KEY != 0);
        let material = if let Some(key) = private_key {
            KeyMaterial::Private(key.clone())
        } else if selection & OSSL_KEYMGMT_SELECT_PUBLIC_KEY != 0 {
            let Some((modulus, exponent, bits)) = source.public_parts() else {
                return null_mut();
            };
            let Some(modulus) = allocation::try_copy_bytes(modulus) else {
                error::raise_memory_failure();
                return null_mut();
            };
            let Some(exponent) = allocation::try_copy_bytes(exponent) else {
                error::raise_memory_failure();
                return null_mut();
            };
            KeyMaterial::Public {
                modulus,
                exponent,
                bits,
            }
        } else {
            return null_mut();
        };
        ProviderKey::new(source.provider_context, material).cast()
    })
}

unsafe extern "C" fn keymgmt_import(
    key_data: *mut c_void,
    selection: c_int,
    input: *const OSSL_PARAM,
) -> c_int {
    ffi_guard!(0, {
        if key_data.is_null() || input.is_null() || selection & OSSL_KEYMGMT_SELECT_KEYPAIR == 0 {
            error::raise(
                INVALID_PARAMETER,
                "RSA key import requires a keypair selection",
            );
            return 0;
        }
        // SAFETY: OpenSSL passes a live provider key allocated by `keymgmt_new`.
        let key = unsafe { &mut *key_data.cast::<ProviderKey>() };
        if key.magic != KEY_MAGIC {
            return 0;
        }
        if selection & OSSL_KEYMGMT_SELECT_PRIVATE_KEY == 0 {
            // SAFETY: the input is a valid terminated OpenSSL parameter array.
            return i32::from(unsafe { import_public(key, input) });
        }

        // SAFETY: the input is a valid terminated OpenSSL parameter array.
        let key_param = unsafe { params::locate(input, c"cng-key-name") };
        // SAFETY: the located parameter remains live with the input array.
        let key_name = match unsafe { params::get_utf8_owned(key_param, 512) } {
            Ok(value) => value,
            Err(params::Utf8OwnedError::Memory) => {
                error::raise_memory_failure();
                return 0;
            }
            Err(params::Utf8OwnedError::Invalid) => {
                error::raise(
                    INVALID_PARAMETER,
                    "cng-key-name is required as a bounded UTF-8 string",
                );
                return 0;
            }
        };
        // SAFETY: the input is a valid terminated OpenSSL parameter array.
        let provider_param = unsafe { params::locate(input, c"cng-provider-name") };
        let provider_name = if provider_param.is_null() {
            let Some(value) = allocation::try_copy_str(DEFAULT_PROVIDER_NAME) else {
                error::raise_memory_failure();
                return 0;
            };
            value
        } else {
            // SAFETY: the located parameter remains live with the input array.
            match unsafe { params::get_utf8_owned(provider_param, 512) } {
                Ok(value) => value,
                Err(params::Utf8OwnedError::Memory) => {
                    error::raise_memory_failure();
                    return 0;
                }
                Err(params::Utf8OwnedError::Invalid) => {
                    error::raise(
                        INVALID_PARAMETER,
                        "cng-provider-name is not a bounded UTF-8 string",
                    );
                    return 0;
                }
            }
        };
        match CngKey::open_or_create(&provider_name, &key_name, RSA_BITS) {
            Ok(loaded) => {
                key.material = KeyMaterial::Private(loaded);
                1
            }
            Err(cng_error) => {
                error::raise_cng(CNG_FAILURE, &cng_error);
                0
            }
        }
    })
}

unsafe extern "C" fn keymgmt_import_types(selection: c_int) -> *const OSSL_PARAM {
    ffi_guard!(null(), {
        if selection & OSSL_KEYMGMT_SELECT_PRIVATE_KEY != 0 {
            PRIVATE_IMPORT_TYPES.0.as_ptr()
        } else if selection & OSSL_KEYMGMT_SELECT_PUBLIC_KEY != 0 {
            PUBLIC_IMPORT_TYPES.0.as_ptr()
        } else {
            null()
        }
    })
}

unsafe extern "C" fn keymgmt_has(key_data: *const c_void, selection: c_int) -> c_int {
    ffi_guard!(0, {
        if key_data.is_null() {
            return 0;
        }
        // SAFETY: OpenSSL passes a live provider key for the duration of this call.
        let key = unsafe { &*key_data.cast::<ProviderKey>() };
        i32::from(
            key.is_valid()
                && (selection & OSSL_KEYMGMT_SELECT_PRIVATE_KEY == 0
                    || key.private_key().is_some())
                && (selection & OSSL_KEYMGMT_SELECT_PUBLIC_KEY == 0
                    || key.public_parts().is_some()),
        )
    })
}

unsafe extern "C" fn keymgmt_validate(
    key_data: *const c_void,
    selection: c_int,
    check_type: c_int,
) -> c_int {
    ffi_guard!(0, {
        if !matches!(
            check_type,
            OSSL_KEYMGMT_VALIDATE_FULL_CHECK | OSSL_KEYMGMT_VALIDATE_QUICK_CHECK
        ) {
            return 0;
        }
        // SAFETY: forwards the same live key pointer and selection.
        if unsafe { keymgmt_has(key_data, selection) } != 1 {
            return 0;
        }
        if selection & OSSL_KEYMGMT_SELECT_KEYPAIR == 0 {
            return 1;
        }
        // SAFETY: `keymgmt_has` established a valid non-null provider key.
        i32::from(unsafe { (&*key_data.cast::<ProviderKey>()).public_is_valid() })
    })
}

unsafe extern "C" fn keymgmt_match(
    left_data: *const c_void,
    right_data: *const c_void,
    selection: c_int,
) -> c_int {
    ffi_guard!(0, {
        if left_data.is_null() || right_data.is_null() {
            return 0;
        }
        // SAFETY: OpenSSL passes two live provider keys for this synchronous comparison.
        let left = unsafe { &*left_data.cast::<ProviderKey>() };
        // SAFETY: same invariant as `left`.
        let right = unsafe { &*right_data.cast::<ProviderKey>() };
        if !left.is_valid() || !right.is_valid() {
            return 0;
        }
        i32::from(selection & OSSL_KEYMGMT_SELECT_KEYPAIR == 0 || names_match(left, right))
    })
}

unsafe extern "C" fn keymgmt_get_params(key_data: *mut c_void, output: *mut OSSL_PARAM) -> c_int {
    ffi_guard!(0, {
        if key_data.is_null() || output.is_null() {
            return 0;
        }
        // SAFETY: OpenSSL passes a live provider key and mutable terminated parameter array.
        let key = unsafe { &*key_data.cast::<ProviderKey>() };
        let Some((modulus, exponent, bits)) = key.public_parts() else {
            error::raise(KEY_NOT_LOADED, "no CNG key has been loaded");
            return 0;
        };

        // SAFETY: each locate/set pair operates on the live mutable output array.
        let parameter = unsafe { params::locate_mut(output, c"bits") };
        let parameter_set = parameter.is_null() || {
            // SAFETY: the located output parameter remains live and writable.
            unsafe { params::set_int(parameter, bits as c_int, false) }
        };
        if !parameter_set {
            return 0;
        }
        // SAFETY: same output-array invariant.
        let parameter = unsafe { params::locate_mut(output, c"security-bits") };
        let parameter_set = parameter.is_null() || {
            // SAFETY: the located output parameter remains live and writable.
            unsafe { params::set_int(parameter, key.security_bits() as c_int, false) }
        };
        if !parameter_set {
            return 0;
        }
        // SAFETY: same output-array invariant.
        let parameter = unsafe { params::locate_mut(output, c"max-size") };
        let parameter_set = parameter.is_null() || {
            // SAFETY: the located output parameter remains live and writable.
            unsafe { params::set_int(parameter, key.max_size() as c_int, false) }
        };
        if !parameter_set {
            return 0;
        }
        // SAFETY: same output-array invariant; the returned string is static.
        let parameter = unsafe { params::locate_mut(output, c"default-digest") };
        let parameter_set = parameter.is_null() || {
            // SAFETY: the located output parameter remains live and the string is static.
            unsafe { params::set_utf8(parameter, c"SHA256") }
        };
        if !parameter_set {
            return 0;
        }
        // SAFETY: same output-array invariant.
        let parameter = unsafe { params::locate_mut(output, c"n") };
        let parameter_set = parameter.is_null() || {
            // SAFETY: the located output parameter and provider key remain live.
            unsafe { set_bn_param(key, parameter, modulus) }
        };
        if !parameter_set {
            return 0;
        }
        // SAFETY: same output-array invariant.
        let parameter = unsafe { params::locate_mut(output, c"e") };
        let parameter_set = parameter.is_null() || {
            // SAFETY: the located output parameter and provider key remain live.
            unsafe { set_bn_param(key, parameter, exponent) }
        };
        if !parameter_set {
            return 0;
        }

        if let Some(private) = key.private_key() {
            // SAFETY: same output-array invariant. Gettable metadata declares UTF8_STRING, so
            // OpenSSL copies these dynamic strings during the callback.
            let parameter = unsafe { params::locate_mut(output, c"cng-key-name") };
            let parameter_set = parameter.is_null() || {
                // SAFETY: the output parameter remains live and OpenSSL copies UTF8_STRING data.
                unsafe { params::set_utf8_str(parameter, private.key_name()) }
            };
            if !parameter_set {
                return 0;
            }
            // SAFETY: same output-array and synchronous-copy invariant.
            let parameter = unsafe { params::locate_mut(output, c"cng-provider-name") };
            let parameter_set = parameter.is_null() || {
                // SAFETY: the output parameter remains live and OpenSSL copies UTF8_STRING data.
                unsafe { params::set_utf8_str(parameter, private.provider_name()) }
            };
            if !parameter_set {
                return 0;
            }
        }
        1
    })
}

unsafe extern "C" fn keymgmt_gettable_params(_provider_context: *mut c_void) -> *const OSSL_PARAM {
    ffi_guard!(null(), { GETTABLE_PARAMS.0.as_ptr() })
}

unsafe extern "C" fn keymgmt_export(
    key_data: *mut c_void,
    selection: c_int,
    callback: OsslCallback,
    callback_argument: *mut c_void,
) -> c_int {
    ffi_guard!(0, {
        if key_data.is_null() {
            return 0;
        }
        // SAFETY: OpenSSL passes a live provider key.
        let key = unsafe { &*key_data.cast::<ProviderKey>() };
        if !key.is_valid() {
            return 0;
        }
        if selection & OSSL_KEYMGMT_SELECT_PRIVATE_KEY != 0 {
            error::raise(
                PRIVATE_EXPORT_DISABLED,
                "the CNG private key cannot be exported",
            );
            return 0;
        }
        if selection & OSSL_KEYMGMT_SELECT_PUBLIC_KEY == 0 {
            return 0;
        }
        // SAFETY: callback and callback argument are valid only for this synchronous export.
        i32::from(unsafe { export_public(key, callback, callback_argument) })
    })
}

unsafe extern "C" fn keymgmt_export_types(selection: c_int) -> *const OSSL_PARAM {
    ffi_guard!(null(), {
        if selection & OSSL_KEYMGMT_SELECT_PRIVATE_KEY != 0
            || selection & OSSL_KEYMGMT_SELECT_PUBLIC_KEY == 0
        {
            null()
        } else {
            EXPORT_TYPES.0.as_ptr()
        }
    })
}

unsafe extern "C" fn keymgmt_query_operation_name(operation_id: c_int) -> *const i8 {
    ffi_guard!(null(), {
        if operation_id == openssl_provider_sys::OSSL_OP_SIGNATURE {
            c"RSA".as_ptr()
        } else {
            null()
        }
    })
}

pub static KEYMGMT_FUNCTIONS: [OsslDispatch; 15] = [
    OsslDispatch {
        function_id: OSSL_FUNC_KEYMGMT_NEW,
        function: keymgmt_new as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_KEYMGMT_LOAD,
        function: keymgmt_load as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_KEYMGMT_FREE,
        function: keymgmt_free as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_KEYMGMT_DUP,
        function: keymgmt_dup as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_KEYMGMT_IMPORT,
        function: keymgmt_import as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_KEYMGMT_IMPORT_TYPES,
        function: keymgmt_import_types as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_KEYMGMT_HAS,
        function: keymgmt_has as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_KEYMGMT_VALIDATE,
        function: keymgmt_validate as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_KEYMGMT_MATCH,
        function: keymgmt_match as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_KEYMGMT_GET_PARAMS,
        function: keymgmt_get_params as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_KEYMGMT_GETTABLE_PARAMS,
        function: keymgmt_gettable_params as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_KEYMGMT_EXPORT,
        function: keymgmt_export as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_KEYMGMT_EXPORT_TYPES,
        function: keymgmt_export_types as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_KEYMGMT_QUERY_OPERATION_NAME,
        function: keymgmt_query_operation_name as *const c_void,
    },
    OsslDispatch {
        function_id: 0,
        function: null(),
    },
];
