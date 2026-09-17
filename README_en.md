# Cortrace

[简体中文](README.md) | **English**

**Cortrace** turns a raw ARM CoreSight ETM trace byte stream into a
function-level [Perfetto](https://ui.perfetto.dev) timeline. It decodes with
the official ARM/Linaro reference decoder
[OpenCSD](https://github.com/Linaro/OpenCSD), reconstructs the call stack, and
(planned) pushes to the Perfetto web UI in one step.

> Design philosophy and roadmap live in [`docs/00-architecture.md`](docs/00-architecture.md).

## Why

The trimmed-down ETMv4 decoders other projects use go wrong when capture
quality is marginal: on a corrupt byte they keep walking with a stale PC,
fabricating up to ~4.6x the real instruction count and hundreds of phantom
function re-entries. OpenCSD handles the same stream correctly and honestly
marks undecodable regions instead of inventing program flow. Cortrace
outsources the hardest part -- ETM decode -- to OpenCSD and owns only the
deterministic layer above it: call-stack reconstruction, time-base alignment,
and Perfetto export.

The prototype produced a call graph that matches the ELF **edge-for-edge
(0 mismatch)**, with nesting balanced. The current `cortrace-decode` CLI runs
the full offline pipeline (real OpenCSD library -> call-stack machine) and
reproduces that result on a CoreMark slice: **11/11 call edges against the ELF,
0 mismatch, begin/end balanced, 14 SysTick exceptions rendered**.

## Build

Requires CMake >= 3.16, a C++17 compiler, and optionally `libopencsd-dev` for
the decoder adapter. The core library + tests build without OpenCSD; with
OpenCSD present it additionally produces the `cortrace-decode` CLI.

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j
ctest --test-dir build --output-on-failure
```

## Offline decode (cortrace-decode)

Run the full pipeline on a deframed ETMv4 byte stream and report quality
metrics (balance, call edges). `mem.bin` is a flat image of the ELF and
`mem_base` is its lowest LMA (`08000000` on the STM32H743, typically).

```sh
arm-none-eabi-objcopy -O binary fw.elf mem.bin      # flat image
arm-none-eabi-nm fw.elf > syms.nm                   # symbol table
build/cortrace-decode capture.etm.bin mem.bin 08000000 syms.nm --edges edges.tsv
```

Criteria: `begins == ends` (balanced), and every edge in `edges.tsv`
corresponds to a real `bl` in the ELF (0 mismatch).

## Coverage

```sh
cmake -S . -B build -DENABLE_COVERAGE=ON
cmake --build build --target coverage   # runs tests + gcovr, fails below the gate
```

Set the gate threshold with `-DCORTRACE_MIN_COVERAGE=<percent>` (default 80).
The HTML report lands in `build/coverage-html/`.

## Formatting

WebKit-based clang-format (see `.clang-format`).

```sh
scripts/format.sh          # C/C++ format in place (clang-format)
scripts/format.sh --check  # CI mode: fail on any diff
```

Python helper scripts (`scripts/*.py`) use black + pylint (see `pyproject.toml`,
`.pylintrc`):

```sh
scripts/format-py.sh          # black format in place
scripts/format-py.sh --check  # CI mode: black --check + pylint
python -m pytest scripts/tests --cov --cov-fail-under=80   # tests + coverage gate (80%)
```

## Git hooks

Enforce the format check on every commit:

```sh
scripts/install-hooks.sh   # sets core.hooksPath = .githooks
```

The `pre-commit` hook **blocks the commit** whenever any staged C/C++ source
does not conform to `.clang-format`, or any staged Python fails black/pylint.

## Layout

```
include/cortrace/   Public headers (element / symbols / callstack ...)
src/                Core implementation (no OpenCSD dependency)
tests/              Zero-dependency unit tests + framework
cmake/              CodeCoverage.cmake (gcovr + gate)
scripts/            format.sh, format-py.sh, install-hooks.sh, *.py + tests/
.githooks/          pre-commit (C/C++ + Python format/lint gate)
.github/workflows/  ci.yml (format + build + test + coverage + Python gate)
docs/               design docs (architecture, Perfetto bridges, fusion, ...)
```

## Related repos

- [**cortrace-fpga**](https://github.com/FASTSHIFT/cortrace-fpga) — Artix-7 parallel-ETM capture appliance that streams trace here over UDP
- [**stm32h743-etm-trace-firmware**](https://github.com/FASTSHIFT/stm32h743-etm-trace-firmware) — deterministic selftrace target firmware

## License

MIT © 2026 VIFEX (see [`LICENSE`](LICENSE)).
