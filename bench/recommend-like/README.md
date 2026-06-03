# Bench Recommend-Like

模拟搜推请求类应用 (短生命周期、高 churn、稳态 dirty 不回收) 的 jemalloc allocator 微基准。
三条独立入口:

1. **Sanity 模式** (`run_sanity.sh`) — 验证 bench 跑出的 KPI 形态符合真实服务 stat 数据, 末尾 PASS/FAIL。
2. **对比模式** (`run.sh`) — 同一份 bench, B0 (baseline) vs A (待评估) 多轮 LD_PRELOAD 切换, 输出 metric 中位数 + delta% + MAD 噪声。
3. **bench.sh 集成入口** — 与 mimalloc-bench 主流程对接, 用 `../../bench.sh <jealloc> recommend-like` 把单次跑结果写进 `benchres.csv` (供 `graphs.py` 出图)。仅接受 jemalloc 系 allocator (白名单过滤, 见下)。

## Jemalloc-Only

bench 二进制编译期链接 jemalloc, 通过 `mallctl()` / `malloc_stats_print()` 读取 5 项 KPI
(allocated, dirty, metadata, edata, churn rate)。

LD_PRELOAD 切到非 jemalloc allocator (tcmalloc / mimalloc / glibc) 时, `malloc/free` 被替换
但 `mallctl` 仍解析到链接进来的 libjemalloc.so, stats 与实际 malloc 分裂, **KPI 数据不可信**。

因此本 case 不进 `tests_quickt` 默认套件, 仅以 jemalloc 系 allocator 调用 — 见下文
"bench.sh 集成入口" 的白名单过滤。

## 文件清单

| 文件                       | 作用                                                                 |
|----------------------------|----------------------------------------------------------------------|
| `recommend_like_bench.c`   | bench 主体 (PCG32 + size-distribution alias 表 + worker/sampler 线程) |
| `run_sanity.sh`            | 单 allocator 跑一次 → 末尾调 `check_kpi.py` 判 5 项 PASS/FAIL          |
| `check_kpi.py`             | 读 sanity.csv + log, 对照固定阈值给 PASS/FAIL                          |
| `run.sh`                   | B0 vs A × ROUNDS 轮 driver, 收尾调 `analyze.py`                       |
| `analyze.py`               | 多轮 CSV 聚合, 输出 9 行 metric 中位数对比表 + MAD/median 段           |
| `test_analyze.py`          | `analyze.py` 的单例测试 (14 case, 纯 stdlib): 锁 CSV 13 列契约 + MAD/median 百分比格式 + 禁 PASS/FAIL |
| `../../bench.sh` recommend-like 分支 | 把本 case 接进 mimalloc-bench 主流程, 内含 jemalloc 系白名单过滤 + skip warning |

## 构建 bench 二进制 (一次性)

bench 二进制编译期需要链接一份 jemalloc 来取 `mallctl()` 接口。默认走仓库内置的
jemalloc 5.3.0 (`extern/je/`), 不依赖任何仓库外路径:

```bash
# 先产出 extern/je/lib/libjemalloc.so 与 extern/je/include/jemalloc/*
./build-bench-env.sh je

# 编译 bench
cd out/bench
cmake ../../bench       # cmake cache 通常已存在; 不在时先 cmake
make -j$(nproc) recommend-like-bench
```

想用别的 jemalloc 源码树(例如自己实验的版本)替换默认链接目标:

```bash
cd out/bench
cmake -DJEMALLOC_PREFIX=/path/to/jemalloc ../../bench
make recommend-like-bench
```

`JEMALLOC_PREFIX` 被 CMake CACHE, 切换值时要么 `-DJEMALLOC_PREFIX=...` 显式覆盖,
要么先 `cmake -UJEMALLOC_PREFIX ../../bench` 清掉旧值。

⚠️ 这里链接的 jemalloc 只提供 `mallctl` / `malloc_stats_print` 接口符号 — 运行时
LD_PRELOAD 进来的 myje / myje-base (jemalloc fork) 会覆盖 `malloc/free` 与 stats 解析。
即编译期链接哪份 .so 不影响最终跑哪份 allocator。

## Sanity 模式: KPI 形态校验

```bash
# 默认: WORKSET=2GB / THREADS=min(nproc,100) / DURATION=90s
bench/recommend-like/run_sanity.sh

# Lab 完整跑 (8GB / 180s, 绑 NUMA node 2 → nproc 自动取 96 核)
WORKSET=8 DURATION=180 NUMA_NODE=2 bench/recommend-like/run_sanity.sh
```

