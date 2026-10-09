# The cortrace version.
#
# Single source of truth: the VERSION file at the repository root (PEP 440, see
# cmake/VersionUtil.cmake): 1.0.0, 1.0.0a1, 1.0.0rc1, 1.0.0.post1 ...
#
# What gets reported (cortrace version, cortrace-decode --version, the .deb):
#   - HEAD is exactly the commit tagged v<VERSION>  ->  <VERSION>        (a release)
#   - any other commit                              ->  <VERSION>+git<N>.<hash>
#       (a development build of that version; N = commits in the history)
# -DCORTRACE_VERSION=<pep440> overrides the whole thing.
#
# Provides: CORTRACE_VERSION (PEP 440, shown to users),
#           CORTRACE_DEB_VERSION (the same, in Debian ordering form),
#           ${CMAKE_BINARY_DIR}/_version.py (read by the Python package).
include(${CMAKE_CURRENT_LIST_DIR}/VersionUtil.cmake)

set(CORTRACE_VERSION "" CACHE STRING "Override the version (PEP 440, e.g. 1.0.0a1)")

set(_ct_version_file ${CMAKE_SOURCE_DIR}/VERSION)
if(NOT EXISTS ${_ct_version_file})
    message(FATAL_ERROR "VERSION file missing: ${_ct_version_file}")
endif()
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS ${_ct_version_file})
file(READ ${_ct_version_file} _ct_base)
string(STRIP "${_ct_base}" _ct_base)
cortrace_version_valid("${_ct_base}" _ct_ok)
if(NOT _ct_ok OR _ct_base MATCHES "\\+")
    message(FATAL_ERROR
        "VERSION '${_ct_base}' is not a release version. Use PEP 440 "
        "MAJOR.MINOR.PATCH with an optional aN/bN/rcN, .postN, .devN suffix "
        "(e.g. 1.0.0, 1.0.0a1, 1.0.0rc1); the +git label is added by the build.")
endif()

if(CORTRACE_VERSION)
    cortrace_version_valid("${CORTRACE_VERSION}" _ct_ok)
    if(NOT _ct_ok)
        message(FATAL_ERROR "-DCORTRACE_VERSION='${CORTRACE_VERSION}' is not a valid PEP 440 version")
    endif()
    set(_ct_full "${CORTRACE_VERSION}")
else()
    set(_ct_full "${_ct_base}+nogit")
    find_package(Git QUIET)
    if(GIT_FOUND)
        execute_process(
            COMMAND ${GIT_EXECUTABLE} describe --exact-match --tags HEAD
            WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
            OUTPUT_VARIABLE _ct_tag
            OUTPUT_STRIP_TRAILING_WHITESPACE
            ERROR_QUIET)
        if(_ct_tag STREQUAL "v${_ct_base}")
            set(_ct_full "${_ct_base}")
        else()
            execute_process(
                COMMAND ${GIT_EXECUTABLE} rev-list --count HEAD
                WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
                OUTPUT_VARIABLE _ct_count
                OUTPUT_STRIP_TRAILING_WHITESPACE
                ERROR_QUIET)
            execute_process(
                COMMAND ${GIT_EXECUTABLE} rev-parse --short HEAD
                WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
                OUTPUT_VARIABLE _ct_hash
                OUTPUT_STRIP_TRAILING_WHITESPACE
                ERROR_QUIET)
            if(_ct_count AND _ct_hash)
                set(_ct_full "${_ct_base}+git${_ct_count}.${_ct_hash}")
            endif()
        endif()
    endif()
endif()

set(CORTRACE_VERSION "${_ct_full}")
cortrace_deb_version("${CORTRACE_VERSION}" CORTRACE_DEB_VERSION)
message(STATUS "cortrace version: ${CORTRACE_VERSION} (deb ${CORTRACE_DEB_VERSION})")

# The Python package reports this version (cortrace/__init__.py imports it).
file(WRITE ${CMAKE_BINARY_DIR}/_version.py "__version__ = \"${CORTRACE_VERSION}\"\n")
