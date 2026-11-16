#ifndef MICROSOFT_IDENTITY_NATIVE_PRIMITIVES_H
#define MICROSOFT_IDENTITY_NATIVE_PRIMITIVES_H

#include <stddef.h>
#include <stdint.h>

#ifdef _WIN32
#ifdef MICROSOFT_IDENTITY_NATIVE_BUILD
#define MICROSOFT_IDENTITY_NATIVE_API __declspec(dllexport)
#else
#define MICROSOFT_IDENTITY_NATIVE_API __declspec(dllimport)
#endif
#else
#define MICROSOFT_IDENTITY_NATIVE_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define MICROSOFT_IDENTITY_NATIVE_ABI_VERSION 1U
#define MICROSOFT_IDENTITY_NATIVE_MAX_HEADERS 16U
#define MICROSOFT_IDENTITY_NATIVE_MAX_HEADER_BYTES 16384U
#define MICROSOFT_IDENTITY_NATIVE_MAX_HEADER_NAME_BYTES 64U
#define MICROSOFT_IDENTITY_NATIVE_MAX_HEADER_VALUE_BYTES 12288U
#define MICROSOFT_IDENTITY_NATIVE_MAX_BODY_BYTES 1048576U
#define MICROSOFT_IDENTITY_NATIVE_MAX_RESPONSE_BYTES 1048576U

typedef enum microsoft_identity_native_error_code_st {
    MICROSOFT_IDENTITY_NATIVE_ERROR_NONE = 0,
    MICROSOFT_IDENTITY_NATIVE_ERROR_INVALID_ARGUMENT = 1,
    MICROSOFT_IDENTITY_NATIVE_ERROR_LIMIT = 2,
    MICROSOFT_IDENTITY_NATIVE_ERROR_PROVIDER = 3,
    MICROSOFT_IDENTITY_NATIVE_ERROR_STORE = 4,
    MICROSOFT_IDENTITY_NATIVE_ERROR_CERTIFICATE = 5,
    MICROSOFT_IDENTITY_NATIVE_ERROR_DNS = 6,
    MICROSOFT_IDENTITY_NATIVE_ERROR_CONNECT = 7,
    MICROSOFT_IDENTITY_NATIVE_ERROR_TLS = 8,
    MICROSOFT_IDENTITY_NATIVE_ERROR_IO = 9,
    MICROSOFT_IDENTITY_NATIVE_ERROR_HTTP = 10,
    MICROSOFT_IDENTITY_NATIVE_ERROR_MEMORY = 11
} MICROSOFT_IDENTITY_NATIVE_ERROR_CODE;

typedef struct microsoft_identity_native_header_st {
    /* ABI strings are UTF-8; HTTP header fields are restricted to ASCII. */
    const char *name;
    const char *value;
} MICROSOFT_IDENTITY_NATIVE_HEADER;

typedef struct microsoft_identity_native_request_st {
    uint32_t struct_size;
    uint32_t abi_version;
    /* All string fields use UTF-8. Protocol fields are validated as ASCII. */
    const char *provider_module_path;
    const char *store_uri;
    const char *client_certificate_path;
    const char *ca_bundle_path;
    const char *host;
    uint16_t port;
    const char *method;
    const char *target;
    const MICROSOFT_IDENTITY_NATIVE_HEADER *headers;
    size_t header_count;
    const unsigned char *body;
    size_t body_size;
    uint32_t connect_timeout_ms;
    uint32_t io_timeout_ms;
} MICROSOFT_IDENTITY_NATIVE_REQUEST;

typedef struct microsoft_identity_native_response_st {
    /*
     * Owning move-only output. Pass a fresh/zeroed instance to
     * microsoft_identity_native_request().
     * On success, do not copy or modify its pointers/lengths. Call
     * microsoft_identity_native_response_free() exactly once on the original instance.
     */
    int http_status;
    char *headers;
    size_t headers_length;
    unsigned char *body;
    size_t body_length;
} MICROSOFT_IDENTITY_NATIVE_RESPONSE;

