#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

#include <bcrypt.h>
#include <openssl/core_names.h>
#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/params.h>
#include <openssl/provider.h>

#include "cng_key.h"
#include "cng_provider_params.h"

static const unsigned char test_message[] =
    "OpenSSL provider signing with a non-exportable CNG key";

static void print_openssl_errors(const char *operation)
{
    fprintf(stderr, "%s failed.\n", operation);
    ERR_print_errors_fp(stderr);
}

static int split_provider_path(const char *input,
                               char *directory,
                               size_t directory_size,
                               char *module_name,
                               size_t module_name_size)
{
    char full_path[32768];
    char *slash;
    char *dot;
    DWORD length;

    length = GetFullPathNameA(input, (DWORD)sizeof(full_path), full_path, NULL);
    if (length == 0 || length >= sizeof(full_path)) {
        fprintf(stderr, "Unable to resolve provider DLL path: Win32 %lu.\n",
                GetLastError());
        return 0;
    }

    slash = strrchr(full_path, '\\');
    if (slash == NULL || slash == full_path) {
        fprintf(stderr, "Provider DLL path must include a directory.\n");
        return 0;
    }
    *slash = '\0';

    if (strlen(full_path) + 1 > directory_size
        || strlen(slash + 1) + 1 > module_name_size) {
        fprintf(stderr, "Provider DLL path is too long.\n");
        return 0;
    }
    strcpy_s(directory, directory_size, full_path);
    strcpy_s(module_name, module_name_size, slash + 1);

    dot = strrchr(module_name, '.');
    if (dot != NULL && _stricmp(dot, ".dll") == 0) {
        *dot = '\0';
    }
    return module_name[0] != '\0';
}

static EVP_PKEY *create_cng_pkey(OSSL_LIB_CTX *libctx,
                                  const char *property_query,
                                  char *key_name,
                                  char *provider_name)
{
    EVP_PKEY_CTX *context = NULL;
    EVP_PKEY *key = NULL;
    OSSL_PARAM params[3];

    context = EVP_PKEY_CTX_new_from_name(libctx, "RSA", property_query);
    if (context == NULL) {
        print_openssl_errors("EVP_PKEY_CTX_new_from_name");
        goto cleanup;
    }
    if (EVP_PKEY_fromdata_init(context) <= 0) {
        print_openssl_errors("EVP_PKEY_fromdata_init");
        goto cleanup;
    }

    params[0] = OSSL_PARAM_construct_utf8_string(CNG_PARAM_KEY_NAME,
                                                  key_name, 0);
    params[1] = OSSL_PARAM_construct_utf8_string(CNG_PARAM_PROVIDER_NAME,
                                                  provider_name, 0);
    params[2] = OSSL_PARAM_construct_end();

    if (EVP_PKEY_fromdata(context, &key, EVP_PKEY_KEYPAIR, params) <= 0) {
        print_openssl_errors("EVP_PKEY_fromdata for the named CNG key");
        key = NULL;
    }

cleanup:
    EVP_PKEY_CTX_free(context);
    return key;
}

static EVP_PKEY *create_standard_public_key(OSSL_LIB_CTX *libctx,
                                             EVP_PKEY *cng_key)
{
    EVP_PKEY_CTX *context = NULL;
    EVP_PKEY *public_key = NULL;
    OSSL_PARAM *public_params = NULL;

    if (EVP_PKEY_todata(cng_key, EVP_PKEY_PUBLIC_KEY, &public_params) <= 0) {
        print_openssl_errors("EVP_PKEY_todata for public key");
        goto cleanup;
    }

    context = EVP_PKEY_CTX_new_from_name(libctx, "RSA", "provider=default");
    if (context == NULL || EVP_PKEY_fromdata_init(context) <= 0
        || EVP_PKEY_fromdata(context, &public_key, EVP_PKEY_PUBLIC_KEY,
                             public_params)
               <= 0) {
        print_openssl_errors("Default-provider RSA public-key import");
        EVP_PKEY_free(public_key);
        public_key = NULL;
    }

cleanup:
    OSSL_PARAM_free(public_params);
    EVP_PKEY_CTX_free(context);
    return public_key;
}

