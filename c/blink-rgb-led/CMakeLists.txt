# The following five lines of boilerplate have to be in your project's
# CMakeLists in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.16)

# WAMR (pulled in transitively via wendy_core -> wendy_wasm) declares
# cmake_minimum_required(VERSION 3.5), which CMake 4.x rejects outright.
# Allow it via the compatibility policy, same as wendy-lite itself does.
set(CMAKE_POLICY_VERSION_MINIMUM 3.5)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project({{.APP_ID}})
