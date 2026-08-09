# Cortrace — code coverage via gcovr with a minimum line-coverage gate.
#
# Usage (from top-level CMakeLists.txt):
#   include(cmake/CodeCoverage.cmake)
#   enable_coverage_if_requested()                 # adds flags when -DENABLE_COVERAGE=ON
#   add_coverage_target(<test-target> <min-percent>)
#
# Produces:
#   - `coverage` target: runs tests, writes coverage.xml + HTML, prints summary,
#     and FAILS if line coverage < the minimum threshold.

option(ENABLE_COVERAGE "Build with coverage instrumentation (gcov)" OFF)

function(enable_coverage_if_requested)
    if(ENABLE_COVERAGE)
        if(NOT CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
            message(FATAL_ERROR "Coverage requires GCC or Clang")
        endif()
        message(STATUS "Coverage instrumentation: ENABLED")
        add_compile_options(--coverage -O0 -g -fprofile-arcs -ftest-coverage)
        add_link_options(--coverage)
    endif()
endfunction()

# add_coverage_target(<test_target> <min_line_percent>)
function(add_coverage_target test_target min_percent)
    if(NOT ENABLE_COVERAGE)
        return()
    endif()
    find_program(GCOVR_EXECUTABLE gcovr)
    if(NOT GCOVR_EXECUTABLE)
        message(WARNING "gcovr not found; `coverage` target unavailable")
        return()
    endif()
    set(_html_dir "${CMAKE_BINARY_DIR}/coverage-html")
    add_custom_target(coverage
        COMMAND ${CMAKE_CTEST_COMMAND} --output-on-failure
        COMMAND ${CMAKE_COMMAND} -E make_directory ${_html_dir}
        COMMAND ${GCOVR_EXECUTABLE}
                --root ${CMAKE_SOURCE_DIR}
                --filter "${CMAKE_SOURCE_DIR}/src/.*"
                --exclude ".*/tests/.*"
                --exclude ".*/tools/.*"
                --xml ${CMAKE_BINARY_DIR}/coverage.xml
                --html-details ${_html_dir}/index.html
                --print-summary
                --fail-under-line ${min_percent}
        WORKING_DIRECTORY ${CMAKE_BINARY_DIR}
        COMMENT "Running tests + gcovr (fail under ${min_percent}% line coverage)"
        VERBATIM)
    add_dependencies(coverage ${test_target})
endfunction()
