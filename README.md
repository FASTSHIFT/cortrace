# Cortrace

**简体中文** | [English](README_en.md)

**Cortrace** 把 ARM CoreSight ETM trace 原始字节流转换成函数级
[Perfetto](https://ui.perfetto.dev) 时间线。它用 ARM/Linaro 官方参考解码器
[OpenCSD](https://github.com/Linaro/OpenCSD) 解码，重建调用栈，并（规划中）
一键推送到 Perfetto 网页端。

> 设计理念与路线图见 [`docs/00-architecture.md`](docs/00-architecture.md)。

## 为什么

其他项目使用的精简 ETMv4 解码器在采集质量边际时会出错：遇到损坏字节就带着过时的 PC
继续行走，捏造多达约 4.6 倍的真实指令数和数百次假函数重入。OpenCSD 对同一条流处理正确，
并把无法解码的区段如实标记，而不是编造程序流。Cortrace 把最难的 ETM 解码外包给 OpenCSD，
只拥有其上确定性的层——调用栈重建、时间基对齐、Perfetto 导出。

原型已产出与 ELF **逐条调用边完全一致（0 mismatch）** 的调用图，且嵌套配平。
当前 `cortrace-decode` CLI 已跑通完整离线链路（OpenCSD 真链库 → 调用栈机），在 CoreMark
slice 上复现该结果：**11/11 调用边对 ELF、0 mismatch、begin/end 配平、14 个 SysTick 渲染**。

## 构建

需要 CMake ≥ 3.16、C++17 编译器，以及（可选）`libopencsd-dev` 用于解码器适配层。
核心库 + 测试在没有 OpenCSD 时也能构建；装了 OpenCSD 时额外产出 `cortrace-decode` CLI。

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j
ctest --test-dir build --output-on-failure
```

## 离线解码（cortrace-decode）

在已 deframe 的 ETMv4 字节流上跑完整链路，报告质量指标（配平、调用边）。
`mem.bin` 是 ELF 的扁平镜像，`mem_base` 是其最低 LMA（STM32H743 一般是 `08000000`）。

```sh
arm-none-eabi-objcopy -O binary fw.elf mem.bin      # 扁平镜像
arm-none-eabi-nm fw.elf > syms.nm                   # 符号表
build/cortrace-decode capture.etm.bin mem.bin 08000000 syms.nm --edges edges.tsv
```

判据：`begins == ends`（配平）、`edges.tsv` 每条边都对应 ELF 里真实的 `bl`（0 mismatch）。

## 覆盖率

```sh
cmake -S . -B build -DENABLE_COVERAGE=ON
cmake --build build --target coverage   # 跑测试 + gcovr，低于门禁则失败
```

门禁阈值用 `-DCORTRACE_MIN_COVERAGE=<百分比>`（默认 80）。HTML 报告在
`build/coverage-html/`。

## 代码格式化

基于 WebKit 的 clang-format（见 `.clang-format`）。

```sh
scripts/format.sh          # 原地格式化
scripts/format.sh --check  # CI 模式：有差异则失败
```

## Git 钩子

在每次提交时强制格式检查：

```sh
scripts/install-hooks.sh   # 设置 core.hooksPath = .githooks
```

`pre-commit` 钩子会在任何已暂存的 C/C++ 源码不符合 `.clang-format` 时**阻止提交**。

## 目录结构

```
include/cortrace/   公共头文件（element / symbols / callstack …）
src/                核心实现（不依赖 OpenCSD）
tests/              零依赖单元测试 + 框架
cmake/              CodeCoverage.cmake（gcovr + 门禁）
scripts/            format.sh, install-hooks.sh
.githooks/          pre-commit（格式门禁）
.github/workflows/  ci.yml（格式 + 构建 + 测试 + 覆盖率门禁）
docs/               设计文档（架构、Perfetto 直连/融合/Record 桥……）
```

## 相关仓库

- [**cortrace-fpga**](https://github.com/FASTSHIFT/cortrace-fpga) — Artix-7 并口 ETM 采集设备，通过 UDP 把 trace 送来解码
- [**stm32h743-etm-trace-firmware**](https://github.com/FASTSHIFT/stm32h743-etm-trace-firmware) — 确定性 selftrace 目标板固件

## 许可证

MIT © 2026 VIFEX（见 [`LICENSE`](LICENSE)）。
