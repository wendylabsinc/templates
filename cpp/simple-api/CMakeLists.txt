cmake_minimum_required(VERSION 3.16)
project({{.APP_ID}} VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Drogon REQUIRED)

add_executable({{.APP_ID}} main.cpp)

target_link_libraries({{.APP_ID}} PRIVATE Drogon::Drogon)
