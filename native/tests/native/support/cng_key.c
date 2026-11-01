#include "cng_key.h"

#include <bcrypt.h>
#include <limits.h>
#include <ncrypt.h>
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct cng_key_st {
    volatile LONG references;
    SRWLOCK sign_lock;
    NCRYPT_PROV_HANDLE provider;
    NCRYPT_KEY_HANDLE key;
    char *provider_name;
    char *key_name;
    unsigned int bits;
    unsigned int security_bits;
    unsigned char *modulus;
    size_t modulus_length;
    unsigned char *exponent;
    size_t exponent_length;
};

static void secure_free_string(char *value)
{
    if (value != NULL) {
        SecureZeroMemory(value, strlen(value));
        free(value);
    }
}

static void set_error(char *error, size_t error_size, const char *format, ...)
{
    va_list args;

    if (error == NULL || error_size == 0) {
        return;
    }

    va_start(args, format);
    (void)vsnprintf(error, error_size, format, args);
    va_end(args);
    error[error_size - 1] = '\0';
}

static char *duplicate_string(const char *value)
{
    size_t length;
    char *copy;

    if (value == NULL) {
        return NULL;
    }

    length = strlen(value) + 1;
    copy = (char *)malloc(length);
    if (copy != NULL) {
        memcpy(copy, value, length);
    }
    return copy;
}

static wchar_t *utf8_to_wide(const char *value, char *error, size_t error_size)
{
    int required;
    wchar_t *wide;

    if (value == NULL || value[0] == '\0') {
        set_error(error, error_size, "A non-empty UTF-8 name is required.");
        return NULL;
    }

    required = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value, -1,
                                   NULL, 0);
    if (required <= 0) {
        set_error(error, error_size,
                  "MultiByteToWideChar size query failed with Win32 error %lu.",
                  GetLastError());
        return NULL;
    }

    wide = (wchar_t *)calloc((size_t)required, sizeof(wchar_t));
    if (wide == NULL) {
        set_error(error, error_size, "Out of memory converting a CNG name.");
        return NULL;
    }

    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value, -1, wide,
                            required) != required) {
        set_error(error, error_size,
                  "MultiByteToWideChar conversion failed with Win32 error %lu.",
                  GetLastError());
        free(wide);
        return NULL;
    }

    return wide;
}

static unsigned int rsa_security_bits(unsigned int bits)
{
    if (bits >= 15360) {
        return 256;
    }
    if (bits >= 7680) {
        return 192;
    }
    if (bits >= 3072) {
        return 128;
    }
    if (bits >= 2048) {
        return 112;
    }
    if (bits >= 1024) {
        return 80;
    }
    return 0;
}

static int is_missing_key_status(SECURITY_STATUS status)
{
    return status == NTE_BAD_KEYSET || status == NTE_NOT_FOUND;
}

static int read_dword_property(NCRYPT_KEY_HANDLE key,
                               LPCWSTR property,
                               const char *property_name,
                               DWORD *value,
                               char *error,
                               size_t error_size)
{
    DWORD result_size = 0;
    SECURITY_STATUS status;

    status = NCryptGetProperty(key, property, (PBYTE)value, sizeof(*value),
                              &result_size, 0);
    if (status != ERROR_SUCCESS || result_size != sizeof(*value)) {
        set_error(error, error_size,
                  "NCryptGetProperty(%s) failed with status 0x%08lX.",
                  property_name, (unsigned long)status);
        return 0;
    }

    return 1;
}

