use std::ffi::{CStr, c_char, c_int};
use std::ptr::copy_nonoverlapping;

use openssl_provider_sys::{
    OSSL_PARAM, OSSL_PARAM_INTEGER, OSSL_PARAM_OCTET_STRING, OSSL_PARAM_UNSIGNED_INTEGER,
    OSSL_PARAM_UTF8_PTR, OSSL_PARAM_UTF8_STRING,
};

use crate::allocation;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Utf8OwnedError {
    Invalid,
    Memory,
}

pub unsafe fn locate(mut params: *const OSSL_PARAM, name: &CStr) -> *const OSSL_PARAM {
    if params.is_null() {
        return std::ptr::null();
    }
    loop {
        // SAFETY: callers provide a valid OSSL_PARAM array terminated by a null key.
        let param = unsafe { &*params };
        if param.key.is_null() {
            return std::ptr::null();
        }
        // SAFETY: OpenSSL parameter keys are NUL-terminated strings.
        if unsafe { CStr::from_ptr(param.key) } == name {
            return params;
        }
        // SAFETY: the terminator check above permits advancing to the next array element.
        params = unsafe { params.add(1) };
    }
}

pub unsafe fn locate_mut(params: *mut OSSL_PARAM, name: &CStr) -> *mut OSSL_PARAM {
    // SAFETY: mutable and const OSSL_PARAM arrays have identical traversal rules.
    unsafe { locate(params.cast_const(), name).cast_mut() }
}

pub unsafe fn get_utf8_owned(
    param: *const OSSL_PARAM,
    maximum: usize,
) -> Result<String, Utf8OwnedError> {
    if param.is_null() || maximum == 0 {
        return Err(Utf8OwnedError::Invalid);
    }
    // SAFETY: the pointer was checked and comes from an OpenSSL parameter array.
    let param = unsafe { &*param };
    let bytes = if param.data_type == OSSL_PARAM_UTF8_STRING {
        if param.data.is_null() || param.data_size == 0 || param.data_size >= maximum {
            return Err(Utf8OwnedError::Invalid);
        }
        // SAFETY: OpenSSL declares `data_size` readable bytes for UTF8_STRING.
        let bytes = unsafe { std::slice::from_raw_parts(param.data.cast::<u8>(), param.data_size) };
        if bytes.contains(&0) {
            return Err(Utf8OwnedError::Invalid);
        }
        bytes
    } else if param.data_type == OSSL_PARAM_UTF8_PTR {
        if param.data.is_null() {
            return Err(Utf8OwnedError::Invalid);
        }
        // SAFETY: UTF8_PTR data points to a valid pointer-sized slot containing a C string.
        let value = unsafe { *param.data.cast::<*const c_char>() };
        if value.is_null() {
            return Err(Utf8OwnedError::Invalid);
        }
        let mut length = 0;
        while length < maximum {
            // SAFETY: OpenSSL's UTF8_PTR contract supplies a readable NUL-terminated string.
            if unsafe { *value.add(length) } == 0 {
                break;
            }
            length += 1;
        }
        if length == maximum {
            return Err(Utf8OwnedError::Invalid);
        }
        // SAFETY: the bounded scan above found a terminator after `length` readable bytes.
        unsafe { std::slice::from_raw_parts(value.cast::<u8>(), length) }
    } else {
        return Err(Utf8OwnedError::Invalid);
    };
    let value = std::str::from_utf8(bytes).map_err(|_| Utf8OwnedError::Invalid)?;
    allocation::try_copy_str(value).ok_or(Utf8OwnedError::Memory)
}

pub unsafe fn get_int(param: *const OSSL_PARAM) -> Option<c_int> {
    if param.is_null() {
        return None;
    }
    // SAFETY: the pointer was checked and comes from an OpenSSL parameter array.
    let param = unsafe { &*param };
    if param.data.is_null() {
        return None;
    }
    match (param.data_type, param.data_size) {
        (OSSL_PARAM_INTEGER, 4) => {
            // SAFETY: data_size confirms a readable i32 slot; unaligned access is permitted.
            Some(unsafe { param.data.cast::<i32>().read_unaligned() })
        }
        (OSSL_PARAM_INTEGER, 8) => {
            // SAFETY: data_size confirms a readable i64 slot; unaligned access is permitted.
            i32::try_from(unsafe { param.data.cast::<i64>().read_unaligned() }).ok()
        }
        (OSSL_PARAM_UNSIGNED_INTEGER, 4) => {
            // SAFETY: data_size confirms a readable u32 slot; unaligned access is permitted.
            i32::try_from(unsafe { param.data.cast::<u32>().read_unaligned() }).ok()
        }
        (OSSL_PARAM_UNSIGNED_INTEGER, 8) => {
            // SAFETY: data_size confirms a readable u64 slot; unaligned access is permitted.
            i32::try_from(unsafe { param.data.cast::<u64>().read_unaligned() }).ok()
        }
        _ => None,
    }
}

