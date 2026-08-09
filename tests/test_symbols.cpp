// Cortrace — SymbolTable unit tests.
//
// SPDX-License-Identifier: MIT
#include "cortrace/symbols.hpp"
#include "test_framework.hpp"

using cortrace::SymbolTable;

TEST(symbols_function_at_basic)
{
    SymbolTable t;
    t.add(0x1000, "alpha");
    t.add(0x2000, "beta");
    t.add(0x3000, "gamma");
    t.finalize();

    CHECK_EQ(t.function_at(0x1000), std::string("alpha"));
    CHECK_EQ(t.function_at(0x1004), std::string("alpha")); // inside alpha
    CHECK_EQ(t.function_at(0x1fff), std::string("alpha"));
    CHECK_EQ(t.function_at(0x2000), std::string("beta"));
    CHECK_EQ(t.function_at(0x3500), std::string("gamma"));
}

TEST(symbols_below_first_is_unknown)
{
    SymbolTable t;
    t.add(0x1000, "alpha");
    t.finalize();
    CHECK_EQ(t.function_at(0x0800), std::string("?"));
}

TEST(symbols_empty_is_unknown)
{
    SymbolTable t;
    CHECK(t.empty());
    CHECK_EQ(t.function_at(0x1234), std::string("?"));
}

TEST(symbols_is_function_entry)
{
    SymbolTable t;
    t.add(0x2000, "beta");
    t.add(0x1000, "alpha"); // out of order on purpose
    t.finalize();

    CHECK(t.is_function_entry(0x1000));
    CHECK(t.is_function_entry(0x2000));
    CHECK(!t.is_function_entry(0x1004)); // mid-function, not an entry
    CHECK(!t.is_function_entry(0x0fff));
}

TEST(symbols_unsorted_add_then_finalize)
{
    SymbolTable t;
    t.add(0x3000, "gamma");
    t.add(0x1000, "alpha");
    t.add(0x2000, "beta");
    t.finalize();
    CHECK_EQ(t.size(), static_cast<std::size_t>(3));
    CHECK_EQ(t.function_at(0x2500), std::string("beta"));
}
