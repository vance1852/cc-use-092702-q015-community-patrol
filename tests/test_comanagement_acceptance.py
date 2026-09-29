from __future__ import annotations

import unittest
from pathlib import Path

from comanagement.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class ComanagementAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["missing_tables"], [])
        self.assertTrue(result["audit_valid"])
        # 同序号重放被吸收，分叉被识别，缺口先标记后补传。
        self.assertEqual(result["ingest"]["replayed"], [1])
        self.assertEqual(result["ingest"]["forked"], [3])
        self.assertEqual(result["ingest"]["gap_filled"], [29])
        # 跨村组转派后任务升到版本 2。
        self.assertEqual(result["task_versions"]["t-fire-0920"], 2)
        # 无争议 4 笔先进支付清单，争议 1 笔解冻后再支付。
        self.assertEqual(result["partial_payment_entries"], 4)
        self.assertEqual(result["recovered_open_disputes"], 1)
        self.assertEqual(result["final_state"], "paid")
        self.assertEqual(result["paid_total_cny"], "490.00")


if __name__ == "__main__":
    unittest.main()
