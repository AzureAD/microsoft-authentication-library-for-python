#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>

#include <openssl/err.h>
#include <openssl/provider.h>
#include <openssl/ssl.h>

#define SERVER_REQUEST_LIMIT 65536U
#define SERVER_ACCEPT_TIMEOUT_SECONDS 30L

static const char response_text[] =
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: text/plain\r\n"
    "Content-Length: 17\r\n"
    "Connection: close\r\n"
    "\r\n"
    "cng mTLS success\n";

static const char close_delimited_response[] =
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: text/plain\r\n"
    "Connection: close\r\n"
    "\r\n"
    "partial";

static const char early_hints_response[] =
    "HTTP/1.1 103 Early Hints\r\n"
    "Content-Length: 999\r\n"
    "\r\n";

static void print_errors(const char *operation)
{
    fprintf(stderr, "%s failed.\n", operation);
    ERR_print_errors_fp(stderr);
}

static const unsigned char *find_bytes(const unsigned char *buffer,
                                       size_t length,
                                       const char *needle,
                                       size_t needle_length)
{
    size_t index;

    for (index = 0; index + needle_length <= length; ++index) {
        if (memcmp(buffer + index, needle, needle_length) == 0) {
            return buffer + index;
        }
    }
    return NULL;
}

static int read_request(SSL *ssl, int require_token_bound_header,
                        const char *mode)
{
    unsigned char buffer[SERVER_REQUEST_LIMIT];
    size_t length = 0;
    const unsigned char *header_end = NULL;

    while (length < sizeof(buffer)) {
        size_t received = 0;
        if (SSL_read_ex(ssl, buffer + length, sizeof(buffer) - length,
                        &received)
            != 1) {
            return 0;
        }
        length += received;
        header_end = find_bytes(buffer, length, "\r\n\r\n", 4);
        if (header_end != NULL) {
            break;
        }
    }
    if (header_end == NULL) {
        return 0;
    }
    if (strcmp(mode, "token-pop") == 0 || strcmp(mode, "token-bearer") == 0) {
        char expected_body[512];
        char expected_length[64];
        size_t body_length;
        size_t header_length = (size_t)(header_end - buffer) + 4;
        static const char target[] = "POST /tenant/oauth2/v2.0/token HTTP/1.1\r\n";

        if (sprintf_s(expected_body, sizeof(expected_body),
                      "client_id=canonical-client&grant_type=client_credentials&"
                      "scope=https%%3A%%2F%%2Fvault.azure.net%%2F.default&token_type=%s",
                      strcmp(mode, "token-pop") == 0 ? "mtls_pop" : "bearer") < 0) {
            return 0;
        }
        body_length = strlen(expected_body);
        if (sprintf_s(expected_length, sizeof(expected_length),
                      "\r\nContent-Length: %zu\r\n", body_length) < 0
            || length < sizeof(target) - 1
            || memcmp(buffer, target, sizeof(target) - 1) != 0
            || find_bytes(buffer, header_length, expected_length,
                          strlen(expected_length)) == NULL
            || find_bytes(buffer, header_length,
                          "\r\nContent-Type: application/x-www-form-urlencoded\r\n",
                          sizeof("\r\nContent-Type: application/x-www-form-urlencoded\r\n") - 1) == NULL
            || find_bytes(buffer, header_length,
                          "\r\nclient-request-id: token-correlation\r\n",
                          sizeof("\r\nclient-request-id: token-correlation\r\n") - 1) == NULL
            || find_bytes(buffer, header_length, "Authorization:", 14) != NULL) {
            return 0;
        }
        while (length < header_length + body_length) {
            size_t received = 0;
            if (SSL_read_ex(ssl, buffer + length, sizeof(buffer) - length,
                            &received) != 1 || received == 0) {
                return 0;
            }
            length += received;
        }
        return length == header_length + body_length
               && memcmp(buffer + header_length, expected_body, body_length) == 0;
    }
    if (length < sizeof("POST /mtls HTTP/1.1\r\n") - 1
        || memcmp(buffer, "POST /mtls HTTP/1.1\r\n",
                  sizeof("POST /mtls HTTP/1.1\r\n") - 1)
               != 0) {
        return 0;
    }
    if (strcmp(mode, "success") == 0
        && find_bytes(buffer, (size_t)(header_end - buffer) + 2,
                      "\r\nx-ms-client-request-id: resource-correlation\r\n",
                      sizeof("\r\nx-ms-client-request-id: resource-correlation\r\n") - 1) == NULL) {
        return 0;
    }
    return !require_token_bound_header
           || find_bytes(buffer, (size_t)(header_end - buffer) + 2,
                         "x-ms-tokenboundauth: true\r\n",
                         sizeof("x-ms-tokenboundauth: true\r\n") - 1) != NULL;
}

