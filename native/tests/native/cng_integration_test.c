#include <direct.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

#include <bcrypt.h>
#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/provider.h>
#include <openssl/store.h>

#include "cng_key.h"
#include "microsoft_identity_native_primitives.h"
#include "cng_provider_params.h"

#define PROCESS_TIMEOUT_MS 30000U

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
    return 1;
}

static int url_encode(const char *input, char *output, size_t output_size)
{
    static const char hex[] = "0123456789ABCDEF";
    size_t source;
    size_t destination = 0;

    for (source = 0; input[source] != '\0'; ++source) {
        unsigned char value = (unsigned char)input[source];
        int unreserved =
            (value >= 'A' && value <= 'Z')
            || (value >= 'a' && value <= 'z')
            || (value >= '0' && value <= '9')
            || value == '-' || value == '.' || value == '_' || value == '~';

        if (unreserved) {
            if (destination + 1 >= output_size) {
                return 0;
            }
            output[destination++] = (char)value;
        } else {
            if (destination + 3 >= output_size) {
                return 0;
            }
            output[destination++] = '%';
            output[destination++] = hex[value >> 4];
            output[destination++] = hex[value & 15U];
        }
    }
    output[destination] = '\0';
    return 1;
}

static int make_store_uri(const char *key_name,
                          const char *provider_name,
                          char *uri,
                          size_t uri_size)
{
    char encoded_key[1024];
    char encoded_provider[1024];

    return url_encode(key_name, encoded_key, sizeof(encoded_key))
           && url_encode(provider_name, encoded_provider,
                         sizeof(encoded_provider))
           && sprintf_s(uri, uri_size, "cng://?key=%s&provider=%s",
                        encoded_key, encoded_provider)
                  >= 0;
}

static int generate_owned_key_name(char *key_name, size_t key_name_size)
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
                   "openssl-cng-integration-%lu-"
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
        if (!cng_key_open(CNG_DEFAULT_PROVIDER_NAME, key_name, &existing,
                          error, sizeof(error))) {
            SecureZeroMemory(random_bytes, sizeof(random_bytes));
            return 1;
        }
        cng_key_free(existing);
    }
    SecureZeroMemory(random_bytes, sizeof(random_bytes));
    return 0;
}

static int make_path(char *output,
                     size_t output_size,
                     const char *directory,
                     const char *name)
{
    return sprintf_s(output, output_size, "%s\\%s", directory, name) >= 0;
}

static wchar_t *utf8_to_wide(const char *value)
{
    int required = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value,
                                       -1, NULL, 0);
    wchar_t *wide;

    if (required <= 0) {
        return NULL;
    }
    wide = (wchar_t *)calloc((size_t)required, sizeof(*wide));
    if (wide == NULL
        || MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value, -1,
                               wide, required)
               != required) {
        free(wide);
        return NULL;
    }
    return wide;
}

static int create_utf8_directory(const char *path)
{
    wchar_t *wide = utf8_to_wide(path);
    int result =
        wide != NULL
        && (CreateDirectoryW(wide, NULL) || GetLastError() == ERROR_ALREADY_EXISTS);

    free(wide);
    return result;
}

static int copy_utf8_file(const char *source, const char *destination)
{
    wchar_t *source_wide = utf8_to_wide(source);
    wchar_t *destination_wide = utf8_to_wide(destination);
    int result = source_wide != NULL && destination_wide != NULL
                 && CopyFileW(source_wide, destination_wide, FALSE);

    free(destination_wide);
    free(source_wide);
    return result;
}

static int start_process(const char *command_line, PROCESS_INFORMATION *process)
{
    STARTUPINFOA startup;
    char mutable_command[32768];

    if (strcpy_s(mutable_command, sizeof(mutable_command), command_line)
        != 0) {
        return 0;
    }
    SecureZeroMemory(&startup, sizeof(startup));
    SecureZeroMemory(process, sizeof(*process));
    startup.cb = sizeof(startup);
    if (!CreateProcessA(NULL, mutable_command, NULL, NULL, FALSE,
                        CREATE_NO_WINDOW, NULL, NULL, &startup, process)) {
        fprintf(stderr, "CreateProcess failed with Win32 error %lu.\n",
                GetLastError());
        return 0;
    }
    CloseHandle(process->hThread);
    process->hThread = NULL;
    return 1;
}

