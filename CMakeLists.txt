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
enable_coverage_if_requested()

# ---- core library ---------------------------------------------------------
# The decoder-facing core: symbol table + call-stack machine. Deliberately has
# NO OpenCSD dependency so it is fully unit-testable with synthetic elements.
add_library(cortrace_core
    src/symbols.cpp
    src/callstack.cpp)
target_include_directories(cortrace_core PUBLIC ${CMAKE_SOURCE_DIR}/include)

# ---- optional OpenCSD-backed decoder adapter ------------------------------
# Built only when libopencsd is present, so the core + tests build everywhere.
find_library(OPENCSD_LIB opencsd)
find_library(OPENCSD_C_API_LIB opencsd_c_api)
find_path(OPENCSD_INCLUDE_DIR opencsd/c_api/opencsd_c_api.h)
if(OPENCSD_LIB AND OPENCSD_C_API_LIB AND OPENCSD_INCLUDE_DIR)
    message(STATUS "OpenCSD found: building decoder adapter")
    # src/opencsd_decoder.cpp will land here in M1; guard keeps M0 green.
    if(EXISTS ${CMAKE_SOURCE_DIR}/src/opencsd_decoder.cpp)
        target_sources(cortrace_core PRIVATE src/opencsd_decoder.cpp)
        target_include_directories(cortrace_core PRIVATE ${OPENCSD_INCLUDE_DIR})
        target_link_libraries(cortrace_core PRIVATE ${OPENCSD_C_API_LIB} ${OPENCSD_LIB})
        target_compile_definitions(cortrace_core PUBLIC CORTRACE_HAVE_OPENCSD=1)
    endif()
else()
    message(STATUS "OpenCSD not found: core-only build (decoder adapter skipped)")
endif()

# ---- tests ----------------------------------------------------------------
enable_testing()
add_executable(cortrace_tests
    tests/test_main.cpp
    tests/test_symbols.cpp
    tests/test_callstack.cpp)
target_link_libraries(cortrace_tests PRIVATE cortrace_core)
target_include_directories(cortrace_tests PRIVATE ${CMAKE_SOURCE_DIR}/tests)
add_test(NAME unit COMMAND cortrace_tests)

# ---- coverage target with a minimum line-coverage gate --------------------
set(CORTRACE_MIN_COVERAGE 80 CACHE STRING "Minimum line coverage percent for the gate")
add_coverage_target(cortrace_tests ${CORTRACE_MIN_COVERAGE})
