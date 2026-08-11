cmake_minimum_required(VERSION 3.20)

project(helm_apx_tools VERSION 0.1.0 LANGUAGES C)

include(GNUInstallDirs)
find_package(PkgConfig REQUIRED)
pkg_check_modules(LIBUSB REQUIRED IMPORTED_TARGET libusb-1.0)

add_library(apx_transport STATIC
    src/apx_transport.c
)

target_compile_features(apx_transport PUBLIC c_std_11)
target_include_directories(apx_transport
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
        $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>
)
target_link_libraries(apx_transport PUBLIC PkgConfig::LIBUSB)

add_executable(apxctl
    src/apxctl.c
)
target_compile_features(apxctl PRIVATE c_std_11)
target_link_libraries(apxctl PRIVATE apx_transport)

if(CMAKE_C_COMPILER_ID MATCHES "AppleClang|Clang|GNU")
    target_compile_options(apx_transport PRIVATE -Wall -Wextra -Wpedantic -Werror)
    target_compile_options(apxctl PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()

install(TARGETS apxctl apx_transport
    RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
    ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
)
install(FILES include/apx_transport.h DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})

enable_testing()
add_test(NAME apxctl_help COMMAND apxctl help)
