# Unit test for cmake/VersionUtil.cmake:  cmake -P cmake/tests/version_test.cmake
get_filename_component(_here "${CMAKE_CURRENT_LIST_DIR}" ABSOLUTE)
include(${_here}/../VersionUtil.cmake)

set(_failures 0)
macro(check_eq actual expected what)
    if(NOT "${actual}" STREQUAL "${expected}")
        message(SEND_ERROR "${what}: got '${actual}', expected '${expected}'")
        math(EXPR _failures "${_failures} + 1")
    endif()
endmacro()

# --- valid versions and their Debian form -----------------------------------
foreach(case
        "1.0.0|1.0.0"
        "1.0.0a1|1.0.0~a1"
        "1.0.0b2|1.0.0~b2"
        "1.0.0rc1|1.0.0~rc1"
        "1.0.0.dev3|1.0.0~~dev3"
        "1.0.0a1.dev2|1.0.0~a1~dev2"
        "1.0.0.post1|1.0.0+post1"
        "1.0.0.post1.dev2|1.0.0+post1~dev2"
        "1.2.10|1.2.10"
        "1.0.0+git5.abc1234|1.0.0+git5.abc1234"
        "1.0.0a1+git5.abc1234|1.0.0~a1+git5.abc1234"
        # a hash that looks like a pre-release tag must survive untouched
        "1.0.0+git66.9b4cafe|1.0.0+git66.9b4cafe"
        "1.0.0rc2+git1.1a2b3c4|1.0.0~rc2+git1.1a2b3c4")
    string(REPLACE "|" ";" parts "${case}")
    list(GET parts 0 version)
    list(GET parts 1 expected)
    cortrace_version_valid("${version}" ok)
    check_eq("${ok}" "TRUE" "valid(${version})")
    cortrace_deb_version("${version}" deb)
    check_eq("${deb}" "${expected}" "deb(${version})")
endforeach()

# --- invalid versions ---------------------------------------------------------
foreach(bad "1.0" "1" "v1.0.0" "1.0.0-rc1" "1.0.0a" "1.0.0.alpha1" "1.0.0 " ""
            "1.0.0+" "1.0.0+git 5" "1.0.0++x" "1.0.0.post" "a1.0.0")
    cortrace_version_valid("${bad}" ok)
    check_eq("${ok}" "FALSE" "valid('${bad}')")
endforeach()

# --- the Debian form must keep PEP 440's ordering under dpkg ------------------
find_program(_dpkg dpkg)
if(_dpkg)
    # each version must sort strictly before the next
    set(_ordered 1.0.0.dev1 1.0.0a1.dev1 1.0.0a1 1.0.0a2 1.0.0b1 1.0.0rc1 1.0.0
                 1.0.0.post1.dev1 1.0.0.post1 1.0.1.dev1 1.0.1)
    set(_debs "")
    foreach(v ${_ordered})
        cortrace_deb_version("${v}" d)
        list(APPEND _debs "${d}")
    endforeach()
    list(LENGTH _debs _n)
    math(EXPR _last "${_n} - 2")
    foreach(i RANGE 0 ${_last})
        math(EXPR j "${i} + 1")
        list(GET _debs ${i} lo)
        list(GET _debs ${j} hi)
        execute_process(COMMAND ${_dpkg} --compare-versions "${lo}" lt "${hi}"
                        RESULT_VARIABLE rc)
        check_eq("${rc}" "0" "dpkg order ${lo} < ${hi}")
    endforeach()
endif()

if(_failures)
    message(FATAL_ERROR "${_failures} version check(s) failed")
endif()
message(STATUS "version helpers OK")