pub unsafe fn set_int(param: *mut OSSL_PARAM, value: c_int, unsigned: bool) -> bool {
    if param.is_null() {
        return false;
    }
    // SAFETY: the pointer was checked and comes from a mutable OpenSSL parameter array.
    let param = unsafe { &mut *param };
    match (param.data_type, param.data_size) {
        (OSSL_PARAM_INTEGER, 4) => {
            param.return_size = 4;
            if !param.data.is_null() {
                // SAFETY: data_size confirms a writable four-byte slot.
                unsafe { param.data.cast::<i32>().write_unaligned(value) };
            }
            true
        }
        (OSSL_PARAM_INTEGER, 8) => {
            param.return_size = 8;
            if !param.data.is_null() {
                // SAFETY: data_size confirms a writable eight-byte slot.
                unsafe { param.data.cast::<i64>().write_unaligned(i64::from(value)) };
            }
            true
        }
        (OSSL_PARAM_UNSIGNED_INTEGER, 4) if unsigned || value >= 0 => {
            param.return_size = 4;
            if !param.data.is_null() {
                // SAFETY: data_size confirms a writable four-byte slot.
                unsafe { param.data.cast::<u32>().write_unaligned(value as u32) };
            }
            true
        }
        (OSSL_PARAM_UNSIGNED_INTEGER, 8) if unsigned || value >= 0 => {
            param.return_size = 8;
            if !param.data.is_null() {
                // SAFETY: data_size confirms a writable eight-byte slot.
                unsafe { param.data.cast::<u64>().write_unaligned(value as u64) };
            }
            true
        }
        (data_type, 0)
            if param.data.is_null()
                && matches!(data_type, OSSL_PARAM_INTEGER | OSSL_PARAM_UNSIGNED_INTEGER) =>
        {
            param.return_size = 4;
            true
        }
        _ => false,
    }
}

unsafe fn set_utf8_bytes(param: *mut OSSL_PARAM, bytes: &[u8]) -> bool {
    if param.is_null() {
        return false;
    }
    // SAFETY: the pointer was checked and comes from a mutable OpenSSL parameter array.
    let param = unsafe { &mut *param };
    if param.data_type != OSSL_PARAM_UTF8_STRING || bytes.contains(&0) {
        return false;
    }
    param.return_size = bytes.len();
    if param.data.is_null() {
        return true;
    }
    if param.data_size < bytes.len() {
        return false;
    }
    // SAFETY: data_size confirms enough writable bytes for the copied string.
    unsafe {
        copy_nonoverlapping(bytes.as_ptr(), param.data.cast::<u8>(), bytes.len());
        if param.data_size > bytes.len() {
            param.data.cast::<u8>().add(bytes.len()).write(0);
        }
    }
    true
}

pub unsafe fn set_utf8(param: *mut OSSL_PARAM, value: &CStr) -> bool {
    // SAFETY: forwards the same live parameter and NUL-free C string contents.
    unsafe { set_utf8_bytes(param, value.to_bytes()) }
}

pub unsafe fn set_utf8_str(param: *mut OSSL_PARAM, value: &str) -> bool {
    // SAFETY: forwards the same live parameter and borrowed UTF-8 bytes.
    unsafe { set_utf8_bytes(param, value.as_bytes()) }
}

pub unsafe fn set_utf8_static(param: *mut OSSL_PARAM, value: &'static CStr) -> bool {
    if param.is_null() {
        return false;
    }
    // SAFETY: the pointer was checked and comes from a mutable OpenSSL parameter array.
    let param = unsafe { &mut *param };
    if param.data_type == OSSL_PARAM_UTF8_STRING {
        // SAFETY: forwards the same live parameter; the copy setter accepts any CStr lifetime.
        return unsafe { set_utf8(param, value) };
    }
    if param.data_type != OSSL_PARAM_UTF8_PTR {
        return false;
    }
    param.return_size = value.to_bytes().len();
    if param.data.is_null() {
        return true;
    }
    // SAFETY: UTF8_PTR data is a writable pointer slot; data_size describes the referenced string,
    // not the slot. `value` has static lifetime.
    unsafe { param.data.cast::<*const c_char>().write(value.as_ptr()) };
    true
}

