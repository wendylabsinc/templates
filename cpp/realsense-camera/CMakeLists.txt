cmake_minimum_required(VERSION 3.16)
project({{.APP_ID}} VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Drogon REQUIRED)
find_package(PkgConfig REQUIRED)

pkg_check_modules(REALSENSE2 REQUIRED realsense2)

find_path(TURBOJPEG_INCLUDE_DIR turbojpeg.h)
find_library(TURBOJPEG_LIBRARY turbojpeg)
if(NOT TURBOJPEG_INCLUDE_DIR OR NOT TURBOJPEG_LIBRARY)
    message(FATAL_ERROR "TurboJPEG development files were not found")
endif()

add_executable({{.APP_ID}} main.cpp)

target_include_directories({{.APP_ID}} PRIVATE
    ${REALSENSE2_INCLUDE_DIRS}
    ${TURBOJPEG_INCLUDE_DIR}
)
target_link_directories({{.APP_ID}} PRIVATE ${REALSENSE2_LIBRARY_DIRS})
target_link_libraries({{.APP_ID}} PRIVATE
    Drogon::Drogon
    ${REALSENSE2_LIBRARIES}
    ${TURBOJPEG_LIBRARY}
)
