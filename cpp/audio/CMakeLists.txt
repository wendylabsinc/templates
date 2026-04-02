cmake_minimum_required(VERSION 3.16)
project({{.APP_ID}} VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Drogon REQUIRED)
find_package(PkgConfig REQUIRED)

pkg_check_modules(GST REQUIRED gstreamer-1.0)
pkg_check_modules(GST_APP REQUIRED gstreamer-app-1.0)

add_executable({{.APP_ID}} main.cpp)

target_include_directories({{.APP_ID}} PRIVATE
    ${GST_INCLUDE_DIRS}
    ${GST_APP_INCLUDE_DIRS}
)

target_link_libraries({{.APP_ID}} PRIVATE
    Drogon::Drogon
    ${GST_LIBRARIES}
    ${GST_APP_LIBRARIES}
)
