#!/usr/bin/env python3
"""analyze.py 单测；纯 stdlib，运行 `python3 test_analyze.py`."""
import os
import subprocess
import tempfile
import unittest

import analyze


CSV_HEADER = (
    "t_sec,rss_kb,active_kb,allocated_kb,dirty_kb,metadata_kb,edata_kb,"
    "lex_native,alloc_mid_large,cum_allocs,cum_frees,nmalloc_per_sec,ndalloc_per_sec\n"
)


def _write_csv(path, rows):
    """rows: list of dict 按 header 顺序写出."""
    cols = CSV_HEADER.strip().split(",")
    with open(path, "w") as f:
        f.write(CSV_HEADER)
        for r in rows:
            f.write(",".join(str(r[c]) for c in cols) + "\n")


class TestParse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _make_csv(self, name, rows):
        p = os.path.join(self.tmp.name, name)
        _write_csv(p, rows)
        return p

    def test_parse_basic_medians(self):
        # 10 行；去前 30% (3 行) → body 是 row[3..9]
        # body rss_kb 中位数 = median(4,5,6,7,8,9,10) * 1024 = 7 * 1024 (KB→MB / 1024)
        rows = []
        for i in range(10):
            rows.append({
                "t_sec": (i + 1) * 15,
                "rss_kb": (i + 1) * 1024,       # 1024..10240 KB → 1..10 MB
                "active_kb": 0,
                "allocated_kb": (i + 1) * 512,  # 512..5120 KB
                "dirty_kb": 100,
                "metadata_kb": 50,
                "edata_kb": 25,
                "lex_native": 7,
                "alloc_mid_large": i * 100,     # 末行 = 900
                "cum_allocs": (i + 1) * 1000,   # 末行 = 10000
                "cum_frees": (i + 1) * 950,     # 末行 = 9500
                "nmalloc_per_sec": 2_000_000,   # 2M/s → 2.0 mops
                "ndalloc_per_sec": 1_900_000,
            })
        path = self._make_csv("B0-r1.csv", rows)
        out = analyze.parse(path)
        self.assertIsNotNone(out)
        # body = rows[3:] = 7 行, rss_kb 中位 = (4..10)*1024 → median=7*1024=7168 KB → 7.0 MB
        self.assertAlmostEqual(out["rss_mb"], 7.0, places=3)
        self.assertAlmostEqual(out["allocated_mb"], 3.5, places=3)  # 7*512=3584/1024=3.5
        self.assertAlmostEqual(out["dirty_mb"], 100 / 1024.0, places=3)
        self.assertAlmostEqual(out["meta_mb"], 50 / 1024.0, places=3)
        self.assertAlmostEqual(out["edata_mb"], 25 / 1024.0, places=3)
        self.assertEqual(out["lex"], 7)
        self.assertEqual(out["mid_lg"], 900)             # last row
        self.assertAlmostEqual(out["mops"], 2.0, places=3)
        self.assertAlmostEqual(out["churn"], 9500 / 10000.0, places=4)

    def test_parse_empty_csv_returns_none(self):
        path = os.path.join(self.tmp.name, "empty.csv")
        with open(path, "w") as f:
            f.write(CSV_HEADER)  # header only, no data
        self.assertIsNone(analyze.parse(path))

    def test_parse_missing_column_returns_none(self):
        # 故意写一份缺列的 CSV
        path = os.path.join(self.tmp.name, "bad.csv")
        with open(path, "w") as f:
            f.write("t_sec,rss_kb\n0,1024\n10,2048\n")
        self.assertIsNone(analyze.parse(path))

    def test_parse_churn_zero_when_no_allocs(self):
        rows = [{
            "t_sec": 15, "rss_kb": 1024, "active_kb": 0, "allocated_kb": 512,
            "dirty_kb": 0, "metadata_kb": 0, "edata_kb": 0, "lex_native": 0,
            "alloc_mid_large": 0, "cum_allocs": 0, "cum_frees": 0,
            "nmalloc_per_sec": 0, "ndalloc_per_sec": 0,
        }]
        path = self._make_csv("zero.csv", rows)
        out = analyze.parse(path)
        self.assertEqual(out["churn"], 0.0)