static int wait_process(PROCESS_INFORMATION *process, DWORD timeout_ms)
{
    DWORD wait_result;
    DWORD exit_code = STILL_ACTIVE;

    wait_result = WaitForSingleObject(process->hProcess, timeout_ms);
    if (wait_result != WAIT_OBJECT_0
        || !GetExitCodeProcess(process->hProcess, &exit_code)) {
        return 0;
    }
    return exit_code == 0;
}

static void stop_process(PROCESS_INFORMATION *process)
{
    if (process->hProcess != NULL) {
        if (WaitForSingleObject(process->hProcess, 0) == WAIT_TIMEOUT) {
            TerminateProcess(process->hProcess, 1);
            WaitForSingleObject(process->hProcess, 5000);
        }
        CloseHandle(process->hProcess);
        process->hProcess = NULL;
    }
}

static int run_process(const char *command_line)
{
    PROCESS_INFORMATION process;
    int result;

    if (!start_process(command_line, &process)) {
        return 0;
    }
    result = wait_process(&process, PROCESS_TIMEOUT_MS);
    stop_process(&process);
    return result;
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
    return public_key;
}

static int malformed_store_uris_are_rejected(OSSL_LIB_CTX *libctx)
{
    static const char *invalid[] = {
        "cng://",
        "cng://?provider=Microsoft%20Software%20Key%20Storage%20Provider",
        "cng://?key=a&key=b",
        "cng://?key=a&unknown=b",
        "cng://?key=a&provider=x&provider=y",
        "cng://?key=a%",
        "cng://?key=a+b",
        "cng://?key=",
        "cng://?key=a&"
    };
    char oversized[700];
    size_t index;

    memset(oversized, 'a', sizeof(oversized));
    memcpy(oversized, "cng://?key=", 11);
    oversized[sizeof(oversized) - 1] = '\0';

    for (index = 0; index < sizeof(invalid) / sizeof(invalid[0]); ++index) {
        OSSL_STORE_CTX *store =
            OSSL_STORE_open_ex(invalid[index], libctx,
                               "provider=microsoft_identity_native_provider", NULL, NULL, NULL,
                               NULL, NULL);
        if (store != NULL) {
            OSSL_STORE_close(store);
            fprintf(stderr, "Malformed STORE URI was accepted: %s\n",
                    invalid[index]);
            return 0;
        }
        ERR_clear_error();
    }
    {
        OSSL_STORE_CTX *store =
            OSSL_STORE_open_ex(oversized, libctx,
                               "provider=microsoft_identity_native_provider",
                               NULL, NULL, NULL, NULL, NULL);
        if (store != NULL) {
            OSSL_STORE_close(store);
            fprintf(stderr, "Oversized STORE URI field was accepted.\n");
            return 0;
        }
        ERR_clear_error();
    }
    return 1;
}

static int pubkey_store_expectation_is_rejected(OSSL_LIB_CTX *libctx,
                                                const char *uri)
{
    OSSL_STORE_CTX *store =
        OSSL_STORE_open_ex(uri, libctx,
                           "provider=microsoft_identity_native_provider", NULL, NULL,
                           NULL, NULL, NULL);
    int result;

    if (store == NULL) {
        return 0;
    }
    ERR_clear_error();
    result = OSSL_STORE_expect(store, OSSL_STORE_INFO_PUBKEY) == 0;
    OSSL_STORE_close(store);
    ERR_clear_error();
    if (!result) {
        fprintf(stderr, "STORE accepted an unsupported PUBKEY expectation.\n");
    }
    return result;
}

