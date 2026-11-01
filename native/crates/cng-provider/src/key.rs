use std::ffi::{c_int, c_void};
use std::ptr::null_mut;

use cng_core::CngKey;
use openssl_provider_sys::{
    BIGNUM, BN_bin2bn, BN_bn2bin, BN_free, BN_is_negative, BN_is_odd, BN_num_bits, OSSL_PARAM,
    OSSL_PARAM_BLD_free, OSSL_PARAM_BLD_new, OSSL_PARAM_BLD_push_BN, OSSL_PARAM_BLD_to_param,
    OSSL_PARAM_free, OSSL_PARAM_get_BN, OSSL_PARAM_set_BN, OsslCallback,
};
use zeroize::Zeroize;

use crate::allocation;
use crate::error::{self, INVALID_PARAMETER};
use crate::params;

pub const KEY_MAGIC: u32 = 0x434E_474B;
const MAX_MODULUS_ENCODING_BYTES: usize = 16_384 / 8 + size_of::<u64>();
const MAX_EXPONENT_ENCODING_BYTES: usize = size_of::<u64>();

#[cfg(test)]
thread_local! {
    static BN_CONVERSION_ATTEMPTS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

#[derive(Clone)]
pub enum KeyMaterial {
    Empty,
    Private(CngKey),
    Public {
        modulus: Vec<u8>,
        exponent: Vec<u8>,
        bits: u32,
    },
}

pub struct ProviderKey {
    pub magic: u32,
    pub provider_context: *mut c_void,
    pub material: KeyMaterial,
}

impl ProviderKey {
    pub fn new(provider_context: *mut c_void, material: KeyMaterial) -> *mut Self {
        let Some(key) = allocation::try_box(Self {
            magic: KEY_MAGIC,
            provider_context,
            material,
        }) else {
            error::raise_memory_failure();
            return null_mut();
        };
        Box::into_raw(key)
    }

    pub fn is_valid(&self) -> bool {
        self.magic == KEY_MAGIC && !matches!(self.material, KeyMaterial::Empty)
    }

    pub fn private_key(&self) -> Option<&CngKey> {
        match &self.material {
            KeyMaterial::Private(key) => Some(key),
            KeyMaterial::Empty | KeyMaterial::Public { .. } => None,
        }
    }

    pub fn public_parts(&self) -> Option<(&[u8], &[u8], u32)> {
        match &self.material {
            KeyMaterial::Private(key) => {
                Some((key.public_modulus(), key.public_exponent(), key.bits()))
            }
            KeyMaterial::Public {
                modulus,
                exponent,
                bits,
            } => Some((modulus, exponent, *bits)),
            KeyMaterial::Empty => None,
        }
    }

    pub fn security_bits(&self) -> u32 {
        match &self.material {
            KeyMaterial::Private(key) => key.security_bits(),
            KeyMaterial::Public { bits, .. } => rsa_security_bits(*bits),
            KeyMaterial::Empty => 0,
        }
    }

    pub fn max_size(&self) -> usize {
        self.public_parts()
            .map_or(0, |(modulus, _, _)| modulus.len())
    }

    pub fn public_is_valid(&self) -> bool {
        let Some((modulus, exponent, bits)) = self.public_parts() else {
            return false;
        };
        modulus.len() >= 256
            && !modulus.iter().all(|value| *value == 0)
            && modulus.last().is_some_and(|value| value & 1 == 1)
            && !exponent.is_empty()
            && exponent.last().is_some_and(|value| value & 1 == 1)
            && (exponent.len() > 1 || exponent[0] >= 3)
            && bits >= 2_048
            && self.security_bits() >= 112
    }
}

impl Drop for ProviderKey {
    fn drop(&mut self) {
        self.magic = 0;
        if let KeyMaterial::Public {
            modulus, exponent, ..
        } = &mut self.material
        {
            modulus.zeroize();
            exponent.zeroize();
        }
    }
}

pub unsafe fn free_provider_key(pointer: *mut ProviderKey) {
    if pointer.is_null() {
        return;
    }
    // SAFETY: callers transfer a Box allocation created by `ProviderKey::new` exactly once.
    let mut key = unsafe { Box::from_raw(pointer) };
    if key.magic != KEY_MAGIC {
        std::mem::forget(key);
        return;
    }
    key.magic = 0;
}

pub unsafe fn import_public(key: &mut ProviderKey, params_array: *const OSSL_PARAM) -> bool {
    // SAFETY: the parameter array is OpenSSL-owned and terminated.
    let modulus_param = unsafe { params::locate(params_array, c"n") };
    // SAFETY: the parameter array is OpenSSL-owned and terminated.
    let exponent_param = unsafe { params::locate(params_array, c"e") };
    if modulus_param.is_null() || exponent_param.is_null() {
        error::raise(
            INVALID_PARAMETER,
            "RSA public import requires both modulus and exponent",
        );
        return false;
    }
    // SAFETY: both parameters were located in the live OpenSSL-owned array.
    let modulus = unsafe { &*modulus_param };
    // SAFETY: same invariant for the exponent parameter.
    let exponent = unsafe { &*exponent_param };
    if [modulus, exponent].iter().any(|parameter| {
        parameter.data_type != openssl_provider_sys::OSSL_PARAM_UNSIGNED_INTEGER
            || parameter.data.is_null()
            || parameter.data_size == 0
    }) || modulus.data_size > MAX_MODULUS_ENCODING_BYTES
        || exponent.data_size > MAX_EXPONENT_ENCODING_BYTES
    {
        error::raise(
            INVALID_PARAMETER,
            "RSA public parameter encodings exceed their bounds or have an invalid type",
        );
        return false;
    }

    let mut modulus_bn: *mut BIGNUM = null_mut();
    let mut exponent_bn: *mut BIGNUM = null_mut();
    #[cfg(test)]
    BN_CONVERSION_ATTEMPTS.with(|attempts| attempts.set(attempts.get() + 1));
    // SAFETY: OpenSSL owns the source parameters and initializes the returned BIGNUM pointers.
    let parsed = unsafe {
        OSSL_PARAM_get_BN(modulus_param, &raw mut modulus_bn) == 1
            && OSSL_PARAM_get_BN(exponent_param, &raw mut exponent_bn) == 1
    };
    if !parsed || modulus_bn.is_null() || exponent_bn.is_null() {
        // SAFETY: null is accepted and any returned BIGNUMs are owned here.
        unsafe {
            BN_free(modulus_bn);
            BN_free(exponent_bn);
        }
        error::raise(INVALID_PARAMETER, "RSA public parameters are invalid");
        return false;
    }

    // SAFETY: both BIGNUM pointers are live and owned here.
    let modulus_bits = unsafe { BN_num_bits(modulus_bn) };
    // SAFETY: both BIGNUM pointers are live and owned here.
    let exponent_bits = unsafe { BN_num_bits(exponent_bn) };
    // SAFETY: both BIGNUM pointers are live and owned here.
    let valid = unsafe {
        BN_is_negative(modulus_bn) == 0
            && BN_is_negative(exponent_bn) == 0
            && BN_is_odd(modulus_bn) == 1
            && BN_is_odd(exponent_bn) == 1
    } && (2_048..=16_384).contains(&modulus_bits)
        && (2..=32).contains(&exponent_bits);
    if !valid {
        // SAFETY: both BIGNUMs are owned here.
        unsafe {
            BN_free(modulus_bn);
            BN_free(exponent_bn);
        }
        error::raise(
            INVALID_PARAMETER,
            "a bounded odd RSA modulus and odd exponent are required",
        );
        return false;
    }

    let Some(mut modulus) = allocation::try_zeroed_bytes(((modulus_bits + 7) / 8) as usize) else {
        // SAFETY: both BIGNUMs are owned here.
        unsafe {
            BN_free(modulus_bn);
            BN_free(exponent_bn);
        }
        error::raise_memory_failure();
        return false;
    };
    let Some(mut exponent) = allocation::try_zeroed_bytes(((exponent_bits + 7) / 8) as usize)
    else {
        // SAFETY: both BIGNUMs are owned here.
        unsafe {
            BN_free(modulus_bn);
            BN_free(exponent_bn);
        }
        error::raise_memory_failure();
        return false;
    };
    // SAFETY: output buffers are sized from BN_num_bits and both BIGNUMs remain live.
    let converted = unsafe {
        BN_bn2bin(modulus_bn, modulus.as_mut_ptr()) == modulus.len() as c_int
            && BN_bn2bin(exponent_bn, exponent.as_mut_ptr()) == exponent.len() as c_int
    };
    // SAFETY: both BIGNUMs are owned here and no longer needed.
    unsafe {
        BN_free(modulus_bn);
        BN_free(exponent_bn);
    }
    if !converted
        || exponent
            .iter()
            .fold(0_u64, |value, byte| (value << 8) | u64::from(*byte))
            < 3
    {
        modulus.zeroize();
        exponent.zeroize();
        error::raise(INVALID_PARAMETER, "unable to retain RSA public parameters");
        return false;
    }

    key.material = KeyMaterial::Public {
        modulus,
        exponent,
        bits: modulus_bits as u32,
    };
    true
}

pub unsafe fn set_bn_param(key: &ProviderKey, param: *mut OSSL_PARAM, bytes: &[u8]) -> bool {
    if param.is_null() || bytes.len() > c_int::MAX as usize {
        return false;
    }
    // SAFETY: `bytes` is readable and the returned BIGNUM is owned here.
    let number = unsafe { BN_bin2bn(bytes.as_ptr(), bytes.len() as c_int, null_mut()) };
    if number.is_null() {
        error::raise_memory_failure();
        return false;
    }
    // SAFETY: the mutable parameter and BIGNUM are live for this immediate OpenSSL call.
    let result = unsafe { OSSL_PARAM_set_BN(param, number) == 1 };
    // SAFETY: `number` is owned here.
    unsafe { BN_free(number) };
    if !result {
        let _ = key;
        error::raise(
            INVALID_PARAMETER,
            "unable to return an RSA public parameter",
        );
    }
    result
}

pub unsafe fn export_public(
    key: &ProviderKey,
    callback: OsslCallback,
    callback_argument: *mut c_void,
) -> bool {
    let Some(callback) = callback else {
        return false;
    };
    let Some((modulus, exponent, _)) = key.public_parts() else {
        return false;
    };
    if modulus.len() > c_int::MAX as usize || exponent.len() > c_int::MAX as usize {
        return false;
    }

    // SAFETY: input slices are readable and the returned BIGNUMs are owned here.
    let modulus_bn = unsafe { BN_bin2bn(modulus.as_ptr(), modulus.len() as c_int, null_mut()) };
    // SAFETY: input slices are readable and the returned BIGNUMs are owned here.
    let exponent_bn = unsafe { BN_bin2bn(exponent.as_ptr(), exponent.len() as c_int, null_mut()) };
    // SAFETY: returns an owned builder or null.
    let builder = unsafe { OSSL_PARAM_BLD_new() };
    let mut built = null_mut();
    // SAFETY: all pointers are live, and OpenSSL copies/retains values through the builder.
    let okay = !modulus_bn.is_null()
        && !exponent_bn.is_null()
        && !builder.is_null()
        && unsafe { OSSL_PARAM_BLD_push_BN(builder, c"n".as_ptr(), modulus_bn) == 1 }
        // SAFETY: same live-builder and BIGNUM invariant.
        && unsafe { OSSL_PARAM_BLD_push_BN(builder, c"e".as_ptr(), exponent_bn) == 1 };
    if okay {
        // SAFETY: builder is live and returns an independently owned parameter array.
        built = unsafe { OSSL_PARAM_BLD_to_param(builder) };
    }
    let result = if built.is_null() {
        error::raise_memory_failure();
        false
    } else {
        // SAFETY: `built` is a valid terminated parameter array for this synchronous callback.
        unsafe { callback(built, callback_argument) == 1 }
    };
    // SAFETY: all allocations are owned here and null is accepted by these free functions.
    unsafe {
        OSSL_PARAM_free(built);
        OSSL_PARAM_BLD_free(builder);
        BN_free(modulus_bn);
        BN_free(exponent_bn);
    }
    result
}

fn rsa_security_bits(bits: u32) -> u32 {
    match bits {
        15_360.. => 256,
        7_680.. => 192,
        3_072.. => 128,
        2_048.. => 112,
        1_024.. => 80,
        _ => 0,
    }
}

pub fn names_match(left: &ProviderKey, right: &ProviderKey) -> bool {
    let Some((left_modulus, left_exponent, _)) = left.public_parts() else {
        return false;
    };
    let Some((right_modulus, right_exponent, _)) = right.public_parts() else {
        return false;
    };
    left_modulus == right_modulus && left_exponent == right_exponent
}

#[cfg(test)]
mod tests {
    use super::*;

    fn import_encoded_public(modulus: &mut [u8], exponent: &mut [u8]) -> bool {
        let parameters = [
            OSSL_PARAM {
                key: c"n".as_ptr(),
                data_type: openssl_provider_sys::OSSL_PARAM_UNSIGNED_INTEGER,
                data: modulus.as_mut_ptr().cast(),
                data_size: modulus.len(),
                return_size: usize::MAX,
            },
            OSSL_PARAM {
                key: c"e".as_ptr(),
                data_type: openssl_provider_sys::OSSL_PARAM_UNSIGNED_INTEGER,
                data: exponent.as_mut_ptr().cast(),
                data_size: exponent.len(),
                return_size: usize::MAX,
            },
            openssl_provider_sys::param_end(),
        ];
        let mut key = ProviderKey {
            magic: KEY_MAGIC,
            provider_context: null_mut(),
            material: KeyMaterial::Empty,
        };
        // SAFETY: both native-endian encoding buffers and the terminated array remain live.
        unsafe { import_public(&mut key, parameters.as_ptr()) }
    }

    #[test]
    fn oversized_encodings_never_reach_bignum_conversion() {
        for (modulus_length, exponent_length) in [
            (MAX_MODULUS_ENCODING_BYTES + 1, 8),
            (256, MAX_EXPONENT_ENCODING_BYTES + 1),
            (65_536, 8),
            (256, 65_536),
        ] {
            for significant in [false, true] {
                let mut modulus = vec![if significant { 0x81 } else { 0 }; modulus_length];
                modulus[0] = 1;
                modulus[255] = 0x80;
                let mut exponent = vec![if significant { 3 } else { 0 }; exponent_length];
                exponent[0] = 3;
                BN_CONVERSION_ATTEMPTS.set(0);
                assert!(!import_encoded_public(&mut modulus, &mut exponent));
                assert_eq!(BN_CONVERSION_ATTEMPTS.get(), 0);
            }
        }
    }

    #[test]
    fn bounded_encodings_preserve_padding_and_mathematical_limits() {
        for (bits, padding) in [(2_048, 0), (16_384, 0), (16_384, size_of::<u64>())] {
            let mut modulus = vec![0; bits / 8 + padding];
            modulus[0] = 1;
            modulus[bits / 8 - 1] = 0x80;
            for exponent_length in [4, 8] {
                let mut exponent = vec![0; exponent_length];
                exponent[0] = 3;
                assert!(import_encoded_public(&mut modulus, &mut exponent));
            }
        }
        let mut modulus = vec![0; 256];
        modulus[0] = 1;
        modulus[255] = 0x80;
        assert!(!import_encoded_public(&mut modulus, &mut [1, 0, 0, 0, 1]));
        assert!(!import_encoded_public(&mut modulus, &mut [2]));
        let mut too_many_bits = vec![0; 2049];
        too_many_bits[0] = 1;
        too_many_bits[2048] = 1;
        assert!(!import_encoded_public(&mut too_many_bits, &mut [3]));
    }

    fn public_key(modulus_last_byte: u8) -> ProviderKey {
        let mut modulus = vec![0x80; 256];
        modulus[255] = modulus_last_byte;
        ProviderKey {
            magic: KEY_MAGIC,
            provider_context: null_mut(),
            material: KeyMaterial::Public {
                modulus,
                exponent: vec![3],
                bits: 2_048,
            },
        }
    }

    #[test]
    fn public_validation_accepts_odd_rsa_modulus() {
        assert!(public_key(1).public_is_valid());
    }

    #[test]
    fn public_validation_rejects_even_rsa_modulus() {
        assert!(!public_key(2).public_is_valid());
    }

    #[test]
    fn public_import_rejects_even_rsa_modulus() {
        let mut modulus = [0x80_u8; 256];
        modulus[255] = 2;
        let exponent = [3_u8];
        let mut modulus_storage = [0_u8; 256];
        let mut exponent_storage = [0_u8; 8];
        let mut modulus_parameter = OSSL_PARAM {
            key: c"n".as_ptr(),
            data_type: openssl_provider_sys::OSSL_PARAM_UNSIGNED_INTEGER,
            data: modulus_storage.as_mut_ptr().cast(),
            data_size: modulus_storage.len(),
            return_size: usize::MAX,
        };
        let mut exponent_parameter = OSSL_PARAM {
            key: c"e".as_ptr(),
            data_type: openssl_provider_sys::OSSL_PARAM_UNSIGNED_INTEGER,
            data: exponent_storage.as_mut_ptr().cast(),
            data_size: exponent_storage.len(),
            return_size: usize::MAX,
        };
        // SAFETY: source bytes are readable and returned BIGNUMs are owned by this test.
        let modulus_bn = unsafe { BN_bin2bn(modulus.as_ptr(), modulus.len() as c_int, null_mut()) };
        // SAFETY: same invariant for the exponent.
        let exponent_bn =
            unsafe { BN_bin2bn(exponent.as_ptr(), exponent.len() as c_int, null_mut()) };
        assert!(!modulus_bn.is_null());
        assert!(!exponent_bn.is_null());
        // SAFETY: parameters and BIGNUMs are live for these immediate conversion calls.
        let modulus_set = unsafe { OSSL_PARAM_set_BN(&raw mut modulus_parameter, modulus_bn) };
        assert_eq!(modulus_set, 1);
        // SAFETY: same live parameter and BIGNUM invariant.
        let exponent_set = unsafe { OSSL_PARAM_set_BN(&raw mut exponent_parameter, exponent_bn) };
        assert_eq!(exponent_set, 1);
        // SAFETY: both BIGNUMs are owned by this test.
        unsafe {
            BN_free(modulus_bn);
            BN_free(exponent_bn);
        }
        let parameters = [
            modulus_parameter,
            exponent_parameter,
            openssl_provider_sys::param_end(),
        ];
        let mut key = ProviderKey {
            magic: KEY_MAGIC,
            provider_context: null_mut(),
            material: KeyMaterial::Empty,
        };

        // SAFETY: the parameter array is live, valid, and terminated.
        assert!(!unsafe { import_public(&mut key, parameters.as_ptr()) });
        assert!(matches!(key.material, KeyMaterial::Empty));
    }
}
