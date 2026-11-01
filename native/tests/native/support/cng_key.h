#ifndef CNG_KEY_H
#define CNG_KEY_H

#include <stddef.h>
#include <windows.h>

#include "cng_provider_params.h"

typedef struct cng_key_st CNG_KEY;

typedef enum cng_rsa_padding_st {
    CNG_RSA_PADDING_PKCS1 = 1,
    CNG_RSA_PADDING_PSS = 2
} CNG_RSA_PADDING;

int cng_key_open_or_create(const char *provider_name,
                           const char *key_name,
                           unsigned int bits,
                           CNG_KEY **out_key,
                           char *error,
                           size_t error_size);

int cng_key_open(const char *provider_name,
                 const char *key_name,
                 CNG_KEY **out_key,
                 char *error,
                 size_t error_size);

int cng_key_up_ref(CNG_KEY *key);
void cng_key_free(CNG_KEY *key);

const char *cng_key_provider_name(const CNG_KEY *key);
const char *cng_key_name(const CNG_KEY *key);
unsigned int cng_key_bits(const CNG_KEY *key);
unsigned int cng_key_security_bits(const CNG_KEY *key);
size_t cng_key_max_signature_size(const CNG_KEY *key);
const unsigned char *cng_key_public_modulus(const CNG_KEY *key,
                                            size_t *length);
const unsigned char *cng_key_public_exponent(const CNG_KEY *key,
                                             size_t *length);

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
                      size_t error_size);

int cng_private_export_is_blocked(const char *provider_name,
                                  const char *key_name,
                                  char *error,
                                  size_t error_size);

int cng_delete_named_key(const char *provider_name,
                         const char *key_name,
                         char *error,
                         size_t error_size);

#endif
