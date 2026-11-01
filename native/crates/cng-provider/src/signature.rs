use std::ffi::{CStr, c_char, c_int, c_void};
use std::ptr::{null, null_mut};

use cng_core::{CngKey, Digest, HashState, RsaPadding};
use openssl_provider_sys::{
    OSSL_FUNC_SIGNATURE_DIGEST_SIGN, OSSL_FUNC_SIGNATURE_DIGEST_SIGN_FINAL,
    OSSL_FUNC_SIGNATURE_DIGEST_SIGN_INIT, OSSL_FUNC_SIGNATURE_DIGEST_SIGN_UPDATE,
    OSSL_FUNC_SIGNATURE_DUPCTX, OSSL_FUNC_SIGNATURE_FREECTX, OSSL_FUNC_SIGNATURE_GET_CTX_PARAMS,
    OSSL_FUNC_SIGNATURE_GETTABLE_CTX_PARAMS, OSSL_FUNC_SIGNATURE_NEWCTX,
    OSSL_FUNC_SIGNATURE_SET_CTX_PARAMS, OSSL_FUNC_SIGNATURE_SETTABLE_CTX_PARAMS, OSSL_PARAM,
    OSSL_PARAM_INTEGER, OSSL_PARAM_OCTET_STRING, OSSL_PARAM_UTF8_STRING, OsslDispatch,
    RSA_PKCS1_PADDING, RSA_PKCS1_PSS_PADDING, RSA_PSS_SALTLEN_AUTO,
    RSA_PSS_SALTLEN_AUTO_DIGEST_MAX, RSA_PSS_SALTLEN_DIGEST, RSA_PSS_SALTLEN_MAX, param_definition,
    param_end,
};

use crate::allocation;
use crate::error::{
    self, CNG_FAILURE, INVALID_PARAMETER, KEY_NOT_LOADED, SIGNATURE_FAILURE, UNSUPPORTED_DIGEST,
    UNSUPPORTED_PADDING,
};
use crate::key::{KEY_MAGIC, ProviderKey};
use crate::params;

struct StaticParams<const N: usize>([OSSL_PARAM; N]);
// SAFETY: these arrays contain only immutable static key pointers and null data pointers.
unsafe impl<const N: usize> Sync for StaticParams<N> {}

static SETTABLE_PARAMS: StaticParams<7> = StaticParams([
    param_definition(c"digest".as_ptr(), OSSL_PARAM_UTF8_STRING),
    param_definition(c"pad-mode".as_ptr(), OSSL_PARAM_UTF8_STRING),
    param_definition(c"pad-mode".as_ptr(), OSSL_PARAM_INTEGER),
    param_definition(c"mgf1-digest".as_ptr(), OSSL_PARAM_UTF8_STRING),
    param_definition(c"saltlen".as_ptr(), OSSL_PARAM_UTF8_STRING),
    param_definition(c"saltlen".as_ptr(), OSSL_PARAM_INTEGER),
    param_end(),
]);
static GETTABLE_PARAMS: StaticParams<8> = StaticParams([
    param_definition(c"pad-mode".as_ptr(), OSSL_PARAM_UTF8_STRING),
    param_definition(c"pad-mode".as_ptr(), OSSL_PARAM_INTEGER),
    param_definition(c"digest".as_ptr(), OSSL_PARAM_UTF8_STRING),
    param_definition(c"mgf1-digest".as_ptr(), OSSL_PARAM_UTF8_STRING),
    param_definition(c"saltlen".as_ptr(), OSSL_PARAM_UTF8_STRING),
    param_definition(c"saltlen".as_ptr(), OSSL_PARAM_INTEGER),
    param_definition(c"algorithm-id".as_ptr(), OSSL_PARAM_OCTET_STRING),
    param_end(),
]);

pub struct SignatureContext {
    provider_context: *mut c_void,
    key: Option<CngKey>,
    hash: Option<HashState>,
    digest: Option<Digest>,
    hash_started: bool,
    padding: c_int,
    pss_salt_length: c_int,
    mgf1_digest: Option<Digest>,
    mgf1_explicit: bool,
}

impl SignatureContext {
    fn new(provider_context: *mut c_void) -> Self {
        Self {
            provider_context,
            key: None,
            hash: None,
            digest: None,
            hash_started: false,
            padding: RSA_PKCS1_PADDING,
            pss_salt_length: RSA_PSS_SALTLEN_DIGEST,
            mgf1_digest: None,
            mgf1_explicit: false,
        }
    }

    fn set_digest(&mut self, name: &str) -> bool {
        let Some(digest) = Digest::parse(name) else {
            error::raise(
                UNSUPPORTED_DIGEST,
                format_args!("digest {name} is not supported"),
            );
            return false;
        };
        if self.hash_started && self.digest != Some(digest) {
            error::raise(
                INVALID_PARAMETER,
                "the digest cannot change after message data is supplied",
            );
            return false;
        }
        if self.mgf1_explicit && self.mgf1_digest != Some(digest) {
            error::raise(
                UNSUPPORTED_DIGEST,
                "Windows CNG requires the PSS MGF1 digest to match the message digest",
            );
            return false;
        }
        if self.digest == Some(digest) && self.hash.is_some() {
            return true;
        }
        let hash = match HashState::new(digest) {
            Ok(hash) => hash,
            Err(cng_error) => {
                error::raise_cng(CNG_FAILURE, &cng_error);
                return false;
            }
        };
        self.hash = Some(hash);
        self.digest = Some(digest);
        if !self.mgf1_explicit {
            self.mgf1_digest = Some(digest);
        }
        true
    }

