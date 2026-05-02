cmake_minimum_required(VERSION 3.20)
project(orca_headless CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Disable everything from OrcaSlicer's CMake project that we don't need.
# This must happen BEFORE add_subdirectory.
set(SLIC3R_GUI OFF CACHE BOOL "" FORCE)
set(SLIC3R_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(SLIC3R_PCH OFF CACHE BOOL "" FORCE)
set(SLIC3R_STATIC ON CACHE BOOL "" FORCE)
set(BUILD_TESTING OFF CACHE BOOL "" FORCE)
set(ORCA_TOOLS OFF CACHE BOOL "" FORCE)
set(BBL_RELEASE_TO_PUBLIC OFF CACHE BOOL "" FORCE)

# Pull in OrcaSlicer's full CMake build with GUI / extra tools disabled.
# This builds:
#   - deps_src/ vendored libraries (admesh, libigl, libnest2d, mcut, …)
#   - src/libslic3r — our actual link target
# It does NOT build:
#   - src/slic3r/ (GUI), src/libvgcode/ (GUI), src/dev-utils/ tools
add_subdirectory(${CMAKE_SOURCE_DIR}/../vendor/OrcaSlicer
                 ${CMAKE_BINARY_DIR}/orcaslicer-build)

add_executable(orca-headless
    src/orca_headless.cpp
)

target_include_directories(orca-headless PRIVATE
    ${CMAKE_SOURCE_DIR}/../vendor/OrcaSlicer/src
    ${CMAKE_BINARY_DIR}/orcaslicer-build/src/libslic3r
)

target_link_libraries(orca-headless PRIVATE libslic3r)

install(TARGETS orca-headless RUNTIME DESTINATION bin)
