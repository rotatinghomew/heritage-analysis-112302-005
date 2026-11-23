import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))

import capacity_audit as ca  # noqa: E402


class FunctionTests(unittest.TestCase):
    def test_to_min_and_fmt_roundtrip(self):
        self.assertEqual(ca.to_min("09:10"), 550)
        self.assertEqual(ca.fmt(550), "09:10")

    def test_overlap(self):
        self.assertEqual(ca.overlap(0, 10, 5, 15), 5)
        self.assertEqual(ca.overlap(0, 5, 5, 10), 0)

    def test_free_slots_blocks_intersecting_windows(self):
        # 21 分钟节拍；09:00-09:21 被占用，只有 09:21 起的窗口可售
        slots = ca.free_slots((540, 603), [(540, 561)], 21)
        self.assertEqual(slots, [561, 582])

    def test_audit_finds_breach_missing_leave_and_closure(self):
        audit = ca.load(ROOT / "fixtures")
        ca.check_capacity(audit)
        ca.check_dwell(audit)
        ca.check_evacuation(audit)
        ca.check_closures(audit)
        codes = {f.code for f in audit.findings}
        self.assertIn("CAPACITY-BREACH", codes)
        self.assertIn("MISSING-LEAVE", codes)
        self.assertIn("UNCERTAIN-EVAC", codes)
        self.assertIn("NODE-CLOSURE", codes)
        # A+B 超载窗口的量化口径
        breach = next(f for f in audit.findings if f.code == "CAPACITY-BREACH")
        self.assertEqual(breach.window, "09:10-09:12")

    def test_committable_freezes_breach_and_unknown_windows(self):
        audit = ca.load(ROOT / "fixtures")
        rows = ca.committable(audit)
        status = {(r["day"], r["node"]): r["status"] for r in rows}
        self.assertEqual(status[(1, "stair")], "超载-冻结")
        self.assertEqual(status[(2, "stair")], "暂停承诺")
        self.assertEqual(status[(2, "refuge")], "关闭")
        self.assertEqual(status[(1, "refuge")], "无观测")
        # 冻结窗口当前核准名额为 0；改约释放只是探索参考且保留最早团单
        d1 = next(r for r in rows if r["day"] == 1 and r["node"] == "stair")
        self.assertEqual(d1["approved_slots"], 0)
        self.assertIn("保留 A 按原约兑现", d1["rebooking_note"])

    def test_pairing_cannot_be_rearranged(self):
        audit = ca.load(ROOT / "fixtures")
        pairs = ca.pairing_feasibility(audit)
        self.assertEqual(len(pairs), 1)
        self.assertFalse(pairs[0]["feasible"])
        self.assertEqual(pairs[0]["people"], 18)

    def test_scenarios_share_same_horizon_and_monotone_gains(self):
        audit = ca.load(ROOT / "fixtures")
        rows = ca.scenario_table(audit)
        counts = [r["stair_entries_per_3h"] for r in rows[1:]]
        self.assertEqual(counts, sorted(counts))  # 收紧停留只在相同 3h 口径下比较
        self.assertEqual(rows[1]["stair_entries_per_3h"], 8)


if __name__ == "__main__":
    unittest.main()