    unsafe fn set_params(&mut self, input: *const OSSL_PARAM) -> bool {
        if input.is_null() {
            return true;
        }
        let mut padding = self.padding;
        let mut salt_length = self.pss_salt_length;
        let mut digest = self.digest;
        let mut mgf1_digest = self.mgf1_digest;
        let mut mgf1_explicit = self.mgf1_explicit;

        // SAFETY: `input` is an OpenSSL-owned terminated parameter array.
        let parameter = unsafe { params::locate(input, c"pad-mode") };
        if !parameter.is_null() {
            // SAFETY: the located parameter remains live with `input`.
            let value = match unsafe { parse_padding(parameter) } {
                Ok(Some(value)) => value,
                Ok(None) | Err(params::Utf8OwnedError::Invalid) => {
                    error::raise(
                        UNSUPPORTED_PADDING,
                        "only PKCS#1 v1.5 and RSA-PSS signing are supported",
                    );
                    return false;
                }
                Err(params::Utf8OwnedError::Memory) => {
                    error::raise_memory_failure();
                    return false;
                }
            };
            padding = value;
        }

        // SAFETY: `input` is an OpenSSL-owned terminated parameter array.
        let parameter = unsafe { params::locate(input, c"digest") };
        if !parameter.is_null() {
            // SAFETY: the located parameter remains live with `input`.
            let value = match unsafe { params::get_utf8_owned(parameter, 64) } {
                Ok(value) => value,
                Err(params::Utf8OwnedError::Memory) => {
                    error::raise_memory_failure();
                    return false;
                }
                Err(params::Utf8OwnedError::Invalid) => {
                    error::raise(INVALID_PARAMETER, "the digest parameter is invalid");
                    return false;
                }
            };
            let Some(value) = Digest::parse(&value) else {
                error::raise(
                    UNSUPPORTED_DIGEST,
                    format_args!("digest {value} is not supported"),
                );
                return false;
            };
            if self.hash_started && self.digest != Some(value) {
                error::raise(
                    INVALID_PARAMETER,
                    "the digest cannot change after message data is supplied",
                );
                return false;
            }
            digest = Some(value);
        }

        if padding != RSA_PKCS1_PSS_PADDING {
            mgf1_digest = None;
            mgf1_explicit = false;
        }

        // SAFETY: `input` is an OpenSSL-owned terminated parameter array.
        let parameter = unsafe { params::locate(input, c"mgf1-digest") };
        if !parameter.is_null() {
            if padding != RSA_PKCS1_PSS_PADDING {
                error::raise(INVALID_PARAMETER, "an MGF1 digest requires RSA-PSS padding");
                return false;
            }
            // SAFETY: the located parameter remains live with `input`.
            let value = match unsafe { params::get_utf8_owned(parameter, 64) } {
                Ok(value) => value,
                Err(params::Utf8OwnedError::Memory) => {
                    error::raise_memory_failure();
                    return false;
                }
                Err(params::Utf8OwnedError::Invalid) => {
                    error::raise(INVALID_PARAMETER, "the MGF1 digest parameter is invalid");
                    return false;
                }
            };
            let parsed_mgf1 = Digest::parse(&value);
            if parsed_mgf1.is_none() || parsed_mgf1 != digest {
                error::raise(
                    UNSUPPORTED_DIGEST,
                    "Windows CNG requires the PSS MGF1 digest to match the message digest",
                );
                return false;
            }
            mgf1_digest = parsed_mgf1;
            mgf1_explicit = true;
        }

        // SAFETY: `input` is an OpenSSL-owned terminated parameter array.
        let parameter = unsafe { params::locate(input, c"saltlen") };
        if !parameter.is_null() {
            // SAFETY: the located parameter remains live with `input`.
            let value = match unsafe { parse_salt_length(parameter) } {
                Ok(Some(value)) => value,
                Ok(None) | Err(params::Utf8OwnedError::Invalid) => {
                    error::raise(INVALID_PARAMETER, "the RSA-PSS salt length is invalid");
                    return false;
                }
                Err(params::Utf8OwnedError::Memory) => {
                    error::raise_memory_failure();
                    return false;
                }
            };
            if padding != RSA_PKCS1_PSS_PADDING {
                error::raise(
                    INVALID_PARAMETER,
                    "RSA-PSS salt length requires RSA-PSS padding",
                );
                return false;
            }
            salt_length = value;
        }

        let replacement_hash = if digest != self.digest || self.hash.is_none() {
            match digest {
                Some(digest) => match HashState::new(digest) {
                    Ok(hash) => Some(hash),
                    Err(cng_error) => {
                        error::raise_cng(CNG_FAILURE, &cng_error);
                        return false;
                    }
                },
                None => None,
            }
        } else {
            None
        };
        if !mgf1_explicit {
            mgf1_digest = digest;
        }
        self.padding = padding;
        self.pss_salt_length = salt_length;
        self.digest = digest;
        self.mgf1_digest = mgf1_digest;
        self.mgf1_explicit = mgf1_explicit;
        if let Some(hash) = replacement_hash {
            self.hash = Some(hash);
        }
        true
    }

    fn resolved_padding(&self) -> Option<RsaPadding> {
        match self.padding {
            RSA_PKCS1_PADDING => Some(RsaPadding::Pkcs1),
            RSA_PKCS1_PSS_PADDING => {
                let key = self.key.as_ref()?;
                let digest = self.digest?;
                resolved_pss_salt_length(
                    self.pss_salt_length,
                    key.bits(),
                    key.max_signature_size(),
                    digest.output_len(),
                )
                .map(|salt_len| RsaPadding::Pss { salt_len })
            }
            _ => None,
        }
    }

