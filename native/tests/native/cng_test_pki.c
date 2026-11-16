#include <direct.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

#include <openssl/core_names.h>
#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/provider.h>
#include <openssl/store.h>
#include <openssl/x509v3.h>

#include "cng_provider_params.h"

static void print_errors(const char *operation)
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
    DWORD length =
        GetFullPathNameA(input, (DWORD)sizeof(full_path), full_path, NULL);

    if (length == 0 || length >= sizeof(full_path)) {
        return 0;
    }
    slash = strrchr(full_path, '\\');
    if (slash == NULL || slash == full_path) {
        return 0;
    }
    *slash = '\0';
    if (strcpy_s(directory, directory_size, full_path) != 0
        || strcpy_s(module_name, module_name_size, slash + 1) != 0) {
        return 0;
    }
    dot = strrchr(module_name, '.');
    if (dot != NULL && _stricmp(dot, ".dll") == 0) {
        *dot = '\0';
    }
    return module_name[0] != '\0';
}

static int output_path(char *path,
                       size_t path_size,
                       const char *directory,
                       const char *name)
{
    return sprintf_s(path, path_size, "%s\\%s", directory, name) >= 0;
}

static EVP_PKEY *create_cng_key(OSSL_LIB_CTX *libctx,
                                 const char *property_query,
                                 char *key_name,
                                 char *provider_name)
{
    EVP_PKEY_CTX *context =
        EVP_PKEY_CTX_new_from_name(libctx, "RSA", property_query);
    EVP_PKEY *key = NULL;
    OSSL_PARAM params[3];

    params[0] = OSSL_PARAM_construct_utf8_string(
        CNG_PARAM_KEY_NAME, key_name, 0);
    params[1] = OSSL_PARAM_construct_utf8_string(
        CNG_PARAM_PROVIDER_NAME, provider_name, 0);
    params[2] = OSSL_PARAM_construct_end();
    if (context == NULL || EVP_PKEY_fromdata_init(context) <= 0
        || EVP_PKEY_fromdata(context, &key, EVP_PKEY_KEYPAIR, params) <= 0) {
        print_errors("Creating the persisted CNG test key");
        EVP_PKEY_free(key);
        key = NULL;
    }
    EVP_PKEY_CTX_free(context);
    return key;
}

static EVP_PKEY *load_store_key(OSSL_LIB_CTX *libctx, const char *uri)
{
    OSSL_STORE_CTX *store =
        OSSL_STORE_open_ex(uri, libctx,
                           "provider=microsoft_identity_native_provider", NULL, NULL,
                           NULL, NULL, NULL);
    OSSL_STORE_INFO *info = NULL;
    EVP_PKEY *key = NULL;

    if (store == NULL || !OSSL_STORE_expect(store, OSSL_STORE_INFO_PKEY)) {
        print_errors("Opening the CNG STORE URI");
        goto cleanup;
    }
    while (!OSSL_STORE_eof(store)) {
        info = OSSL_STORE_load(store);
        if (info == NULL) {
            break;
        }
        if (OSSL_STORE_INFO_get_type(info) == OSSL_STORE_INFO_PKEY) {
            key = OSSL_STORE_INFO_get1_PKEY(info);
            OSSL_STORE_INFO_free(info);
            info = NULL;
            break;
        }
        OSSL_STORE_INFO_free(info);
        info = NULL;
    }
    if (key == NULL) {
        print_errors("Loading the CNG STORE key");
    }

cleanup:
    OSSL_STORE_INFO_free(info);
    if (store != NULL) {
        OSSL_STORE_close(store);
    }
    return key;
}