static int export_public_blob(CNG_KEY *key, char *error, size_t error_size)
{
    SECURITY_STATUS status;
    DWORD blob_size = 0;
    DWORD written = 0;
    unsigned char *blob = NULL;
    BCRYPT_RSAKEY_BLOB *header;
    size_t expected_size;
    unsigned char *cursor;

    status = NCryptExportKey(key->key, 0, BCRYPT_RSAPUBLIC_BLOB, NULL, NULL, 0,
                            &blob_size, 0);
    if (status != ERROR_SUCCESS) {
        set_error(error, error_size,
                  "NCryptExportKey public size query failed with status "
                  "0x%08lX.",
                  (unsigned long)status);
        return 0;
    }

    blob = (unsigned char *)malloc(blob_size);
    if (blob == NULL) {
        set_error(error, error_size, "Out of memory exporting the public key.");
        return 0;
    }

    status = NCryptExportKey(key->key, 0, BCRYPT_RSAPUBLIC_BLOB, NULL, blob,
                            blob_size, &written, 0);
    if (status != ERROR_SUCCESS || written != blob_size) {
        set_error(error, error_size,
                  "NCryptExportKey public export failed with status 0x%08lX.",
                  (unsigned long)status);
        free(blob);
        return 0;
    }

    if (blob_size < sizeof(*header)) {
        set_error(error, error_size, "CNG returned a truncated RSA public blob.");
        free(blob);
        return 0;
    }

    header = (BCRYPT_RSAKEY_BLOB *)blob;
    expected_size = sizeof(*header) + (size_t)header->cbPublicExp
                    + (size_t)header->cbModulus;
    if (header->Magic != BCRYPT_RSAPUBLIC_MAGIC
        || header->BitLength == 0
        || header->cbPublicExp == 0
        || header->cbModulus == 0
        || expected_size != blob_size) {
        set_error(error, error_size, "CNG returned an invalid RSA public blob.");
        free(blob);
        return 0;
    }

    key->exponent = (unsigned char *)malloc(header->cbPublicExp);
    key->modulus = (unsigned char *)malloc(header->cbModulus);
    if (key->exponent == NULL || key->modulus == NULL) {
        set_error(error, error_size,
                  "Out of memory retaining the RSA public key.");
        free(blob);
        return 0;
    }

    cursor = blob + sizeof(*header);
    memcpy(key->exponent, cursor, header->cbPublicExp);
    cursor += header->cbPublicExp;
    memcpy(key->modulus, cursor, header->cbModulus);
    key->exponent_length = header->cbPublicExp;
    key->modulus_length = header->cbModulus;
    key->bits = header->BitLength;
    key->security_bits = rsa_security_bits(key->bits);
    free(blob);
    return 1;
}