static int confirm_provider_private_export_blocked(EVP_PKEY *key)
{
    OSSL_PARAM *private_params = NULL;
    int result;

    ERR_clear_error();
    result = EVP_PKEY_todata(key, EVP_PKEY_PRIVATE_KEY, &private_params);
    OSSL_PARAM_free(private_params);
    if (result > 0) {
        fprintf(stderr,
                "Provider unexpectedly exported private-key parameters.\n");
        return 0;
    }
    ERR_clear_error();
    return 1;
}

static int sign_and_verify(OSSL_LIB_CTX *libctx,
                           EVP_PKEY *signing_key,
                           EVP_PKEY *verification_key,
                           const char *signing_property_query,
                           const char *digest_name,
                           const char *padding_name,
                           const char *signing_salt_name,
                           const char *verification_salt_name)
{
    EVP_MD_CTX *sign_context = NULL;
    EVP_MD_CTX *verify_context = NULL;
    unsigned char *signature = NULL;
    size_t signature_length = 0;
    char padding[16];
    char signing_salt[32];
    char verification_salt[32];
    OSSL_PARAM signing_params[3];
    OSSL_PARAM verification_params[3];
    int is_pss;
    int result = 0;

    is_pss = _stricmp(padding_name, OSSL_PKEY_RSA_PAD_MODE_PSS) == 0;
    if (strcpy_s(padding, sizeof(padding), padding_name) != 0) {
        return 0;
    }

    signing_params[0] = OSSL_PARAM_construct_utf8_string(
        OSSL_SIGNATURE_PARAM_PAD_MODE, padding, 0);
    verification_params[0] = OSSL_PARAM_construct_utf8_string(
        OSSL_SIGNATURE_PARAM_PAD_MODE, padding, 0);
    if (is_pss) {
        if (strcpy_s(signing_salt, sizeof(signing_salt),
                     signing_salt_name)
                != 0
            || strcpy_s(verification_salt, sizeof(verification_salt),
                        verification_salt_name)
                   != 0) {
            return 0;
        }
        signing_params[1] = OSSL_PARAM_construct_utf8_string(
            OSSL_SIGNATURE_PARAM_PSS_SALTLEN, signing_salt, 0);
        signing_params[2] = OSSL_PARAM_construct_end();
        verification_params[1] = OSSL_PARAM_construct_utf8_string(
            OSSL_SIGNATURE_PARAM_PSS_SALTLEN, verification_salt, 0);
        verification_params[2] = OSSL_PARAM_construct_end();
    } else {
        signing_params[1] = OSSL_PARAM_construct_end();
        signing_params[2] = OSSL_PARAM_construct_end();
        verification_params[1] = OSSL_PARAM_construct_end();
        verification_params[2] = OSSL_PARAM_construct_end();
    }

    sign_context = EVP_MD_CTX_new();
    verify_context = EVP_MD_CTX_new();
    if (sign_context == NULL || verify_context == NULL) {
        print_openssl_errors("EVP_MD_CTX_new");
        goto cleanup;
    }

    if (EVP_DigestSignInit_ex(sign_context, NULL, digest_name, libctx,
                              signing_property_query, signing_key,
                              signing_params)
            <= 0
        || EVP_DigestSignUpdate(sign_context, test_message,
                                sizeof(test_message) - 1)
               <= 0
        || EVP_DigestSignFinal(sign_context, NULL, &signature_length) <= 0) {
        print_openssl_errors("EVP_DigestSign size query");
        goto cleanup;
    }

    signature = (unsigned char *)malloc(signature_length);
    if (signature == NULL) {
        fprintf(stderr, "Unable to allocate the signature buffer.\n");
        goto cleanup;
    }
    if (EVP_DigestSignFinal(sign_context, signature, &signature_length) <= 0) {
        print_openssl_errors("EVP_DigestSign");
        goto cleanup;
    }

    if (EVP_DigestVerifyInit_ex(verify_context, NULL, digest_name, libctx,
                                "provider=default", verification_key,
                                verification_params)
            <= 0
        || EVP_DigestVerifyUpdate(verify_context, test_message,
                                  sizeof(test_message) - 1)
               <= 0
        || EVP_DigestVerifyFinal(verify_context, signature,
                                 signature_length)
               <= 0) {
        print_openssl_errors("Default-provider EVP_DigestVerify");
        goto cleanup;
    }

    printf("Verified %s RSA signature with %s padding (%zu bytes).\n",
           digest_name, padding_name, signature_length);
    result = 1;

cleanup:
    free(signature);
    EVP_MD_CTX_free(sign_context);
    EVP_MD_CTX_free(verify_context);
    return result;
}

