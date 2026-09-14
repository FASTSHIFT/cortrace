// Cortrace — minimal leveled logger.
//
// Purpose: stop swallowing error/anomaly branches silently, without scattering
// raw fprintf(stderr) calls. Diagnostics go through CT_LOG_* macros with a
// runtime-filterable level; the tool's *primary report* (decode result tables,
// coverage, verdicts) is NOT logging and keeps writing to stdout/stderr
// directly.
//
// Level defaults to Warn and is set explicitly by the application, e.g. from a
// CLI flag (--log-level) via cortrace::log::set_level() / set_level_from_str().
// There is deliberately no environment-variable coupling.
//
// The macros are zero-overhead below the active level: the message expression
// is only evaluated if it will be printed (the level check short-circuits
// before the varargs are formatted).
//
// Usage:
//   CT_LOG_ERROR("opencsd fatal at byte %zu", n);
//   CT_LOG_WARN("dropped call: callee 0x%x not at a function entry", addr);
//   CT_LOG_DEBUG("range [%x,%x) cc=%u", s, e, cc);
//
// SPDX-License-Identifier: MIT
#ifndef CORTRACE_LOG_HPP
#define CORTRACE_LOG_HPP

#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

namespace cortrace {
namespace log {

    enum class Level : int {
        Error = 0,
        Warn = 1,
        Info = 2,
        Debug = 3,
        Trace = 4,
    };

    inline const char* level_name(Level l)
    {
        switch (l) {
        case Level::Error:
            return "ERROR";
        case Level::Warn:
            return "WARN";
        case Level::Info:
            return "INFO";
        case Level::Debug:
            return "DEBUG";
        case Level::Trace:
            return "TRACE";
        }
        return "?";
    }

    inline Level parse_level(const char* s, Level fallback)
    {
        if (!s || !*s)
            return fallback;
        // numeric?
        if (s[0] >= '0' && s[0] <= '9') {
            int v = std::atoi(s);
            if (v < 0)
                v = 0;
            if (v > 4)
                v = 4;
            return static_cast<Level>(v);
        }
#if defined(_WIN32)
#define CT_STRcasecmp _stricmp
#else
#define CT_STRcasecmp strcasecmp
#endif
        if (CT_STRcasecmp(s, "error") == 0)
            return Level::Error;
        if (CT_STRcasecmp(s, "warn") == 0 || CT_STRcasecmp(s, "warning") == 0)
            return Level::Warn;
        if (CT_STRcasecmp(s, "info") == 0)
            return Level::Info;
        if (CT_STRcasecmp(s, "debug") == 0)
            return Level::Debug;
        if (CT_STRcasecmp(s, "trace") == 0)
            return Level::Trace;
#undef CT_STRcasecmp
        return fallback;
    }

    // The active level lives in one function-local static so the header is
    // self-contained (no .cpp needed). Defaults to Warn; the application sets
    // it (e.g. from a --log-level CLI flag).
    inline Level& active_level()
    {
        static Level level = Level::Warn;
        return level;
    }

    inline void set_level(Level l) { active_level() = l; }

    // Set from a string ("error|warn|info|debug|trace" or "0".."4"); unknown
    // strings leave the level unchanged. Returns the resulting level.
    inline Level set_level_from_str(const std::string& s)
    {
        set_level(parse_level(s.c_str(), active_level()));
        return active_level();
    }

    inline bool enabled(Level l) { return static_cast<int>(l) <= static_cast<int>(active_level()); }

    // Emit one formatted line to stderr with a level tag. Called only after the
    // level check in the macro, so formatting cost is only paid when printed.
    inline void emit(Level l, const char* fmt, ...)
    {
        std::fprintf(stderr, "[%s] ", level_name(l));
        va_list ap;
        va_start(ap, fmt);
        std::vfprintf(stderr, fmt, ap);
        va_end(ap);
        std::fputc('\n', stderr);
    }

} // namespace log
} // namespace cortrace

// Macros: the level guard short-circuits so args aren't evaluated when the
// level is disabled. do/while(0) keeps them statement-safe.
#define CT_LOG(level, ...)                                                                         \
    do {                                                                                           \
        if (::cortrace::log::enabled(level))                                                       \
            ::cortrace::log::emit(level, __VA_ARGS__);                                             \
    } while (0)

#define CT_LOG_ERROR(...) CT_LOG(::cortrace::log::Level::Error, __VA_ARGS__)
#define CT_LOG_WARN(...) CT_LOG(::cortrace::log::Level::Warn, __VA_ARGS__)
#define CT_LOG_INFO(...) CT_LOG(::cortrace::log::Level::Info, __VA_ARGS__)
#define CT_LOG_DEBUG(...) CT_LOG(::cortrace::log::Level::Debug, __VA_ARGS__)
#define CT_LOG_TRACE(...) CT_LOG(::cortrace::log::Level::Trace, __VA_ARGS__)

#endif // CORTRACE_LOG_HPP