static int cng_key_open_internal(const char *provider_name,
                                 const char *key_name,
                                 unsigned int bits,
                                 int create_if_missing,
                                 CNG_KEY **out_key,
                                 char *error,
                                 size_t error_size)
{
    CNG_KEY *result = NULL;
    wchar_t *provider_wide = NULL;
    wchar_t *key_wide = NULL;
    SECURITY_STATUS status;
    DWORD export_policy = 0;
    DWORD key_usage = NCRYPT_ALLOW_SIGNING_FLAG;
    DWORD actual_key_usage = 0;
    int created = 0;

    if (out_key == NULL
        || (create_if_missing && bits < 2048U)) {
        set_error(error, error_size, "Invalid CNG key arguments.");
        return 0;
    }
    *out_key = NULL;

    if (provider_name == NULL || provider_name[0] == '\0') {
        provider_name = CNG_DEFAULT_PROVIDER_NAME;
    }
    if (key_name == NULL || key_name[0] == '\0') {
        key_name = CNG_DEFAULT_KEY_NAME;
    }

    provider_wide = utf8_to_wide(provider_name, error, error_size);
    key_wide = utf8_to_wide(key_name, error, error_size);
    if (provider_wide == NULL || key_wide == NULL) {
        goto cleanup;
    }

    result = (CNG_KEY *)calloc(1, sizeof(*result));
    if (result == NULL) {
        set_error(error, error_size, "Out of memory creating a CNG key object.");
        goto cleanup;
    }
    result->references = 1;
    InitializeSRWLock(&result->sign_lock);

    result->provider_name = duplicate_string(provider_name);
    result->key_name = duplicate_string(key_name);
    if (result->provider_name == NULL || result->key_name == NULL) {
        set_error(error, error_size, "Out of memory retaining CNG key names.");
        goto cleanup;
    }

    status = NCryptOpenStorageProvider(&result->provider, provider_wide, 0);
    if (status != ERROR_SUCCESS) {
        set_error(error, error_size,
                  "NCryptOpenStorageProvider failed with status 0x%08lX.",
                  (unsigned long)status);
        goto cleanup;
    }

    status = NCryptOpenKey(result->provider, &result->key, key_wide, 0, 0);
    if (is_missing_key_status(status)) {
        if (!create_if_missing) {
            set_error(error, error_size,
                      "The named CNG key does not exist.");
            goto cleanup;
        }
        status = NCryptCreatePersistedKey(result->provider, &result->key,
                                          NCRYPT_RSA_ALGORITHM, key_wide, 0, 0);
        if (status != ERROR_SUCCESS) {
            set_error(error, error_size,
                      "NCryptCreatePersistedKey failed with status 0x%08lX.",
                      (unsigned long)status);
            goto cleanup;
        }
        created = 1;

        status = NCryptSetProperty(result->key, NCRYPT_LENGTH_PROPERTY,
                                   (PBYTE)&bits, sizeof(bits), 0);
        if (status != ERROR_SUCCESS) {
            set_error(error, error_size,
                      "NCryptSetProperty(%s) failed with status 0x%08lX.",
                      "Length", (unsigned long)status);
            goto cleanup;
        }

        status = NCryptSetProperty(result->key, NCRYPT_KEY_USAGE_PROPERTY,
                                   (PBYTE)&key_usage, sizeof(key_usage), 0);
        if (status != ERROR_SUCCESS) {
            set_error(error, error_size,
                      "NCryptSetProperty(%s) failed with status 0x%08lX.",
                      "Key Usage", (unsigned long)status);
            goto cleanup;
        }

        status = NCryptSetProperty(result->key, NCRYPT_EXPORT_POLICY_PROPERTY,
                                   (PBYTE)&export_policy,
                                   sizeof(export_policy), 0);
        if (status != ERROR_SUCCESS) {
            set_error(error, error_size,
                      "NCryptSetProperty(%s) failed with status 0x%08lX.",
                      "Export Policy", (unsigned long)status);
            goto cleanup;
        }

        status = NCryptFinalizeKey(result->key, 0);
        if (status != ERROR_SUCCESS) {
            set_error(error, error_size,
                      "NCryptFinalizeKey failed with status 0x%08lX.",
                      (unsigned long)status);
            goto cleanup;
        }
    } else if (status != ERROR_SUCCESS) {
        set_error(error, error_size,
                  "NCryptOpenKey failed with status 0x%08lX.",
                  (unsigned long)status);
        goto cleanup;
    }

    if (!read_dword_property(result->key, NCRYPT_EXPORT_POLICY_PROPERTY,
                             "Export Policy", &export_policy, error,
                             error_size)) {
        goto cleanup;
    }
    if (export_policy != 0) {
        set_error(error, error_size,
                  "The persisted key has export policy 0x%08lX; a "
                  "non-exportable private key is required.",
                  (unsigned long)export_policy);
        goto cleanup;
    }

    if (!read_dword_property(result->key, NCRYPT_KEY_USAGE_PROPERTY,
                             "Key Usage", &actual_key_usage, error,
                             error_size)) {
        goto cleanup;
    }
    if (actual_key_usage != NCRYPT_ALLOW_ALL_USAGES
        && (actual_key_usage & NCRYPT_ALLOW_SIGNING_FLAG) == 0) {
        set_error(error, error_size,
                  "The persisted key does not allow RSA signing.");
        goto cleanup;
    }

    if (!export_public_blob(result, error, error_size)) {
        goto cleanup;
    }
    if (result->bits < 2048U || result->security_bits < 112U) {
        set_error(error, error_size,
                  "The persisted RSA key is too small (%u bits).",
                  result->bits);
        goto cleanup;
    }
    if (bits != 0 && result->bits != bits) {
        set_error(error, error_size,
                  "The persisted key is %u bits; %u bits were requested.",
                  result->bits, bits);
        goto cleanup;
    }

    free(provider_wide);
    free(key_wide);
    *out_key = result;
    return 1;

cleanup:
    free(provider_wide);
    free(key_wide);
    if (created && result != NULL && result->key != 0) {
        if (NCryptDeleteKey(result->key, 0) == ERROR_SUCCESS) {
            result->key = 0;
        }
    }
    cng_key_free(result);
    return 0;
}

int cng_key_open_or_create(const char *provider_name,
                           const char *key_name,
                           unsigned int bits,
                           CNG_KEY **out_key,
                           char *error,
                           size_t error_size)
{
    return cng_key_open_internal(provider_name, key_name, bits, 1, out_key,
                                 error, error_size);
}

int cng_key_open(const char *provider_name,
                 const char *key_name,
                 CNG_KEY **out_key,
                 char *error,
                 size_t error_size)
{
    return cng_key_open_internal(provider_name, key_name, 0, 0, out_key,
                                 error, error_size);
}

int cng_key_up_ref(CNG_KEY *key)
{
    LONG current;

    if (key == NULL) {
        return 0;
    }
    current = InterlockedCompareExchange(&key->references, 0, 0);
    while (current > 0) {
        if (current == LONG_MAX) {
            return 0;
        }
        LONG observed = InterlockedCompareExchange(
            &key->references, current + 1, current);
        if (observed == current) {
            return 1;
        }
        current = observed;
    }
    return 0;
}

