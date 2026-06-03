#!/usr/bin/env bash
# 验证 recommend_like_bench 跑出的 KPI 形态匹配搜推请求应用 stat 真实数据。
#
# 默认参数: THREADS=min(nproc,100), WORKSET=2GB, DURATION=90s
# NUMA_NODE 已设时 THREADS 透过 numactl 取有效 cpuset 核数 (lab 单 NUMA = 96).
# Lab 完整跑: WORKSET=8 DURATION=180 NUMA_NODE=2
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# bench 二进制由 malloc-bench CMake 构建产物提供
# 默认查找路径: ../../out/bench/recommend-like-bench (malloc-bench 标准 build 目录)
JE_BIN=${JE_BIN:-"$SCRIPT_DIR/../../out/bench/recommend-like-bench"}
if [[ ! -x "$JE_BIN" ]]; then
  echo "ERROR: recommend-like-bench not found at $JE_BIN" >&2
  echo "Build first: cd /home/hxq/workspace/malloc-bench/out/bench && make recommend-like-bench (cmake cache should already exist)" >&2
  exit 2
fi

mkdir -p out

WORKSET=${WORKSET:-2}

# NUMA_NODE=N: 将进程的 CPU 与内存都绑定到该 NUMA 节点 (lab 上多 socket
# 时控制干扰)。空值表示不做 numa 绑定 (docker / 单 socket 默认)。
NUMA_NODE=${NUMA_NODE:-}
NUMA_PREFIX=()
if [[ -n "$NUMA_NODE" ]]; then
    NUMA_PREFIX=(numactl --cpunodebind="$NUMA_NODE" --membind="$NUMA_NODE")
fi

# DEFAULT_THREADS 必须感知 NUMA_NODE: 裸 nproc 在外层 shell 拿到的是宿主全核数
# (lab 384), 而进 cpuset 后只有 96 核, min(384,100)=100 会过载推高 RSS。
if [[ -n "$NUMA_NODE" ]] && command -v numactl >/dev/null 2>&1; then
    EFFECTIVE_NPROC=$(numactl --cpunodebind="$NUMA_NODE" --membind="$NUMA_NODE" -- nproc)
else
    EFFECTIVE_NPROC=$(nproc)
fi
DEFAULT_THREADS=$(( EFFECTIVE_NPROC > 100 ? 100 : EFFECTIVE_NPROC ))
THREADS=${THREADS:-$DEFAULT_THREADS}
DURATION=${DURATION:-90}
STAT_PRINT=${STAT_PRINT:-15}
# decay 关闭让 dirty 累积到 5-30% 区间, 对应搜推请求应用 stat 中 dirty_decay_ms=
# 10800000ms (3小时几乎不回收) 的稳态形态.
# narenas=4 < threads=8 让 arena 处于高 churn 状态.
MALLOC_CONF_OPT=${MALLOC_CONF:-"dirty_decay_ms:-1,muzzy_decay_ms:-1,narenas:4"}

CSV=out/sanity.csv
LOG=out/sanity.log

echo "MALLOC_CONF=$MALLOC_CONF_OPT"
[[ ${#NUMA_PREFIX[@]} -gt 0 ]] && echo "NUMA: ${NUMA_PREFIX[*]}"
echo "Running: $JE_BIN --workset $WORKSET --threads $THREADS --duration $DURATION --stat-print $STAT_PRINT"
MALLOC_CONF="$MALLOC_CONF_OPT" \
    "${NUMA_PREFIX[@]}" "$JE_BIN" --workset "$WORKSET" --threads "$THREADS" --duration "$DURATION" \
        --stat-print "$STAT_PRINT" --csv "$CSV" > "$LOG" 2>&1

echo "--- last 3 CSV rows ---"
tail -3 "$CSV"
echo "--- KPI check ---"
python3 "$SCRIPT_DIR/check_kpi.py" "$CSV" "$LOG"