    fn sign_digest(
        &self,
        digest_bytes: &[u8],
        signature: *mut u8,
        signature_length: *mut usize,
        signature_size: usize,
    ) -> c_int {
        let (Some(key), Some(digest)) = (&self.key, self.digest) else {
            return 0;
        };
        if signature_length.is_null() {
            return 0;
        }
        let Some(padding) = self.resolved_padding() else {
            error::raise(
                INVALID_PARAMETER,
                "the RSA padding or PSS salt length is invalid for this key",
            );
            return 0;
        };
        let output = if signature.is_null() {
            None
        } else {
            // SAFETY: OpenSSL declares the signature pointer writable for `signature_size` bytes.
            Some(unsafe { std::slice::from_raw_parts_mut(signature, signature_size) })
        };
        match key.sign_digest(digest, padding, digest_bytes, output) {
            Ok(written) => {
                // SAFETY: the pointer was checked and OpenSSL owns writable size_t storage.
                unsafe { *signature_length = written };
                1
            }
            Err(cng_error) => {
                error::raise_cng(SIGNATURE_FAILURE, &cng_error);
                0
            }
        }
    }
}

unsafe fn parse_padding(
    parameter: *const OSSL_PARAM,
) -> Result<Option<c_int>, params::Utf8OwnedError> {
    // SAFETY: caller provides a live parameter pointer.
    if let Some(value) = unsafe { params::get_int(parameter) } {
        return Ok(matches!(value, RSA_PKCS1_PADDING | RSA_PKCS1_PSS_PADDING).then_some(value));
    }
    // SAFETY: caller provides a live parameter pointer.
    let value = unsafe { params::get_utf8_owned(parameter, 32) }?;
    if value.eq_ignore_ascii_case("pkcs1") {
        Ok(Some(RSA_PKCS1_PADDING))
    } else if value.eq_ignore_ascii_case("pss") {
        Ok(Some(RSA_PKCS1_PSS_PADDING))
    } else {
        Ok(None)
    }
}

unsafe fn parse_salt_length(
    parameter: *const OSSL_PARAM,
) -> Result<Option<c_int>, params::Utf8OwnedError> {
    // SAFETY: caller provides a live parameter pointer.
    if let Some(value) = unsafe { params::get_int(parameter) } {
        return Ok((value >= 0
            || matches!(
                value,
                RSA_PSS_SALTLEN_DIGEST
                    | RSA_PSS_SALTLEN_MAX
                    | RSA_PSS_SALTLEN_AUTO
                    | RSA_PSS_SALTLEN_AUTO_DIGEST_MAX
            ))
        .then_some(value));
    }
    // SAFETY: caller provides a live parameter pointer.
    let value = unsafe { params::get_utf8_owned(parameter, 32) }?;
    if value.eq_ignore_ascii_case("digest") {
        Ok(Some(RSA_PSS_SALTLEN_DIGEST))
    } else if value.eq_ignore_ascii_case("max") {
        Ok(Some(RSA_PSS_SALTLEN_MAX))
    } else if value.eq_ignore_ascii_case("auto") {
        Ok(Some(RSA_PSS_SALTLEN_AUTO))
    } else if value.eq_ignore_ascii_case("auto-digestmax") {
        Ok(Some(RSA_PSS_SALTLEN_AUTO_DIGEST_MAX))
    } else {
        Ok(value.parse::<c_int>().ok().filter(|parsed| *parsed >= 0))
    }
}

pub fn resolved_pss_salt_length(
    configured: c_int,
    key_bits: u32,
    modulus_bytes: usize,
    digest_bytes: usize,
) -> Option<u32> {
    let mut maximum = modulus_bytes.checked_sub(digest_bytes + 2)?;
    if key_bits & 7 == 1 {
        maximum = maximum.checked_sub(1)?;
    }
    let resolved = match configured {
        RSA_PSS_SALTLEN_DIGEST => digest_bytes,
        RSA_PSS_SALTLEN_MAX | RSA_PSS_SALTLEN_AUTO => maximum,
        RSA_PSS_SALTLEN_AUTO_DIGEST_MAX => maximum.min(digest_bytes),
        explicit if explicit >= 0 => usize::try_from(explicit)
            .ok()
            .filter(|value| *value <= maximum)?,
        _ => return None,
    };
    u32::try_from(resolved).ok()
}

unsafe extern "C" fn signature_newctx(
    provider_context: *mut c_void,
    _properties: *const c_char,
) -> *mut c_void {
    ffi_guard!(null_mut(), {
        let Some(context) = allocation::try_box(SignatureContext::new(provider_context)) else {
            error::raise_memory_failure();
            return null_mut();
        };
        Box::into_raw(context).cast()
    })
}

unsafe extern "C" fn signature_freectx(signature_context: *mut c_void) {
    ffi_guard_void!({
        if !signature_context.is_null() {
            // SAFETY: OpenSSL releases a Box allocation created by `signature_newctx` exactly once.
            drop(unsafe { Box::from_raw(signature_context.cast::<SignatureContext>()) });
        }
    });
}