void cng_key_free(CNG_KEY *key)
{
    if (key == NULL) {
        return;
    }
    if (InterlockedDecrement(&key->references) != 0) {
        return;
    }

    if (key->key != 0) {
        (void)NCryptFreeObject(key->key);
    }
    if (key->provider != 0) {
        (void)NCryptFreeObject(key->provider);
    }
    secure_free_string(key->provider_name);
    secure_free_string(key->key_name);
    free(key->modulus);
    free(key->exponent);
    SecureZeroMemory(key, sizeof(*key));
    free(key);
}

const char *cng_key_provider_name(const CNG_KEY *key)
{
    return key == NULL ? NULL : key->provider_name;
}

const char *cng_key_name(const CNG_KEY *key)
{
    return key == NULL ? NULL : key->key_name;
}

unsigned int cng_key_bits(const CNG_KEY *key)
{
    return key == NULL ? 0 : key->bits;
}

unsigned int cng_key_security_bits(const CNG_KEY *key)
{
    return key == NULL ? 0 : key->security_bits;
}

size_t cng_key_max_signature_size(const CNG_KEY *key)
{
    return key == NULL ? 0 : key->modulus_length;
}

const unsigned char *cng_key_public_modulus(const CNG_KEY *key,
                                            size_t *length)
{
    if (length != NULL) {
        *length = key == NULL ? 0 : key->modulus_length;
    }
    return key == NULL ? NULL : key->modulus;
}

const unsigned char *cng_key_public_exponent(const CNG_KEY *key,
                                             size_t *length)
{
    if (length != NULL) {
        *length = key == NULL ? 0 : key->exponent_length;
    }
    return key == NULL ? NULL : key->exponent;
}

int cng_key_sign_hash(CNG_KEY *key,
                      LPCWSTR bcrypt_digest_name,
                      CNG_RSA_PADDING padding,
                      ULONG pss_salt_length,
                      const unsigned char *hash,
                      size_t hash_length,
                      unsigned char *signature,
                      size_t signature_size,
                      size_t *signature_length,
                      char *error,
                      size_t error_size)
{
    SECURITY_STATUS status;
    DWORD required = 0;
    DWORD written = 0;
    DWORD flags;
    void *padding_info;
    BCRYPT_PKCS1_PADDING_INFO pkcs1_info;
    BCRYPT_PSS_PADDING_INFO pss_info;

    if (key == NULL || key->key == 0 || bcrypt_digest_name == NULL
        || hash == NULL || hash_length > MAXDWORD
        || signature_length == NULL || signature_size > MAXDWORD) {
        set_error(error, error_size, "Invalid NCryptSignHash arguments.");
        return 0;
    }

    if (padding == CNG_RSA_PADDING_PKCS1) {
        pkcs1_info.pszAlgId = bcrypt_digest_name;
        padding_info = &pkcs1_info;
        flags = NCRYPT_PAD_PKCS1_FLAG;
    } else if (padding == CNG_RSA_PADDING_PSS) {
        pss_info.pszAlgId = bcrypt_digest_name;
        pss_info.cbSalt = pss_salt_length;
        padding_info = &pss_info;
        flags = NCRYPT_PAD_PSS_FLAG;
    } else {
        set_error(error, error_size, "Unsupported CNG RSA padding mode.");
        return 0;
    }

    AcquireSRWLockExclusive(&key->sign_lock);
    status = NCryptSignHash(key->key, padding_info, (PBYTE)hash,
                            (DWORD)hash_length, NULL, 0, &required, flags);
    if (status != ERROR_SUCCESS) {
        ReleaseSRWLockExclusive(&key->sign_lock);
        set_error(error, error_size,
                  "NCryptSignHash size query failed with status 0x%08lX.",
                  (unsigned long)status);
        return 0;
    }

    *signature_length = required;
    if (signature == NULL) {
        ReleaseSRWLockExclusive(&key->sign_lock);
        return 1;
    }
    if (signature_size < required) {
        ReleaseSRWLockExclusive(&key->sign_lock);
        set_error(error, error_size,
                  "Signature buffer is %zu bytes; %lu bytes are required.",
                  signature_size, (unsigned long)required);
        return 0;
    }

    status = NCryptSignHash(key->key, padding_info, (PBYTE)hash,
                            (DWORD)hash_length, signature,
                            (DWORD)signature_size, &written, flags);
    ReleaseSRWLockExclusive(&key->sign_lock);
    if (status != ERROR_SUCCESS) {
        set_error(error, error_size,
                  "NCryptSignHash failed with status 0x%08lX.",
                  (unsigned long)status);
        return 0;
    }

    *signature_length = written;
    return 1;
}