static EVP_PKEY *create_default_public_key(OSSL_LIB_CTX *libctx,
                                            EVP_PKEY *source)
{
    EVP_PKEY_CTX *context = NULL;
    EVP_PKEY *public_key = NULL;
    OSSL_PARAM *params = NULL;

    if (EVP_PKEY_todata(source, EVP_PKEY_PUBLIC_KEY, &params) <= 0) {
        goto cleanup;
    }
    context = EVP_PKEY_CTX_new_from_name(libctx, "RSA", "provider=default");
    if (context == NULL || EVP_PKEY_fromdata_init(context) <= 0
        || EVP_PKEY_fromdata(context, &public_key, EVP_PKEY_PUBLIC_KEY,
                             params)
               <= 0) {
        EVP_PKEY_free(public_key);
        public_key = NULL;
    }

cleanup:
    OSSL_PARAM_free(params);
    EVP_PKEY_CTX_free(context);
    if (public_key == NULL) {
        print_errors("Converting the CNG public key");
    }
    return public_key;
}

static EVP_PKEY *generate_rsa_key(OSSL_LIB_CTX *libctx)
{
    EVP_PKEY_CTX *context =
        EVP_PKEY_CTX_new_from_name(libctx, "RSA", "provider=default");
    EVP_PKEY *key = NULL;
    unsigned int bits = 2048;
    OSSL_PARAM params[] = {
        OSSL_PARAM_uint(OSSL_PKEY_PARAM_RSA_BITS, &bits),
        OSSL_PARAM_END
    };

    if (context == NULL || EVP_PKEY_keygen_init(context) <= 0
        || EVP_PKEY_CTX_set_params(context, params) <= 0
        || EVP_PKEY_generate(context, &key) <= 0) {
        print_errors("Generating a test RSA key");
        EVP_PKEY_free(key);
        key = NULL;
    }
    EVP_PKEY_CTX_free(context);
    return key;
}

static X509_NAME *create_name(const char *common_name)
{
    X509_NAME *name = X509_NAME_new();

    if (name == NULL
        || X509_NAME_add_entry_by_txt(
               name, "O", MBSTRING_ASC,
               (const unsigned char *)"OpenSSL CNG local tests", -1, -1, 0)
               != 1
        || X509_NAME_add_entry_by_txt(
               name, "CN", MBSTRING_ASC,
               (const unsigned char *)common_name, -1, -1, 0)
               != 1) {
        X509_NAME_free(name);
        return NULL;
    }
    return name;
}

static int add_extension(X509 *certificate,
                         X509 *issuer,
                         int nid,
                         const char *value)
{
    X509V3_CTX context;
    X509_EXTENSION *extension;
    int result;

    X509V3_set_ctx(&context, issuer, certificate, NULL, NULL, 0);
    extension = X509V3_EXT_conf_nid(NULL, &context, nid, (char *)value);
    if (extension == NULL) {
        return 0;
    }
    result = X509_add_ext(certificate, extension, -1);
    X509_EXTENSION_free(extension);
    return result == 1;
}

static int sign_certificate(OSSL_LIB_CTX *libctx,
                            X509 *certificate,
                            EVP_PKEY *issuer_key)
{
    EVP_MD_CTX *context = EVP_MD_CTX_new();
    int result =
        context != NULL
        && EVP_DigestSignInit_ex(context, NULL, "SHA256", libctx,
                                 "provider=default", issuer_key, NULL)
               > 0
        && X509_sign_ctx(certificate, context) > 0;

    EVP_MD_CTX_free(context);
    return result;
}