unsafe extern "C" fn signature_dupctx(signature_context: *mut c_void) -> *mut c_void {
    ffi_guard!(null_mut(), {
        if signature_context.is_null() {
            return null_mut();
        }
        // SAFETY: OpenSSL passes a live signature context.
        let source = unsafe { &*signature_context.cast::<SignatureContext>() };
        let hash = match &source.hash {
            Some(value) => match value.try_clone() {
                Ok(copy) => Some(copy),
                Err(cng_error) => {
                    error::raise_cng(CNG_FAILURE, &cng_error);
                    return null_mut();
                }
            },
            None => None,
        };
        let Some(context) = allocation::try_box(SignatureContext {
            provider_context: source.provider_context,
            key: source.key.clone(),
            hash,
            digest: source.digest,
            hash_started: source.hash_started,
            padding: source.padding,
            pss_salt_length: source.pss_salt_length,
            mgf1_digest: source.mgf1_digest,
            mgf1_explicit: source.mgf1_explicit,
        }) else {
            error::raise_memory_failure();
            return null_mut();
        };
        Box::into_raw(context).cast()
    })
}

unsafe extern "C" fn digest_sign_init(
    signature_context: *mut c_void,
    digest_name: *const c_char,
    provider_key: *mut c_void,
    input: *const OSSL_PARAM,
) -> c_int {
    ffi_guard!(0, {
        if signature_context.is_null() || provider_key.is_null() {
            return 0;
        }
        // SAFETY: OpenSSL passes live contexts and provider key data.
        let context = unsafe { &mut *signature_context.cast::<SignatureContext>() };
        // SAFETY: provider key data was allocated by this provider.
        let key = unsafe { &*provider_key.cast::<ProviderKey>() };
        if key.magic != KEY_MAGIC || key.private_key().is_none() {
            error::raise(KEY_NOT_LOADED, "DigestSignInit requires a loaded CNG key");
            return 0;
        }

        context.hash = None;
        context.digest = None;
        context.mgf1_digest = None;
        context.mgf1_explicit = false;
        context.hash_started = false;
        context.key = key.private_key().cloned();
        context.padding = RSA_PKCS1_PADDING;
        context.pss_salt_length = RSA_PSS_SALTLEN_DIGEST;
        let digest = if digest_name.is_null() {
            "SHA256"
        } else {
            // SAFETY: OpenSSL supplies a NUL-terminated digest name.
            match unsafe { std::ffi::CStr::from_ptr(digest_name) }.to_str() {
                Ok(value) => value,
                Err(_) => return 0,
            }
        };
        if !context.set_digest(digest) {
            return 0;
        }
        // SAFETY: `input` is null or a live terminated parameter array.
        i32::from(unsafe { context.set_params(input) })
    })
}

unsafe extern "C" fn digest_sign_update(
    signature_context: *mut c_void,
    data: *const u8,
    data_length: usize,
) -> c_int {
    ffi_guard!(0, {
        if signature_context.is_null() || (data.is_null() && data_length != 0) {
            return 0;
        }
        // SAFETY: OpenSSL passes a live signature context.
        let context = unsafe { &mut *signature_context.cast::<SignatureContext>() };
        let Some(hash) = context.hash.as_mut() else {
            return 0;
        };
        let bytes = if data_length == 0 {
            &[][..]
        } else {
            // SAFETY: OpenSSL declares `data` readable for `data_length` bytes.
            unsafe { std::slice::from_raw_parts(data, data_length) }
        };
        match hash.update(bytes) {
            Ok(()) => {
                context.hash_started |= data_length != 0;
                1
            }
            Err(cng_error) => {
                error::raise_cng(CNG_FAILURE, &cng_error);
                0
            }
        }
    })
}

unsafe extern "C" fn digest_sign_final(
    signature_context: *mut c_void,
    signature: *mut u8,
    signature_length: *mut usize,
    signature_size: usize,
) -> c_int {
    ffi_guard!(0, {
        if signature_context.is_null() {
            return 0;
        }
        // SAFETY: OpenSSL passes a live signature context.
        let context = unsafe { &*signature_context.cast::<SignatureContext>() };
        let Some(hash) = &context.hash else {
            return 0;
        };
        match hash.finish_clone() {
            Ok(digest) => context.sign_digest(
                digest.as_slice(),
                signature,
                signature_length,
                signature_size,
            ),
            Err(cng_error) => {
                error::raise_cng(CNG_FAILURE, &cng_error);
                0
            }
        }
    })
}

unsafe extern "C" fn digest_sign(
    signature_context: *mut c_void,
    signature: *mut u8,
    signature_length: *mut usize,
    signature_size: usize,
    message: *const u8,
    message_length: usize,
) -> c_int {
    ffi_guard!(0, {
        if signature_context.is_null() || (message.is_null() && message_length != 0) {
            return 0;
        }
        // SAFETY: OpenSSL passes a live signature context.
        let context = unsafe { &*signature_context.cast::<SignatureContext>() };
        let Some(digest_algorithm) = context.digest else {
            return 0;
        };
        let message = if message_length == 0 {
            &[][..]
        } else {
            // SAFETY: OpenSSL declares `message` readable for `message_length` bytes.
            unsafe { std::slice::from_raw_parts(message, message_length) }
        };
        match HashState::hash_message(digest_algorithm, message) {
            Ok(digest) => context.sign_digest(
                digest.as_slice(),
                signature,
                signature_length,
                signature_size,
            ),
            Err(cng_error) => {
                error::raise_cng(CNG_FAILURE, &cng_error);
                0
            }
        }
    })
}

unsafe extern "C" fn signature_set_ctx_params(
    signature_context: *mut c_void,
    input: *const OSSL_PARAM,
) -> c_int {
    ffi_guard!(0, {
        if signature_context.is_null() {
            return 0;
        }
        // SAFETY: OpenSSL passes a live signature context and a null or terminated parameter array.
        i32::from(unsafe { (&mut *signature_context.cast::<SignatureContext>()).set_params(input) })
    })
}

