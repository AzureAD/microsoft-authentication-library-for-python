use std::alloc::{Layout, alloc, dealloc};
use std::ffi::{c_char, c_void};
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::ptr::null_mut;

use cng_core::{CngError, CngKey, Digest, RsaPadding};
use zeroize::Zeroizing;

use crate::abi::{
    ABI_VERSION, BridgeFailure, ErrorCode, MicrosoftIdentityNativeError, clear_error, write_error,
};
use crate::http::read_utf8;

#[repr(C)]
pub struct MicrosoftIdentityNativePublicKey {
    struct_size: u32,
    abi_version: u32,
    bits: u32,
    exponent: u32,
    modulus_length: u32,
    modulus: [u8; 2_048],
}

fn cng_failure(error: &CngError) -> BridgeFailure {
    if error.is_memory_failure() {
        BridgeFailure::memory()
    } else {
        BridgeFailure::system(
            ErrorCode::Provider,
            error.status(),
            "The protected CNG key operation failed; no software fallback was used.",
        )
    }
}

fn guarded(
    error: *mut MicrosoftIdentityNativeError,
    operation: impl FnOnce() -> Result<(), BridgeFailure>,
) -> i32 {
    // SAFETY: the exported caller supplies optional writable error storage.
    unsafe { clear_error(error) };
    match catch_unwind(AssertUnwindSafe(operation)) {
        Ok(Ok(())) => 1,
        Ok(Err(failure)) => {
            // SAFETY: the optional error pointer retains its caller-owned lifetime.
            unsafe { write_error(error, &failure) };
            0
        }
        Err(_) => {
            // SAFETY: the optional error pointer retains its caller-owned lifetime.
            unsafe {
                write_error(
                    error,
                    &BridgeFailure::new(
                        ErrorCode::Io,
                        "A native key operation panic was contained.",
                    ),
                );
            }
            0
        }
    }
}

fn invalid_input() -> BridgeFailure {
    BridgeFailure::new(
        ErrorCode::InvalidArgument,
        "The native key input or output storage is invalid.",
    )
}

