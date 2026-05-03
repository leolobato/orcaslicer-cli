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

# OrcaSlicer's src/CMakeLists.txt unconditionally creates an `OrcaSlicer`
# executable target (the GUI app entry point) — it's outside the
# `if(ORCA_TOOLS)` block. SLIC3R_GUI=OFF prevents wx linking but the target
# still gets compiled by `cmake --build`, which fails on missing wx headers.
# Excluding it from `all` lets us build only orca-headless.
if (TARGET OrcaSlicer)
    set_target_properties(OrcaSlicer PROPERTIES EXCLUDE_FROM_ALL TRUE)
endif()

add_executable(orca-headless
    src/orca_headless.cpp
    src/json_io.cpp
    src/progress.cpp
)

target_include_directories(orca-headless PRIVATE
    ${CMAKE_SOURCE_DIR}/../vendor/OrcaSlicer/src
    ${CMAKE_SOURCE_DIR}/../vendor/OrcaSlicer/deps_src
    # Generated headers (libslic3r_version.h) — configure_file output lives
    # in the libslic3r build dir; we add its parent so `<libslic3r/x.h>`
    # resolves the same way it does inside libslic3r itself.
    ${CMAKE_BINARY_DIR}/orcaslicer-build/src
)

target_link_libraries(orca-headless PRIVATE libslic3r nlohmann_json)

install(TARGETS orca-headless RUNTIME DESTINATION bin)
