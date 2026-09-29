from __future__ import annotations

import unittest
from decimal import Decimal

from comanagement.pricing import (
    LineFact,
    PricingRule,
    ReceiptFact,
    blockade_covers,
    compute_line,
    dedupe_checkins,
    money,
)


RULE = PricingRule(
    rule_id="r", version=1, task_family="fire_lookout", task_kind="planned",
    base_unit="checkpoint", base_rate=Decimal("30"), reinforcement_rate=Decimal("0"),
    repeat_within_minutes=30, road_closure_excuse_code="ROAD_CLOSED",
)
REIN = PricingRule(
    rule_id="r", version=1, task_family="fire_lookout", task_kind="reinforcement",
    base_unit="checkpoint", base_rate=Decimal("30"), reinforcement_rate=Decimal("20"),
    repeat_within_minutes=30, road_closure_excuse_code="ROAD_CLOSED",
)
EVENT_RULE = PricingRule(
    rule_id="e", version=2, task_family="wildlife_conflict", task_kind="planned",
    base_unit="event", base_rate=Decimal("50"), reinforcement_rate=Decimal("0"),
    repeat_within_minutes=30, road_closure_excuse_code="ROAD_CLOSED",
)
SHIFT_RULE = PricingRule(
    rule_id="s", version=1, task_family="waste_haul", task_kind="planned",
    base_unit="shift", base_rate=Decimal("80"), reinforcement_rate=Decimal("0"),
    repeat_within_minutes=30, road_closure_excuse_code="ROAD_CLOSED",
)


class PricingTests(unittest.TestCase):
    def test_checkpoint_line_excuses_missing_points(self) -> None:
        line = compute_line(RULE, LineFact(
            person_id="p1", task_state="completed",
            required_checkpoints=("c1", "c2", "c3"),
            reached={"c1": 11, "c2": 12}, excused=frozenset({"c3"}),
            signed_on=True, signed_off=True,
        ))
        self.assertEqual(line["base_amount_cny"], "60.00")
        self.assertEqual(line["excused_amount_cny"], "30.00")
        self.assertEqual(line["deducted_amount_cny"], "0.00")
        self.assertEqual(line["total_amount_cny"], "90.00")
        self.assertEqual([u["state"] for u in line["units"]], ["reached", "reached", "excused"])

    def test_missing_checkpoint_is_deducted_not_excused(self) -> None:
        line = compute_line(RULE, LineFact(
            person_id="p1", task_state="incomplete",
            required_checkpoints=("c1", "c2"), reached={"c1": 11},
        ))
        self.assertEqual(line["base_amount_cny"], "30.00")
        self.assertEqual(line["deducted_amount_cny"], "30.00")
        self.assertEqual(line["total_amount_cny"], "30.00")

    def test_reinforcement_adds_per_unit_premium(self) -> None:
        line = compute_line(REIN, LineFact(
            person_id="p1", task_state="completed",
            required_checkpoints=("c1", "c2", "c3"),
            reached={"c1": 1, "c2": 2, "c3": 3}, signed_on=True, signed_off=True,
        ))
        self.assertEqual(line["base_amount_cny"], "90.00")
        self.assertEqual(line["reinforcement_amount_cny"], "60.00")
        self.assertEqual(line["total_amount_cny"], "150.00")

    def test_shift_excused_by_blockade_still_pays_full(self) -> None:
        line = compute_line(SHIFT_RULE, LineFact(
            person_id="p1", task_state="incomplete", signed_on=True, shift_excused=True,
        ))
        self.assertEqual(line["total_amount_cny"], "80.00")
        self.assertEqual(line["excused_amount_cny"], "80.00")
        self.assertEqual(line["deducted_amount_cny"], "0.00")

    def test_shift_without_signon_pays_nothing(self) -> None:
        line = compute_line(SHIFT_RULE, LineFact(person_id="p1", task_state="incomplete"))
        self.assertEqual(line["total_amount_cny"], "0.00")
        self.assertEqual(line["deducted_amount_cny"], "80.00")

    def test_event_pricing_uses_valid_events(self) -> None:
        line = compute_line(EVENT_RULE, LineFact(
            person_id="p1", task_state="completed", valid_events=(7, 8),
        ))
        self.assertEqual(line["total_amount_cny"], "100.00")
        self.assertEqual(len(line["units"]), 2)

    def test_dedupe_window_keeps_first_checkin(self) -> None:
        events = [
            ReceiptFact(1, "p1", "checkpoint", "c1", "2026-09-10T01:10:00Z"),
            ReceiptFact(2, "p1", "checkpoint", "c1", "2026-09-10T01:20:00Z"),
            ReceiptFact(3, "p1", "checkpoint", "c1", "2026-09-10T02:00:00Z"),
        ]
        valid, duplicates = dedupe_checkins(events, 30)
        self.assertEqual(valid, (1, 3))
        self.assertEqual(duplicates[0]["duplicate_of_receipt_id"], 1)

    def test_dedupe_report_uses_fingerprint_not_time_window(self) -> None:
        events = [
            ReceiptFact(1, "p1", "report", None, "2026-09-10T03:00:00Z", fingerprint="fp-a"),
            ReceiptFact(2, "p1", "report", None, "2026-09-10T03:05:00Z", fingerprint="fp-a"),
            ReceiptFact(3, "p1", "report", None, "2026-09-10T03:06:00Z", fingerprint="fp-b"),
        ]
        valid, duplicates = dedupe_checkins(events, 30)
        self.assertEqual(valid, (1, 3))
        self.assertEqual(len(duplicates), 1)

    def test_blockade_covers_window(self) -> None:
        self.assertTrue(blockade_covers(
            "2026-09-10T00:00:00Z", "2026-09-10T23:59:59Z",
            "2026-09-10T01:00:00Z", "2026-09-10T02:00:00Z",
        ))
        self.assertFalse(blockade_covers(
            "2026-09-11T00:00:00Z", "2026-09-11T23:59:59Z",
            "2026-09-10T01:00:00Z", "2026-09-10T02:00:00Z",
        ))
        # 开放结束的封路覆盖之后所有时间。
        self.assertTrue(blockade_covers(
            "2026-09-10T00:00:00Z", None,
            "2026-09-12T01:00:00Z", "2026-09-12T02:00:00Z",
        ))

    def test_money_rounds_half_up_to_cent(self) -> None:
        self.assertEqual(money(Decimal("1.005")), Decimal("1.01"))
        self.assertEqual(money(Decimal("1.004")), Decimal("1.00"))


if __name__ == "__main__":
    unittest.main()
