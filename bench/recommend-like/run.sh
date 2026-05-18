#!/usr/bin/env bash
# B0 (myje-base) vs A (myje) 对比跑 recommend-like-bench × ROUNDS 轮.
#
# 不 build .so; 直接 LD_PRELOAD bench-local.sh 注册的两份 .so.
# 模拟搜推稳态: dirty_decay_ms=-1, muzzy_decay_ms=-1, narenas=4.
#
# 默认: ROUNDS=5 WORKSET=8GB THREADS=min(nproc,100) DURATION=120s
# NUMA_NODE 已设时 THREADS 透过 numactl 取有效 cpuset 核数 (lab 单 NUMA = 96);
# 无 NUMA_NODE 时按裸 nproc (docker / 小机器).
# 覆盖: SO_A / SO_B0 / NUMA_NODE / MALLOC_CONF 等环境变量.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

ROUNDS=${ROUNDS:-5}
WORKSET=${WORKSET:-8}
NUMA_NODE=${NUMA_NODE:-}
# DEFAULT_THREADS 必须感知 NUMA_NODE: 裸 nproc 在外层 shell 拿到的是宿主全核数
# (lab 384), 而进 cpuset 后只有 96 核, min(384,100)=100 会过载推高 RSS。
if [[ -n "$NUMA_NODE" ]] && command -v numactl >/dev/null 2>&1; then
  EFFECTIVE_NPROC=$(numactl --cpunodebind="$NUMA_NODE" --membind="$NUMA_NODE" -- nproc)
else
  EFFECTIVE_NPROC=$(nproc)
fi
DEFAULT_THREADS=$(( EFFECTIVE_NPROC > 100 ? 100 : EFFECTIVE_NPROC ))
THREADS=${THREADS:-$DEFAULT_THREADS}
DURATION=${DURATION:-120}
STAT_PRINT=${STAT_PRINT:-15}
MALLOC_CONF_OPT=${MALLOC_CONF:-"dirty_decay_ms:-1,muzzy_decay_ms:-1,narenas:96"}

# PROD_LIKE=1 (默认) 启用 disable_tcache, 让 ≤32K dirty 形态接近生产 je_log_2752193 样本.
# 实测 lab 8GB/96t/120s: ≤32K ndirty 1848 → 31101 (16.8×), 形态从 1.4% 涨到 7% 占总 dirty.
# 设为 0 切回老 baseline (tcache:true), 用于对照 jemalloc 默认行为的优化效果.
PROD_LIKE=${PROD_LIKE:-1}
# 等价"请求级 alloc/free 时序"模拟. 0=老 random 路径 (与 disable_tcache 配合反而效果更好,
# 见 README "模式选择" 段). 默认 0 = 不启用 batch.
BATCH_SIZE=${BATCH_SIZE:-0}
BATCH_KEEP_PCT=${BATCH_KEEP_PCT:-0.05}

# bench 二进制由 malloc-bench CMake 构建产物提供
JE_BIN=${JE_BIN:-"$SCRIPT_DIR/../../out/bench/recommend-like-bench"}

# 默认从 ../../bench-local.sh 解析 myje / myje-base 的 .so 路径
BENCH_LOCAL="$SCRIPT_DIR/../../bench-local.sh"
resolve_alloc() {
  # bench-local.sh 行形如:  alloc_lib_add "myje"  "/path/libjemalloc.so"
  # 路径不应包含空格/引号.
  local name=$1
  [[ -f "$BENCH_LOCAL" ]] || return 0
  awk -v n="$name" '
    $1 == "alloc_lib_add" {
      gsub(/"/, "", $2); gsub(/"/, "", $3);
      if ($2 == n) { print $3; exit }
    }' "$BENCH_LOCAL"
}
SO_A=${SO_A:-$(resolve_alloc myje)}
SO_B0=${SO_B0:-$(resolve_alloc myje-base)}

# 预检
if [[ ! -x "$JE_BIN" ]]; then
  echo "ERROR: recommend-like-bench not built at $JE_BIN" >&2
  echo "  Build first: cd $(dirname "$JE_BIN") && make recommend-like-bench" >&2
  exit 2
fi
missing=0
for tag in A B0; do
  var="SO_$tag"
  val=${!var:-}
  if [[ -z "$val" || ! -f "$val" ]]; then
    echo "ERROR: $var missing or not a file: '${val:-<empty>}'" >&2
    missing=1
  fi
done
if [[ "$missing" == "1" ]]; then
  echo "  Either register myje / myje-base in $BENCH_LOCAL via alloc_lib_add," >&2
  echo "  or pass explicitly: SO_A=/path/x.so SO_B0=/path/y.so $0" >&2
  exit 2
fi

NUMA_PREFIX=()
if [[ -n "$NUMA_NODE" ]]; then
  NUMA_PREFIX=(numactl --cpunodebind="$NUMA_NODE" --membind="$NUMA_NODE")
fi

mkdir -p out

echo "config: ROUNDS=$ROUNDS WORKSET=${WORKSET}GB THREADS=$THREADS DURATION=${DURATION}s STAT_PRINT=${STAT_PRINT}s"
echo "MALLOC_CONF=$MALLOC_CONF_OPT"
echo "PROD_LIKE=$PROD_LIKE  BATCH_SIZE=$BATCH_SIZE"
echo "SO_B0=$SO_B0"
echo "SO_A =$SO_A"
[[ ${#NUMA_PREFIX[@]} -gt 0 ]] && echo "NUMA: ${NUMA_PREFIX[*]}"
# 仅清对比模式产物 (不动 sanity.csv 等)
find out -maxdepth 1 \( -name "B0-r*.csv" -o -name "A-r*.csv" \
                     -o -name "B0-r*.log" -o -name "A-r*.log" \) -delete

run_one() {
  local cfg=$1 so=$2 r=$3
  echo "  $cfg round $r"
  local extra=()
  [[ "$PROD_LIKE" == "1" ]] && extra+=("--disable-tcache")
  [[ "$BATCH_SIZE" -gt 0 ]] && extra+=("--batch-size" "$BATCH_SIZE" "--batch-keep-pct" "$BATCH_KEEP_PCT")
  LD_PRELOAD="$so" MALLOC_CONF="$MALLOC_CONF_OPT" \
    "${NUMA_PREFIX[@]}" "$JE_BIN" \
      --workset "$WORKSET" --threads "$THREADS" --duration "$DURATION" \
      --stat-print "$STAT_PRINT" --csv "out/$cfg-r$r.csv" \
      "${extra[@]}" \
    > "out/$cfg-r$r.log" 2>&1
}

declare -a CFGS=("B0|$SO_B0" "A|$SO_A")
for entry in "${CFGS[@]}"; do
  IFS='|' read -r cfg so <<< "$entry"
  for r in $(seq 1 "$ROUNDS"); do
    run_one "$cfg" "$so" "$r"
  done
done

echo "--- analyze ---"
python3 "$SCRIPT_DIR/analyze.py" out
