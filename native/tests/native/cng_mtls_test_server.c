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

static int read_request(SSL *ssl)
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
    if (header_end == NULL
        || length < sizeof("POST /mtls HTTP/1.1\r\n") - 1
        || memcmp(buffer, "POST /mtls HTTP/1.1\r\n",
                  sizeof("POST /mtls HTTP/1.1\r\n") - 1)
               != 0) {
        return 0;
    }
    return 1;
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

    if (argc != 7) {
        fprintf(stderr,
                "Usage: %s <ca.pem> <server-cert.pem> <server-key.pem> "
                "<ready-file> <max-connections> "
                "<success|early-hints|close-delimited|tls-truncate|drip>\n",
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
        && strcmp(mode, "early-hints") != 0
        && strcmp(mode, "close-delimited") != 0
        && strcmp(mode, "tls-truncate") != 0
        && strcmp(mode, "drip") != 0) {
        fprintf(stderr, "The response mode is invalid.\n");
        return EXIT_FAILURE;
    }
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
                       SSL_VERIFY_PEER | SSL_VERIFY_FAIL_IF_NO_PEER_CERT,
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
            && (peer_certificate = SSL_get1_peer_certificate(ssl)) != NULL
            && read_request(ssl)) {
            if (strcmp(mode, "success") == 0) {
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