static int generate_owned_key_name(const char *provider_name,
                                   char *key_name,
                                   size_t key_name_size)
{
    unsigned char random_bytes[16];
    char error[512] = { 0 };
    unsigned int attempt;

    for (attempt = 0; attempt < 8; ++attempt) {
        CNG_KEY *existing = NULL;

        if (!BCRYPT_SUCCESS(BCryptGenRandom(
                NULL, random_bytes, sizeof(random_bytes),
                BCRYPT_USE_SYSTEM_PREFERRED_RNG))
            || sprintf_s(
                   key_name, key_name_size,
                   "openssl-cng-provider-test-%lu-"
                   "%02x%02x%02x%02x%02x%02x%02x%02x"
                   "%02x%02x%02x%02x%02x%02x%02x%02x",
                   GetCurrentProcessId(), random_bytes[0], random_bytes[1],
                   random_bytes[2], random_bytes[3], random_bytes[4],
                   random_bytes[5], random_bytes[6], random_bytes[7],
                   random_bytes[8], random_bytes[9], random_bytes[10],
                   random_bytes[11], random_bytes[12], random_bytes[13],
                   random_bytes[14], random_bytes[15])
               < 0) {
            SecureZeroMemory(random_bytes, sizeof(random_bytes));
            return 0;
        }
        error[0] = '\0';
        if (!cng_key_open(provider_name, key_name, &existing, error,
                          sizeof(error))) {
            SecureZeroMemory(random_bytes, sizeof(random_bytes));
            return 1;
        }
        cng_key_free(existing);
    }
    SecureZeroMemory(random_bytes, sizeof(random_bytes));
    return 0;
}