static int wait_for_port(const char *ready_file, unsigned short *port)
{
    unsigned int attempt;

    for (attempt = 0; attempt < 200; ++attempt) {
        FILE *file = NULL;
        unsigned int parsed;

        if (fopen_s(&file, ready_file, "rb") == 0 && file != NULL) {
            if (fscanf_s(file, "%u", &parsed) == 1 && parsed > 0
                && parsed <= 65535U) {
                fclose(file);
                *port = (unsigned short)parsed;
                return 1;
            }
            fclose(file);
        }
        Sleep(50);
    }
    return 0;
}

static void initialize_request(MICROSOFT_IDENTITY_NATIVE_REQUEST *request,
                               const char *provider_path,
                               const char *store_uri,
                               const char *certificate_path,
                               const char *ca_path,
                               const char *host,
                               unsigned short port)
{
    SecureZeroMemory(request, sizeof(*request));
    request->struct_size = sizeof(*request);
    request->abi_version = MICROSOFT_IDENTITY_NATIVE_ABI_VERSION;
    request->provider_module_path = provider_path;
    request->store_uri = store_uri;
    request->client_certificate_path = certificate_path;
    request->ca_bundle_path = ca_path;
    request->host = host;
    request->port = port;
    request->method = "POST";
    request->target = "/mtls";
    request->connect_timeout_ms = 5000;
    request->io_timeout_ms = 5000;
}

