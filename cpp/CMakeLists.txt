cmake_minimum_required(VERSION 3.20)
project(orca_headless CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Pull libslic3r from the vendored OrcaSlicer source. We only build the
# library targets we depend on, not the GUI or upstream CLI.
set(SLIC3R_GUI OFF CACHE BOOL "" FORCE)
set(SLIC3R_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(BBL_RELEASE_TO_PUBLIC OFF CACHE BOOL "" FORCE)

add_subdirectory(${CMAKE_SOURCE_DIR}/../vendor/OrcaSlicer/src/libslic3r
                 ${CMAKE_BINARY_DIR}/libslic3r-build)

add_executable(orca-headless
    src/orca_headless.cpp
)

target_include_directories(orca-headless PRIVATE
    ${CMAKE_SOURCE_DIR}/../vendor/OrcaSlicer/src
    ${CMAKE_BINARY_DIR}/libslic3r-build
)

target_link_libraries(orca-headless PRIVATE libslic3r)

install(TARGETS orca-headless RUNTIME DESTINATION bin)