int cng_private_export_is_blocked(const char *provider_name,
                                  const char *key_name,
                                  char *error,
                                  size_t error_size)
{
    NCRYPT_PROV_HANDLE provider = 0;
    NCRYPT_KEY_HANDLE key = 0;
    wchar_t *provider_wide = NULL;
    wchar_t *key_wide = NULL;
    SECURITY_STATUS status;
    DWORD private_blob_size = 0;
    DWORD export_policy = 0;
    int result = 0;

    if (provider_name == NULL || provider_name[0] == '\0') {
        provider_name = CNG_DEFAULT_PROVIDER_NAME;
    }
    if (key_name == NULL || key_name[0] == '\0') {
        key_name = CNG_DEFAULT_KEY_NAME;
    }

    provider_wide = utf8_to_wide(provider_name, error, error_size);
    key_wide = utf8_to_wide(key_name, error, error_size);
    if (provider_wide == NULL || key_wide == NULL) {
        goto cleanup;
    }

    status = NCryptOpenStorageProvider(&provider, provider_wide, 0);
    if (status != ERROR_SUCCESS) {
        set_error(error, error_size,
                  "NCryptOpenStorageProvider failed with status 0x%08lX.",
                  (unsigned long)status);
        goto cleanup;
    }
    status = NCryptOpenKey(provider, &key, key_wide, 0, 0);
    if (status != ERROR_SUCCESS) {
        set_error(error, error_size,
                  "NCryptOpenKey failed with status 0x%08lX.",
                  (unsigned long)status);
        goto cleanup;
    }
    if (!read_dword_property(key, NCRYPT_EXPORT_POLICY_PROPERTY,
                             "Export Policy", &export_policy, error,
                             error_size)) {
        goto cleanup;
    }
    if (export_policy != 0) {
        set_error(error, error_size,
                  "The persisted key export policy is 0x%08lX, not zero.",
                  (unsigned long)export_policy);
        goto cleanup;
    }

    status = NCryptExportKey(key, 0, BCRYPT_RSAPRIVATE_BLOB, NULL, NULL, 0,
                            &private_blob_size, 0);
    if (status == ERROR_SUCCESS) {
        set_error(error, error_size,
                  "NCryptExportKey unexpectedly allowed a private-export "
                  "size query.");
        goto cleanup;
    }

    result = 1;

cleanup:
    if (key != 0) {
        (void)NCryptFreeObject(key);
    }
    if (provider != 0) {
        (void)NCryptFreeObject(provider);
    }
    free(provider_wide);
    free(key_wide);
    return result;
}

int cng_delete_named_key(const char *provider_name,
                         const char *key_name,
                         char *error,
                         size_t error_size)
{
    NCRYPT_PROV_HANDLE provider = 0;
    NCRYPT_KEY_HANDLE key = 0;
    wchar_t *provider_wide = NULL;
    wchar_t *key_wide = NULL;
    SECURITY_STATUS status;
    int result = 0;

    if (provider_name == NULL || provider_name[0] == '\0') {
        provider_name = CNG_DEFAULT_PROVIDER_NAME;
    }
    if (key_name == NULL || key_name[0] == '\0') {
        key_name = CNG_DEFAULT_KEY_NAME;
    }

    provider_wide = utf8_to_wide(provider_name, error, error_size);
    key_wide = utf8_to_wide(key_name, error, error_size);
    if (provider_wide == NULL || key_wide == NULL) {
        goto cleanup;
    }

    status = NCryptOpenStorageProvider(&provider, provider_wide, 0);
    if (status != ERROR_SUCCESS) {
        set_error(error, error_size,
                  "NCryptOpenStorageProvider failed with status 0x%08lX.",
                  (unsigned long)status);
        goto cleanup;
    }

    status = NCryptOpenKey(provider, &key, key_wide, 0, 0);
    if (is_missing_key_status(status)) {
        result = 1;
        goto cleanup;
    }
    if (status != ERROR_SUCCESS) {
        set_error(error, error_size,
                  "NCryptOpenKey for deletion failed with status 0x%08lX.",
                  (unsigned long)status);
        goto cleanup;
    }

    status = NCryptDeleteKey(key, 0);
    if (status != ERROR_SUCCESS) {
        set_error(error, error_size,
                  "NCryptDeleteKey failed with status 0x%08lX.",
                  (unsigned long)status);
        goto cleanup;
    }
    key = 0;
    result = 1;

cleanup:
    if (key != 0) {
        (void)NCryptFreeObject(key);
    }
    if (provider != 0) {
        (void)NCryptFreeObject(provider);
    }
    free(provider_wide);
    free(key_wide);
    return result;
}