unsafe extern "C" fn signature_settable_ctx_params(
    _signature_context: *mut c_void,
    _provider_context: *mut c_void,
) -> *const OSSL_PARAM {
    ffi_guard!(null(), { SETTABLE_PARAMS.0.as_ptr() })
}

unsafe extern "C" fn signature_get_ctx_params(
    signature_context: *mut c_void,
    output: *mut OSSL_PARAM,
) -> c_int {
    ffi_guard!(0, {
        if signature_context.is_null() || output.is_null() {
            return 0;
        }
        // SAFETY: OpenSSL passes a live signature context and mutable terminated parameter array.
        let context = unsafe { &*signature_context.cast::<SignatureContext>() };
        // SAFETY: each locate/set pair operates on the live output array.
        let parameter = unsafe { params::locate_mut(output, c"pad-mode") };
        if !parameter.is_null() {
            // SAFETY: the output parameter is live and its requested type controls encoding.
            if !unsafe { set_padding_output(parameter, context.padding) } {
                return 0;
            }
        }
        // SAFETY: same output-array invariant.
        let parameter = unsafe { params::locate_mut(output, c"digest") };
        if !parameter.is_null() {
            let Some(digest) = context.digest else {
                return 0;
            };
            let value = digest_name(digest);
            // SAFETY: value is static and the output parameter is live.
            if !unsafe { params::set_utf8(parameter, value) } {
                return 0;
            }
        }
        // SAFETY: same output-array invariant.
        let parameter = unsafe { params::locate_mut(output, c"mgf1-digest") };
        if !parameter.is_null() {
            let Some(digest) = context.mgf1_digest else {
                return 0;
            };
            // SAFETY: value is static and the output parameter is live.
            if !unsafe { params::set_utf8(parameter, digest_name(digest)) } {
                return 0;
            }
        }
        // SAFETY: same output-array invariant.
        let parameter = unsafe { params::locate_mut(output, c"saltlen") };
        if !parameter.is_null() {
            // SAFETY: the output parameter is live and its requested type controls encoding.
            if !unsafe { set_salt_length_output(parameter, context.pss_salt_length) } {
                return 0;
            }
        }
        // SAFETY: same output-array invariant.
        let parameter = unsafe { params::locate_mut(output, c"algorithm-id") };
        if !parameter.is_null() {
            if context.padding != RSA_PKCS1_PADDING {
                error::raise(
                    UNSUPPORTED_PADDING,
                    "AlgorithmIdentifier is available only for PKCS#1 v1.5 signatures",
                );
                return 0;
            }
            let Some(digest) = context.digest else {
                return 0;
            };
            // SAFETY: the output parameter is live and the algorithm identifier is static.
            if !unsafe { params::set_octets(parameter, algorithm_identifier(digest)) } {
                return 0;
            }
        }
        1
    })
}

unsafe fn set_padding_output(parameter: *mut OSSL_PARAM, padding: c_int) -> bool {
    // SAFETY: caller provides a live output parameter.
    match unsafe { (*parameter).data_type } {
        OSSL_PARAM_INTEGER => {
            // SAFETY: forwards the same live integer output parameter.
            unsafe { params::set_int(parameter, padding, false) }
        }
        OSSL_PARAM_UTF8_STRING => {
            let value = match padding {
                RSA_PKCS1_PADDING => c"pkcs1",
                RSA_PKCS1_PSS_PADDING => c"pss",
                _ => return false,
            };
            // SAFETY: forwards the same live UTF8 output parameter and static text.
            unsafe { params::set_utf8(parameter, value) }
        }
        _ => false,
    }
}

unsafe fn set_salt_length_output(parameter: *mut OSSL_PARAM, salt_length: c_int) -> bool {
    // SAFETY: caller provides a live output parameter.
    match unsafe { (*parameter).data_type } {
        OSSL_PARAM_INTEGER => {
            // SAFETY: forwards the same live signed integer output parameter.
            unsafe { params::set_int(parameter, salt_length, false) }
        }
        OSSL_PARAM_UTF8_STRING => {
            let mut explicit_buffer = [0_u8; 12];
            let value = match salt_length {
                RSA_PSS_SALTLEN_DIGEST => c"digest",
                RSA_PSS_SALTLEN_MAX => c"max",
                RSA_PSS_SALTLEN_AUTO => c"auto",
                RSA_PSS_SALTLEN_AUTO_DIGEST_MAX => c"auto-digestmax",
                explicit if explicit >= 0 => decimal_cstr(explicit, &mut explicit_buffer),
                _ => return false,
            };
            // SAFETY: OpenSSL copies gettable UTF8_STRING values synchronously.
            unsafe { params::set_utf8(parameter, value) }
        }
        _ => false,
    }
}

unsafe extern "C" fn signature_gettable_ctx_params(
    _signature_context: *mut c_void,
    _provider_context: *mut c_void,
) -> *const OSSL_PARAM {
    ffi_guard!(null(), { GETTABLE_PARAMS.0.as_ptr() })
}

fn decimal_cstr(value: c_int, buffer: &mut [u8; 12]) -> &CStr {
    let negative = value < 0;
    let mut magnitude = if negative {
        -i64::from(value)
    } else {
        i64::from(value)
    } as u64;
    let mut index = buffer.len() - 1;
    loop {
        index -= 1;
        buffer[index] = b'0' + (magnitude % 10) as u8;
        magnitude /= 10;
        if magnitude == 0 {
            break;
        }
    }
    if negative {
        index -= 1;
        buffer[index] = b'-';
    }
    // SAFETY: the constructed suffix contains only sign/digit bytes followed by the untouched
    // terminating NUL in the last buffer slot.
    unsafe { CStr::from_bytes_with_nul_unchecked(&buffer[index..]) }
}