环境变量: `WORKSET` (GB) / `THREADS` / `DURATION` (s) / `STAT_PRINT` (s) / `NUMA_NODE` / `MALLOC_CONF`。
默认 `THREADS=min(nproc,100)`。`NUMA_NODE` 已设时脚本透过
`numactl --cpunodebind=$NUMA_NODE -- nproc` 取**有效** cpuset 核数, 避免外层
shell 拿到宿主全核 (lab 384) 后算出 100 但 cpuset 内只 96 核, 4 线程过载推高
RSS、把 Allocated/RSS 顶过阈值。docker / 小机器无 NUMA_NODE 时按裸 nproc。

**期望 KPI 范围** (lab 8GB/96t/180s 全部命中):

| KPI                | min   | max   | 说明                                        |
|--------------------|-------|-------|---------------------------------------------|
| Allocated/RSS      | 0.85  | 1.00  | 大部分 RSS 是用户数据                       |
| Dirty/RSS          | 0.05  | 0.30  | 跑 >=120s 后 dirty 累积可见                 |
| Metadata/Allocated | 0.008 | 0.05  | 元数据合理                                  |
| ChurnRate          | 0.92  | 0.999 | `cum_frees / cum_allocs`                    |
| 16-32K alloc cnt   | 100   | 1e12  | 16K-32K extent 流量存在 (4K-page 必要前提) |

失败常见原因: 跑得太短 (<60s, dirty 没累积) / workset 太小 (<2GB, 16-32K 采样不到) /
churn 偏低 (workset 对 thread 太大, worker 一直 alloc 不 free, 减 workset)。

## 对比模式: B0 vs A

回答 "A 相对 B0 在 RSS / dirty / edata / 吞吐 上差多少", 不做 PASS/FAIL — 阈值由消费者自定。

**前提**: `bench-local.sh` 注册好 `myje` (A) 与 `myje-base` (B0):

```bash
alloc_lib_add "myje"      "/path/to/jemalloc-experimental/lib/libjemalloc.so"
alloc_lib_add "myje-base" "/path/to/jemalloc-base/lib/libjemalloc.so"
```

或直接用 `SO_A` / `SO_B0` env 覆盖 (跳过 `bench-local.sh` 解析)。

**运行**:

```bash
# 默认 5 轮 × 120s × 8GB × min(nproc,100) 线程, 稳态配置 (decay disabled, narenas=4)
bench/recommend-like/run.sh

# lab 推荐: 绑 NUMA node 2 (cpuset 限制后 nproc=96, 默认 THREADS 自动取 96 即满负载)
NUMA_NODE=2 bench/recommend-like/run.sh

# NUMA 域 1/3 负载基线 (低争用对照)
THREADS=32 NUMA_NODE=2 bench/recommend-like/run.sh

# 噪声基线: A 与 B0 指向同一份 .so, delta 应在 ±2% 内
SO_A=/path/x.so SO_B0=/path/x.so bench/recommend-like/run.sh

# 单跑 analyze 对已有 out/ 重新算 (不再起 bench)
python3 bench/recommend-like/analyze.py bench/recommend-like/out
```

环境变量: `ROUNDS` (默认 5) / `WORKSET` (默认 8GB) / `THREADS` (默认 `min(nproc,100)`) /
`DURATION` (默认 120s) / `STAT_PRINT` (默认 15s) / `NUMA_NODE` / `MALLOC_CONF` /
`SO_A` / `SO_B0` / `JE_BIN`。`NUMA_NODE` 已设时脚本透过
`numactl --cpunodebind=$NUMA_NODE -- nproc` 取**有效** cpuset 核数 (lab 单 NUMA
为 96), 避免外层 shell 拿到宿主全核数 (384) 后 4 线程过载。无 `NUMA_NODE` 时按裸 nproc。

每次跑前 `run.sh` 仅清 `out/{B0,A}-r*.{csv,log}` 4 个 glob, 不动 `sanity.csv` 等无关文件。

## bench.sh 集成入口: 与主流程对接

`recommend-like` 注册在 `tests_all5`, 用 mimalloc-bench 主入口 `bench.sh` 跑时, 把
elapsed / RSS / page-faults 等 7 列汇总写进 `out/bench/benchres.csv`, 与其他 bench
统一出图。**不**走 `run_sanity.sh` 那套 KPI 形态校验 — 那个仍由 sanity 入口完成。

