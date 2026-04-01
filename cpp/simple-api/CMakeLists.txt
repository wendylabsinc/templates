cmake_minimum_required(VERSION 3.16)
project(simple-api VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Drogon REQUIRED)

add_executable(simple-api main.cpp)

target_link_libraries(simple-api PRIVATE Drogon::Drogon)