int main(int argc, char **argv)
{
    static const char *digests[] = { "SHA256", "SHA384", "SHA512" };
    OSSL_LIB_CTX *libctx = NULL;
    OSSL_PROVIDER *default_provider = NULL;
    OSSL_PROVIDER *cng_provider = NULL;
    EVP_PKEY *cng_key = NULL;
    EVP_PKEY *public_key = NULL;
    char provider_directory[32768];
    char module_name[256];
    char provider_property_query[320];
    char generated_key_name[128];
    char error[512] = { 0 };
    char *key_name;
    char *cng_provider_name;
    int key_created = 0;
    int result = EXIT_FAILURE;
    size_t digest_index;

    if (argc < 2 || argc > 3) {
        fprintf(stderr,
                "Usage: %s <full-path-to-microsoft_identity_native_provider.dll> "
                "[CNG-provider-name]\n",
                argv[0]);
        return EXIT_FAILURE;
    }

    if (!split_provider_path(argv[1], provider_directory,
                             sizeof(provider_directory), module_name,
                             sizeof(module_name))) {
        return EXIT_FAILURE;
    }
    if (sprintf_s(provider_property_query,
                  sizeof(provider_property_query), "provider=%s",
                  module_name)
        < 0) {
        return EXIT_FAILURE;
    }

    cng_provider_name =
        argc >= 3 ? argv[2] : CNG_DEFAULT_PROVIDER_NAME;
    if (!generate_owned_key_name(cng_provider_name, generated_key_name,
                                 sizeof(generated_key_name))) {
        fprintf(stderr, "Unable to allocate a unique process-owned key name.\n");
        return EXIT_FAILURE;
    }
    key_name = generated_key_name;

    libctx = OSSL_LIB_CTX_new();
    if (libctx == NULL
        || !OSSL_PROVIDER_set_default_search_path(libctx,
                                                   provider_directory)) {
        print_openssl_errors("OpenSSL library context initialization");
        goto cleanup;
    }

    default_provider = OSSL_PROVIDER_load(libctx, "default");
    cng_provider = OSSL_PROVIDER_load(libctx, module_name);
    if (default_provider == NULL || cng_provider == NULL) {
        print_openssl_errors("OSSL_PROVIDER_load");
        goto cleanup;
    }

    cng_key = create_cng_pkey(libctx, provider_property_query, key_name,
                              cng_provider_name);
    if (cng_key == NULL) {
        goto cleanup;
    }
    key_created = 1;
    if (EVP_PKEY_get_bits(cng_key) != 2048
        || EVP_PKEY_get_size(cng_key) != 256) {
        fprintf(stderr, "Provider key metadata is not 2048-bit RSA.\n");
        goto cleanup;
    }

    public_key = create_standard_public_key(libctx, cng_key);
    if (public_key == NULL
        || !confirm_provider_private_export_blocked(cng_key)) {
        goto cleanup;
    }

    if (!cng_private_export_is_blocked(cng_provider_name, key_name, error,
                                       sizeof(error))) {
        fprintf(stderr, "CNG private export check failed: %s\n", error);
        goto cleanup;
    }

    for (digest_index = 0;
         digest_index < sizeof(digests) / sizeof(digests[0]);
         ++digest_index) {
        if (!sign_and_verify(libctx, cng_key, public_key,
                             provider_property_query,
                             digests[digest_index],
                             OSSL_PKEY_RSA_PAD_MODE_PKCSV15, NULL, NULL)
            || !sign_and_verify(libctx, cng_key, public_key,
                                provider_property_query,
                                digests[digest_index],
                                OSSL_PKEY_RSA_PAD_MODE_PSS,
                                OSSL_PKEY_RSA_PSS_SALT_LEN_DIGEST,
                                OSSL_PKEY_RSA_PSS_SALT_LEN_DIGEST)) {
            goto cleanup;
        }
    }
    if (!sign_and_verify(libctx, cng_key, public_key,
                         provider_property_query, "SHA256",
                         OSSL_PKEY_RSA_PAD_MODE_PSS, "auto-digestmax",
                         OSSL_PKEY_RSA_PSS_SALT_LEN_DIGEST)) {
        goto cleanup;
    }

    printf("Private-key export is unavailable through OpenSSL and CNG.\n");
    result = EXIT_SUCCESS;

cleanup:
    EVP_PKEY_free(public_key);
    EVP_PKEY_free(cng_key);
    if (cng_provider != NULL) {
        (void)OSSL_PROVIDER_unload(cng_provider);
    }
    if (default_provider != NULL) {
        (void)OSSL_PROVIDER_unload(default_provider);
    }
    OSSL_LIB_CTX_free(libctx);

    if (key_created) {
        error[0] = '\0';
        if (!cng_delete_named_key(cng_provider_name, key_name, error,
                                  sizeof(error))) {
            fprintf(stderr, "Test-key cleanup failed: %s\n", error);
            result = EXIT_FAILURE;
        } else {
            printf("Deleted process-owned test CNG key '%s'.\n", key_name);
        }
    }
    return result;
}