### 仅 jemalloc 系 allocator (白名单过滤)

由于 bench 二进制编译期硬链 jemalloc 取 `mallctl`, LD_PRELOAD 非 jemalloc 系
allocator 时 stats 不可信 — 进 `bench.sh` 时会被白名单过滤掉, 并打 `warning`
跳过整轮:

```bash
# 默认白名单: je myje myje-base
cd out/bench
../../bench.sh je recommend-like              # OK, 跑
../../bench.sh myje myje-base recommend-like  # OK, 跑两轮 (用 bench-local.sh 注册的)
../../bench.sh tc mi recommend-like           # 全部被过滤 → warning + skip
../../bench.sh mi je recommend-like           # 仅 je 进入, mi 被过滤
```

覆盖白名单 (例如再加 `xje` 一类自定义 jemalloc fork):

```bash
RECOMMEND_LIKE_ALLOCS="je myje myje-base xje" ../../bench.sh xje recommend-like
```

### 默认参数

`bench.sh` 入口跑 `recommend-like-bench --workset 8 --threads min(procs,100) --duration 60`。
其中 `procs` 来自 `bench.sh --procs=N` 或 `nproc`。`--duration 60` 偏短: 仅作为
统一出图的"够用值", **不**保证 dirty 累积到 spec 区间; 想看完整 KPI 形态请走
`run_sanity.sh` 或 `run.sh`。

## 输出位置

| 路径                                         | 内容                                                  |
|----------------------------------------------|-------------------------------------------------------|
| `bench/recommend-like/out/sanity.csv`        | sanity 模式时序采样 (KB 单位)                          |
| `bench/recommend-like/out/sanity.log`        | sanity 模式 bench stderr + `malloc_stats_print()` 快照 |
| `bench/recommend-like/out/{B0,A}-r{1..N}.csv`| 对比模式每轮 CSV (KB 单位, 13 列)                      |
| `bench/recommend-like/out/{B0,A}-r{1..N}.log`| 对比模式每轮 stderr                                    |
| stdout                                       | sanity: PASS/FAIL 5 项; 对比: 9 行 metric 表 + MAD     |

**CSV 13 列** (`recommend_like_bench.c::csv_header`, 每 `STAT_PRINT` 秒一行):

```
t_sec, rss_kb, active_kb, allocated_kb, dirty_kb, metadata_kb, edata_kb,
lex_native, alloc_mid_large, cum_allocs, cum_frees,
nmalloc_per_sec, ndalloc_per_sec
```

## 对比表 9 行 metric

`analyze.py` 输出, 每行给 `B0 / A / delta vs B0%`:

| metric        | 含义                                              | 取值方式      |
|---------------|---------------------------------------------------|---------------|
| rss_mb        | 物理常驻 (`stats.resident`)                       | 后 70% 中位数 |
| allocated_mb  | 用户视角 live bytes (`stats.allocated`)           | 后 70% 中位数 |
| dirty_mb      | dirty pages (`resident - active - metadata`)      | 后 70% 中位数 |
| meta_mb       | 元数据总开销 (`stats.metadata`)                   | 后 70% 中位数 |
| edata_mb      | extent metadata pool (`stats.metadata_edata`)     | 后 70% 中位数 |
| lex           | 16-32K 真实 lextents (4K-page 下非零)             | 后 70% 中位数 |
| mid_lg        | 累计 16-32K alloc 次数 (worker 计数, 跨 page)     | 末行末值      |
| mops          | M-allocs/s 吞吐                                   | 后 70% 中位数 |
| churn         | `cum_frees / cum_allocs` (alloc:free 平衡)        | 末行末值      |

"后 70% 中位数" = 丢弃前 30% warmup 行后对余下行取每列 median。

## MAD/median 段

`analyze.py` 末尾输出哨兵指标 (`rss_mb` + `mops`) 跨轮稳定性, MAD 除以 median 转百分比:

```
MAD/median (run-to-run, <2% 稳定):
rss_mb            0.41%      0.27%
mops              1.12%      0.51%
```

**判定**: <2% 视为稳定; delta 量级**远超** MAD/median 才是可信信号。等价用法见
`out/bench/benchres-exp26.csv` 中 `MAD/median*100%` 段。

噪声基线 (`SO_A=$SO_B0`) 跑出的 delta 应在 ±2% 内, 可作为该机器 / 配置下的下限参考。

## 接口单例测试

```bash
python3 bench/recommend-like/test_analyze.py
```
