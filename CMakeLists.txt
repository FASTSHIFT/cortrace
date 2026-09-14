cmake_minimum_required(VERSION 3.16)
project(cortrace VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

list(APPEND CMAKE_MODULE_PATH "${CMAKE_SOURCE_DIR}/cmake")
include(CodeCoverage)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Debug CACHE STRING "Build type" FORCE)
endif()

add_compile_options(-Wall -Wextra -Wpedantic)

# -Werror is opt-in via `-DCORTRACE_WERROR=ON` (or CI sets it). Local dev
# builds don't want it (unfinished code, third-party header noise), but CI
# demands zero warnings so any new regression fails the build.
option(CORTRACE_WERROR "Treat warnings as errors" OFF)
if(CORTRACE_WERROR)
    add_compile_options(-Werror)
endif()

# AddressSanitizer + UBSan: catch memory errors (use-after-free, OOB, leaks) and
# undefined behaviour in the Debug build and in CI. Opt-in via -DCORTRACE_ASAN=ON
# so the coverage build (which must run clean under gcov) is unaffected. ASAN and
# gcov coverage don't mix well, so treat them as mutually exclusive.
option(CORTRACE_ASAN "Build with AddressSanitizer + UBSan" OFF)
if(CORTRACE_ASAN)
    if(ENABLE_COVERAGE)
        message(FATAL_ERROR "CORTRACE_ASAN and ENABLE_COVERAGE are mutually exclusive")
    endif()
    message(STATUS "Sanitizers: AddressSanitizer + UBSan enabled")
    add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer -g)
    add_link_options(-fsanitize=address,undefined)
endif()

enable_coverage_if_requested()