int main(int argc, char **argv)
{
    char key_name[128];
    char store_uri[CNG_MAX_STORE_URI];
    char artifact_root[32768];
    char artifact_directory[32768];
    char provider_directory[32768];
    char module_name[256];
    char command_line[32768];
    char ca_path[32768];
    char server_cert_path[32768];
    char server_key_path[32768];
    char client_cert_path[32768];
    char wrong_cert_path[32768];
    char ready_file[32768];
    char unicode_directory[32768];
    char unicode_provider_path[32768];
    char unicode_client_cert_path[32768];
    char unicode_ca_path[32768];
    OSSL_LIB_CTX *libctx = NULL;
    OSSL_PROVIDER *default_provider = NULL;
    OSSL_PROVIDER *cng_provider = NULL;
    EVP_PKEY *key = NULL;
    EVP_PKEY *key_duplicate = NULL;
    EVP_PKEY *public_key = NULL;
    EVP_PKEY_CTX *key_check = NULL;
    OSSL_PARAM *private_params = NULL;
    PROCESS_INFORMATION server_process;
    unsigned short port = 0;
    MICROSOFT_IDENTITY_NATIVE_REQUEST request;
    MICROSOFT_IDENTITY_NATIVE_RESPONSE response;
    MICROSOFT_IDENTITY_NATIVE_ERROR bridge_error;
    MICROSOFT_IDENTITY_NATIVE_HEADER headers[2];
    const unsigned char body[] = "local request body";
    char platform_error[512] = { 0 };
    CNG_KEY *deleted_key = NULL;
    int server_started = 0;
    int owns_key_name = 0;
    int result = EXIT_FAILURE;

    SecureZeroMemory(&server_process, sizeof(server_process));
    SecureZeroMemory(&response, sizeof(response));
    if (argc != 6) {
        fprintf(stderr,
                "Usage: %s <provider-dll> <signing-test-exe> <pki-exe> "
                "<server-exe> <artifact-root>\n",
                argv[0]);
        return EXIT_FAILURE;
    }
    if (!generate_owned_key_name(key_name, sizeof(key_name))
        || !make_store_uri(key_name, CNG_DEFAULT_PROVIDER_NAME, store_uri,
                           sizeof(store_uri))) {
        return EXIT_FAILURE;
    }
    owns_key_name = 1;

    if (strcpy_s(artifact_root, sizeof(artifact_root), argv[5]) != 0
        || (_mkdir(artifact_root) != 0 && errno != EEXIST)
        || sprintf_s(artifact_directory, sizeof(artifact_directory),
                     "%s\\run-%lu", artifact_root, GetCurrentProcessId())
               < 0
        || (_mkdir(artifact_directory) != 0 && errno != EEXIST)
        || !make_path(ca_path, sizeof(ca_path), artifact_directory, "ca.pem")
        || !make_path(server_cert_path, sizeof(server_cert_path),
                      artifact_directory, "server-cert.pem")
        || !make_path(server_key_path, sizeof(server_key_path),
                      artifact_directory, "server-key.pem")
        || !make_path(client_cert_path, sizeof(client_cert_path),
                      artifact_directory, "client-cert.pem")
        || !make_path(wrong_cert_path, sizeof(wrong_cert_path),
                      artifact_directory, "wrong-client-cert.pem")
        || !make_path(ready_file, sizeof(ready_file), artifact_directory,
                      "server.ready")
        || !make_path(unicode_directory, sizeof(unicode_directory),
                      artifact_directory,
                      "unicode-\xE6\xB5\x8B\xE8\xAF\x95")
        || !create_utf8_directory(unicode_directory)
        || !make_path(unicode_provider_path, sizeof(unicode_provider_path),
                      unicode_directory, "microsoft_identity_native_provider.dll")
        || !make_path(unicode_client_cert_path,
                      sizeof(unicode_client_cert_path), unicode_directory,
                      "client-cert.pem")
        || !make_path(unicode_ca_path, sizeof(unicode_ca_path),
                      unicode_directory, "ca.pem")) {
        fprintf(stderr, "Unable to prepare the build-tree artifact path.\n");
        goto cleanup;
    }

    if (sprintf_s(command_line, sizeof(command_line), "\"%s\" \"%s\"",
                  argv[2], argv[1])
            < 0
        || !run_process(command_line)) {
        fprintf(stderr, "The preserved provider signing test failed.\n");
        goto cleanup;
    }

    if (sprintf_s(command_line, sizeof(command_line),
                  "\"%s\" \"%s\" \"%s\" \"%s\" \"%s\" \"%s\" "
                  "\"pki.ready\"",
                  argv[3], argv[1], artifact_directory, key_name,
                  CNG_DEFAULT_PROVIDER_NAME, store_uri)
            < 0
        || !run_process(command_line)) {
        fprintf(stderr, "Local PKI generation failed.\n");
        goto cleanup;
    }
    if (!copy_utf8_file(argv[1], unicode_provider_path)
        || !copy_utf8_file(client_cert_path, unicode_client_cert_path)
        || !copy_utf8_file(ca_path, unicode_ca_path)) {
        fprintf(stderr, "Unable to prepare Unicode-path test artifacts.\n");
        goto cleanup;
    }

    if (!split_provider_path(argv[1], provider_directory,
                             sizeof(provider_directory), module_name,
                             sizeof(module_name))) {
        goto cleanup;
    }
    libctx = OSSL_LIB_CTX_new();
    if (libctx == NULL
        || !OSSL_PROVIDER_set_default_search_path(libctx,
                                                   provider_directory)
        || (default_provider = OSSL_PROVIDER_load(libctx, "default")) == NULL
        || (cng_provider = OSSL_PROVIDER_load(libctx, module_name)) == NULL) {
        ERR_print_errors_fp(stderr);
        goto cleanup;
    }
    key = load_store_key(libctx, store_uri);
    if (key == NULL) {
        fprintf(stderr, "The STORE URI returned no key.\n");
        ERR_print_errors_fp(stderr);
        goto cleanup;
    }
    key_duplicate = EVP_PKEY_dup(key);
    public_key = create_default_public_key(libctx, key);
    key_check = EVP_PKEY_CTX_new_from_pkey(libctx, key, NULL);
    if (key_duplicate == NULL || public_key == NULL
        || EVP_PKEY_eq(key, public_key) != 1 || key_check == NULL
        || EVP_PKEY_public_check(key_check) <= 0
        || EVP_PKEY_private_check(key_check) <= 0
        || !malformed_store_uris_are_rejected(libctx)
        || !pubkey_store_expectation_is_rejected(libctx, store_uri)) {
        fprintf(stderr, "STORE loading, duplication, matching, or validation failed.\n");
        ERR_print_errors_fp(stderr);
        goto cleanup;
    }
    EVP_PKEY_free(key);
    key = key_duplicate;
    key_duplicate = NULL;

    ERR_clear_error();
    if (EVP_PKEY_todata(key, EVP_PKEY_PRIVATE_KEY, &private_params) > 0
        || !cng_private_export_is_blocked(
            CNG_DEFAULT_PROVIDER_NAME, key_name, platform_error,
            sizeof(platform_error))) {
        fprintf(stderr, "Private key export was not blocked.\n");
        goto cleanup;
    }
    OSSL_PARAM_free(private_params);
    private_params = NULL;
    ERR_clear_error();

    initialize_request(&request, argv[1], store_uri, wrong_cert_path, ca_path,
                       "localhost", 1);
    if (microsoft_identity_native_request(&request, &response, &bridge_error)
        || bridge_error.code != MICROSOFT_IDENTITY_NATIVE_ERROR_CERTIFICATE) {
        fprintf(stderr,
                "A mismatched client key and certificate was not rejected "
                "(code %d).\n",
                bridge_error.code);
        goto cleanup;
    }

    initialize_request(&request, argv[1], store_uri, client_cert_path, ca_path,
                       "localhost", 1);
    request.target = "https://localhost/mtls";
    if (microsoft_identity_native_request(&request, &response, &bridge_error)
        || bridge_error.code != MICROSOFT_IDENTITY_NATIVE_ERROR_INVALID_ARGUMENT) {
        fprintf(stderr, "An absolute request target was not rejected.\n");
        goto cleanup;
    }
    headers[0].name = "X-Request-ID";
    headers[0].value = "safe\r\nInjected: yes";
    initialize_request(&request, argv[1], store_uri, client_cert_path, ca_path,
                       "localhost", 1);
    request.headers = headers;
    request.header_count = 1;
    if (microsoft_identity_native_request(&request, &response, &bridge_error)
        || bridge_error.code != MICROSOFT_IDENTITY_NATIVE_ERROR_INVALID_ARGUMENT) {
        fprintf(stderr, "CRLF header injection was not rejected.\n");
        goto cleanup;
    }

    DeleteFileA(ready_file);
    if (sprintf_s(command_line, sizeof(command_line),
                  "\"%s\" \"%s\" \"%s\" \"%s\" \"%s\" 2 success",
                  argv[4], ca_path, server_cert_path, server_key_path,
                  ready_file)
            < 0
        || !start_process(command_line, &server_process)) {
        fprintf(stderr, "Unable to start the local mTLS server.\n");
        goto cleanup;
    }
    server_started = 1;
    if (!wait_for_port(ready_file, &port)) {
        fprintf(stderr, "The local mTLS server did not become ready.\n");
        goto cleanup;
    }

    initialize_request(&request, argv[1], store_uri, client_cert_path, ca_path,
                       "127.0.0.1", port);
    if (microsoft_identity_native_request(&request, &response, &bridge_error)
        || bridge_error.code != MICROSOFT_IDENTITY_NATIVE_ERROR_TLS) {
        fprintf(stderr,
                "Hostname verification failure was not reported "
                "(code %d, system %d): %s\n",
                bridge_error.code, bridge_error.system_error,
                bridge_error.message);
        goto cleanup;
    }

    headers[0].name = "Accept";
    headers[0].value = "text/plain";
    headers[1].name = "X-Request-ID";
    headers[1].value = "cng-local-integration";
    initialize_request(&request, unicode_provider_path, store_uri,
                       unicode_client_cert_path, unicode_ca_path, "localhost",
                       port);
    request.headers = headers;
    request.header_count = 2;
    request.body = body;
    request.body_size = sizeof(body) - 1;
    if (!microsoft_identity_native_request(&request, &response, &bridge_error)
        || response.http_status != 200
        || response.headers == NULL
        || strstr(response.headers, "Content-Type: text/plain") == NULL
        || response.body_length != sizeof("cng mTLS success\n") - 1
        || memcmp(response.body, "cng mTLS success\n",
                  sizeof("cng mTLS success\n") - 1)
               != 0) {
        fprintf(stderr, "The local mTLS request failed: %s\n",
                bridge_error.message);
        goto cleanup;
    }
    microsoft_identity_native_response_free(&response);

    if (!wait_process(&server_process, PROCESS_TIMEOUT_MS)) {
        fprintf(stderr, "The local mTLS server did not exit successfully.\n");
        goto cleanup;
    }
    stop_process(&server_process);
    server_started = 0;

    DeleteFileA(ready_file);
    if (sprintf_s(command_line, sizeof(command_line),
                  "\"%s\" \"%s\" \"%s\" \"%s\" \"%s\" 1 early-hints",
                  argv[4], ca_path, server_cert_path, server_key_path,
                  ready_file)
            < 0
        || !start_process(command_line, &server_process)) {
        fprintf(stderr, "Unable to start the informational test server.\n");
        goto cleanup;
    }
    server_started = 1;
    if (!wait_for_port(ready_file, &port)) {
        fprintf(stderr, "The informational test server was not ready.\n");
        goto cleanup;
    }
    initialize_request(&request, unicode_provider_path, store_uri,
                       unicode_client_cert_path, unicode_ca_path, "localhost",
                       port);
    if (!microsoft_identity_native_request(&request, &response, &bridge_error)
        || response.http_status != 200
        || response.body_length != sizeof("cng mTLS success\n") - 1
        || memcmp(response.body, "cng mTLS success\n",
                  sizeof("cng mTLS success\n") - 1)
               != 0) {
        fprintf(stderr, "The informational response flow failed: %s\n",
                bridge_error.message);
        goto cleanup;
    }
    microsoft_identity_native_response_free(&response);
    if (!wait_process(&server_process, PROCESS_TIMEOUT_MS)) {
        fprintf(stderr, "The informational test server did not exit.\n");
        goto cleanup;
    }
    stop_process(&server_process);
    server_started = 0;

    DeleteFileA(ready_file);
    if (sprintf_s(command_line, sizeof(command_line),
                  "\"%s\" \"%s\" \"%s\" \"%s\" \"%s\" 1 tls-truncate",
                  argv[4], ca_path, server_cert_path, server_key_path,
                  ready_file)
            < 0
        || !start_process(command_line, &server_process)) {
        fprintf(stderr, "Unable to start the TLS truncation test server.\n");
        goto cleanup;
    }
    server_started = 1;
    if (!wait_for_port(ready_file, &port)) {
        fprintf(stderr, "The TLS truncation test server was not ready.\n");
        goto cleanup;
    }
    initialize_request(&request, unicode_provider_path, store_uri,
                       unicode_client_cert_path, unicode_ca_path, "localhost",
                       port);
    if (!microsoft_identity_native_request(&request, &response, &bridge_error)
        || response.http_status != 200
        || response.body_length != sizeof("cng mTLS success\n") - 1
        || memcmp(response.body, "cng mTLS success\n",
                  sizeof("cng mTLS success\n") - 1)
               != 0) {
        fprintf(stderr,
                "A completely framed response without close_notify failed: %s\n",
                bridge_error.message);
        goto cleanup;
    }
    microsoft_identity_native_response_free(&response);
    if (!wait_process(&server_process, PROCESS_TIMEOUT_MS)) {
        fprintf(stderr, "The TLS truncation server did not exit cleanly.\n");
        goto cleanup;
    }
    stop_process(&server_process);
    server_started = 0;

    DeleteFileA(ready_file);
    if (sprintf_s(command_line, sizeof(command_line),
                  "\"%s\" \"%s\" \"%s\" \"%s\" \"%s\" "
                  "1 close-delimited",
                  argv[4], ca_path, server_cert_path, server_key_path,
                  ready_file)
            < 0
        || !start_process(command_line, &server_process)) {
        fprintf(stderr, "Unable to start the close-delimited test server.\n");
        goto cleanup;
    }
    server_started = 1;
    if (!wait_for_port(ready_file, &port)) {
        fprintf(stderr, "The close-delimited test server was not ready.\n");
        goto cleanup;
    }
    initialize_request(&request, unicode_provider_path, store_uri,
                       unicode_client_cert_path, unicode_ca_path, "localhost",
                       port);
    if (microsoft_identity_native_request(&request, &response, &bridge_error)
        || bridge_error.code != MICROSOFT_IDENTITY_NATIVE_ERROR_HTTP) {
        fprintf(stderr,
                "A close-delimited response was not rejected (code %d).\n",
                bridge_error.code);
        goto cleanup;
    }
    if (!wait_process(&server_process, PROCESS_TIMEOUT_MS)) {
        fprintf(stderr, "The close-delimited server did not exit cleanly.\n");
        goto cleanup;
    }
    stop_process(&server_process);
    server_started = 0;

    DeleteFileA(ready_file);
    if (sprintf_s(command_line, sizeof(command_line),
                  "\"%s\" \"%s\" \"%s\" \"%s\" \"%s\" 1 drip",
                  argv[4], ca_path, server_cert_path, server_key_path,
                  ready_file)
            < 0
        || !start_process(command_line, &server_process)) {
        fprintf(stderr, "Unable to start the deadline test server.\n");
        goto cleanup;
    }
    server_started = 1;
    if (!wait_for_port(ready_file, &port)) {
        fprintf(stderr, "The deadline test server was not ready.\n");
        goto cleanup;
    }
    initialize_request(&request, unicode_provider_path, store_uri,
                       unicode_client_cert_path, unicode_ca_path, "localhost",
                       port);
    request.io_timeout_ms = 250;
    if (microsoft_identity_native_request(&request, &response, &bridge_error)
        || bridge_error.code != MICROSOFT_IDENTITY_NATIVE_ERROR_IO
        || bridge_error.system_error != WSAETIMEDOUT) {
        fprintf(stderr,
                "The monotonic total I/O deadline was not enforced "
                "(code %d, system %d).\n",
                bridge_error.code, bridge_error.system_error);
        goto cleanup;
    }
    if (!wait_process(&server_process, PROCESS_TIMEOUT_MS)) {
        fprintf(stderr, "The deadline test server did not exit cleanly.\n");
        goto cleanup;
    }
    stop_process(&server_process);
    server_started = 0;
    printf("STORE, provider ownership, and local mTLS integration passed.\n");
    result = EXIT_SUCCESS;

cleanup:
    microsoft_identity_native_response_free(&response);
    if (server_started) {
        stop_process(&server_process);
    }
    OSSL_PARAM_free(private_params);
    EVP_PKEY_CTX_free(key_check);
    EVP_PKEY_free(public_key);
    EVP_PKEY_free(key_duplicate);
    EVP_PKEY_free(key);
    if (cng_provider != NULL) {
        OSSL_PROVIDER_unload(cng_provider);
    }
    if (default_provider != NULL) {
        OSSL_PROVIDER_unload(default_provider);
    }
    OSSL_LIB_CTX_free(libctx);

    if (owns_key_name) {
        platform_error[0] = '\0';
        if (!cng_delete_named_key(CNG_DEFAULT_PROVIDER_NAME, key_name,
                                  platform_error, sizeof(platform_error))) {
            fprintf(stderr, "CNG test-key deletion failed: %s\n",
                    platform_error);
            result = EXIT_FAILURE;
        } else if (cng_key_open(CNG_DEFAULT_PROVIDER_NAME, key_name,
                                &deleted_key, platform_error,
                                sizeof(platform_error))) {
            fprintf(stderr, "The CNG test key still exists after deletion.\n");
            cng_key_free(deleted_key);
            result = EXIT_FAILURE;
        } else {
            printf("Deleted process-owned CNG test key '%s'.\n", key_name);
        }
    }
    return result;
}
