#include "microsoft_identity_native_primitives.h"

#include <windows.h>

#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv) {
    MICROSOFT_IDENTITY_NATIVE_HEADER headers[3];
    MICROSOFT_IDENTITY_NATIVE_REQUEST request = {0};
    MICROSOFT_IDENTITY_NATIVE_RESPONSE response = {0};
    MICROSOFT_IDENTITY_NATIVE_ERROR error = {0};
    char authorization[MICROSOFT_IDENTITY_NATIVE_MAX_HEADER_VALUE_BYTES + 1U];
    const char *token = getenv("NATIVE_PRIMITIVES_MTLS_POP_TOKEN");
    char *port_end = NULL;
    unsigned long port;
    uint32_t actual_abi;
    int written;
    int succeeded;

    if (argc != 8 || token == NULL || token[0] == '\0') {
        fprintf(stderr,
                "Usage: %s <provider.dll> <store-uri> <client.pem> <ca.pem> "
                "<host> <port> <target>\n"
                "Set NATIVE_PRIMITIVES_MTLS_POP_TOKEN to an already-acquired "
                "token.\n",
                argv[0]);
        return 2;
    }

    actual_abi = microsoft_identity_native_abi_version();
    if (actual_abi != MICROSOFT_IDENTITY_NATIVE_ABI_VERSION) {
        fprintf(stderr,
                "Unsupported Native Primitives ABI: expected %u, received %u.\n",
                MICROSOFT_IDENTITY_NATIVE_ABI_VERSION, actual_abi);
        return 1;
    }

    written = snprintf(authorization, sizeof(authorization), "mtls_pop %s", token);
    if (written < 0 || (size_t)written >= sizeof(authorization)) {
        fprintf(stderr, "The supplied token is too large.\n");
        return 2;
    }
    port = strtoul(argv[6], &port_end, 10);
    if (port_end == argv[6] || *port_end != '\0' || port == 0 || port > 65535) {
        SecureZeroMemory(authorization, sizeof(authorization));
        fprintf(stderr, "The port must be an integer from 1 through 65535.\n");
        return 2;
    }

    headers[0].name = "Authorization";
    headers[0].value = authorization;
    headers[1].name = "x-ms-tokenboundauth";
    headers[1].value = "true";
    headers[2].name = "Accept";
    headers[2].value = "application/json";

    request.struct_size = (uint32_t)sizeof(request);
    request.abi_version = MICROSOFT_IDENTITY_NATIVE_ABI_VERSION;
    request.provider_module_path = argv[1];
    request.store_uri = argv[2];
    request.client_certificate_path = argv[3];
    request.ca_bundle_path = argv[4];
    request.host = argv[5];
    request.port = (uint16_t)port;
    request.method = "GET";
    request.target = argv[7];
    request.headers = headers;
    request.header_count = 3;

    succeeded = microsoft_identity_native_request(&request, &response, &error);
    SecureZeroMemory(authorization, sizeof(authorization));
    if (!succeeded) {
        fprintf(stderr, "Native Primitives error %d: %s\n",
                (int)error.code, error.message);
        return 1;
    }

    printf("HTTP %d\n", response.http_status);
    if (response.body_length != 0) {
        fwrite(response.body, 1, response.body_length, stdout);
        fputc('\n', stdout);
    }
    microsoft_identity_native_response_free(&response);
    return 0;
}