fn digest_name(digest: Digest) -> &'static std::ffi::CStr {
    match digest {
        Digest::Sha256 => c"SHA256",
        Digest::Sha384 => c"SHA384",
        Digest::Sha512 => c"SHA512",
    }
}

fn algorithm_identifier(digest: Digest) -> &'static [u8] {
    match digest {
        Digest::Sha256 => &[
            0x30, 0x0d, 0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x0b, 0x05,
            0x00,
        ],
        Digest::Sha384 => &[
            0x30, 0x0d, 0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x0c, 0x05,
            0x00,
        ],
        Digest::Sha512 => &[
            0x30, 0x0d, 0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x0d, 0x05,
            0x00,
        ],
    }
}

pub static SIGNATURE_FUNCTIONS: [OsslDispatch; 11] = [
    OsslDispatch {
        function_id: OSSL_FUNC_SIGNATURE_NEWCTX,
        function: signature_newctx as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_SIGNATURE_DIGEST_SIGN_INIT,
        function: digest_sign_init as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_SIGNATURE_DIGEST_SIGN_UPDATE,
        function: digest_sign_update as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_SIGNATURE_DIGEST_SIGN_FINAL,
        function: digest_sign_final as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_SIGNATURE_DIGEST_SIGN,
        function: digest_sign as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_SIGNATURE_FREECTX,
        function: signature_freectx as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_SIGNATURE_DUPCTX,
        function: signature_dupctx as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_SIGNATURE_GET_CTX_PARAMS,
        function: signature_get_ctx_params as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_SIGNATURE_GETTABLE_CTX_PARAMS,
        function: signature_gettable_ctx_params as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_SIGNATURE_SET_CTX_PARAMS,
        function: signature_set_ctx_params as *const c_void,
    },
    OsslDispatch {
        function_id: OSSL_FUNC_SIGNATURE_SETTABLE_CTX_PARAMS,
        function: signature_settable_ctx_params as *const c_void,
    },
    // The terminating entry is appended by `signature_dispatch()` below because this exact array
    // length is exposed to static initialization.
];

pub static SIGNATURE_DISPATCH: [OsslDispatch; 12] = [
    SIGNATURE_FUNCTIONS[0],
    SIGNATURE_FUNCTIONS[1],
    SIGNATURE_FUNCTIONS[2],
    SIGNATURE_FUNCTIONS[3],
    SIGNATURE_FUNCTIONS[4],
    SIGNATURE_FUNCTIONS[5],
    SIGNATURE_FUNCTIONS[6],
    SIGNATURE_FUNCTIONS[7],
    SIGNATURE_FUNCTIONS[8],
    SIGNATURE_FUNCTIONS[9],
    SIGNATURE_FUNCTIONS[10],
    OsslDispatch {
        function_id: 0,
        function: null(),
    },
];

#[cfg(test)]
mod tests {
    use super::*;
    use openssl_provider_sys::{
        EVP_SIGNATURE_fetch, EVP_SIGNATURE_free, EVP_SIGNATURE_gettable_ctx_params,
        EVP_SIGNATURE_settable_ctx_params, OSSL_LIB_CTX_free, OSSL_LIB_CTX_new, OSSL_PROVIDER_load,
        OSSL_PROVIDER_unload, param_int, param_utf8_string,
    };

    unsafe fn metadata_types(mut parameters: *const OSSL_PARAM, key: &CStr) -> Vec<u32> {
        let mut types = Vec::new();
        while !parameters.is_null() {
            // SAFETY: callers provide a terminated metadata array.
            let parameter = unsafe { &*parameters };
            if parameter.key.is_null() {
                break;
            }
            // SAFETY: metadata keys are NUL-terminated static strings.
            if unsafe { CStr::from_ptr(parameter.key) } == key {
                types.push(parameter.data_type);
            }
            // SAFETY: the terminator check permits advancing one entry.
            parameters = unsafe { parameters.add(1) };
        }
        types
    }

    fn assert_dual_text_integer_metadata(parameters: *const OSSL_PARAM) {
        for key in [c"pad-mode", c"saltlen"] {
            // SAFETY: callers pass a live terminated metadata array.
            let types = unsafe { metadata_types(parameters, key) };
            assert!(types.contains(&OSSL_PARAM_UTF8_STRING), "{key:?} UTF8");
            assert!(types.contains(&OSSL_PARAM_INTEGER), "{key:?} integer");
        }
    }

    fn get_padding_and_salt(context: &mut SignatureContext) -> (c_int, c_int, String, String) {
        let mut padding = 0;
        let mut salt_length = 0;
        let mut padding_text = [0_i8; 32];
        let mut salt_text = [0_i8; 32];
        let mut integer_output = [
            param_int(c"pad-mode".as_ptr(), &raw mut padding),
            param_int(c"saltlen".as_ptr(), &raw mut salt_length),
            param_end(),
        ];
        // SAFETY: context and the terminated output array are live.
        let integer_result = unsafe {
            signature_get_ctx_params(
                std::ptr::from_mut(context).cast(),
                integer_output.as_mut_ptr(),
            )
        };
        assert_eq!(integer_result, 1);
        let mut text_output = [
            param_utf8_string(
                c"pad-mode".as_ptr(),
                padding_text.as_mut_ptr(),
                padding_text.len(),
            ),
            param_utf8_string(c"saltlen".as_ptr(), salt_text.as_mut_ptr(), salt_text.len()),
            param_end(),
        ];
        // SAFETY: context and the terminated output array are live.
        let text_result = unsafe {
            signature_get_ctx_params(std::ptr::from_mut(context).cast(), text_output.as_mut_ptr())
        };
        assert_eq!(text_result, 1);
        // SAFETY: successful UTF8_STRING writes NUL-terminated both oversized buffers.
        let padding_text = unsafe { CStr::from_ptr(padding_text.as_ptr()) }
            .to_str()
            .expect("padding UTF-8")
            .to_owned();
        // SAFETY: same successful UTF8_STRING output invariant.
        let salt_text = unsafe { CStr::from_ptr(salt_text.as_ptr()) }
            .to_str()
            .expect("salt UTF-8")
            .to_owned();
        (padding, salt_length, padding_text, salt_text)
    }

