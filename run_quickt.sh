#!/usr/bin/env bash
# run_quickt.sh — run quickt benchmark suite then analyze results
#
# Usage:
#   ./run_quickt.sh [allocators...]
#
# Default allocators: auto-detected from CPU part (kq_08/kq_09/kq_12), plus je tc mi
# Example: ./run_quickt.sh kq_08 je tc mi
#
# Override rounds / cores via env vars:
#   ROUNDS=3 PROCS=4 ./run_quickt.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_OUT="${REPO_ROOT}/out/bench"
BENCHRES="${BENCH_OUT}/benchres.csv"

ROUNDS="${ROUNDS:-5}"
PROCS="${PROCS:-$(nproc)}"

# ── Auto-detect KP allocator variant from CPU part number ─────────────────────
detect_kp_alloc() {
    local cpu_part
    cpu_part=$(grep -m1 "CPU part" /proc/cpuinfo 2>/dev/null | awk '{print $NF}' | tr '[:upper:]' '[:lower:]')
    case "${cpu_part}" in
        0xd06) echo "kq_12" ;;
        0xd02) echo "kq_09" ;;
        *)     echo "kq_08" ;;  # d01 or generic ARM
    esac
}

if [ $# -gt 0 ]; then
    ALLOCS="$*"
else
    KP_ALLOC="$(detect_kp_alloc)"
    ALLOCS="${KP_ALLOC} je tc mi"
fi

echo "========================================"
echo "  malloc-bench quickt run"
echo "  allocators : ${ALLOCS}"
echo "  rounds     : ${ROUNDS}"
echo "  procs      : ${PROCS}"
echo "========================================"

mkdir -p "${BENCH_OUT}"
cd "${BENCH_OUT}"

# ── 1. Run benchmarks ────────────────────────────────────────────────────────
"${REPO_ROOT}/bench.sh" --procs="${PROCS}" -r="${ROUNDS}" -n=1 -s=1 \
    ${ALLOCS} quickt

# ── 2. Analyze and append statistics ─────────────────────────────────────────
python3 "${REPO_ROOT}/scripts/analyze_bench.py" "${BENCHRES}"