typedef struct microsoft_identity_native_error_st {
    MICROSOFT_IDENTITY_NATIVE_ERROR_CODE code;
    int system_error;
    unsigned long openssl_error;
    char message[512];
} MICROSOFT_IDENTITY_NATIVE_ERROR;

typedef struct microsoft_identity_native_key_st MICROSOFT_IDENTITY_NATIVE_KEY;

typedef struct microsoft_identity_native_public_key_st {
    uint32_t struct_size;
    uint32_t abi_version;
    uint32_t bits;
    uint32_t exponent;
    uint32_t modulus_length;
    unsigned char modulus[2048];
} MICROSOFT_IDENTITY_NATIVE_PUBLIC_KEY;

MICROSOFT_IDENTITY_NATIVE_API uint32_t
microsoft_identity_native_abi_version(void);
/*
 * `response` must point to fresh or zeroed storage with no outstanding allocation.
 * The implementation resets it before use. A successful response owns native allocations until
 * microsoft_identity_native_response_free() is called; copying the structure duplicates
 * ownership and is invalid.
 */
MICROSOFT_IDENTITY_NATIVE_API int microsoft_identity_native_request(
    const MICROSOFT_IDENTITY_NATIVE_REQUEST *request,
    MICROSOFT_IDENTITY_NATIVE_RESPONSE *response,
    MICROSOFT_IDENTITY_NATIVE_ERROR *error);
MICROSOFT_IDENTITY_NATIVE_API void microsoft_identity_native_response_free(
    MICROSOFT_IDENTITY_NATIVE_RESPONSE *response);

/*
 * Additive ABI v1 key primitives. Only microsoft-identity-native-vbs-* names are
 * accepted. No software fallback. Successful open returns one owned key object.
 */
MICROSOFT_IDENTITY_NATIVE_API int microsoft_identity_native_key_open_protected(
    const char *name,
    MICROSOFT_IDENTITY_NATIVE_KEY **key,
    MICROSOFT_IDENTITY_NATIVE_ERROR *error);
/* Initialize public_key.struct_size and .abi_version before calling. */
MICROSOFT_IDENTITY_NATIVE_API int microsoft_identity_native_key_public(
    const MICROSOFT_IDENTITY_NATIVE_KEY *key,
    MICROSOFT_IDENTITY_NATIVE_PUBLIC_KEY *public_key,
    MICROSOFT_IDENTITY_NATIVE_ERROR *error);
/* SHA-256 digest, PSS SHA-256/MGF1 SHA-256, salt32; buffers must not alias. */
MICROSOFT_IDENTITY_NATIVE_API int microsoft_identity_native_key_sign_sha256_pss(
    const MICROSOFT_IDENTITY_NATIVE_KEY *key,
    const unsigned char *digest,
    size_t digest_length,
    unsigned char *signature,
    size_t signature_capacity,
    size_t *written,
    MICROSOFT_IDENTITY_NATIVE_ERROR *error);
/*
 * Borrows the NCrypt handle without transferring ownership. Retain the key
 * through all uses; never free/delete the borrowed handle.
 */
MICROSOFT_IDENTITY_NATIVE_API int microsoft_identity_native_key_borrow_handle(
    const MICROSOFT_IDENTITY_NATIVE_KEY *key,
    uintptr_t *handle,
    MICROSOFT_IDENTITY_NATIVE_ERROR *error);
/*
 * Free each successful key object once, with no concurrent operations or
 * outstanding borrowed handles. NULL is accepted. Does not delete the key.
 */
MICROSOFT_IDENTITY_NATIVE_API void microsoft_identity_native_key_free(
    MICROSOFT_IDENTITY_NATIVE_KEY *key);

#ifdef __cplusplus
}
#endif

#endif