    #[test]
    fn resolves_pss_salt_modes() {
        assert_eq!(
            resolved_pss_salt_length(RSA_PSS_SALTLEN_DIGEST, 2_048, 256, 32),
            Some(32)
        );
        assert_eq!(
            resolved_pss_salt_length(RSA_PSS_SALTLEN_MAX, 2_048, 256, 32),
            Some(222)
        );
        assert_eq!(
            resolved_pss_salt_length(RSA_PSS_SALTLEN_AUTO_DIGEST_MAX, 2_048, 256, 32),
            Some(32)
        );
        assert_eq!(resolved_pss_salt_length(223, 2_048, 256, 32), None);
    }

    #[test]
    fn rsa_signature_metadata_matches_default_provider_dual_types() {
        assert_dual_text_integer_metadata(SETTABLE_PARAMS.0.as_ptr());
        assert_dual_text_integer_metadata(GETTABLE_PARAMS.0.as_ptr());

        // SAFETY: creates an owned private OpenSSL context for default-provider comparison.
        let libctx = unsafe { OSSL_LIB_CTX_new() };
        assert!(!libctx.is_null());
        // SAFETY: context and static provider name are live.
        let provider = unsafe { OSSL_PROVIDER_load(libctx, c"default".as_ptr()) };
        assert!(!provider.is_null());
        // SAFETY: context and static algorithm/property strings are live.
        let signature =
            unsafe { EVP_SIGNATURE_fetch(libctx, c"RSA".as_ptr(), c"provider=default".as_ptr()) };
        assert!(!signature.is_null());
        // SAFETY: the fetched signature remains live.
        let default_settable = unsafe { EVP_SIGNATURE_settable_ctx_params(signature) };
        // SAFETY: the fetched signature remains live.
        let default_gettable = unsafe { EVP_SIGNATURE_gettable_ctx_params(signature) };
        assert_dual_text_integer_metadata(default_settable);
        assert_dual_text_integer_metadata(default_gettable);
        // SAFETY: all handles are owned by this test and released in dependency order.
        unsafe {
            EVP_SIGNATURE_free(signature);
            let _ = OSSL_PROVIDER_unload(provider);
            OSSL_LIB_CTX_free(libctx);
        }
    }

    #[test]
    fn rsa_padding_and_salt_parameters_round_trip_in_both_types() {
        let cases = [
            (RSA_PSS_SALTLEN_DIGEST, "digest"),
            (RSA_PSS_SALTLEN_MAX, "max"),
            (RSA_PSS_SALTLEN_AUTO, "auto"),
            (RSA_PSS_SALTLEN_AUTO_DIGEST_MAX, "auto-digestmax"),
            (32, "32"),
        ];
        for (salt_length, salt_text) in cases {
            let mut context = SignatureContext::new(null_mut());
            let mut padding = RSA_PKCS1_PSS_PADDING;
            let mut salt = salt_length;
            let integer_input = [
                param_int(c"pad-mode".as_ptr(), &raw mut padding),
                param_int(c"saltlen".as_ptr(), &raw mut salt),
                param_end(),
            ];
            // SAFETY: the terminated input array remains live for the call.
            assert!(unsafe { context.set_params(integer_input.as_ptr()) });
            assert_eq!(
                get_padding_and_salt(&mut context),
                (
                    RSA_PKCS1_PSS_PADDING,
                    salt_length,
                    "pss".to_owned(),
                    salt_text.to_owned()
                )
            );

            let mut text_context = SignatureContext::new(null_mut());
            let mut padding_text = *b"pss";
            let mut salt_text_bytes = salt_text.as_bytes().to_vec();
            let text_input = [
                param_utf8_string(
                    c"pad-mode".as_ptr(),
                    padding_text.as_mut_ptr().cast(),
                    padding_text.len(),
                ),
                param_utf8_string(
                    c"saltlen".as_ptr(),
                    salt_text_bytes.as_mut_ptr().cast(),
                    salt_text_bytes.len(),
                ),
                param_end(),
            ];
            // SAFETY: both text buffers and the terminated input array remain live.
            assert!(unsafe { text_context.set_params(text_input.as_ptr()) });
            let round_trip = get_padding_and_salt(&mut text_context);
            assert_eq!(round_trip.0, RSA_PKCS1_PSS_PADDING);
            assert_eq!(round_trip.1, salt_length);
            assert_eq!(round_trip.2, "pss");
            assert_eq!(round_trip.3, salt_text);
        }

        let mut pkcs1_context = SignatureContext::new(null_mut());
        let mut pkcs1 = RSA_PKCS1_PADDING;
        let integer_input = [param_int(c"pad-mode".as_ptr(), &raw mut pkcs1), param_end()];
        // SAFETY: the terminated integer input remains live.
        assert!(unsafe { pkcs1_context.set_params(integer_input.as_ptr()) });
        let round_trip = get_padding_and_salt(&mut pkcs1_context);
        assert_eq!(round_trip.0, RSA_PKCS1_PADDING);
        assert_eq!(round_trip.2, "pkcs1");

        let mut pkcs1_text_context = SignatureContext::new(null_mut());
        let mut pkcs1_text = *b"pkcs1";
        let text_input = [
            param_utf8_string(
                c"pad-mode".as_ptr(),
                pkcs1_text.as_mut_ptr().cast(),
                pkcs1_text.len(),
            ),
            param_end(),
        ];
        // SAFETY: the text buffer and terminated input remain live.
        assert!(unsafe { pkcs1_text_context.set_params(text_input.as_ptr()) });
        assert_eq!(
            get_padding_and_salt(&mut pkcs1_text_context).0,
            RSA_PKCS1_PADDING
        );
    }

