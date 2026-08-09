// Cortrace — minimal zero-dependency unit-test framework.
//
// Header-only, no external deps (keeps CI hermetic). Register cases with
// TEST(name){...}, assert with CHECK / CHECK_EQ. main() is provided by
// test_main.cpp which links all test translation units.
//
// SPDX-License-Identifier: MIT
#ifndef CORTRACE_TEST_FRAMEWORK_HPP
#define CORTRACE_TEST_FRAMEWORK_HPP

#include <cstdio>
#include <functional>
#include <sstream>
#include <string>
#include <vector>

namespace cortrace_test {

struct Case {
    std::string name;
    std::function<void()> fn;
};

inline std::vector<Case>& registry()
{
    static std::vector<Case> cases;
    return cases;
}

struct Registrar {
    Registrar(const std::string& name, std::function<void()> fn)
    {
        registry().push_back({ name, std::move(fn) });
    }
};

// Thrown on a failed assertion to abort the current case.
struct Failure {
    std::string msg;
};

inline int& failure_count()
{
    static int n = 0;
    return n;
}

inline void fail(const std::string& file, int line, const std::string& expr)
{
    std::ostringstream os;
    os << file << ":" << line << ": FAILED: " << expr;
    throw Failure { os.str() };
}

// Runs all registered cases. Returns process exit code (0 = all pass).
inline int run_all()
{
    int passed = 0, failed = 0;
    for (auto& c : registry()) {
        try {
            c.fn();
            std::printf("[ PASS ] %s\n", c.name.c_str());
            ++passed;
        } catch (const Failure& f) {
            std::printf("[ FAIL ] %s\n         %s\n", c.name.c_str(), f.msg.c_str());
            ++failed;
        } catch (const std::exception& e) {
            std::printf(
                "[ FAIL ] %s\n         unexpected exception: %s\n", c.name.c_str(), e.what());
            ++failed;
        }
    }
    std::printf("\n%d passed, %d failed, %zu total\n", passed, failed, registry().size());
    return failed == 0 ? 0 : 1;
}

} // namespace cortrace_test

#define TEST(name)                                                                                 \
    static void test_##name();                                                                     \
    static ::cortrace_test::Registrar registrar_##name(#name, test_##name);                        \
    static void test_##name()

#define CHECK(expr)                                                                                \
    do {                                                                                           \
        if (!(expr))                                                                               \
            ::cortrace_test::fail(__FILE__, __LINE__, #expr);                                      \
    } while (0)

#define CHECK_EQ(a, b)                                                                             \
    do {                                                                                           \
        auto _va = (a);                                                                            \
        auto _vb = (b);                                                                            \
        if (!(_va == _vb)) {                                                                       \
            std::ostringstream _os;                                                                \
            _os << #a " == " #b " (" << _va << " vs " << _vb << ")";                               \
            ::cortrace_test::fail(__FILE__, __LINE__, _os.str());                                  \
        }                                                                                          \
    } while (0)

#endif // CORTRACE_TEST_FRAMEWORK_HPP
