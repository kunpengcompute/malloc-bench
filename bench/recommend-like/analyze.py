#!/usr/bin/env python3
"""B0(myje-base) vs A(myje) 多轮 recommend-like-bench CSV 对比.

读 KB 单位 CSV (recommend_like_bench.c csv_header), 输出 MB.
不做 PASS/FAIL — 只出中位数表 + delta% + MAD.
"""
import csv
import glob
import statistics
import sys


def parse(path):
    """单 CSV → metric dict; 失败返回 None.

    去前 30% warmup, body 取每列中位数 (持续型);
    累积型 (alloc_mid_large/cum_allocs/cum_frees) 取末行;
    churn = cum_frees / cum_allocs (末行); 0 alloc 时 churn=0.
    """
    try:
        with open(path) as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return None
    if not rows:
        return None
    body = rows[max(1, int(len(rows) * 0.3)):] or rows[-1:]
    last = rows[-1]
    try:
        def med(k):
            return statistics.median(float(r[k]) for r in body)
        cum_a = float(last["cum_allocs"])
        cum_f = float(last["cum_frees"])
        return {
            "rss_mb":       med("rss_kb")       / 1024.0,
            "allocated_mb": med("allocated_kb") / 1024.0,
            "dirty_mb":     med("dirty_kb")     / 1024.0,
            "meta_mb":      med("metadata_kb")  / 1024.0,
            "edata_mb":     med("edata_kb")     / 1024.0,
            "lex":          med("lex_native"),
            "mid_lg":       float(last["alloc_mid_large"]),
            "mops":         med("nmalloc_per_sec") / 1e6,
            "churn":        (cum_f / cum_a) if cum_a else 0.0,
        }
    except (KeyError, ValueError) as e:
        print(f"warn parsing {path}: {e}", file=sys.stderr)
        return None


GROUPS = ["B0", "A"]
KEYS = ["rss_mb", "allocated_mb", "dirty_mb", "meta_mb", "edata_mb",
        "lex", "mid_lg", "mops", "churn"]


def aggregate(out_dir):
    """扫 out_dir/{B0,A}-r*.csv, 返回 (agg, GROUPS, KEYS).

    agg[group][key] = list of per-CSV metric value (跨轮).
    Parse 失败的 CSV 自动跳过.
    """
    agg = {g: {k: [] for k in KEYS} for g in GROUPS}
    for g in GROUPS:
        for path in sorted(glob.glob(f"{out_dir}/{g}-r*.csv")):
            r = parse(path)
            if r:
                for k in KEYS:
                    agg[g][k].append(r[k])
    return agg, GROUPS, KEYS


def med(lst):
    return statistics.median(lst) if lst else 0


def mad(lst):
    if not lst:
        return 0
    m = statistics.median(lst)
    return statistics.median(abs(x - m) for x in lst)


def format_report(agg, groups, keys):
    lines = []
    header = f"{'metric':<14} " + " ".join(f"{g:>10}" for g in groups) + "   delta vs B0"
    lines.append(header)
    for k in keys:
        b = med(agg["B0"][k])
        a = med(agg["A"][k])
        delta = (a / b - 1) * 100 if b else 0.0
        # mops/churn 小数点后变化才可见；其余 metric 0.01 量级已经足够
        if k in ("mops", "churn"):
            lines.append(f"{k:<14} {b:>10.4f} {a:>10.4f}    {delta:+.2f}%")
        else:
            lines.append(f"{k:<14} {b:>10.2f} {a:>10.2f}    {delta:+.2f}%")
    lines.append("")
    lines.append("MAD/median (run-to-run, <2% 稳定):")
    for k in ("rss_mb", "mops"):
        b_med, a_med = med(agg["B0"][k]), med(agg["A"][k])
        b_pct = mad(agg["B0"][k]) / b_med * 100 if b_med else 0.0
        a_pct = mad(agg["A"][k])  / a_med * 100 if a_med else 0.0
        lines.append(f"{k:<14} {b_pct:>9.2f}% {a_pct:>9.2f}%")
    return "\n".join(lines)


def main(out_dir):
    agg, groups, keys = aggregate(out_dir)
    print(format_report(agg, groups, keys))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: analyze.py <out_dir>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