    #[test]
    fn rsa_salt_integer_rejects_unknown_negative_values() {
        let mut context = SignatureContext::new(null_mut());
        context.padding = RSA_PKCS1_PSS_PADDING;
        let mut invalid = -5;
        let input = [
            param_int(c"saltlen".as_ptr(), &raw mut invalid),
            param_end(),
        ];
        // SAFETY: the terminated input array remains live for the call.
        assert!(!unsafe { context.set_params(input.as_ptr()) });
    }

    #[test]
    fn failed_parameter_updates_leave_signature_state_unchanged() {
        let mut context = SignatureContext::new(null_mut());
        let mut initial_digest = *b"SHA256";
        let initial = [
            param_utf8_string(
                c"digest".as_ptr(),
                initial_digest.as_mut_ptr().cast(),
                initial_digest.len(),
            ),
            param_end(),
        ];
        // SAFETY: the digest buffer and terminated parameter array remain live.
        assert!(unsafe { context.set_params(initial.as_ptr()) });

        let mut pss = RSA_PKCS1_PSS_PADDING;
        let mut replacement_digest = *b"SHA384";
        let mut incompatible_mgf1 = *b"SHA256";
        let invalid_mgf1 = [
            param_int(c"pad-mode".as_ptr(), &raw mut pss),
            param_utf8_string(
                c"digest".as_ptr(),
                replacement_digest.as_mut_ptr().cast(),
                replacement_digest.len(),
            ),
            param_utf8_string(
                c"mgf1-digest".as_ptr(),
                incompatible_mgf1.as_mut_ptr().cast(),
                incompatible_mgf1.len(),
            ),
            param_end(),
        ];
        // SAFETY: all buffers and the terminated parameter array remain live.
        assert!(!unsafe { context.set_params(invalid_mgf1.as_ptr()) });
        assert_eq!(context.digest, Some(Digest::Sha256));
        assert_eq!(context.padding, RSA_PKCS1_PADDING);
        assert_eq!(context.mgf1_digest, Some(Digest::Sha256));
        assert!(!context.mgf1_explicit);

        let mut replacement_digest = *b"SHA384";
        let mut invalid_salt = -5;
        let invalid_salt_update = [
            param_int(c"pad-mode".as_ptr(), &raw mut pss),
            param_utf8_string(
                c"digest".as_ptr(),
                replacement_digest.as_mut_ptr().cast(),
                replacement_digest.len(),
            ),
            param_int(c"saltlen".as_ptr(), &raw mut invalid_salt),
            param_end(),
        ];
        // SAFETY: all buffers and the terminated parameter array remain live.
        assert!(!unsafe { context.set_params(invalid_salt_update.as_ptr()) });
        assert_eq!(context.digest, Some(Digest::Sha256));
        assert_eq!(context.padding, RSA_PKCS1_PADDING);
    }

    #[test]
    fn leaving_pss_clears_explicit_mgf1_state() {
        let mut context = SignatureContext::new(null_mut());
        let mut pss = RSA_PKCS1_PSS_PADDING;
        let mut sha256 = *b"SHA256";
        let mut mgf1 = *b"SHA256";
        let pss_input = [
            param_int(c"pad-mode".as_ptr(), &raw mut pss),
            param_utf8_string(c"digest".as_ptr(), sha256.as_mut_ptr().cast(), sha256.len()),
            param_utf8_string(
                c"mgf1-digest".as_ptr(),
                mgf1.as_mut_ptr().cast(),
                mgf1.len(),
            ),
            param_end(),
        ];
        // SAFETY: all buffers and the terminated parameter array remain live.
        assert!(unsafe { context.set_params(pss_input.as_ptr()) });
        assert!(context.mgf1_explicit);

        let mut pkcs1 = RSA_PKCS1_PADDING;
        let pkcs1_input = [param_int(c"pad-mode".as_ptr(), &raw mut pkcs1), param_end()];
        // SAFETY: the terminated parameter array remains live.
        assert!(unsafe { context.set_params(pkcs1_input.as_ptr()) });
        assert!(!context.mgf1_explicit);
        assert_eq!(context.mgf1_digest, context.digest);

        let mut sha384 = *b"SHA384";
        let digest_input = [
            param_utf8_string(c"digest".as_ptr(), sha384.as_mut_ptr().cast(), sha384.len()),
            param_end(),
        ];
        // SAFETY: the digest buffer and terminated parameter array remain live.
        assert!(unsafe { context.set_params(digest_input.as_ptr()) });
        assert_eq!(context.digest, Some(Digest::Sha384));
        assert_eq!(context.mgf1_digest, Some(Digest::Sha384));
    }
}
