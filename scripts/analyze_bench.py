#!/usr/bin/env python3
"""
Statistical analysis of benchres.csv:
1. Compute per-column median across repeated rounds, append to file
2. Compute MAD (Median Absolute Deviation) = median(|xi-median|)/median * 100%, append to file
3. Compute ratio of each row vs the first allocator of the same benchmark ((v/v1)-1)*100%, append to file
"""

import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_CSV_PATH = Path(__file__).resolve().parent.parent / "out" / "bench" / "benchres.csv"

# ── 1. Parse raw data ────────────────────────────────────────────────────────
# Only lines whose 4th token is a plain integer are raw bench.sh output rows.
# Statistics rows appended by a previous run use fixed-width decimals (e.g.
# "4064.0000"), so their 4th token contains a decimal point and are skipped.
# This makes the script safe to re-run on a file that already has appended stats.

_RAW_LINE = re.compile(r'^\S+\s+\S+\s+\S+\s+\d+\s')

def _parse_elapsed(s: str) -> float:
    """Handle both 'SS.ss' and 'M:SS.ss' formats from /usr/bin/time."""
    if ":" in s:
        m, sec = s.split(":", 1)
        return int(m) * 60 + float(sec)
    return float(s)

rows = []
csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV_PATH

with csv_path.open() as f:
    for line in f:
        line = line.rstrip("\n")
        if not line.strip() or line.startswith("#"):
            continue
        if not _RAW_LINE.match(line):
            continue
        parts = line.split()
        if len(parts) < 8:
            continue
        benchmark = parts[0]
        allocator = parts[1]
        # elapsed may be "M:SS.ss"; remaining 5 fields are plain numbers
        nums = [str(_parse_elapsed(parts[2]))] + parts[3:]
        rows.append((benchmark, allocator, nums))

# ── 2. Group by (benchmark, allocator) ───────────────────────────────────────────

NUM_COLS = 6
groups: dict[tuple, list[list[float]]] = defaultdict(list)
for benchmark, allocator, nums in rows:
    key = (benchmark, allocator)
    try:
        values = [float(v) for v in nums]
    except ValueError:
        continue
    groups[key].append(values)

# Ordered (benchmark, allocator) list preserving first-seen order
seen = []
seen_set = set()
for benchmark, allocator, _ in rows:
    key = (benchmark, allocator)
    if key not in seen_set:
        seen.append(key)
        seen_set.add(key)

# ── 3. Compute per-column medians ────────────────────────────────────────────────

def median_row(values_list: list[list[float]]) -> list[float]:
    """Return per-column median across all rounds."""
    n_cols = len(values_list[0])
    return [statistics.median(v[c] for v in values_list) for c in range(n_cols)]

medians: dict[tuple, list[float]] = {}
for key in seen:
    medians[key] = median_row(groups[key])

# ── 4. Compute relative MAD = MAD/median * 100% ──────────────────────────────────

def mad_row(values_list: list[list[float]]) -> list[float | str]:
    """Return per-column relative MAD (%), or 'N/A' when median is zero."""
    n_cols = len(values_list[0])
    result = []
    for c in range(n_cols):
        col_vals = [v[c] for v in values_list]
        med = statistics.median(col_vals)
        if med == 0:
            result.append("N/A")
        else:
            mad = statistics.median(abs(v - med) for v in col_vals)
            result.append(mad / med * 100)
    return result

mads: dict[tuple, list] = {}
for key in seen:
    mads[key] = mad_row(groups[key])

# ── 5. Compute ratio table vs first allocator per benchmark ──────────────────────

# Use the first allocator seen for each benchmark as the baseline
first_alloc_per_bench: dict[str, list[float]] = {}
for (benchmark, allocator) in seen:
    if benchmark not in first_alloc_per_bench:
        first_alloc_per_bench[benchmark] = medians[(benchmark, allocator)]

def ratio_row(benchmark: str, med: list[float]) -> list[float | str]:
    base = first_alloc_per_bench[benchmark]
    result = []
    for b, v in zip(base, med):
        if b == 0:
            result.append("N/A")
        else:
            result.append((v / b - 1) * 100)
    return result

# ── 6. Format output rows ────────────────────────────────────────────────────────

COL_HEADERS = ["elapsed", "rss", "user", "sys", "page-faults", "page-reclaims"]

COL_WIDTHS = [12, 12, 12, 12, 12, 14]

def fmt_val(v, decimals=4, width=12) -> str:
    if isinstance(v, str):
        return f"{v:>{width}}"
    return f"{v:>{width}.{decimals}f}"

def make_line(benchmark: str, allocator: str, values, decimals=4) -> str:
    parts = [f"{benchmark:<16}", f"{allocator:<8}"]
    parts += [fmt_val(v, decimals, w) for v, w in zip(values, COL_WIDTHS)]
    return " ".join(parts)

# ── 7. Append results to file ────────────────────────────────────────────────────

def make_header_line():
    return f"# {'benchmark':<16} {'alloc':<8} " + " ".join(
        f"{h:>{w}}" for h, w in zip(COL_HEADERS, COL_WIDTHS)
    )

HEADER_MEDIAN = "\n# ===== MEDIAN (5 rounds) =====\n" + make_header_line() + "\n"
HEADER_MAD = "\n# ===== MAD: relative median absolute deviation (MAD/median*100%, 5 rounds) =====\n" + make_header_line() + "\n"
HEADER_RATIO = "\n# ===== RATIO vs FIRST ALLOCATOR per benchmark (%), (v/v1-1)*100 =====\n" + make_header_line() + "\n"

with csv_path.open("a") as f:
    # --- median table ---
    f.write(HEADER_MEDIAN)
    for key in seen:
        benchmark, allocator = key
        line = make_line(benchmark, allocator, medians[key], decimals=4)
        f.write(line + "\n")

    # --- MAD table ---
    f.write(HEADER_MAD)
    for key in seen:
        benchmark, allocator = key
        line = make_line(benchmark, allocator, mads[key], decimals=2)
        f.write(line + "\n")

    # --- ratio table ---
    f.write(HEADER_RATIO)
    for key in seen:
        benchmark, allocator = key
        ratios = ratio_row(benchmark, medians[key])
        line = make_line(benchmark, allocator, ratios, decimals=2)
        f.write(line + "\n")

print("Analysis complete. Results appended to:", csv_path)

# ── 8. Print summary to terminal ──────────────────────────────────────────────────

print("\n" + "=" * 70)
print("MEDIAN TABLE (first 2 cols: identifier, rest: per-metric medians)")
print("=" * 70)
header = f"{'benchmark':<16} {'alloc':<8} " + " ".join(f"{h:>{w}}" for h, w in zip(COL_HEADERS, COL_WIDTHS))
print(header)
print("-" * len(header))
for key in seen:
    benchmark, allocator = key
    print(make_line(benchmark, allocator, medians[key], decimals=4))

print("\n" + "=" * 70)
print("MAD TABLE = MAD/median * 100%  (lower is more stable, robust to outliers)")
print("=" * 70)
print(header)
print("-" * len(header))
for key in seen:
    benchmark, allocator = key
    print(make_line(benchmark, allocator, mads[key], decimals=2))

print("\n" + "=" * 70)
print("RATIO TABLE vs first allocator per benchmark (%), (v/v1-1)*100")
print("=" * 70)
print(header)
print("-" * len(header))
for key in seen:
    benchmark, allocator = key
    ratios = ratio_row(benchmark, medians[key])
    print(make_line(benchmark, allocator, ratios, decimals=2))
