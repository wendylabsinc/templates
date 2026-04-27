cmake_minimum_required(VERSION 3.16)
project({{.APP_ID}} VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Drogon REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(GST REQUIRED gstreamer-1.0 gstreamer-app-1.0)
pkg_check_modules(TURBOJPEG REQUIRED libturbojpeg)

# ONNX Runtime is installed under /usr/local by both Dockerfile bases.
find_path(ONNXRUNTIME_INCLUDE_DIR
    NAMES onnxruntime/core/session/onnxruntime_cxx_api.h onnxruntime_cxx_api.h
    PATHS /usr/local/include /usr/include
    PATH_SUFFIXES "" onnxruntime
    REQUIRED)
find_library(ONNXRUNTIME_LIB onnxruntime
    PATHS /usr/local/lib /usr/lib /usr/lib/aarch64-linux-gnu /usr/lib/x86_64-linux-gnu
    REQUIRED)

add_executable({{.APP_ID}} main.cpp)
target_include_directories({{.APP_ID}} PRIVATE
    ${GST_INCLUDE_DIRS}
    ${TURBOJPEG_INCLUDE_DIRS}
    ${ONNXRUNTIME_INCLUDE_DIR}
    ${ONNXRUNTIME_INCLUDE_DIR}/onnxruntime)
target_link_libraries({{.APP_ID}} PRIVATE
    Drogon::Drogon
    ${GST_LIBRARIES}
    ${ONNXRUNTIME_LIB}
    ${TURBOJPEG_LIBRARIES}
    pthread)
