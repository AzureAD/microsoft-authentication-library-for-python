cmake_minimum_required(VERSION 3.21)

project(MicrosoftIdentityNativePrimitivesNativeTests VERSION 0.2.0 LANGUAGES C)

if(NOT WIN32)
    message(FATAL_ERROR "The native test harness requires Windows CNG.")
endif()

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

find_package(OpenSSL 3.6 REQUIRED COMPONENTS Crypto SSL)

add_library(rust_native_provider MODULE IMPORTED GLOBAL)
set_target_properties(rust_native_provider PROPERTIES
    IMPORTED_CONFIGURATIONS "DEBUG;RELEASE"
    IMPORTED_LOCATION_DEBUG "${CMAKE_CURRENT_SOURCE_DIR}/target/debug/microsoft_identity_native_provider.dll"
    IMPORTED_LOCATION_RELEASE "${CMAKE_CURRENT_SOURCE_DIR}/target/release/microsoft_identity_native_provider.dll"
)

add_library(rust_native_primitives SHARED IMPORTED GLOBAL)
set_target_properties(rust_native_primitives PROPERTIES
    IMPORTED_CONFIGURATIONS "DEBUG;RELEASE"
    IMPORTED_LOCATION_DEBUG "${CMAKE_CURRENT_SOURCE_DIR}/target/debug/microsoft_identity_native_primitives.dll"
    IMPORTED_IMPLIB_DEBUG "${CMAKE_CURRENT_SOURCE_DIR}/target/debug/microsoft_identity_native_primitives.dll.lib"
    IMPORTED_LOCATION_RELEASE "${CMAKE_CURRENT_SOURCE_DIR}/target/release/microsoft_identity_native_primitives.dll"
    IMPORTED_IMPLIB_RELEASE "${CMAKE_CURRENT_SOURCE_DIR}/target/release/microsoft_identity_native_primitives.dll.lib"
)

add_library(cng_test_support STATIC
    tests/native/support/cng_key.c
)
target_include_directories(cng_test_support
    PUBLIC
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
        "${CMAKE_CURRENT_SOURCE_DIR}/tests/native/support"
)
target_link_libraries(cng_test_support PUBLIC ncrypt bcrypt)
target_compile_definitions(cng_test_support PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)

add_executable(native_provider_test
    tests/native/cng_provider_test.c
)
target_include_directories(native_provider_test
    PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
        "${CMAKE_CURRENT_SOURCE_DIR}/tests/native/support"
)
target_link_libraries(native_provider_test PRIVATE OpenSSL::Crypto cng_test_support)
target_compile_definitions(native_provider_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)

add_executable(native_test_pki
    tests/native/cng_test_pki.c
)
target_include_directories(native_test_pki PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/include")
target_link_libraries(native_test_pki PRIVATE OpenSSL::Crypto)
target_compile_definitions(native_test_pki PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)

add_executable(native_mtls_test_server
    tests/native/cng_mtls_test_server.c
)
target_link_libraries(native_mtls_test_server
    PRIVATE
        OpenSSL::SSL
        OpenSSL::Crypto
        ws2_32
)
target_compile_definitions(native_mtls_test_server PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)

add_executable(native_integration_test
    tests/native/cng_integration_test.c
)
target_include_directories(native_integration_test
    PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/include"
        "${CMAKE_CURRENT_SOURCE_DIR}/tests/native/support"
)
target_link_libraries(native_integration_test
    PRIVATE
        rust_native_primitives
        cng_test_support
        OpenSSL::Crypto
)
target_compile_definitions(native_integration_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)

add_executable(microsoft_identity_native_example
    examples/c/mtls_request.c
)
target_include_directories(microsoft_identity_native_example
    PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/include"
)
target_link_libraries(microsoft_identity_native_example PRIVATE rust_native_primitives)
target_compile_definitions(microsoft_identity_native_example
    PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX
)

if(MSVC)
    foreach(target
            cng_test_support
            native_provider_test
            native_test_pki
            native_mtls_test_server
            native_integration_test
            microsoft_identity_native_example)
        target_compile_options(${target} PRIVATE /W4)
    endforeach()
endif()

include(CTest)
if(BUILD_TESTING)
    add_test(
        NAME native_provider_test
        COMMAND native_provider_test "$<TARGET_FILE:rust_native_provider>"
    )
    add_test(
        NAME native_integration_test
        COMMAND native_integration_test
            "$<TARGET_FILE:rust_native_provider>"
            "$<TARGET_FILE:native_provider_test>"
            "$<TARGET_FILE:native_test_pki>"
            "$<TARGET_FILE:native_mtls_test_server>"
            "$<TARGET_FILE_DIR:native_integration_test>\\test-artifacts"
    )
endif()
