#ifndef CNG_PROVIDER_PARAMS_H
#define CNG_PROVIDER_PARAMS_H

#define CNG_PARAM_KEY_NAME "cng-key-name"
#define CNG_PARAM_PROVIDER_NAME "cng-provider-name"

#define CNG_DEFAULT_PROVIDER_NAME "Microsoft Software Key Storage Provider"
#define CNG_DEFAULT_KEY_NAME "openssl-cng-provider-poc"

#define CNG_STORE_SCHEME "cng"
#define CNG_STORE_URI_PREFIX "cng://?"
#define CNG_MAX_KEY_NAME 512
#define CNG_MAX_PROVIDER_NAME 512
#define CNG_MAX_STORE_URI 2048

#endif
