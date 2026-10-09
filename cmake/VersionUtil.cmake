# Version string helpers (pure functions, no git, so they can be unit-tested
# with `cmake -P cmake/tests/version_test.cmake`).
#
# cortrace versions follow PEP 440, the subset
#
#     MAJOR.MINOR.PATCH [ (a|b|rc)N ] [ .postN ] [ .devN ] [ +LOCAL ]
#
# e.g. 1.0.0   1.0.0a1   1.0.0b2   1.0.0rc1   1.0.0.post1   1.0.0.dev3
# LOCAL is a build suffix such as git5.abc1234 (letters, digits, dots).

set(_CT_BASE_RE "^[0-9]+\\.[0-9]+\\.[0-9]+((a|b|rc)[0-9]+)?(\\.post[0-9]+)?(\\.dev[0-9]+)?$")
set(_CT_LOCAL_RE "^[0-9A-Za-z]+(\\.[0-9A-Za-z]+)*$")

# cortrace_version_split(<version> <base_out> <local_out>)
# Splits "1.0.0a1+git5.abc" into "1.0.0a1" and "git5.abc" (local may be empty).
function(cortrace_version_split version base_out local_out)
    string(FIND "${version}" "+" plus)
    if(plus EQUAL -1)
        set(${base_out} "${version}" PARENT_SCOPE)
        set(${local_out} "" PARENT_SCOPE)
    else()
        string(SUBSTRING "${version}" 0 ${plus} base)
        math(EXPR rest "${plus} + 1")
        string(SUBSTRING "${version}" ${rest} -1 local)
        set(${base_out} "${base}" PARENT_SCOPE)
        set(${local_out} "${local}" PARENT_SCOPE)
    endif()
endfunction()

# cortrace_version_valid(<version> <out_bool>)
function(cortrace_version_valid version out)
    cortrace_version_split("${version}" base local)
    set(ok FALSE)
    if(base MATCHES "${_CT_BASE_RE}")
        if(NOT version MATCHES "\\+" OR local MATCHES "${_CT_LOCAL_RE}")
            set(ok TRUE)
        endif()
    endif()
    set(${out} ${ok} PARENT_SCOPE)
endfunction()

# cortrace_deb_version(<version> <out>)
# PEP 440 -> Debian version, keeping the same ordering under dpkg:
#   1.0.0a1 -> 1.0.0~a1     (pre-releases sort BEFORE the final release)
#   1.0.0.dev3 -> 1.0.0~~dev3   (before 1.0.0a1, as in PEP 440)
#   1.0.0a1.dev2 -> 1.0.0~a1~dev2
#   1.0.0.post1 -> 1.0.0+post1
#   1.0.0+git5.abc -> 1.0.0+git5.abc
# The pre-release rewrites touch only the part before '+', so a hash such as
# "9b4cafe" in the local label is never mistaken for a "b4" pre-release.
function(cortrace_deb_version version out)
    cortrace_version_split("${version}" base local)
    # A dev release of a FINAL version (1.0.0.dev1) sorts before every
    # pre-release of it (1.0.0a1), so it needs a double tilde; a dev release of
    # a pre-release or post-release sorts just below that release (single tilde).
    if(base MATCHES "[0-9](a|b|rc)[0-9]+" OR base MATCHES "\\.post[0-9]+")
        set(dev_mark "~dev")
    else()
        set(dev_mark "~~dev")
    endif()
    string(REGEX REPLACE "([0-9])(a|b|rc)([0-9]+)" "\\1~\\2\\3" base "${base}")
    string(REGEX REPLACE "\\.dev([0-9]+)" "${dev_mark}\\1" base "${base}")
    string(REGEX REPLACE "\\.post([0-9]+)" "+post\\1" base "${base}")
    if(local)
        set(base "${base}+${local}")
    endif()
    set(${out} "${base}" PARENT_SCOPE)
endfunction()
