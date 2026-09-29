from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from comanagement.clock import FrozenClock
from comanagement.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from comanagement.service import ComanagementService
from comanagement.storage import connect


class ComanagementServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc))
        self.service = ComanagementService(self.connection, self.clock)
        self._seed()

    def tearDown(self) -> None:
        self.connection.close()

    def _seed(self) -> None:
        service = self.service
        service.create_user("station1", "保护站", "station")
        service.create_user("fin1", "财务", "finance")
        service.create_user("audit1", "审计", "auditor")
        service.create_organization("station1", {"org_id": "vg-east", "name": "东坡", "kind": "village_group"})
        service.create_organization("station1", {"org_id": "vg-west", "name": "西坡", "kind": "village_group"})
        for uid, name, role, org in (
            ("head-east", "东坡组长", "village_head", "vg-east"),
            ("head-west", "西坡组长", "village_head", "vg-west"),
            ("v-a", "甲", "villager", "vg-east"),
            ("v-b", "乙", "villager", "vg-east"),
            ("v-w", "丙", "villager", "vg-west"),
        ):
            service.create_user(uid, name, role, org)
        service.create_service_area("station1", {
            "service_area_id": "area-1", "name": "一区", "geometry": [{"lat": 1, "lng": 2}],
        })
        for cp in ("cp-1", "cp-2"):
            service.create_checkpoint("station1", {
                "checkpoint_id": cp, "service_area_id": "area-1", "name": cp, "position": {},
            })
        for person, family in (("v-a", "fire_lookout"), ("v-b", "fire_lookout"), ("v-w", "fire_lookout")):
            service.grant_qualification("station1", {
                "person_id": person, "task_family": family, "level": "L1",
                "valid_from": "2026-01-01", "valid_until": "2026-12-31",
            })
        service.publish_pricing_rule("station1", {
            "rule_id": "price-fire", "task_family": "fire_lookout", "task_kind": "planned",
            "base_unit": "checkpoint", "base_rate_cny": "30.00", "reinforcement_rate_cny": "0",
            "repeat_within_minutes": 30, "road_closure_excuse_code": "ROAD_CLOSED",
        })
        service.register_device("station1", {"device_serial": "dev-a", "person_id": "v-a", "model_name": "A"})
        service.register_device("station1", {"device_serial": "dev-w", "person_id": "v-w", "model_name": "W"})

    def _fire_task(self, task_id: str = "t-1", people=("v-a",)) -> dict:
        return self.service.create_task("head-east", {
            "task_id": task_id, "task_family": "fire_lookout", "task_kind": "planned",
            "service_area_id": "area-1", "scheduled_for": "2026-09-10T01:00:00Z",
            "person_ids": list(people), "pricing_rule_id": "price-fire",
            "idempotency_key": f"{task_id}-key",
            "checkpoints": [{"checkpoint_id": "cp-1", "required": True}, {"checkpoint_id": "cp-2", "required": True}],
        })

    def test_task_versions_are_append_only(self) -> None:
        self._fire_task()
        revised = self.service.revise_task("head-east", "t-1", {
            "task_family": "fire_lookout", "task_kind": "planned", "service_area_id": "area-1",
            "scheduled_for": "2026-09-10T02:00:00Z", "person_ids": ["v-a", "v-b"],
            "pricing_rule_id": "price-fire", "idempotency_key": "t-1-key2",
            "checkpoints": [{"checkpoint_id": "cp-1", "required": True}],
        }, "调整值守时段与人员", 1)
        self.assertEqual(revised["version"], 2)
        detail = self.service.task("t-1")
        self.assertEqual([row["version"] for row in detail["versions"]], [1, 2])
        self.assertEqual(detail["task"]["current_version"], 2)

    def test_started_task_cannot_be_silently_revised_or_transferred(self) -> None:
        self._fire_task()
        self.service.start_task("station1", "t-1")
        with self.assertRaises(InvalidState):
            self.service.revise_task("head-east", "t-1", {
                "task_family": "fire_lookout", "task_kind": "planned", "service_area_id": "area-1",
                "scheduled_for": "2026-09-10T03:00:00Z", "person_ids": ["v-b"],
                "pricing_rule_id": "price-fire", "idempotency_key": "t-1-key2",
                "checkpoints": [{"checkpoint_id": "cp-1", "required": True}],
            }, "想偷偷换人", 1)
        with self.assertRaises(InvalidState):
            self.service.propose_transfer("head-east", "t-1", "vg-west", ["v-w"], "已开始不能转")

    def test_unqualified_person_cannot_be_assigned(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_task("head-east", {
                "task_id": "t-bad", "task_family": "fire_lookout", "task_kind": "planned",
                "service_area_id": "area-1", "scheduled_for": "2026-09-10T01:00:00Z",
                "person_ids": ["head-east"], "pricing_rule_id": "price-fire",
                "idempotency_key": "t-bad-key", "checkpoints": [],
            })

    def test_transfer_requires_receiver_confirmation(self) -> None:
        self._fire_task(task_id="t-2")
        self.service.propose_transfer("head-east", "t-2", "vg-west", ["v-w"], "请西坡接替")
        # 非接收村组不能确认。
        with self.assertRaises(Forbidden):
            self.service.decide_transfer("head-east", "tr-t-2-1", True)
        decided = self.service.decide_transfer("head-west", "tr-t-2-1", True)
        self.assertEqual(decided["task_version"], 2)
        detail = self.service.task("t-2")
        self.assertEqual(detail["task"]["responsible_org_id"], "vg-west")
        v2_people = [row["person_id"] for row in detail["assignments"] if row["version"] == 2]
        self.assertEqual(v2_people, ["v-w"])
        # 旧版本人员在新版本上的回执被拒绝，不能被静默保留。
        with self.assertRaises(ValidationFailed):
            self.service.upload_receipts("v-a", "dev-a", [{
                "sequence_no": 1, "task_id": "t-2", "receipt_type": "sign_on", "person_id": "v-a",
                "occurred_at": "2026-09-10T01:00:00Z", "payload": {},
            }])

    def test_rejected_transfer_keeps_original_assignment(self) -> None:
        self._fire_task(task_id="t-3")
        self.service.propose_transfer("head-east", "t-3", "vg-west", ["v-w"], "试转")
        result = self.service.decide_transfer("head-west", "tr-t-3-1", False, "人手也紧张")
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(self.service.task("t-3")["task"]["responsible_org_id"], "vg-east")

    def test_receipt_replay_is_idempotent_and_fork_is_rejected(self) -> None:
        self._fire_task()
        receipt = {
            "sequence_no": 1, "task_id": "t-1", "receipt_type": "checkpoint", "checkpoint_id": "cp-1",
            "person_id": "v-a", "occurred_at": "2026-09-10T01:10:00Z", "payload": {},
        }
        first = self.service.upload_receipts("v-a", "dev-a", [receipt])
        self.assertEqual(len(first["accepted_receipt_ids"]), 1)
        replay = self.service.upload_receipts("v-a", "dev-a", [receipt])
        self.assertEqual(replay["replayed_sequences"], [1])
        self.assertEqual(replay["accepted_receipt_ids"], [])
        forked = self.service.upload_receipts("v-a", "dev-a", [{
            "sequence_no": 1, "task_id": "t-1", "receipt_type": "checkpoint", "checkpoint_id": "cp-1",
            "person_id": "v-a", "occurred_at": "2026-09-10T01:11:00Z", "payload": {"x": 1},
        }])
        self.assertEqual(forked["forked_sequences"][0]["sequence_no"], 1)
        count = self.connection.execute("SELECT count(*) FROM receipts").fetchone()[0]
        self.assertEqual(count, 1)

    def test_sequence_gap_marked_then_filled(self) -> None:
        self._fire_task()
        gapped = self.service.upload_receipts("v-a", "dev-a", [{
            "sequence_no": 5, "task_id": "t-1", "receipt_type": "sign_on", "person_id": "v-a",
            "occurred_at": "2026-09-10T01:00:00Z", "payload": {},
        }])
        self.assertEqual(gapped["gapped_sequences"], [5])
        filled = self.service.upload_receipts("v-a", "dev-a", [{
            "sequence_no": 4, "task_id": "t-1", "receipt_type": "checkpoint", "checkpoint_id": "cp-1",
            "person_id": "v-a", "occurred_at": "2026-09-10T01:05:00Z", "payload": {},
        }])
        self.assertEqual(filled["gap_filled_sequences"], [4])
        self.assertEqual(self.connection.execute(
            "SELECT ingest_state FROM receipts WHERE sequence_no=5").fetchone()[0], "gapped")

    def test_pricing_rule_snapshot_keeps_old_period_recomputable(self) -> None:
        self._fire_task()
        self.service.upload_receipts("v-a", "dev-a", [
            {"sequence_no": 1, "task_id": "t-1", "receipt_type": "checkpoint", "checkpoint_id": "cp-1",
             "person_id": "v-a", "occurred_at": "2026-09-10T01:10:00Z", "payload": {}},
            {"sequence_no": 2, "task_id": "t-1", "receipt_type": "checkpoint", "checkpoint_id": "cp-2",
             "person_id": "v-a", "occurred_at": "2026-09-10T01:20:00Z", "payload": {}},
            {"sequence_no": 3, "task_id": "t-1", "receipt_type": "sign_on", "person_id": "v-a",
             "occurred_at": "2026-09-10T01:00:00Z", "payload": {}},
            {"sequence_no": 4, "task_id": "t-1", "receipt_type": "sign_off", "person_id": "v-a",
             "occurred_at": "2026-09-10T01:30:00Z", "payload": {}},
        ])
        self.service.open_period("station1", "p1", "2026-09-01", "2026-09-30")
        before = self.service.compose_settlement("station1", "p1")
        self.assertEqual(before["totals"]["total_amount_cny"], "60.00")
        # 新规则版本只影响之后创建的任务；历史任务仍按快照版本复算。
        self.service.publish_pricing_rule("station1", {
            "rule_id": "price-fire", "task_family": "fire_lookout", "task_kind": "planned",
            "base_unit": "checkpoint", "base_rate_cny": "99.00", "reinforcement_rate_cny": "0",
            "repeat_within_minutes": 30, "road_closure_excuse_code": "ROAD_CLOSED",
        })
        again = self.service.compose_settlement("fin1", "p1")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["totals"]["total_amount_cny"], "60.00")

    def test_dispute_freezes_only_its_line_and_undisputed_gets_paid(self) -> None:
        self._fire_task(task_id="t-a")
        self._fire_task(task_id="t-b")
        for task_id, device in (("t-a", "dev-a"),):
            self.service.upload_receipts("v-a", device, [
                {"sequence_no": 1, "task_id": task_id, "receipt_type": "sign_on", "person_id": "v-a",
                 "occurred_at": "2026-09-10T01:00:00Z", "payload": {}},
                {"sequence_no": 2, "task_id": task_id, "receipt_type": "checkpoint", "checkpoint_id": "cp-1",
                 "person_id": "v-a", "occurred_at": "2026-09-10T01:10:00Z", "payload": {}},
                {"sequence_no": 3, "task_id": task_id, "receipt_type": "checkpoint", "checkpoint_id": "cp-2",
                 "person_id": "v-a", "occurred_at": "2026-09-10T01:20:00Z", "payload": {}},
                {"sequence_no": 4, "task_id": task_id, "receipt_type": "sign_off", "person_id": "v-a",
                 "occurred_at": "2026-09-10T01:30:00Z", "payload": {}},
            ])
        # t-b 无任何回执，金额为 0，用于验证争议隔离不影响其他行。
        self.service.close_task("station1", "t-b", "incomplete")
        self.service.open_period("station1", "p1", "2026-09-01", "2026-09-30")
        draft = self.service.compose_settlement("station1", "p1")
        line_a = next(line for line in draft["lines"] if line["task_id"] == "t-a")
        line_b = next(line for line in draft["lines"] if line["task_id"] == "t-b")

        # 保护站对 t-a 有异议：只冻结 t-a。
        self.service.confirm_scope("station1", "p1", "station", [line_a["line_id"]], "disputed", "存疑")
        summary = self.service.period_summary("fin1", "p1")
        self.assertEqual(summary["frozen_line_ids"], [line_a["line_id"]])

        # 无争议的 t-b 三方照常确认并入支付清单。
        for actor, scope in (("station1", "station"), ("head-east", "village_head"), ("fin1", "finance")):
            self.service.confirm_scope(actor, "p1", scope, [line_b["line_id"]], "confirmed", "无异议")
        payment = self.service.finalize_payment("fin1", "p1")
        self.assertEqual(payment["new_payment_entries"], 1)
        self.assertEqual(payment["payment_list"][0]["line_id"], line_b["line_id"])

        # 裁定以追加决定留痕并解冻，之后该行才能支付。
        open_disputes = self.service.recover_open_disputes("audit1")["open_disputes"]
        self.assertEqual(len(open_disputes), 1)
        self.service.resolve_dispute("fin1", open_disputes[0]["dispute_id"], "resolved", "0", "核对轨迹后维持原金额")
        for actor, scope in (("station1", "station"), ("head-east", "village_head"), ("fin1", "finance")):
            self.service.confirm_scope(actor, "p1", scope, [line_a["line_id"]], "confirmed", "按裁定")
        final = self.service.finalize_payment("fin1", "p1")
        self.assertEqual(final["state"], "paid")
        explained = self.service.explain_line("audit1", line_a["line_id"])
        self.assertEqual(explained["adjustment_decisions"][0]["reason"], "核对轨迹后维持原金额")

    def test_open_disputes_survive_restart(self) -> None:
        self._fire_task(task_id="t-restart")
        self.service.close_task("station1", "t-restart", "incomplete")
        self.service.open_period("station1", "p1", "2026-09-01", "2026-09-30")
        draft = self.service.compose_settlement("station1", "p1")
        line_id = draft["lines"][0]["line_id"]
        self.service.confirm_scope("station1", "p1", "station", [line_id], "disputed", "待核")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            file_connection = sqlite3.connect(str(path))
            self.connection.backup(file_connection)
            file_connection.close()
            restarted_connection = connect(path)
            try:
                service = ComanagementService(restarted_connection, self.clock)
                recovered = service.recover_open_disputes("audit1")
                self.assertEqual(len(recovered["open_disputes"]), 1)
                self.assertEqual(recovered["open_disputes"][0]["task_id"], "t-restart")
            finally:
                restarted_connection.close()

    def test_audit_chain_detects_tampering(self) -> None:
        self._fire_task()
        self.assertTrue(self.service.audit_chain("audit1")["valid"])
        self.connection.execute("UPDATE comanage_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit1")["valid"])


if __name__ == "__main__":
    unittest.main()
