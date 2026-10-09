# Install rules and the Debian package (cpack -G DEB).
#
# Layout (prefix /usr):
#   bin/cortrace            Python entry point (runs the cortrace package)
#   bin/cortrace-decode     ETM/DWT/ITM decoder (OpenCSD linked in statically)
#   bin/cortrace-grab       FPGA UDP capture helper
#   lib/python3/dist-packages/cortrace/   the Python package
#   share/doc/cortrace/     README, licences
#
# Build the package from a clean tree:
#   cmake -S . -B build-deb -DCMAKE_BUILD_TYPE=Release -DCORTRACE_OPENCSD_STATIC=ON
#   cmake --build build-deb -j && (cd build-deb && cpack -G DEB)

# ---- version: from the latest vX.Y.Z git tag --------------------------------
# Tagged commit v0.2.0           -> 0.2.0
# 5 commits after v0.2.0         -> 0.2.0+git5.abc1234
# no tag yet                     -> 0.0.0+git<commit count>.abc1234
# -DCORTRACE_VERSION=1.2.3 overrides (CI passes the tag name).
set(CORTRACE_VERSION "" CACHE STRING "Package version override, e.g. 0.2.0")
if(NOT CORTRACE_VERSION)
    find_package(Git QUIET)
    set(_ct_ver "0.0.0+dev")
    if(GIT_FOUND)
        execute_process(
            COMMAND ${GIT_EXECUTABLE} describe --tags --long --match "v[0-9]*"
            WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
            OUTPUT_VARIABLE _ct_desc
            OUTPUT_STRIP_TRAILING_WHITESPACE
            ERROR_QUIET)
        if(_ct_desc MATCHES "^v([0-9][0-9.]*)-([0-9]+)-g([0-9a-f]+)$")
            if(CMAKE_MATCH_2 EQUAL 0)
                set(_ct_ver "${CMAKE_MATCH_1}")
            else()
                set(_ct_ver "${CMAKE_MATCH_1}+git${CMAKE_MATCH_2}.${CMAKE_MATCH_3}")
            endif()
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
                set(_ct_ver "0.0.0+git${_ct_count}.${_ct_hash}")
            endif()
        endif()
    endif()
    set(CORTRACE_VERSION "${_ct_ver}")
endif()
message(STATUS "cortrace version: ${CORTRACE_VERSION}")

# The Python package reports this version (cortrace/__init__.py imports it).
file(WRITE ${CMAKE_BINARY_DIR}/_version.py "__version__ = \"${CORTRACE_VERSION}\"\n")

# ---- install ------------------------------------------------------------------
install(TARGETS cortrace-grab RUNTIME DESTINATION bin)
if(CORTRACE_HAVE_OPENCSD AND TARGET cortrace-decode)
    install(TARGETS cortrace-decode RUNTIME DESTINATION bin)
endif()
install(PROGRAMS ${CMAKE_SOURCE_DIR}/python/bin/cortrace DESTINATION bin)
install(DIRECTORY ${CMAKE_SOURCE_DIR}/python/cortrace
        DESTINATION lib/python3/dist-packages
        FILES_MATCHING PATTERN "*.py"
        PATTERN "__pycache__" EXCLUDE)
install(FILES ${CMAKE_BINARY_DIR}/_version.py
        DESTINATION lib/python3/dist-packages/cortrace)
install(FILES ${CMAKE_SOURCE_DIR}/README.md ${CMAKE_SOURCE_DIR}/README_en.md
        DESTINATION share/doc/cortrace)
install(FILES ${CMAKE_SOURCE_DIR}/LICENSE
        DESTINATION share/doc/cortrace RENAME copyright)
if(EXISTS ${CMAKE_SOURCE_DIR}/third_party/opencsd/LICENSE)
    install(FILES ${CMAKE_SOURCE_DIR}/third_party/opencsd/LICENSE
            DESTINATION share/doc/cortrace RENAME LICENSE.opencsd)
endif()

# ---- CPack: .deb --------------------------------------------------------------
set(CPACK_GENERATOR DEB)
set(CPACK_PACKAGE_NAME cortrace)
set(CPACK_PACKAGE_VERSION "${CORTRACE_VERSION}")
set(CPACK_PACKAGE_CONTACT "VIFEX <vifextech@foxmail.com>")
set(CPACK_PACKAGE_DESCRIPTION_SUMMARY
    "Cortex-M parallel trace (ETM/DWT/ITM) decoder, FPGA capture and Perfetto tools")
set(CPACK_PACKAGE_DESCRIPTION
    "cortrace decodes Cortex-M ETM/DWT/ITM trace captured over a 4/2/1-bit TPIU port into Perfetto traces (call stacks, RTOS thread lanes, software notes on one time axis). It includes the FPGA streamer tools (capture, CSR control, health), a Perfetto Record target (press Start in ui.perfetto.dev) and optional fusion with NuttX nxtrace.")
set(CPACK_STRIP_FILES ON)
set(CPACK_PACKAGING_INSTALL_PREFIX /usr)
set(CPACK_DEBIAN_FILE_NAME DEB-DEFAULT)
set(CPACK_DEBIAN_PACKAGE_SECTION devel)
set(CPACK_DEBIAN_PACKAGE_HOMEPAGE "https://github.com/FASTSHIFT/cortrace")
set(CPACK_DEBIAN_PACKAGE_SHLIBDEPS ON)
set(CPACK_DEBIAN_PACKAGE_DEPENDS "python3 (>= 3.10)")
# arm-none-eabi-nm reads the ELF symbols; openocd arms/reads the target.
set(CPACK_DEBIAN_PACKAGE_RECOMMENDS "binutils-arm-none-eabi")
set(CPACK_DEBIAN_PACKAGE_SUGGESTS "openocd")
set(CPACK_DEBIAN_PACKAGE_CONTROL_EXTRA
    ${CMAKE_SOURCE_DIR}/packaging/debian/postinst
    ${CMAKE_SOURCE_DIR}/packaging/debian/prerm)
include(CPack)