pub unsafe fn set_octets(param: *mut OSSL_PARAM, value: &[u8]) -> bool {
    if param.is_null() {
        return false;
    }
    // SAFETY: the pointer was checked and comes from a mutable OpenSSL parameter array.
    let param = unsafe { &mut *param };
    param.return_size = value.len();
    if param.data.is_null() {
        return true;
    }
    if param.data_type != OSSL_PARAM_OCTET_STRING || param.data_size < value.len() {
        return false;
    }
    // SAFETY: data_size confirms a writable destination at least as large as `value`.
    unsafe { copy_nonoverlapping(value.as_ptr(), param.data.cast::<u8>(), value.len()) };
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dynamic_utf8_never_sets_utf8_pointer_backing() {
        let dynamic = std::ffi::CString::new("dynamic").expect("CString");
        let original = c"unchanged".as_ptr();
        let mut slot = original;
        let mut parameter = OSSL_PARAM {
            key: c"value".as_ptr(),
            data_type: OSSL_PARAM_UTF8_PTR,
            data: (&raw mut slot).cast(),
            data_size: 0,
            return_size: usize::MAX,
        };

        // SAFETY: parameter and pointer slot are live for this immediate setter call.
        assert!(!unsafe { set_utf8(&raw mut parameter, &dynamic) });
        assert_eq!(slot, original);
    }

    #[test]
    fn static_utf8_pointer_setter_requires_static_backing() {
        let mut slot: *const c_char = std::ptr::null();
        let mut parameter = OSSL_PARAM {
            key: c"value".as_ptr(),
            data_type: OSSL_PARAM_UTF8_PTR,
            data: (&raw mut slot).cast(),
            data_size: 0,
            return_size: usize::MAX,
        };

        // SAFETY: parameter and pointer slot are live, and the supplied C string is static.
        assert!(unsafe { set_utf8_static(&raw mut parameter, c"static") });
        assert_eq!(slot, c"static".as_ptr());
        assert_eq!(parameter.return_size, 6);
    }

    #[test]
    fn utf8_string_setter_copies_dynamic_data() {
        let dynamic = std::ffi::CString::new("copy").expect("CString");
        let mut buffer = [0x7f_i8; 8];
        let mut parameter = OSSL_PARAM {
            key: c"value".as_ptr(),
            data_type: OSSL_PARAM_UTF8_STRING,
            data: buffer.as_mut_ptr().cast(),
            data_size: buffer.len(),
            return_size: usize::MAX,
        };

        // SAFETY: parameter and destination buffer are live for this immediate setter call.
        assert!(unsafe { set_utf8(&raw mut parameter, &dynamic) });
        assert_eq!(
            &buffer[..5],
            &[b'c' as i8, b'o' as i8, b'p' as i8, b'y' as i8, 0]
        );
    }

    #[test]
    fn integer_setter_supports_size_queries_for_both_integer_types() {
        for data_type in [OSSL_PARAM_INTEGER, OSSL_PARAM_UNSIGNED_INTEGER] {
            let mut parameter = OSSL_PARAM {
                key: c"value".as_ptr(),
                data_type,
                data: std::ptr::null_mut(),
                data_size: 0,
                return_size: usize::MAX,
            };
            // SAFETY: parameter is live and intentionally requests only the required size.
            assert!(unsafe { set_int(&raw mut parameter, 6, false) });
            assert_eq!(parameter.return_size, size_of::<c_int>());
        }
    }

    #[test]
    fn owned_utf8_reports_allocation_failure() {
        let mut value = *b"value";
        let parameter = OSSL_PARAM {
            key: c"value".as_ptr(),
            data_type: OSSL_PARAM_UTF8_STRING,
            data: value.as_mut_ptr().cast(),
            data_size: value.len(),
            return_size: usize::MAX,
        };
        allocation::fail_string_allocation_after(0);

        assert_eq!(
            // SAFETY: parameter and its UTF-8 backing bytes remain live for this call.
            unsafe { get_utf8_owned(&raw const parameter, 64) },
            Err(Utf8OwnedError::Memory)
        );
    }
}