# ---- core library ---------------------------------------------------------
# The decoder-facing core: symbol table + call-stack machine. Deliberately has
# NO OpenCSD dependency so it is fully unit-testable with synthetic elements.
#
# Sources are auto-collected: drop a new .cpp in src/ and it is picked up on the
# next build (CONFIGURE_DEPENDS re-runs CMake when the set changes). The OpenCSD
# adapter is excluded here and added conditionally below (it needs libopencsd).
file(GLOB CORTRACE_CORE_SRC CONFIGURE_DEPENDS ${CMAKE_SOURCE_DIR}/src/*.cpp)
list(REMOVE_ITEM CORTRACE_CORE_SRC ${CMAKE_SOURCE_DIR}/src/opencsd_decoder.cpp)
add_library(cortrace_core ${CORTRACE_CORE_SRC})
target_include_directories(cortrace_core PUBLIC ${CMAKE_SOURCE_DIR}/include)

# ---- OpenCSD-backed decoder adapter ---------------------------------------
# Preferred: build OpenCSD FROM SOURCE as a vendored submodule
# (third_party/opencsd, pinned v1.8.3) so the whole decode chain is one
# debuggable build -- we can gdb-step the ETMv4 packet processor/decoder and
# read its state machine instead of treating libopencsd as a black box.
# Fallback: system-installed libopencsd (find_library) if the submodule is
# absent (e.g. a checkout without `git submodule update --init`).
set(CORTRACE_HAVE_OPENCSD OFF)
set(OPENCSD_SUBMODULE ${CMAKE_SOURCE_DIR}/third_party/opencsd)
option(CORTRACE_OPENCSD_FROM_SOURCE "Build vendored OpenCSD submodule from source" ON)

if(CORTRACE_OPENCSD_FROM_SOURCE AND EXISTS ${OPENCSD_SUBMODULE}/CMakeLists.txt)
    message(STATUS "OpenCSD: building vendored submodule from source (debuggable)")
    # Keep OpenCSD's own dev/test targets off; we only need the libraries.
    set(OPENCSD_BUILD_DEV_TARGETS OFF CACHE BOOL "" FORCE)
    set(OPENCSD_FULL_PROJECT_LAYOUT OFF CACHE BOOL "" FORCE)
    add_subdirectory(${OPENCSD_SUBMODULE} ${CMAKE_BINARY_DIR}/opencsd EXCLUDE_FROM_ALL)
    if(EXISTS ${CMAKE_SOURCE_DIR}/src/opencsd_decoder.cpp)
        target_sources(cortrace_core PRIVATE src/opencsd_decoder.cpp)
        target_link_libraries(cortrace_core PUBLIC OpenCSD::opencsd_c_api OpenCSD::opencsd)
        target_compile_definitions(cortrace_core PUBLIC CORTRACE_HAVE_OPENCSD=1)
        set(CORTRACE_HAVE_OPENCSD ON)
    endif()
else()
    find_library(OPENCSD_LIB opencsd)
    find_library(OPENCSD_C_API_LIB opencsd_c_api)
    find_path(OPENCSD_INCLUDE_DIR opencsd/c_api/opencsd_c_api.h)
    if(OPENCSD_LIB AND OPENCSD_C_API_LIB AND OPENCSD_INCLUDE_DIR)
        message(STATUS "OpenCSD: using system-installed libopencsd (no source submodule)")
        if(EXISTS ${CMAKE_SOURCE_DIR}/src/opencsd_decoder.cpp)
            target_sources(cortrace_core PRIVATE src/opencsd_decoder.cpp)
            target_include_directories(cortrace_core PUBLIC ${OPENCSD_INCLUDE_DIR})
            target_link_libraries(cortrace_core PUBLIC ${OPENCSD_C_API_LIB} ${OPENCSD_LIB})
            target_compile_definitions(cortrace_core PUBLIC CORTRACE_HAVE_OPENCSD=1)
            set(CORTRACE_HAVE_OPENCSD ON)
        endif()
    else()
        message(STATUS "OpenCSD not found: core-only build (decoder adapter skipped)")
    endif()
endif()

# ---- CLI front end (needs the OpenCSD-backed decoder) ---------------------
# Auto-collect every CLI in tools/ (one executable per .cpp).
# Command-line parsing for the CLIs: p-ranav/argparse, a single-header MIT
# library vendored as a submodule (third_party/argparse, pinned v3.2). Header
# only, so it is just an include directory — no build/link.
set(ARGPARSE_INCLUDE ${CMAKE_SOURCE_DIR}/third_party/argparse/include)

if(CORTRACE_HAVE_OPENCSD)
    file(GLOB CORTRACE_TOOLS CONFIGURE_DEPENDS ${CMAKE_SOURCE_DIR}/tools/*.cpp)
    foreach(tool_src ${CORTRACE_TOOLS})
        get_filename_component(tool_name ${tool_src} NAME_WE)
        string(REPLACE "_" "-" tool_name ${tool_name})
        add_executable(${tool_name} ${tool_src})
        target_link_libraries(${tool_name} PRIVATE cortrace_core)
        target_include_directories(${tool_name} PRIVATE ${ARGPARSE_INCLUDE})
    endforeach()
endif()

# ---- tests ----------------------------------------------------------------
# Auto-collect every test_*.cpp in tests/ (test_main.cpp provides main()).
enable_testing()
file(GLOB CORTRACE_TEST_SRC CONFIGURE_DEPENDS ${CMAKE_SOURCE_DIR}/tests/*.cpp)
add_executable(cortrace_tests ${CORTRACE_TEST_SRC})
target_link_libraries(cortrace_tests PRIVATE cortrace_core)
target_include_directories(cortrace_tests PRIVATE ${CMAKE_SOURCE_DIR}/tests)
add_test(NAME unit COMMAND cortrace_tests)

# ---- coverage target with a minimum line-coverage gate --------------------
set(CORTRACE_MIN_COVERAGE 80 CACHE STRING "Minimum line coverage percent for the gate")
add_coverage_target(cortrace_tests ${CORTRACE_MIN_COVERAGE})