fn own_key(key: CngKey) -> Result<*mut c_void, BridgeFailure> {
    #[expect(
        clippy::cast_ptr_alignment,
        reason = "alloc receives the CngKey alignment explicitly"
    )]
    // SAFETY: the layout is nonzero and alloc returns aligned writable storage or null.
    let pointer = unsafe { alloc(Layout::new::<CngKey>()) }.cast::<CngKey>();
    if pointer.is_null() {
        return Err(BridgeFailure::memory());
    }
    // SAFETY: the allocation is uniquely owned writable CngKey storage.
    unsafe { pointer.write(key) };
    Ok(pointer.cast())
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn microsoft_identity_native_key_open_protected(
    name: *const c_char,
    output: *mut *mut c_void,
    error: *mut MicrosoftIdentityNativeError,
) -> i32 {
    guarded(error, || {
        if output.is_null() {
            return Err(invalid_input());
        }
        // SAFETY: output is caller-provided pointer-sized writable storage.
        unsafe { output.write(null_mut()) };
        // SAFETY: the caller keeps the NUL-terminated key name readable for this bounded scan.
        let name = Zeroizing::new(unsafe {
            read_utf8(
                name,
                255,
                "A bounded native-owned protected key name is required.",
            )
        }?);
        if !name.starts_with(cng_core::PROTECTED_KEY_PREFIX) {
            return Err(invalid_input());
        }
        let key = CngKey::open_or_create_protected(&name).map_err(|error| cng_failure(&error))?;
        let pointer = own_key(key)?;
        // SAFETY: output receives ownership of the uniquely allocated initialized object.
        unsafe { output.write(pointer) };
        Ok(())
    })
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn microsoft_identity_native_key_public(
    key: *const c_void,
    output: *mut MicrosoftIdentityNativePublicKey,
    error: *mut MicrosoftIdentityNativeError,
) -> i32 {
    guarded(error, || {
        if key.is_null() || output.is_null() {
            return Err(invalid_input());
        }
        // SAFETY: callers provide a readable size/version prefix before any full structure access.
        let size = unsafe { output.cast::<u32>().read_unaligned() };
        // SAFETY: the second u32 belongs to that same caller-owned prefix.
        let version = unsafe { output.cast::<u32>().add(1).read_unaligned() };
        if size < size_of::<MicrosoftIdentityNativePublicKey>() as u32 || version != ABI_VERSION {
            return Err(invalid_input());
        }
        // SAFETY: key is a live object returned by key_open_protected, not freed concurrently.
        let key = unsafe { &*key.cast::<CngKey>() };
        let mut public = MicrosoftIdentityNativePublicKey {
            struct_size: size_of::<MicrosoftIdentityNativePublicKey>() as u32,
            abi_version: ABI_VERSION,
            bits: key.bits(),
            exponent: 0,
            modulus_length: key.public_modulus().len() as u32,
            modulus: [0; 2_048],
        };
        if key.public_modulus().len() > public.modulus.len() {
            return Err(BridgeFailure::new(
                ErrorCode::Limit,
                "The RSA public modulus exceeds its native bound.",
            ));
        }
        for byte in key.public_exponent() {
            public.exponent = public
                .exponent
                .checked_mul(256)
                .and_then(|value| value.checked_add(u32::from(*byte)))
                .ok_or_else(invalid_input)?;
        }
        public.modulus[..key.public_modulus().len()].copy_from_slice(key.public_modulus());
        // SAFETY: the checked size/version establish writable storage for the entire output.
        unsafe { output.write(public) };
        Ok(())
    })
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn microsoft_identity_native_key_sign_sha256_pss(
    key: *const c_void,
    digest: *const u8,
    digest_length: usize,
    signature: *mut u8,
    signature_capacity: usize,
    written: *mut usize,
    error: *mut MicrosoftIdentityNativeError,
) -> i32 {
    guarded(error, || {
        if key.is_null()
            || digest.is_null()
            || digest_length != 32
            || written.is_null()
            || signature.is_null()
            || signature_capacity == 0
            || signature_capacity > 2_048
        {
            return Err(invalid_input());
        }
        // SAFETY: written is caller-owned writable storage and is reset before any signing.
        unsafe { written.write(0) };
        // SAFETY: key remains live and is not freed concurrently by the caller.
        let key = unsafe { &*key.cast::<CngKey>() };
        // SAFETY: the digest is caller-owned and exactly 32 readable bytes.
        let digest = unsafe { std::slice::from_raw_parts(digest, digest_length) };
        // SAFETY: the caller owns this bounded writable buffer, distinct from the digest.
        let signature = unsafe { std::slice::from_raw_parts_mut(signature, signature_capacity) };
        let length = key
            .sign_digest(
                Digest::Sha256,
                RsaPadding::Pss { salt_len: 32 },
                digest,
                Some(signature),
            )
            .map_err(|error| cng_failure(&error))?;
        // SAFETY: written retains its writable caller-owned lifetime.
        unsafe { written.write(length) };
        Ok(())
    })
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn microsoft_identity_native_key_borrow_handle(
    key: *const c_void,
    output: *mut usize,
    error: *mut MicrosoftIdentityNativeError,
) -> i32 {
    guarded(error, || {
        if key.is_null() || output.is_null() {
            return Err(invalid_input());
        }
        // SAFETY: key is live for this call and for every later use of the borrowed NCrypt handle.
        // The caller must not free/delete that handle or free key while it is borrowed.
        let handle = unsafe { (&*key.cast::<CngKey>()).native_handle() };
        // SAFETY: output is pointer-sized writable caller storage.
        unsafe { output.write(handle) };
        Ok(())
    })
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn microsoft_identity_native_key_free(key: *mut c_void) {
    let _ = catch_unwind(AssertUnwindSafe(|| {
        if !key.is_null() {
            // SAFETY: key is the unique allocation returned by key_open_protected; no call or
            // borrowed handle may remain in flight. Drop and dealloc use its original layout.
            unsafe {
                std::ptr::drop_in_place(key.cast::<CngKey>());
                dealloc(key.cast(), Layout::new::<CngKey>());
            }
        }
    }));
}

#[cfg(test)]
mod tests {
    use super::*;
    use static_assertions::const_assert_eq;

    const_assert_eq!(size_of::<MicrosoftIdentityNativePublicKey>(), 2_068);

    #[test]
    fn software_fixture_exercises_public_sign_borrow_and_free_not_vbs_creation() {
        use cng_core::{DEFAULT_PROVIDER_NAME, delete_test_key, random_bytes};
        use windows_sys::Win32::Security::Cryptography::{
            BCRYPT_PSS_PADDING_INFO, BCRYPT_SHA256_ALGORITHM, NCRYPT_PAD_PSS_FLAG,
            NCryptVerifySignature,
        };

        let mut random = [0_u8; 16];
        random_bytes(&mut random).expect("fixture key suffix");
        let suffix = random
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        let name = format!("openssl-cng-rust-test-bootstrap-{suffix}");
        let key = CngKey::open_or_create(DEFAULT_PROVIDER_NAME, &name, 2_048)
            .expect("software CNG fixture, not a protected-key creation test");
        let modulus = key.public_modulus().to_vec();
        let pointer = own_key(key).expect("native object allocation");
        let result = catch_unwind(AssertUnwindSafe(|| {
            let mut error = MicrosoftIdentityNativeError::empty();
            let mut public = MicrosoftIdentityNativePublicKey {
                struct_size: size_of::<MicrosoftIdentityNativePublicKey>() as u32,
                abi_version: ABI_VERSION,
                bits: 0,
                exponent: 0,
                modulus_length: 0,
                modulus: [0; 2_048],
            };
            assert_ne!(
                // SAFETY: pointer is the live allocation used by the exported constructor; the
                // software fixture bypasses only provisioning. The output is uniquely writable.
                unsafe {
                    microsoft_identity_native_key_public(pointer, &raw mut public, &raw mut error)
                },
                0
            );
            assert_eq!(public.bits, 2_048);
            assert_eq!(public.exponent, 65_537);
            assert_eq!(&public.modulus[..public.modulus_length as usize], modulus);

            let digest = [0x33; 32];
            let mut signature = [0_u8; 256];
            let mut written = 0;
            assert_ne!(
                // SAFETY: the owned key is live; digest and output are distinct bounded buffers.
                unsafe {
                    microsoft_identity_native_key_sign_sha256_pss(
                        pointer,
                        digest.as_ptr(),
                        digest.len(),
                        signature.as_mut_ptr(),
                        signature.len(),
                        &raw mut written,
                        &raw mut error,
                    )
                },
                0
            );
            assert_eq!(written, signature.len());
            let mut borrowed = 0;
            assert_ne!(
                // SAFETY: the allocation remains live until verification of the borrowed handle ends.
                unsafe {
                    microsoft_identity_native_key_borrow_handle(
                        pointer,
                        &raw mut borrowed,
                        &raw mut error,
                    )
                },
                0
            );
            assert_ne!(borrowed, 0);
            let padding = BCRYPT_PSS_PADDING_INFO {
                pszAlgId: BCRYPT_SHA256_ALGORITHM,
                cbSalt: 32,
            };
            assert_eq!(
                // SAFETY: the borrowed key, padding, hash and signature remain valid for this
                // synchronous verification. The handle is neither freed nor transferred.
                unsafe {
                    NCryptVerifySignature(
                        borrowed,
                        (&raw const padding).cast(),
                        digest.as_ptr(),
                        digest.len() as u32,
                        signature.as_ptr(),
                        written as u32,
                        NCRYPT_PAD_PSS_FLAG,
                    )
                },
                0
            );
        }));
        // SAFETY: this is the unique allocation; verification/borrow has completed or unwound.
        unsafe { microsoft_identity_native_key_free(pointer) };
        delete_test_key(DEFAULT_PROVIDER_NAME, &name).expect("delete exact software fixture");
        result.expect("public/sign/borrow ABI fixture");
    }

    #[test]
    fn unowned_key_names_are_rejected_without_returning_a_handle() {
        let mut key = std::ptr::dangling_mut::<c_void>();
        let mut error = MicrosoftIdentityNativeError::empty();
        // SAFETY: name is static, and both output structures are uniquely writable.
        let result = unsafe {
            microsoft_identity_native_key_open_protected(
                c"another-app-owned-key".as_ptr(),
                &raw mut key,
                &raw mut error,
            )
        };
        assert_eq!(result, 0);
        assert!(key.is_null());
        assert_eq!(error.code, ErrorCode::InvalidArgument);
    }

    #[test]
    fn public_key_version_is_checked_before_dereferencing_the_key() {
        let mut prefix = [8_u32, ABI_VERSION + 1];
        let mut error = MicrosoftIdentityNativeError::empty();
        // SAFETY: only the supplied readable prefix can be examined for the invalid version;
        // the dangling key must not be dereferenced on this validation-failure path.
        let result = unsafe {
            microsoft_identity_native_key_public(
                std::ptr::dangling::<c_void>(),
                prefix.as_mut_ptr().cast(),
                &raw mut error,
            )
        };
        assert_eq!(result, 0);
        assert_eq!(error.code, ErrorCode::InvalidArgument);
    }
}