static X509 *create_certificate(OSSL_LIB_CTX *libctx,
                                long serial,
                                X509_NAME *subject,
                                EVP_PKEY *public_key,
                                X509 *issuer,
                                EVP_PKEY *issuer_key,
                                int certificate_type)
{
    X509 *certificate = X509_new_ex(libctx, "provider=default");
    X509_NAME *issuer_name =
        issuer == NULL ? subject : X509_get_subject_name(issuer);
    int extensions_ok;

    if ((certificate_type != 0 && issuer == NULL) || certificate == NULL
        || X509_set_version(certificate, 2) != 1
        || ASN1_INTEGER_set(X509_get_serialNumber(certificate), serial) != 1
        || X509_gmtime_adj(X509_getm_notBefore(certificate), -60) == NULL
        || X509_gmtime_adj(X509_getm_notAfter(certificate),
                           24L * 60L * 60L)
               == NULL
        || X509_set_subject_name(certificate, subject) != 1
        || X509_set_issuer_name(certificate, issuer_name) != 1
        || X509_set_pubkey(certificate, public_key) != 1) {
        X509_free(certificate);
        return NULL;
    }

    if (certificate_type == 0) {
        extensions_ok =
            add_extension(certificate, certificate, NID_basic_constraints,
                          "critical,CA:TRUE")
            && add_extension(certificate, certificate, NID_key_usage,
                             "critical,keyCertSign,cRLSign")
            && add_extension(certificate, certificate,
                             NID_subject_key_identifier, "hash");
    } else if (certificate_type == 3) {
        extensions_ok =
            add_extension(certificate, issuer, NID_basic_constraints,
                          "critical,CA:TRUE,pathlen:0")
            && add_extension(certificate, issuer, NID_key_usage,
                             "critical,keyCertSign,cRLSign")
            && add_extension(certificate, issuer,
                             NID_subject_key_identifier, "hash")
            && add_extension(certificate, issuer,
                             NID_authority_key_identifier, "keyid:always");
    } else if (certificate_type == 1) {
        extensions_ok =
            add_extension(certificate, issuer, NID_basic_constraints,
                          "critical,CA:FALSE")
            && add_extension(certificate, issuer, NID_key_usage,
                             "critical,digitalSignature,keyEncipherment")
            && add_extension(certificate, issuer, NID_ext_key_usage,
                             "serverAuth")
            && add_extension(certificate, issuer, NID_subject_alt_name,
                             "DNS:localhost")
            && add_extension(certificate, issuer,
                             NID_authority_key_identifier, "keyid:always");
    } else {
        extensions_ok =
            add_extension(certificate, issuer, NID_basic_constraints,
                          "critical,CA:FALSE")
            && add_extension(certificate, issuer, NID_key_usage,
                             "critical,digitalSignature")
            && add_extension(certificate, issuer, NID_ext_key_usage,
                             "clientAuth")
            && add_extension(certificate, issuer,
                             NID_authority_key_identifier, "keyid:always");
    }
    if (!extensions_ok
        || !sign_certificate(libctx, certificate, issuer_key)) {
        X509_free(certificate);
        return NULL;
    }
    return certificate;
}

static X509_REQ *create_cng_csr(OSSL_LIB_CTX *libctx,
                                EVP_PKEY *cng_key,
                                EVP_PKEY *public_key,
                                X509_NAME *subject,
                                const char *property_query)
{
    X509_REQ *request = X509_REQ_new_ex(libctx, "provider=default");
    EVP_MD_CTX *sign_context = EVP_MD_CTX_new();

    if (request == NULL || sign_context == NULL
        || X509_REQ_set_version(request, 0) != 1
        || X509_REQ_set_subject_name(request, subject) != 1
        || X509_REQ_set_pubkey(request, public_key) != 1) {
        print_errors("Preparing the CNG-signed CSR");
        X509_REQ_free(request);
        EVP_MD_CTX_free(sign_context);
        return NULL;
    }
    if (EVP_DigestSignInit_ex(sign_context, NULL, "SHA256", libctx,
                              property_query, cng_key, NULL)
            <= 0) {
        print_errors("Initializing the CNG-signed CSR");
        X509_REQ_free(request);
        EVP_MD_CTX_free(sign_context);
        return NULL;
    }
    if (X509_REQ_sign_ctx(request, sign_context) <= 0) {
        print_errors("Creating and verifying the CNG-signed CSR");
        X509_REQ_free(request);
        request = NULL;
    } else {
        if (X509_REQ_verify(request, public_key) != 1) {
            print_errors("Verifying the CNG-signed CSR");
            X509_REQ_free(request);
            request = NULL;
        }
    }
    EVP_MD_CTX_free(sign_context);
    return request;
}

static int write_certificate(const char *path, X509 *certificate)
{
    BIO *output = BIO_new_file(path, "wb");
    int result =
        output != NULL && PEM_write_bio_X509(output, certificate) == 1;

    BIO_free(output);
    return result;
}

