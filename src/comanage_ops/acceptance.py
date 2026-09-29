"""贯通共管任务、离线回执、跨村组转派、周期结算与争议的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import Conflict, InvalidState
from .service import ComanageService


def _event(event_uuid, seq, task_id, person_id, event_type, client_clock, **extra):
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


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc))
    service = ComanageService(connection, clock)

    # 用户：保护站、两个村组、财务、审计。
    for user_id, role, group in (
        ("station-li", "station", None),
        ("village-a", "village", "cun-a"),
        ("village-b", "village", "cun-b"),
        ("finance-zhao", "finance", None),
        ("auditor-wang", "auditor", None),
    ):
        service.create_user(user_id, user_id, role, group)

    # 基础数据：离线设备、服务区域、人员资格、计价规则。
    service.register_device("station-li", "DEV-SN-0001", "巡护终端A1", "cun-a")
    service.create_area("station-li", {
        "area_id": "area-ridge-1", "name": "北岭共同管护区", "village_group_id": "cun-a",
        "geometry": [{"point_id": "p-gate"}, {"point_id": "p-tower"}, {"point_id": "p-pond"}],
    })
    service.register_qualification("station-li", {"qualification_id": "q-zhang-1", "person_id": "zhang", "kind": "fire_watch", "valid_from": "2026-01-01"})
    service.register_qualification("station-li", {"qualification_id": "q-chen-1", "person_id": "chen", "kind": "fire_watch", "valid_from": "2026-01-01"})
    service.register_qualification("station-li", {"qualification_id": "q-luo-1", "person_id": "luo", "kind": "waste_haul", "valid_from": "2026-01-01"})
    service.create_pricing_rule("finance-zhao", {
        "rule_id": "price-fire", "kind": "fire_watch", "source": "planned", "basis": "per_checkpoint",
        "base_amount_cny": "20.00", "unit_amount_cny": "15.00", "reinforcement_multiplier": "1.5",
        "absolved_ratio": "0.5", "valid_from": "2026-01-01",
    })
    service.create_pricing_rule("finance-zhao", {
        "rule_id": "price-fire-rein", "kind": "fire_watch", "source": "reinforcement", "basis": "per_checkpoint",
        "base_amount_cny": "20.00", "unit_amount_cny": "15.00", "reinforcement_multiplier": "1.5",
        "absolved_ratio": "0.5", "valid_from": "2026-01-01",
    })
    service.create_pricing_rule("finance-zhao", {
        "rule_id": "price-waste", "kind": "waste_haul", "source": "planned", "basis": "per_kilogram",
        "base_amount_cny": "10.00", "unit_amount_cny": "0.80", "reinforcement_multiplier": "1.2",
        "absolved_ratio": "0.4", "valid_from": "2026-01-01",
    })

    # 计划任务：北岭瞭望。临时增援：第二处瞭望。
    service.create_task("station-li", {
        "task_id": "task-fire-01", "kind": "fire_watch", "source": "planned", "area_id": "area-ridge-1",
        "title": "北岭瞭望台九月值守", "planned_start": "2026-09-28T08:00:00Z",
        "checkpoints": [
            {"checkpoint_id": "cp-1", "label": "山门", "point_id": "p-gate", "ordinal": 1},
            {"checkpoint_id": "cp-2", "label": "瞭望塔", "point_id": "p-tower", "ordinal": 2},
            {"checkpoint_id": "cp-3", "label": "水塘", "point_id": "p-pond", "ordinal": 3},
        ],
    })
    service.create_task("station-li", {
        "task_id": "task-fire-02", "kind": "fire_watch", "source": "reinforcement", "area_id": "area-ridge-1",
        "title": "高火险临时增援瞭望", "planned_start": "2026-09-28T09:00:00Z",
        "checkpoints": [
            {"checkpoint_id": "cp-x1", "label": "山门", "point_id": "p-gate", "ordinal": 1},
            {"checkpoint_id": "cp-x2", "label": "瞭望塔", "point_id": "p-tower", "ordinal": 2},
        ],
    })
    service.create_task("station-li", {
        "task_id": "task-waste-01", "kind": "waste_haul", "source": "planned", "area_id": "area-ridge-1",
        "title": "九月底垃圾清运", "planned_start": "2026-09-29T06:00:00Z",
        "checkpoints": [{"checkpoint_id": "cp-w1", "label": "垃圾集中点", "point_id": "p-pond", "ordinal": 1}],
    })

    # 派单需受托村组确认。
    service.propose_assignment("station-li", "task-fire-01", "zhang", "cun-a", "常规排班")
    service.confirm_assignment("village-a", "task-fire-01")
    service.propose_assignment("station-li", "task-fire-02", "zhang", "cun-a", "高火险增援")
    service.confirm_assignment("village-a", "task-fire-02")
    service.propose_assignment("station-li", "task-waste-01", "luo", "cun-a", "月底清运")
    service.confirm_assignment("village-a", "task-waste-01")

    # 离线设备补传：开始、重复签到、检查点、完成。
    first_upload = service.upload_events("village-a", "DEV-SN-0001", [
        _event("evt-1", 1, "task-fire-01", "zhang", "started", "2026-09-28T08:05:00Z"),
        _event("evt-2", 2, "task-fire-01", "zhang", "checkpoint", "2026-09-28T08:20:00Z", checkpoint_id="cp-1"),
        _event("evt-3", 3, "task-fire-01", "zhang", "checkpoint", "2026-09-28T08:20:30Z", checkpoint_id="cp-1"),
        _event("evt-4", 4, "task-fire-01", "zhang", "checkpoint", "2026-09-28T09:10:00Z", checkpoint_id="cp-2"),
    ])
    assert first_upload["accepted_count"] == 3, first_upload
    assert first_upload["rejected_count"] == 1, first_upload
    assert first_upload["rejected"][0]["reason"] == "duplicate_checkin"

    # 重放：整批再次补传，全部按 UUID 识别为重复，不产生新事件。
    replay = service.upload_events("village-a", "DEV-SN-0001", [
        _event("evt-1", 1, "task-fire-01", "zhang", "started", "2026-09-28T08:05:00Z"),
        _event("evt-2", 2, "task-fire-01", "zhang", "checkpoint", "2026-09-28T08:20:00Z", checkpoint_id="cp-1"),
    ])
    assert replay["accepted_count"] == 0 and replay["duplicate_count"] == 2, replay

    # 分叉：同一设备序号 seq=2 对应不同 UUID，必须拒绝整批。
    try:
        service.upload_events("village-a", "DEV-SN-0001", [
            _event("evt-fork", 2, "task-fire-01", "zhang", "checkpoint", "2026-09-28T08:21:00Z", checkpoint_id="cp-2"),
        ])
        raise AssertionError("分叉补传应当被拒绝")
    except Conflict as exc:
        assert "分叉" in str(exc)

    # 完成第一个任务；增援任务开始后尝试静默换人——发起转派即被拒绝。
    service.upload_events("village-a", "DEV-SN-0001", [
        _event("evt-5", 5, "task-fire-01", "zhang", "checkpoint", "2026-09-28T10:00:00Z", checkpoint_id="cp-3"),
        _event("evt-6", 6, "task-fire-01", "zhang", "completed", "2026-09-28T10:30:00Z"),
        _event("evt-7", 7, "task-fire-02", "zhang", "started", "2026-09-28T09:05:00Z"),
    ])
    try:
        service.propose_transfer("village-a", "tr-001", "task-fire-02", "cun-b", "chen", "甲组被抽去抢修")
        raise AssertionError("已开始任务不能转派")
    except InvalidState as exc:
        assert "已经开始" in str(exc)

    # 未开始的垃圾清运任务演示跨村组双方确认转派给乙组具备资格的新人员。
    service.register_qualification("station-li", {"qualification_id": "q-han-1", "person_id": "han", "kind": "waste_haul", "valid_from": "2026-01-01"})
    service.propose_transfer("village-a", "tr-002", "task-waste-01", "cun-b", "han", "乙组顺路")
    after_from = service.confirm_transfer("village-a", "tr-002", "from")
    assert after_from["status"] == "proposed"
    transfer_done = service.confirm_transfer("village-b", "tr-002", "to")
    assert transfer_done["status"] == "accepted" and transfer_done["task_version"] == 3
    # 后台静默换人（旧受托人继续上传）被拒。
    stale = service.upload_events("village-a", "DEV-SN-0001", [
        _event("evt-stale", 90, "task-waste-01", "luo", "started", "2026-09-29T06:05:00Z"),
    ])
    assert stale["rejected_count"] == 1 and "受托人" in stale["rejected"][0]["reason"]

    # 新受托人完成清运，32.5kg。
    service.register_device("station-li", "DEV-SN-0002", "巡护终端B1", "cun-b")
    service.upload_events("village-b", "DEV-SN-0002", [
        _event("evt-w-1", 1, "task-waste-01", "han", "started", "2026-09-29T06:10:00Z"),
        _event("evt-w-2", 2, "task-waste-01", "han", "checkpoint", "2026-09-29T07:30:00Z", checkpoint_id="cp-w1", quantity="32.5"),
        _event("evt-w-3", 3, "task-waste-01", "han", "completed", "2026-09-29T07:45:00Z", quantity="32.5"),
    ])

    # 增援任务因封路未完成，登记合理免责。
    service.upload_events("village-a", "DEV-SN-0001", [
        _event("evt-8", 8, "task-fire-02", "zhang", "absolved", "2026-09-28T11:00:00Z",
               absolved_reason="road_closed", note="林区道路塌方封闭，撤离"),
    ])

    # 周期结算：生成可复算草案。
    draft = service.generate_settlement("station-li", "set-2026-09", "2026-09-01", "2026-09-30")
    assert draft["items"] == 3, draft
    replay_draft = service.generate_settlement("station-li", "set-other", "2026-09-01", "2026-09-30")
    assert replay_draft["replayed"] is True and replay_draft["settlement_id"] == "set-2026-09"

    # 村组、保护站、财务分别确认；对增援免责金额提出争议，只冻结该任务。
    service.confirm_settlement("village-a", "set-2026-09", "village", "甲组核对无误")
    service.confirm_settlement("village-b", "set-2026-09", "village", "乙组核对无误")
    service.confirm_settlement("station-li", "set-2026-09", "station", "保护站复核")
    dispute = service.raise_dispute("finance-zhao", "set-2026-09", "task-fire-02", "增援免责计价需要复核封路记录")
    assert dispute["item_state"] == "frozen"
    service.confirm_settlement("finance-zhao", "set-2026-09", "finance", "无争议行同意支付")

    detail = service.settlement("auditor-wang", "set-2026-09")
    states = {item["task_id"]: item["item_state"] for item in detail["items"]}
    assert states == {"task-fire-01": "confirmed", "task-fire-02": "frozen", "task-waste-01": "confirmed"}, states

    payment = service.payment_list("finance-zhao", "set-2026-09")
    paid_tasks = {item["task_id"] for item in payment["items"]}
    assert paid_tasks == {"task-fire-01", "task-waste-01"}, paid_tasks

    # 无争议部分进入支付。
    paid = service.mark_paid("finance-zhao", "set-2026-09")
    assert paid["paid_rows"] == 2, paid

    # 重启恢复：用同一数据库新建服务实例，未结争议仍可列出并处理。
    clock.advance(hours=2)
    restarted = ComanageService(connection, clock)
    open_disputes = restarted.open_disputes("auditor-wang")
    assert [item["task_id"] for item in open_disputes["open_disputes"]] == ["task-fire-02"]

    # 争议解决：保护站补传封路证据后维持免责比例，重算并解冻，三方重新确认该单行。
    resolve = restarted.resolve_dispute("station-li", "disp-set-2026-09-task-fire-02-finance", "封路告警与影像核实，免责成立")
    assert resolve["status"] == "resolved"
    # 旧确认早于解冻时间：该行仍为草案，不会静默进入支付。
    detail2 = restarted.settlement("auditor-wang", "set-2026-09")
    states2 = {item["task_id"]: item["item_state"] for item in detail2["items"]}
    assert states2["task-fire-02"] == "draft" and states2["task-fire-01"] == "paid", states2
    restarted.confirm_settlement("village-a", "set-2026-09", "village", "认可复核结果")
    restarted.confirm_settlement("station-li", "set-2026-09", "station", "证据齐")
    restarted.confirm_settlement("finance-zhao", "set-2026-09", "finance", "同意支付免责补偿")
    payment2 = restarted.payment_list("finance-zhao", "set-2026-09")
    assert len(payment2["items"]) == 3, payment2
    restarted.mark_paid("finance-zhao", "set-2026-09")

    # 逐笔解释补偿组成，并与现场重算比对。
    explanation = restarted.explain_compensation("auditor-wang", "set-2026-09", "task-fire-01")
    assert explanation["matches_draft"] is True
    assert set(explanation["component"]["effective_event_uuids"]) == {"evt-2", "evt-4", "evt-5"}
    assert explanation["component"]["amount_cny"] == "65.00", explanation["component"]
    waste = restarted.explain_compensation("auditor-wang", "set-2026-09", "task-waste-01")
    assert waste["component"]["amount_cny"] == "36.00", waste["component"]
    absolved = restarted.explain_compensation("auditor-wang", "set-2026-09", "task-fire-02")
    # base 20 × 免责比例 0.5 × 增援倍数 1.5 = 15.00
    assert absolved["component"]["amount_cny"] == "15.00", absolved["component"]

    versions = restarted.task_versions("task-waste-01")
    assert [(v["version"], v["assignee_person_id"], v["assignee_village_group_id"]) for v in versions] == [
        (1, None, None), (2, "luo", "cun-a"), (3, "han", "cun-b"),
    ]
    audit = restarted.audit_chain("auditor-wang")
    assert audit["valid"] is True and audit["events"] > 0

    result = {
        "status": "ok",
        "workspace": workspace.name,
        "draft_items": draft["items"],
        "draft_total_cny": draft["total_amount_cny"],
        "input_sha256": draft["input_sha256"],
        "first_upload": first_upload,
        "replay": replay,
        "transfer": transfer_done,
        "dispute_frozen": dispute,
        "open_after_restart": open_disputes,
        "resolved_amount_cny": resolve["amount_cny"],
        "payment_total_cny": payment2["total_amount_cny"],
        "explanation": {
            "task-fire-01": explanation["component"],
            "task-waste-01": waste["component"],
            "task-fire-02": absolved["component"],
        },
        "task_versions": versions,
        "audit": audit,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行共管任务与补偿核算服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