class TestAggregate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _csv_with_rss(self, name, rss_kb_seq, mops):
        """生成一份指定 rss 序列 + 恒定 nmalloc_per_sec 的 CSV."""
        rows = []
        for i, rss in enumerate(rss_kb_seq):
            rows.append({
                "t_sec": (i + 1) * 15,
                "rss_kb": rss, "active_kb": 0, "allocated_kb": rss // 2,
                "dirty_kb": 0, "metadata_kb": 0, "edata_kb": 0,
                "lex_native": 0, "alloc_mid_large": (i + 1) * 10,
                "cum_allocs": (i + 1) * 1000,
                "cum_frees": (i + 1) * 900,
                "nmalloc_per_sec": int(mops * 1e6),
                "ndalloc_per_sec": int(mops * 0.9e6),
            })
        path = os.path.join(self.tmp.name, name)
        _write_csv(path, rows)
        return path

    def test_aggregate_groups_by_prefix(self):
        # 4 轮 B0 (rss body 中位 7.0 MB) + 4 轮 A (rss body 中位 5.5 MB)
        for r in range(1, 5):
            self._csv_with_rss(f"B0-r{r}.csv", [1024 * x for x in range(1, 11)], 2.0)
            self._csv_with_rss(f"A-r{r}.csv",  [1024 * x for x in range(1, 9)],  1.9)
        agg, groups, keys = analyze.aggregate(self.tmp.name)
        self.assertEqual(groups, ["B0", "A"])
        self.assertIn("rss_mb", keys)
        self.assertEqual(len(agg["B0"]["rss_mb"]), 4)
        self.assertEqual(len(agg["A"]["rss_mb"]), 4)

    def test_aggregate_skips_missing_group(self):
        # 只有 B0, 没有 A
        self._csv_with_rss("B0-r1.csv", [1024, 2048, 3072], 1.0)
        agg, _, _ = analyze.aggregate(self.tmp.name)
        self.assertEqual(len(agg["B0"]["rss_mb"]), 1)
        self.assertEqual(agg["A"]["rss_mb"], [])

    def test_med_empty_returns_zero(self):
        self.assertEqual(analyze.med([]), 0)

    def test_mad_basic(self):
        # median([1,2,3,4,5]) = 3; abs deviations [2,1,0,1,2] → median = 1
        self.assertEqual(analyze.mad([1, 2, 3, 4, 5]), 1)

    def test_mad_empty_returns_zero(self):
        self.assertEqual(analyze.mad([]), 0)


class TestFormat(unittest.TestCase):
    def test_format_report_shape(self):
        # agg 模拟: B0 rss=10MB, A rss=8MB → delta -20%
        agg = {
            "B0": {k: [10.0] * 3 for k in analyze.KEYS},
            "A":  {k: [8.0]  * 3 for k in analyze.KEYS},
        }
        out = analyze.format_report(agg, analyze.GROUPS, analyze.KEYS)
        self.assertIn("metric", out)
        self.assertIn("B0", out); self.assertIn("A", out)
        self.assertIn("rss_mb", out)
        self.assertIn("mops", out)
        self.assertIn("MAD", out)
        # delta = (8/10 - 1) * 100 = -20%
        self.assertIn("-20.00%", out)
        # 不应该出现 PASS/FAIL 字样 (spec §5.4 禁止)
        self.assertNotIn("PASS", out)
        self.assertNotIn("FAIL", out)

    def test_format_report_handles_zero_b0(self):
        # B0 缺数据时 delta 应该是 0 而非除零
        agg = {
            "B0": {k: [] for k in analyze.KEYS},
            "A":  {k: [5.0] for k in analyze.KEYS},
        }
        out = analyze.format_report(agg, analyze.GROUPS, analyze.KEYS)
        self.assertIn("+0.00%", out)  # b=0 时 delta 兜底 0

    def test_format_report_mad_is_relative_percent(self):
        # rss_mb B0 = [10,12,8,11,9]: median=10, |xi-m|=[0,2,2,1,1] → MAD=1
        # → MAD/median = 1/10 * 100 = 10.00%
        agg = {
            "B0": {k: [10.0, 12.0, 8.0, 11.0, 9.0] for k in analyze.KEYS},
            "A":  {k: [5.0] * 5 for k in analyze.KEYS},  # MAD=0 → 0.00%
        }
        out = analyze.format_report(agg, analyze.GROUPS, analyze.KEYS)
        # MAD 段标题包含"MAD/median"字样
        self.assertIn("MAD/median", out)
        # B0 rss MAD/median = 10.00%
        self.assertIn("10.00%", out)
        # A 列恒等 → MAD=0 → "0.00%"
        mad_section = out.split("MAD/median")[1]
        self.assertIn("0.00%", mad_section)


class TestCli(unittest.TestCase):
    def test_main_exits_2_when_no_arg(self):
        script = os.path.join(os.path.dirname(__file__), "analyze.py")
        r = subprocess.run(
            ["python3", script],
            capture_output=True, text=True
        )
        self.assertEqual(r.returncode, 2)

    def test_main_prints_table_for_empty_dir(self):
        script = os.path.join(os.path.dirname(__file__), "analyze.py")
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run(
                ["python3", script, d],
                capture_output=True, text=True
            )
            self.assertEqual(r.returncode, 0)
            self.assertIn("metric", r.stdout)
            self.assertIn("MAD", r.stdout)


if __name__ == "__main__":
    unittest.main()
