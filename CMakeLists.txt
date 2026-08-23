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
#
# Sources are auto-collected: drop a new .cpp in src/ and it is picked up on the
# next build (CONFIGURE_DEPENDS re-runs CMake when the set changes). The OpenCSD
# adapter is excluded here and added conditionally below (it needs libopencsd).
file(GLOB CORTRACE_CORE_SRC CONFIGURE_DEPENDS ${CMAKE_SOURCE_DIR}/src/*.cpp)
list(REMOVE_ITEM CORTRACE_CORE_SRC ${CMAKE_SOURCE_DIR}/src/opencsd_decoder.cpp)
add_library(cortrace_core ${CORTRACE_CORE_SRC})
target_include_directories(cortrace_core PUBLIC ${CMAKE_SOURCE_DIR}/include)

# ---- optional OpenCSD-backed decoder adapter ------------------------------
# Built only when libopencsd is present, so the core + tests build everywhere.
find_library(OPENCSD_LIB opencsd)
find_library(OPENCSD_C_API_LIB opencsd_c_api)
find_path(OPENCSD_INCLUDE_DIR opencsd/c_api/opencsd_c_api.h)
set(CORTRACE_HAVE_OPENCSD OFF)
if(OPENCSD_LIB AND OPENCSD_C_API_LIB AND OPENCSD_INCLUDE_DIR)
    message(STATUS "OpenCSD found: building decoder adapter")
    if(EXISTS ${CMAKE_SOURCE_DIR}/src/opencsd_decoder.cpp)
        target_sources(cortrace_core PRIVATE src/opencsd_decoder.cpp)
        # PUBLIC so tools linking cortrace_core see the OpenCSD headers/macro.
        target_include_directories(cortrace_core PUBLIC ${OPENCSD_INCLUDE_DIR})
        target_link_libraries(cortrace_core PUBLIC ${OPENCSD_C_API_LIB} ${OPENCSD_LIB})
        target_compile_definitions(cortrace_core PUBLIC CORTRACE_HAVE_OPENCSD=1)
        set(CORTRACE_HAVE_OPENCSD ON)
    endif()
else()
    message(STATUS "OpenCSD not found: core-only build (decoder adapter skipped)")
endif()

# ---- CLI front end (needs the OpenCSD-backed decoder) ---------------------
# Auto-collect every CLI in tools/ (one executable per .cpp).
if(CORTRACE_HAVE_OPENCSD)
    file(GLOB CORTRACE_TOOLS CONFIGURE_DEPENDS ${CMAKE_SOURCE_DIR}/tools/*.cpp)
    foreach(tool_src ${CORTRACE_TOOLS})
        get_filename_component(tool_name ${tool_src} NAME_WE)
        string(REPLACE "_" "-" tool_name ${tool_name})
        add_executable(${tool_name} ${tool_src})
        target_link_libraries(${tool_name} PRIVATE cortrace_core)
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