static int request_client_certificate(SSL *ssl, SOCKET client)
{
    u_long nonblocking = 1;
    ULONGLONG deadline = GetTickCount64() + 10000;
    int hello_sent = 0;
    int result = 0;

    if (!SSL_get_secure_renegotiation_support(ssl)
        || ioctlsocket(client, FIONBIO, &nonblocking) == SOCKET_ERROR) {
        return 0;
    }
    SSL_set_verify(ssl, SSL_VERIFY_PEER | SSL_VERIFY_FAIL_IF_NO_PEER_CERT, NULL);
    SSL_set_options(ssl, SSL_OP_NO_SESSION_RESUMPTION_ON_RENEGOTIATION);
    if (SSL_renegotiate(ssl) != 1) {
        goto cleanup;
    }
    while (GetTickCount64() < deadline) {
        unsigned char byte;
        size_t received = 0;
        int operation_result;
        int error;
        fd_set read_set, write_set;
        struct timeval timeout = { 0, 100000 };

        if (!hello_sent) {
            operation_result = SSL_do_handshake(ssl);
            if (operation_result == 1) {
                hello_sent = 1;
                continue;
            }
            error = SSL_get_error(ssl, operation_result);
        } else {
            operation_result = SSL_peek_ex(ssl, &byte, 1, &received);
            error = SSL_get_error(ssl, operation_result);
            if (SSL_total_renegotiations(ssl) == 1
                && !SSL_renegotiate_pending(ssl)) {
                X509 *peer = SSL_get1_peer_certificate(ssl);
                result = peer != NULL && SSL_get_verify_result(ssl) == X509_V_OK;
                X509_free(peer);
                break;
            }
        }
        if (error != SSL_ERROR_WANT_READ && error != SSL_ERROR_WANT_WRITE) {
            break;
        }
        FD_ZERO(&read_set);
        FD_ZERO(&write_set);
        if (error == SSL_ERROR_WANT_READ) {
            FD_SET(client, &read_set);
        } else {
            FD_SET(client, &write_set);
        }
        if (select(0, &read_set, &write_set, NULL, &timeout) == SOCKET_ERROR) {
            break;
        }
    }
cleanup:
    nonblocking = 0;
    if (ioctlsocket(client, FIONBIO, &nonblocking) == SOCKET_ERROR) {
        return 0;
    }
    return result;
}

static int write_all(SSL *ssl, const char *data, size_t length)
{
    while (length != 0) {
        size_t written = 0;
        if (SSL_write_ex(ssl, data, length, &written) != 1 || written == 0) {
            return 0;
        }
        data += written;
        length -= written;
    }
    return 1;
}

static void write_drip_response(SSL *ssl)
{
    static const char *parts[] = {
        "HTTP/1.1 200 OK\r\n",
        "Content-Type: text/plain\r\nContent-Length: 5\r\n\r\n",
        "hello"
    };
    size_t index;

    for (index = 0; index < sizeof(parts) / sizeof(parts[0]); ++index) {
        if (!write_all(ssl, parts[index], strlen(parts[index]))) {
            break;
        }
        if (index + 1 < sizeof(parts) / sizeof(parts[0])) {
            Sleep(175);
        }
    }
}

static int write_ready_file(const char *path, unsigned short port)
{
    FILE *file = NULL;
    int result = 0;

    if (fopen_s(&file, path, "wb") == 0 && file != NULL) {
        result = fprintf(file, "%u\n", (unsigned int)port) > 0
                 && fclose(file) == 0;
        file = NULL;
    }
    if (file != NULL) {
        fclose(file);
    }
    return result;
}

