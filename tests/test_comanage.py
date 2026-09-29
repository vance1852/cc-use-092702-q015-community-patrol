from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from comanage_ops.api import JsonApplication
from comanage_ops.clock import FrozenClock
from comanage_ops.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from comanage_ops.service import ComanageService
from comanage_ops.storage import connect as connect_storage


def event(event_uuid, seq, task_id, person_id, event_type, client_clock, **extra):
    payload = {
        "event_uuid": event_uuid,
        "seq": seq,
        "task_id": task_id,
        "person_id": person_id,
        "event_type": event_type,
        "client_clock": client_clock,
    }
    payload.update(extra)
    return payload


class ComanageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc))
        self.service = ComanageService(self.connection, self.clock)
        for user_id, role, group in (
            ("station1", "station", None),
            ("village-a", "village", "cun-a"),
            ("village-b", "village", "cun-b"),
            ("finance1", "finance", None),
            ("auditor1", "auditor", None),
        ):
            self.service.create_user(user_id, user_id, role, group)
        self.service.register_device("station1", "SN-1", "终端", "cun-a")
        self.service.create_area("station1", {
            "area_id": "area-1", "name": "一区", "village_group_id": "cun-a",
            "geometry": [{"point_id": "p1"}, {"point_id": "p2"}],
        })
        self.service.register_qualification("station1", {"qualification_id": "q1", "person_id": "zhang", "kind": "fire_watch", "valid_from": "2026-01-01"})
        self.service.register_qualification("station1", {"qualification_id": "q2", "person_id": "chen", "kind": "fire_watch", "valid_from": "2026-01-01"})
        self.service.create_pricing_rule("finance1", {
            "rule_id": "r1", "kind": "fire_watch", "source": "planned", "basis": "per_checkpoint",
            "base_amount_cny": "20", "unit_amount_cny": "10", "reinforcement_multiplier": "1.5",
            "absolved_ratio": "0.5", "valid_from": "2026-01-01",
        })

    def tearDown(self) -> None:
        self.connection.close()

    def create_planned_task(self, task_id="t1"):
        return self.service.create_task("station1", {
            "task_id": task_id, "kind": "fire_watch", "source": "planned", "area_id": "area-1",
            "title": "瞭望", "planned_start": "2026-09-28T08:00:00Z",
            "checkpoints": [
                {"checkpoint_id": "c1", "label": "点1", "point_id": "p1", "ordinal": 1},
                {"checkpoint_id": "c2", "label": "点2", "point_id": "p2", "ordinal": 2},
            ],
        })

    def assign_and_confirm(self, task_id="t1", person="zhang", group="cun-a", confirmer="village-a"):
        self.service.propose_assignment("station1", task_id, person, group)
        return self.service.confirm_assignment(confirmer, task_id)

    def complete_task(self, task_id="t1", person="zhang", day="28", seq_base=0):
        return self.service.upload_events("village-a", "SN-1", [
            event(f"e-{task_id}-s", seq_base + 1, task_id, person, "started", f"2026-09-{day}T08:05:00Z"),
            event(f"e-{task_id}-c1", seq_base + 2, task_id, person, "checkpoint", f"2026-09-{day}T08:30:00Z", checkpoint_id="c1"),
            event(f"e-{task_id}-c2", seq_base + 3, task_id, person, "checkpoint", f"2026-09-{day}T09:30:00Z", checkpoint_id="c2"),
            event(f"e-{task_id}-d", seq_base + 4, task_id, person, "completed", f"2026-09-{day}T10:00:00Z"),
        ])

    # ----------------------------------------------------------- 基础数据与权限

    def test_checkpoint_must_lie_inside_area(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_task("station1", {
                "task_id": "tbad", "kind": "fire_watch", "source": "planned", "area_id": "area-1",
                "title": "越界", "planned_start": "2026-09-28T08:00:00Z",
                "checkpoints": [{"checkpoint_id": "cx", "label": "x", "point_id": "p-other", "ordinal": 1}],
            })

    def test_assignment_requires_qualification(self) -> None:
        self.create_planned_task()
        with self.assertRaises(Forbidden):
            self.service.propose_assignment("station1", "t1", "unqualified-person", "cun-a")

    def test_only_assigned_village_confirms(self) -> None:
        self.create_planned_task()
        self.service.propose_assignment("station1", "t1", "zhang", "cun-a")
        with self.assertRaises(Forbidden):
            self.service.confirm_assignment("village-b", "t1")
        self.service.confirm_assignment("village-a", "t1")

    def test_rejected_proposal_leaves_no_version(self) -> None:
        self.create_planned_task()
        self.service.propose_assignment("station1", "t1", "zhang", "cun-a")
        self.service.reject_assignment("village-a", "t1", "人手不足")
        versions = self.service.task_versions("t1")
        self.assertEqual(len(versions), 1)
        self.assertIsNone(versions[0]["assignee_person_id"])

    # ----------------------------------------------------------- 版本与转派

    def test_task_revisions_append_reason(self) -> None:
        self.create_planned_task()
        self.service.revise_task_definition("station1", "t1", {"title": "改名后的瞭望"}, "调度调整")
        versions = self.service.task_versions("t1")
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertEqual(versions[1]["change_reason"], "调度调整")
        self.assertEqual(versions[1]["supersedes_version"], 1)

    def test_transfer_needs_both_parties_and_blocks_started_task(self) -> None:
        self.create_planned_task()
        self.assign_and_confirm()
        # 只一方确认不生效。
        self.service.propose_transfer("village-a", "tr1", "t1", "cun-b", "chen", "乙组接手")
        one = self.service.confirm_transfer("village-a", "tr1", "from")
        self.assertEqual(one["status"], "proposed")
        self.assertEqual(self.service.task("t1")["assignee_person_id"], "zhang")
        done = self.service.confirm_transfer("village-b", "tr1", "to")
        self.assertEqual(done["status"], "accepted")
        self.assertEqual(self.service.task("t1")["assignee_person_id"], "chen")
        self.assertEqual(self.service.task("t1")["assignee_village_group_id"], "cun-b")
        # 已经开始的第二个任务不能再转派。
        self.create_planned_task("t2")
        self.assign_and_confirm("t2", person="chen", group="cun-b", confirmer="village-b")
        self.service.upload_events("village-b", "SN-1", [
            event("e-t2-s", 10, "t2", "chen", "started", "2026-09-28T11:00:00Z"),
        ])
        with self.assertRaises(InvalidState):
            self.service.propose_transfer("village-b", "tr2", "t2", "cun-a", "zhang", "换回甲组")

    def test_transfer_wrong_party_cannot_confirm(self) -> None:
        self.create_planned_task()
        self.assign_and_confirm()
        self.service.propose_transfer("village-a", "tr1", "t1", "cun-b", "chen", "乙组接手")
        with self.assertRaises(Forbidden):
            self.service.confirm_transfer("village-b", "tr1", "from")

    def test_stale_assignee_events_rejected_after_silent_swap_attempt(self) -> None:
        self.create_planned_task()
        self.assign_and_confirm()
        self.service.propose_transfer("village-a", "tr1", "t1", "cun-b", "chen", "乙组接手")
        self.service.confirm_transfer("village-a", "tr1", "from")
        self.service.confirm_transfer("village-b", "tr1", "to")
        result = self.service.upload_events("village-a", "SN-1", [
            event("e-stale", 20, "t1", "zhang", "started", "2026-09-28T08:10:00Z"),
        ])
        self.assertEqual(result["accepted_count"], 0)
        self.assertEqual(result["rejected_count"], 1)
        self.assertIn("受托人", result["rejected"][0]["reason"])

    # ----------------------------------------------------------- 离线补传

    def test_replay_and_fork_detection(self) -> None:
        self.create_planned_task()
        self.assign_and_confirm()
        first = self.service.upload_events("village-a", "SN-1", [
            event("u1", 1, "t1", "zhang", "started", "2026-09-28T08:05:00Z"),
        ])
        self.assertEqual(first["accepted_count"], 1)
        replay = self.service.upload_events("village-a", "SN-1", [
            event("u1", 1, "t1", "zhang", "started", "2026-09-28T08:05:00Z"),
        ])
        self.assertEqual(replay["duplicate_count"], 1)
        self.assertEqual(replay["accepted_count"], 0)
        with self.assertRaises(Conflict):
            self.service.upload_events("village-a", "SN-1", [
                event("u-fork", 1, "t1", "zhang", "started", "2026-09-28T08:06:00Z"),
            ])

    def test_duplicate_checkin_only_first_counts(self) -> None:
        self.create_planned_task()
        self.assign_and_confirm()
        result = self.service.upload_events("village-a", "SN-1", [
            event("u1", 1, "t1", "zhang", "started", "2026-09-28T08:05:00Z"),
            event("u2", 2, "t1", "zhang", "checkpoint", "2026-09-28T08:20:00Z", checkpoint_id="c1"),
            event("u3", 3, "t1", "zhang", "checkpoint", "2026-09-28T08:21:00Z", checkpoint_id="c1"),
        ])
        self.assertEqual(result["accepted_count"], 2)
        self.assertEqual(result["rejected"][0]["reason"], "duplicate_checkin")

    def test_event_decisions_are_append_only(self) -> None:
        self.create_planned_task()
        self.assign_and_confirm()
        self.service.upload_events("village-a", "SN-1", [
            event("u1", 1, "t1", "zhang", "started", "2026-09-28T08:05:00Z"),
        ])
        self.service.decide_event("station1", "u1", "void", "签到时间存疑")
        self.service.decide_event("station1", "u1", "reinstate", "核验影像有效")
        actions = [row[0] for row in self.connection.execute(
            "SELECT action FROM event_decisions WHERE event_uuid='u1' ORDER BY decision_id"
        ).fetchall()]
        self.assertEqual(actions, ["void", "reinstate"])
        state = self.connection.execute(
            "SELECT validity_state FROM task_events WHERE event_uuid='u1'"
        ).fetchone()[0]
        self.assertEqual(state, "accepted")

    def test_absolved_by_road_closure(self) -> None:
        self.create_planned_task()
        self.assign_and_confirm()
        self.service.upload_events("village-a", "SN-1", [
            event("u1", 1, "t1", "zhang", "started", "2026-09-28T08:05:00Z"),
            event("u2", 2, "t1", "zhang", "absolved", "2026-09-28T09:00:00Z",
                  absolved_reason="road_closed", note="封路撤离"),
        ])
        self.assertEqual(self.service.task("t1")["lifecycle_state"], "absolved")

    # ----------------------------------------------------------- 结算、争议与支付

    def _generate(self):
        return self.service.generate_settlement("station1", "s1", "2026-09-01", "2026-09-30")

    def test_draft_is_recomputable_and_idempotent(self) -> None:
        self.create_planned_task()
        self.assign_and_confirm()
        self.complete_task()
        draft = self._generate()
        self.assertEqual(draft["items"], 1)
        again = self.service.generate_settlement("station1", "other-id", "2026-09-01", "2026-09-30")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["settlement_id"], "s1")
        explanation = self.service.explain_compensation("auditor1", "s1", "t1")
        self.assertTrue(explanation["matches_draft"])
        # 20 基础 + 2 检查点 × 10 = 40.00
        self.assertEqual(explanation["component"]["amount_cny"], "40.00")
        self.assertEqual(
            explanation["component"]["effective_event_uuids"],
            ["e-t1-c1", "e-t1-c2"],
        )

    def test_three_parties_each_confirm_own_part(self) -> None:
        self.create_planned_task()
        self.assign_and_confirm()
        self.complete_task()
        self._generate()
        # 财务不能替村组确认。
        with self.assertRaises(Forbidden):
            self.service.confirm_settlement("finance1", "s1", "village")
        self.service.confirm_settlement("village-a", "s1", "village")
        self.service.confirm_settlement("station1", "s1", "station")
        # 只两方确认时不进支付清单。
        self.assertEqual(self.service.payment_list("finance1", "s1")["items"], [])
        self.service.confirm_settlement("finance1", "s1", "finance")
        self.assertEqual(len(self.service.payment_list("finance1", "s1")["items"]), 1)

    def test_dispute_freezes_only_disputed_item(self) -> None:
        self.create_planned_task("t1")
        self.create_planned_task("t2")
        self.assign_and_confirm("t1")
        self.assign_and_confirm("t2")
        self.complete_task("t1")
        self.complete_task("t2", seq_base=10)
        self._generate()
        for actor, party in (("village-a", "village"), ("station1", "station"), ("finance1", "finance")):
            self.service.confirm_settlement(actor, "s1", party)
        self.service.raise_dispute("finance1", "s1", "t1", "金额异议")
        detail = self.service.settlement("auditor1", "s1")
        states = {item["task_id"]: item["item_state"] for item in detail["items"]}
        self.assertEqual(states, {"t1": "frozen", "t2": "confirmed"})
        payment = {item["task_id"] for item in self.service.payment_list("finance1", "s1")["items"]}
        self.assertEqual(payment, {"t2"})
        self.service.mark_paid("finance1", "s1")
        # 已支付的 t2 不受争议处理影响。
        self.clock.advance(days=1)
        self.service.resolve_dispute("station1", "disp-s1-t1-finance", "复核后维持原金额")
        detail2 = self.service.settlement("auditor1", "s1")
        states2 = {item["task_id"]: item["item_state"] for item in detail2["items"]}
        self.assertEqual(states2, {"t1": "draft", "t2": "paid"})
        # t1 需要三方在解冻后重新确认才回到支付清单。
        for actor, party in (("village-a", "village"), ("station1", "station"), ("finance1", "finance")):
            self.service.confirm_settlement(actor, "s1", party)
        payment2 = {item["task_id"] for item in self.service.payment_list("finance1", "s1")["items"]}
        self.assertEqual(payment2, {"t1", "t2"})

    def test_open_disputes_survive_restart(self) -> None:
        self.create_planned_task()
        self.assign_and_confirm()
        self.complete_task()
        self._generate()
        self.service.raise_dispute("village-a", "s1", "t1", "村组认为漏算检查点")
        restarted = ComanageService(self.connection, self.clock)
        opened = restarted.open_disputes("auditor1")["open_disputes"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["task_id"], "t1")

    def test_open_disputes_survive_process_restart_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "comanage.sqlite3"
            connection = connect_storage(db_path)
            service = ComanageService(connection, self.clock)
            for user_id, role, group in (
                ("st", "station", None), ("va", "village", "cun-a"),
                ("fi", "finance", None), ("au", "auditor", None),
            ):
                service.create_user(user_id, user_id, role, group)
            service.register_device("st", "SN-D", "终端", "cun-a")
            service.create_area("st", {
                "area_id": "a1", "name": "一区", "village_group_id": "cun-a",
                "geometry": [{"point_id": "p1"}],
            })
            service.register_qualification("st", {"qualification_id": "q", "person_id": "zhang", "kind": "fire_watch", "valid_from": "2026-01-01"})
            service.create_pricing_rule("fi", {
                "rule_id": "r", "kind": "fire_watch", "source": "planned", "basis": "fixed",
                "base_amount_cny": "50", "unit_amount_cny": "0", "reinforcement_multiplier": "1",
                "absolved_ratio": "0.5", "valid_from": "2026-01-01",
            })
            service.create_task("st", {
                "task_id": "t1", "kind": "fire_watch", "area_id": "a1", "title": "瞭望",
                "planned_start": "2026-09-28T08:00:00Z",
                "checkpoints": [{"checkpoint_id": "c1", "label": "点", "point_id": "p1", "ordinal": 1}],
            })
            service.propose_assignment("st", "t1", "zhang", "cun-a")
            service.confirm_assignment("va", "t1")
            service.upload_events("va", "SN-D", [
                event("u1", 1, "t1", "zhang", "started", "2026-09-28T08:05:00Z"),
                event("u2", 2, "t1", "zhang", "completed", "2026-09-28T09:00:00Z"),
            ])
            service.generate_settlement("st", "s1", "2026-09-01", "2026-09-30")
            service.raise_dispute("fi", "s1", "t1", "财务存疑")
            connection.close()
            # 模拟进程重启：重新打开同一数据库文件。
            reopened = connect_storage(db_path)
            restarted = ComanageService(reopened, self.clock)
            opened = restarted.open_disputes("au")["open_disputes"]
            self.assertEqual([item["task_id"] for item in opened], ["t1"])
            detail = restarted.settlement("au", "s1")
            self.assertEqual(detail["items"][0]["item_state"], "frozen")
            self.assertTrue(restarted.audit_chain("au")["valid"])
            reopened.close()

    def test_settlement_decisions_append_only(self) -> None:
        self.create_planned_task()
        self.assign_and_confirm()
        self.complete_task()
        self._generate()
        types_before = self.connection.execute(
            "SELECT count(*) FROM settlement_decisions WHERE settlement_id='s1'"
        ).fetchone()[0]
        self.service.raise_dispute("finance1", "s1", "t1", "异议")
        self.service.resolve_dispute("station1", "disp-s1-t1-finance", "处理")
        logs = [row[0] for row in self.connection.execute(
            "SELECT decision_type FROM settlement_decisions WHERE settlement_id='s1' ORDER BY decision_id"
        ).fetchall()]
        self.assertEqual(logs, ["draft_generated", "item.frozen", "item.adjusted"])
        self.assertGreater(len(logs), types_before)


class ComanageAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        from comanage_ops.acceptance import run

        result = run(Path(__file__).resolve().parents[1])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["draft_items"], 3)
        self.assertEqual(result["audit"]["valid"], True)
        self.assertEqual(result["resolved_amount_cny"], "15.00")


class ComanageApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(ComanageService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def test_health_and_user(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").body["status"], "ok")
        response = self.app.handle(
            "POST", "/users",
            headers={"X-Actor-Id": "s1"},
            body=json.dumps({"user_id": "s1", "display_name": "保护站", "role": "station"}).encode("utf-8"),
        )
        self.assertEqual(response.status, 201)

    def test_missing_actor_header(self) -> None:
        response = self.app.handle("POST", "/service_areas", body=b"{}")
        self.assertEqual(response.status, 422)


if __name__ == "__main__":
    unittest.main()