static int write_certificate_chain(const char *path,
                                   X509 *certificate,
                                   X509 *intermediate)
{
    BIO *output = BIO_new_file(path, "wb");
    int result =
        output != NULL && PEM_write_bio_X509(output, certificate) == 1
        && PEM_write_bio_X509(output, intermediate) == 1;

    BIO_free(output);
    return result;
}

static int write_private_key(const char *path, EVP_PKEY *key)
{
    BIO *output = BIO_new_file(path, "wb");
    int result = output != NULL
                 && PEM_write_bio_PrivateKey(output, key, NULL, NULL, 0,
                                             NULL, NULL)
                        == 1;

    BIO_free(output);
    return result;
}

static int write_csr(const char *path, X509_REQ *request)
{
    BIO *output = BIO_new_file(path, "wb");
    int result =
        output != NULL && PEM_write_bio_X509_REQ(output, request) == 1;

    BIO_free(output);
    return result;
}

int main(int argc, char **argv)
{
    OSSL_LIB_CTX *libctx = NULL;
    OSSL_PROVIDER *default_provider = NULL;
    OSSL_PROVIDER *cng_provider = NULL;
    EVP_PKEY *created_cng_key = NULL;
    EVP_PKEY *cng_key = NULL;
    EVP_PKEY *cng_public = NULL;
    EVP_PKEY *ca_key = NULL;
    EVP_PKEY *intermediate_key = NULL;
    EVP_PKEY *server_key = NULL;
    EVP_PKEY *wrong_key = NULL;
    X509_NAME *ca_name = NULL;
    X509_NAME *intermediate_name = NULL;
    X509_NAME *server_name = NULL;
    X509_NAME *client_name = NULL;
    X509_NAME *wrong_name = NULL;
    X509 *ca_certificate = NULL;
    X509 *intermediate_certificate = NULL;
    X509 *server_certificate = NULL;
    X509 *client_certificate = NULL;
    X509 *wrong_certificate = NULL;
    X509_REQ *client_request = NULL;
    char provider_directory[32768];
    char module_name[256];
    char property_query[320];
    char path[32768];
    int result = EXIT_FAILURE;

    if (argc != 7) {
        fprintf(stderr,
                "Usage: %s <provider-dll> <output-dir> <key-name> "
                "<CNG-provider-name> <store-uri> <marker-name>\n",
                argv[0]);
        return EXIT_FAILURE;
    }
    if (_mkdir(argv[2]) != 0 && errno != EEXIST) {
        fprintf(stderr, "Unable to create output directory '%s'.\n", argv[2]);
        return EXIT_FAILURE;
    }
    if (!split_provider_path(argv[1], provider_directory,
                             sizeof(provider_directory), module_name,
                             sizeof(module_name))
        || sprintf_s(property_query, sizeof(property_query), "provider=%s",
                     module_name)
               < 0) {
        fprintf(stderr, "The provider path is invalid.\n");
        return EXIT_FAILURE;
    }

    libctx = OSSL_LIB_CTX_new();
    if (libctx == NULL
        || !OSSL_PROVIDER_set_default_search_path(libctx,
                                                   provider_directory)
        || (default_provider = OSSL_PROVIDER_load(libctx, "default")) == NULL
        || (cng_provider = OSSL_PROVIDER_load(libctx, module_name)) == NULL) {
        print_errors("Loading providers for local PKI generation");
        goto cleanup;
    }

    created_cng_key =
        create_cng_key(libctx, property_query, argv[3], argv[4]);
    if (created_cng_key == NULL) {
        goto cleanup;
    }
    EVP_PKEY_free(created_cng_key);
    created_cng_key = NULL;
    cng_key = load_store_key(libctx, argv[5]);
    cng_public = create_default_public_key(libctx, cng_key);
    ca_key = generate_rsa_key(libctx);
    intermediate_key = generate_rsa_key(libctx);
    server_key = generate_rsa_key(libctx);
    wrong_key = generate_rsa_key(libctx);
    ca_name = create_name("OpenSSL CNG Local Test CA");
    intermediate_name = create_name("OpenSSL CNG Client Intermediate");
    server_name = create_name("localhost");
    client_name = create_name("CNG mTLS Client");
    wrong_name = create_name("Wrong mTLS Client");
    if (cng_key == NULL || cng_public == NULL || ca_key == NULL
        || intermediate_key == NULL || server_key == NULL
        || wrong_key == NULL || ca_name == NULL || intermediate_name == NULL
        || server_name == NULL || client_name == NULL || wrong_name == NULL) {
        goto cleanup;
    }

    ca_certificate = create_certificate(libctx, 1, ca_name, ca_key, NULL,
                                        ca_key, 0);
    intermediate_certificate = create_certificate(
        libctx, 2, intermediate_name, intermediate_key, ca_certificate,
        ca_key, 3);
    server_certificate = create_certificate(
        libctx, 3, server_name, server_key, ca_certificate, ca_key, 1);
    client_certificate = create_certificate(
        libctx, 4, client_name, cng_public, intermediate_certificate,
        intermediate_key, 2);
    wrong_certificate = create_certificate(
        libctx, 5, wrong_name, wrong_key, ca_certificate, ca_key, 2);
    client_request = create_cng_csr(libctx, cng_key, cng_public, client_name,
                                    property_query);
    if (ca_certificate == NULL || intermediate_certificate == NULL
        || server_certificate == NULL
        || client_certificate == NULL || wrong_certificate == NULL
        || client_request == NULL
        || X509_check_private_key(client_certificate, cng_key) != 1) {
        print_errors("Creating the local test certificates");
        goto cleanup;
    }

    if (!output_path(path, sizeof(path), argv[2], "ca.pem")
        || !write_certificate(path, ca_certificate)
        || !output_path(path, sizeof(path), argv[2], "server-cert.pem")
        || !write_certificate(path, server_certificate)
        || !output_path(path, sizeof(path), argv[2], "server-key.pem")
        || !write_private_key(path, server_key)
        || !output_path(path, sizeof(path), argv[2], "client-cert.pem")
        || !write_certificate_chain(path, client_certificate,
                                    intermediate_certificate)
        || !output_path(path, sizeof(path), argv[2],
                        "wrong-client-cert.pem")
        || !write_certificate(path, wrong_certificate)
        || !output_path(path, sizeof(path), argv[2], "client.csr.pem")
        || !write_csr(path, client_request)
        || !output_path(path, sizeof(path), argv[2], argv[6])) {
        print_errors("Writing local PKI artifacts");
        goto cleanup;
    }
    {
        FILE *marker = NULL;
        int marker_ok;
        if (fopen_s(&marker, path, "wb") != 0 || marker == NULL) {
            fprintf(stderr, "Unable to write the PKI marker.\n");
            goto cleanup;
        }
        marker_ok = fputs("ready\n", marker) >= 0;
        marker_ok = fclose(marker) == 0 && marker_ok;
        marker = NULL;
        if (!marker_ok) {
            if (marker != NULL) {
                fclose(marker);
            }
            fprintf(stderr, "Unable to write the PKI marker.\n");
            goto cleanup;
        }
    }

    printf("Generated local test PKI in %s using STORE key %s.\n", argv[2],
           argv[3]);
    result = EXIT_SUCCESS;

cleanup:
    X509_REQ_free(client_request);
    X509_free(wrong_certificate);
    X509_free(client_certificate);
    X509_free(server_certificate);
    X509_free(intermediate_certificate);
    X509_free(ca_certificate);
    X509_NAME_free(wrong_name);
    X509_NAME_free(client_name);
    X509_NAME_free(server_name);
    X509_NAME_free(intermediate_name);
    X509_NAME_free(ca_name);
    EVP_PKEY_free(wrong_key);
    EVP_PKEY_free(server_key);
    EVP_PKEY_free(intermediate_key);
    EVP_PKEY_free(ca_key);
    EVP_PKEY_free(cng_public);
    EVP_PKEY_free(cng_key);
    EVP_PKEY_free(created_cng_key);
    if (cng_provider != NULL) {
        OSSL_PROVIDER_unload(cng_provider);
    }
    if (default_provider != NULL) {
        OSSL_PROVIDER_unload(default_provider);
    }
    OSSL_LIB_CTX_free(libctx);
    return result;
}