int main(int argc, char **argv)
{
    WSADATA winsock;
    OSSL_LIB_CTX *libctx = NULL;
    OSSL_PROVIDER *default_provider = NULL;
    SSL_CTX *ssl_ctx = NULL;
    SOCKET listener = INVALID_SOCKET;
    struct sockaddr_in address;
    int address_size = sizeof(address);
    int max_connections;
    const char *mode;
    int connection_index;
    int successful_requests = 0;
    int result = EXIT_FAILURE;
    int delayed_certificate;
    int delayed_deadline;

    if (argc != 7) {
        fprintf(stderr,
                "Usage: %s <ca.pem> <server-cert.pem> <server-key.pem> "
                "<ready-file> <max-connections> "
                "<success|token-pop|token-bearer|early-hints|close-delimited|tls-truncate|drip|delayed-cert|delayed-cert-drip>\n",
                argv[0]);
        return EXIT_FAILURE;
    }
    max_connections = atoi(argv[5]);
    mode = argv[6];
    if (max_connections <= 0 || max_connections > 16) {
        fprintf(stderr, "max-connections must be between 1 and 16.\n");
        return EXIT_FAILURE;
    }
    if (strcmp(mode, "success") != 0
        && strcmp(mode, "token-pop") != 0
        && strcmp(mode, "token-bearer") != 0
        && strcmp(mode, "early-hints") != 0
        && strcmp(mode, "close-delimited") != 0
        && strcmp(mode, "tls-truncate") != 0
        && strcmp(mode, "drip") != 0
        && strcmp(mode, "delayed-cert") != 0
        && strcmp(mode, "delayed-cert-drip") != 0) {
        fprintf(stderr, "The response mode is invalid.\n");
        return EXIT_FAILURE;
    }
    delayed_deadline = strcmp(mode, "delayed-cert-drip") == 0;
    delayed_certificate = strcmp(mode, "delayed-cert") == 0 || delayed_deadline;
    if (WSAStartup(MAKEWORD(2, 2), &winsock) != 0) {
        fprintf(stderr, "WSAStartup failed.\n");
        return EXIT_FAILURE;
    }

    libctx = OSSL_LIB_CTX_new();
    default_provider = OSSL_PROVIDER_load(libctx, "default");
    ssl_ctx = SSL_CTX_new_ex(libctx, "provider=default",
                             TLS_server_method());
    if (libctx == NULL || default_provider == NULL || ssl_ctx == NULL
        || !SSL_CTX_set_min_proto_version(ssl_ctx, TLS1_2_VERSION)
        || (delayed_certificate
            && !SSL_CTX_set_max_proto_version(ssl_ctx, TLS1_2_VERSION))
        || SSL_CTX_use_certificate_chain_file(ssl_ctx, argv[2]) != 1
        || SSL_CTX_use_PrivateKey_file(ssl_ctx, argv[3],
                                       SSL_FILETYPE_PEM)
               != 1
        || SSL_CTX_check_private_key(ssl_ctx) != 1
        || !SSL_CTX_load_verify_locations(ssl_ctx, argv[1], NULL)) {
        print_errors("Configuring the local TLS server");
        goto cleanup;
    }
    SSL_CTX_set_verify(ssl_ctx,
                       delayed_certificate ? SSL_VERIFY_NONE
                                           : SSL_VERIFY_PEER | SSL_VERIFY_FAIL_IF_NO_PEER_CERT,
                       NULL);
    SSL_CTX_set_verify_depth(ssl_ctx, 4);

    listener = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    SecureZeroMemory(&address, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = 0;
    if (listener == INVALID_SOCKET
        || bind(listener, (struct sockaddr *)&address, sizeof(address))
               == SOCKET_ERROR
        || listen(listener, 4) == SOCKET_ERROR
        || getsockname(listener, (struct sockaddr *)&address,
                       &address_size)
               == SOCKET_ERROR
        || !write_ready_file(argv[4], ntohs(address.sin_port))) {
        fprintf(stderr, "Unable to start the loopback test server: %d.\n",
                WSAGetLastError());
        goto cleanup;
    }

    for (connection_index = 0; connection_index < max_connections;
         ++connection_index) {
        fd_set read_set;
        struct timeval timeout;
        SOCKET client;
        SSL *ssl;
        X509 *peer_certificate = NULL;
        int skip_shutdown = 0;

        FD_ZERO(&read_set);
        FD_SET(listener, &read_set);
        timeout.tv_sec = SERVER_ACCEPT_TIMEOUT_SECONDS;
        timeout.tv_usec = 0;
        if (select(0, &read_set, NULL, NULL, &timeout) <= 0) {
            fprintf(stderr, "Timed out waiting for a test connection.\n");
            goto cleanup;
        }
        client = accept(listener, NULL, NULL);
        if (client == INVALID_SOCKET) {
            fprintf(stderr, "accept failed: %d.\n", WSAGetLastError());
            goto cleanup;
        }

        ssl = SSL_new(ssl_ctx);
        if (ssl != NULL && SSL_set_fd(ssl, (int)client) == 1
            && SSL_accept(ssl) == 1
            && SSL_get_verify_result(ssl) == X509_V_OK
            && ((peer_certificate = SSL_get1_peer_certificate(ssl)) != NULL
                || delayed_certificate)
            && (!delayed_certificate || peer_certificate == NULL)
            && read_request(ssl, delayed_certificate, mode)) {
            if (delayed_certificate) {
                if (delayed_deadline) {
                    Sleep(600);
                }
                if (request_client_certificate(ssl, client)) {
                    if (delayed_deadline) {
                        ++successful_requests;
                        Sleep(600);
                        skip_shutdown = 1;
                        (void)write_all(ssl, response_text, sizeof(response_text) - 1);
                    } else if (write_all(ssl, response_text, sizeof(response_text) - 1)) {
                        ++successful_requests;
                        printf("Verified client certificate after secure TLS renegotiation.\n");
                    }
                } else {
                    skip_shutdown = 1;
                    print_errors("Delayed client certificate authentication");
                }
            } else if (strcmp(mode, "token-pop") == 0
                       || strcmp(mode, "token-bearer") == 0) {
                char body[256];
                char response[512];
                if (sprintf_s(body, sizeof(body),
                              "{\"access_token\":\"synthetic-token\","
                              "\"token_type\":\"%s\",\"expires_in\":3600}",
                              strcmp(mode, "token-pop") == 0 ? "mtls_pop" : "bearer") >= 0
                    && sprintf_s(response, sizeof(response),
                                 "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                 "Content-Length: %zu\r\nConnection: close\r\n\r\n%s",
                                 strlen(body), body) >= 0
                    && write_all(ssl, response, strlen(response))) {
                    ++successful_requests;
                    printf("Verified client certificate and bounded token form POST.\n");
                }
            } else if (strcmp(mode, "success") == 0) {
                if (write_all(ssl, response_text,
                              sizeof(response_text) - 1)) {
                    ++successful_requests;
                }
            } else if (strcmp(mode, "early-hints") == 0) {
                if (write_all(ssl, early_hints_response,
                              sizeof(early_hints_response) - 1)
                    && write_all(ssl, response_text,
                                 sizeof(response_text) - 1)) {
                    ++successful_requests;
                }
            } else if (strcmp(mode, "close-delimited") == 0) {
                if (write_all(ssl, close_delimited_response,
                              sizeof(close_delimited_response) - 1)) {
                    ++successful_requests;
                }
                skip_shutdown = 1;
            } else if (strcmp(mode, "tls-truncate") == 0) {
                if (write_all(ssl, response_text,
                              sizeof(response_text) - 1)) {
                    ++successful_requests;
                }
                skip_shutdown = 1;
            } else {
                ++successful_requests;
                write_drip_response(ssl);
            }
        } else {
            ERR_clear_error();
        }
        if (ssl != NULL && !skip_shutdown) {
            (void)SSL_shutdown(ssl);
        }
        X509_free(peer_certificate);
        SSL_free(ssl);
        closesocket(client);
    }

    if (successful_requests != 1) {
        fprintf(stderr, "Expected one successful request, received %d.\n",
                successful_requests);
        goto cleanup;
    }
    result = EXIT_SUCCESS;

cleanup:
    if (listener != INVALID_SOCKET) {
        closesocket(listener);
    }
    SSL_CTX_free(ssl_ctx);
    if (default_provider != NULL) {
        OSSL_PROVIDER_unload(default_provider);
    }
    OSSL_LIB_CTX_free(libctx);
    WSACleanup();
    return result;
}
