"""贯通社区共管任务、离线回执与三方补偿结算的离线验收。

不访问外部网络；为演示“重启后恢复未结争议”，使用临时目录中的 SQLite 文件
并在结算中途关闭后重新打开连接。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import ComanagementService
from .storage import connect, inspect_schema


def _build_service(database: Path, start: datetime) -> tuple[ComanagementService, FrozenClock]:
    connection = connect(database)
    clock = FrozenClock(start)
    return ComanagementService(connection, clock), clock


def run(workspace: Path) -> dict[str, object]:
    start = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "comanagement_acceptance.sqlite3"
        service, clock = _build_service(database, start)
        connection = service.connection

        # 基础账户：先有保护站/财务/审计，再由保护站建组织。
        service.create_user("station1", "保护站值班员", "station")
        service.create_user("fin1", "局财务", "finance")
        service.create_user("audit1", "审计", "auditor")
        service.create_organization("station1", {"org_id": "vg-east", "name": "东坡村组", "kind": "village_group"})
        service.create_organization("station1", {"org_id": "vg-west", "name": "西坡村组", "kind": "village_group"})
        service.create_organization("station1", {"org_id": "st-north", "name": "北山保护站", "kind": "station"})
        for user_id, name, role, org in (
            ("head-east", "东坡组长", "village_head", "vg-east"),
            ("head-west", "西坡组长", "village_head", "vg-west"),
            ("v-a", "巡护员甲", "villager", "vg-east"),
            ("v-b", "巡护员乙", "villager", "vg-east"),
            ("v-w", "巡护员丙", "villager", "vg-west"),
        ):
            service.create_user(user_id, name, role, org)

        # 服务区域与检查点。
        service.create_service_area("station1", {
            "service_area_id": "area-ridge", "name": "北山脊线",
            "geometry": [{"lat": 30.1, "lng": 118.2}, {"lat": 30.2, "lng": 118.3}],
        })
        service.create_service_area("station1", {
            "service_area_id": "area-valley", "name": "西坡沟谷",
            "geometry": [{"lat": 29.9, "lng": 118.0}],
        })
        for checkpoint_id, name in (("cp-1", "北垭口"), ("cp-2", "松林坡"), ("cp-3", "望火台")):
            service.create_checkpoint("station1", {
                "checkpoint_id": checkpoint_id, "service_area_id": "area-ridge",
                "name": name, "position": {"lat": 30.1, "lng": 118.2},
            })

        # 人员资格。
        for person_id, family in (
            ("v-a", "fire_lookout"), ("v-b", "fire_lookout"),
            ("v-a", "waste_haul"), ("v-b", "wildlife_conflict"),
            ("v-w", "fire_lookout"),
        ):
            service.grant_qualification("station1", {
                "person_id": person_id, "task_family": family, "level": "L1",
                "valid_from": "2026-09-01", "valid_until": "2026-12-31",
            })

        # 计价规则版本。
        service.publish_pricing_rule("station1", {
            "rule_id": "price-fire", "task_family": "fire_lookout", "task_kind": "planned",
            "base_unit": "checkpoint", "base_rate_cny": "30.00", "reinforcement_rate_cny": "0.00",
            "repeat_within_minutes": 30, "road_closure_excuse_code": "ROAD_CLOSED",
        })
        service.publish_pricing_rule("station1", {
            "rule_id": "price-fire-rein", "task_family": "fire_lookout", "task_kind": "reinforcement",
            "base_unit": "checkpoint", "base_rate_cny": "30.00", "reinforcement_rate_cny": "20.00",
            "repeat_within_minutes": 30, "road_closure_excuse_code": "ROAD_CLOSED",
        })
        service.publish_pricing_rule("station1", {
            "rule_id": "price-waste", "task_family": "waste_haul", "task_kind": "planned",
            "base_unit": "shift", "base_rate_cny": "80.00", "reinforcement_rate_cny": "0.00",
            "repeat_within_minutes": 30, "road_closure_excuse_code": "ROAD_CLOSED",
        })
        service.publish_pricing_rule("fin1", {
            "rule_id": "price-wild", "task_family": "wildlife_conflict", "task_kind": "planned",
            "base_unit": "event", "base_rate_cny": "50.00", "reinforcement_rate_cny": "0.00",
            "repeat_within_minutes": 30, "road_closure_excuse_code": "ROAD_CLOSED",
        })

        # 设备按实际序号绑定巡护员。
        service.register_device("station1", {"device_serial": "dev-a", "person_id": "v-a", "model_name": "巡护终端A"})
        service.register_device("station1", {"device_serial": "dev-b", "person_id": "v-b", "model_name": "巡护终端B"})
        service.register_device("station1", {"device_serial": "dev-w", "person_id": "v-w", "model_name": "巡护终端W"})

        # 任务 1：计划防火瞭望（3 个检查点），其中望火台因封路合理免责。
        clock.current = datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc)
        service.create_task("head-east", {
            "task_id": "t-fire-0910", "task_family": "fire_lookout", "task_kind": "planned",
            "service_area_id": "area-ridge", "scheduled_for": "2026-09-10T01:00:00Z",
            "person_ids": ["v-a"], "pricing_rule_id": "price-fire", "idempotency_key": "t-fire-0910-key",
            "checkpoints": [
                {"checkpoint_id": "cp-1", "required": True},
                {"checkpoint_id": "cp-2", "required": True},
                {"checkpoint_id": "cp-3", "required": True},
            ],
        })
        service.register_blockade("station1", {
            "blockade_id": "b-0910", "service_area_id": "area-ridge", "checkpoint_id": "cp-3",
            "starts_at": "2026-09-10T00:00:00Z", "ends_at": "2026-09-10T23:59:59Z",
            "reason_code": "ROAD_CLOSED", "evidence": {"source": "林业站封路通告", "doc": "road-0910.pdf"},
        })
        clock.current = datetime(2026, 9, 10, 1, 40, tzinfo=timezone.utc)
        upload1 = service.upload_receipts("v-a", "dev-a", [
            {"sequence_no": 1, "task_id": "t-fire-0910", "receipt_type": "sign_on", "person_id": "v-a",
             "occurred_at": "2026-09-10T01:00:00Z", "payload": {"battery": 0.9}},
            {"sequence_no": 2, "task_id": "t-fire-0910", "receipt_type": "checkpoint", "checkpoint_id": "cp-1",
             "person_id": "v-a", "occurred_at": "2026-09-10T01:10:00Z", "payload": {}},
            {"sequence_no": 3, "task_id": "t-fire-0910", "receipt_type": "checkpoint", "checkpoint_id": "cp-2",
             "person_id": "v-a", "occurred_at": "2026-09-10T01:20:00Z", "payload": {}},
            {"sequence_no": 4, "task_id": "t-fire-0910", "receipt_type": "checkpoint", "checkpoint_id": "cp-2",
             "person_id": "v-a", "occurred_at": "2026-09-10T01:25:00Z", "payload": {"note": "重复签到"}},
            {"sequence_no": 5, "task_id": "t-fire-0910", "receipt_type": "sign_off", "person_id": "v-a",
             "occurred_at": "2026-09-10T01:40:00Z", "payload": {}},
        ])
        # 同序号同内容重放；同序号异内容分叉。
        replay = service.upload_receipts("v-a", "dev-a", [
            {"sequence_no": 1, "task_id": "t-fire-0910", "receipt_type": "sign_on", "person_id": "v-a",
             "occurred_at": "2026-09-10T01:00:00Z", "payload": {"battery": 0.9}},
        ])
        fork = service.upload_receipts("v-a", "dev-a", [
            {"sequence_no": 3, "task_id": "t-fire-0910", "receipt_type": "checkpoint", "checkpoint_id": "cp-2",
             "person_id": "v-a", "occurred_at": "2026-09-10T01:22:00Z", "payload": {"tampered": True}},
        ])
        # 序号缺口与补传。
        gap = service.upload_receipts("v-a", "dev-a", [
            {"sequence_no": 30, "task_id": "t-fire-0910", "receipt_type": "sign_off", "person_id": "v-a",
             "occurred_at": "2026-09-10T02:00:00Z", "payload": {"note": "越过缺口先到"}},
        ])
        gap_fill = service.upload_receipts("v-a", "dev-a", [
            {"sequence_no": 29, "task_id": "t-fire-0910", "receipt_type": "checkpoint", "checkpoint_id": "cp-1",
             "person_id": "v-a", "occurred_at": "2026-09-10T01:55:00Z", "payload": {"note": "缺口补传"}},
        ])

        # 任务 2：临时增援瞭望，完成全部检查点，按增援加价计酬。
        clock.current = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        service.create_task("station1", {
            "task_id": "t-fire-rein-0911", "task_family": "fire_lookout", "task_kind": "reinforcement",
            "service_area_id": "area-ridge", "scheduled_for": "2026-09-11T01:00:00Z",
            "responsible_org_id": "vg-east", "person_ids": ["v-b"], "pricing_rule_id": "price-fire-rein",
            "idempotency_key": "t-fire-rein-0911-key",
            "checkpoints": [
                {"checkpoint_id": "cp-1", "required": True},
                {"checkpoint_id": "cp-2", "required": True},
                {"checkpoint_id": "cp-3", "required": True},
            ],
        })
        clock.current = datetime(2026, 9, 11, 2, 0, tzinfo=timezone.utc)
        service.upload_receipts("v-b", "dev-b", [
            {"sequence_no": 1, "task_id": "t-fire-rein-0911", "receipt_type": "sign_on", "person_id": "v-b",
             "occurred_at": "2026-09-11T01:00:00Z", "payload": {}},
            {"sequence_no": 2, "task_id": "t-fire-rein-0911", "receipt_type": "checkpoint", "checkpoint_id": "cp-1",
             "person_id": "v-b", "occurred_at": "2026-09-11T01:12:00Z", "payload": {}},
            {"sequence_no": 3, "task_id": "t-fire-rein-0911", "receipt_type": "checkpoint", "checkpoint_id": "cp-2",
             "person_id": "v-b", "occurred_at": "2026-09-11T01:24:00Z", "payload": {}},
            {"sequence_no": 4, "task_id": "t-fire-rein-0911", "receipt_type": "checkpoint", "checkpoint_id": "cp-3",
             "person_id": "v-b", "occurred_at": "2026-09-11T01:36:00Z", "payload": {}},
            {"sequence_no": 5, "task_id": "t-fire-rein-0911", "receipt_type": "sign_off", "person_id": "v-b",
             "occurred_at": "2026-09-11T01:50:00Z", "payload": {}},
        ])

        # 任务 3：垃圾清运轮班，全域封路无法完成，签到后合理免责。
        clock.current = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        service.create_task("station1", {
            "task_id": "t-waste-0912", "task_family": "waste_haul", "task_kind": "planned",
            "service_area_id": "area-valley", "scheduled_for": "2026-09-12T01:00:00Z",
            "responsible_org_id": "vg-east", "person_ids": ["v-a"], "pricing_rule_id": "price-waste",
            "idempotency_key": "t-waste-0912-key", "checkpoints": [],
        })
        service.register_blockade("station1", {
            "blockade_id": "b-0912", "service_area_id": "area-valley", "checkpoint_id": None,
            "starts_at": "2026-09-12T00:00:00Z", "ends_at": "2026-09-12T23:59:59Z",
            "reason_code": "ROAD_CLOSED", "evidence": {"source": "道路水毁", "doc": "road-0912.pdf"},
        })
        clock.current = datetime(2026, 9, 12, 1, 10, tzinfo=timezone.utc)
        service.upload_receipts("v-a", "dev-a", [
            {"sequence_no": 31, "task_id": "t-waste-0912", "receipt_type": "sign_on", "person_id": "v-a",
             "occurred_at": "2026-09-12T01:05:00Z", "payload": {}},
        ])
        service.close_task("station1", "t-waste-0912", "incomplete")

        # 任务 4：野生动物冲突上报按件计酬；同内容重复上报只计一次。
        clock.current = datetime(2026, 9, 13, 6, 0, tzinfo=timezone.utc)
        service.create_task("station1", {
            "task_id": "t-wild-0913", "task_family": "wildlife_conflict", "task_kind": "planned",
            "service_area_id": "area-valley", "scheduled_for": "2026-09-13T03:00:00Z",
            "responsible_org_id": "vg-east", "person_ids": ["v-b"], "pricing_rule_id": "price-wild",
            "idempotency_key": "t-wild-0913-key", "checkpoints": [],
        })
        service.upload_receipts("v-b", "dev-b", [
            {"sequence_no": 6, "task_id": "t-wild-0913", "receipt_type": "sign_on", "person_id": "v-b",
             "occurred_at": "2026-09-13T03:00:00Z", "payload": {}},
            {"sequence_no": 7, "task_id": "t-wild-0913", "receipt_type": "report", "person_id": "v-b",
             "occurred_at": "2026-09-13T03:20:00Z", "payload": {"species": "野猪", "damage": "玉米地 2 亩"}},
            {"sequence_no": 8, "task_id": "t-wild-0913", "receipt_type": "report", "person_id": "v-b",
             "occurred_at": "2026-09-13T04:05:00Z", "payload": {"species": "猕猴", "damage": "蜂箱 3 个"}},
            {"sequence_no": 9, "task_id": "t-wild-0913", "receipt_type": "report", "person_id": "v-b",
             "occurred_at": "2026-09-13T04:40:00Z", "payload": {"species": "野猪", "damage": "玉米地 2 亩"}},
            {"sequence_no": 10, "task_id": "t-wild-0913", "receipt_type": "sign_off", "person_id": "v-b",
             "occurred_at": "2026-09-13T05:00:00Z", "payload": {}},
        ])

        # 任务 5：跨村组转派，东坡发起、西坡确认后才换人；新人员在新版本上签到。
        clock.current = datetime(2026, 9, 19, 9, 0, tzinfo=timezone.utc)
        service.create_task("head-east", {
            "task_id": "t-fire-0920", "task_family": "fire_lookout", "task_kind": "planned",
            "service_area_id": "area-ridge", "scheduled_for": "2026-09-20T01:00:00Z",
            "person_ids": ["v-a"], "pricing_rule_id": "price-fire", "idempotency_key": "t-fire-0920-key",
            "checkpoints": [
                {"checkpoint_id": "cp-1", "required": True},
                {"checkpoint_id": "cp-2", "required": True},
                {"checkpoint_id": "cp-3", "required": True},
            ],
        })
        service.propose_transfer("head-east", "t-fire-0920", "vg-west", ["v-w"], "东坡当日人手不足，商请西坡接替")
        transfer = service.decide_transfer("head-west", "tr-t-fire-0920-1", True, "西坡同意承接")
        clock.current = datetime(2026, 9, 20, 2, 0, tzinfo=timezone.utc)
        service.upload_receipts("v-w", "dev-w", [
            {"sequence_no": 1, "task_id": "t-fire-0920", "receipt_type": "sign_on", "person_id": "v-w",
             "occurred_at": "2026-09-20T01:00:00Z", "payload": {}},
            {"sequence_no": 2, "task_id": "t-fire-0920", "receipt_type": "checkpoint", "checkpoint_id": "cp-1",
             "person_id": "v-w", "occurred_at": "2026-09-20T01:10:00Z", "payload": {}},
            {"sequence_no": 3, "task_id": "t-fire-0920", "receipt_type": "checkpoint", "checkpoint_id": "cp-2",
             "person_id": "v-w", "occurred_at": "2026-09-20T01:20:00Z", "payload": {}},
            {"sequence_no": 4, "task_id": "t-fire-0920", "receipt_type": "checkpoint", "checkpoint_id": "cp-3",
             "person_id": "v-w", "occurred_at": "2026-09-20T01:30:00Z", "payload": {}},
            {"sequence_no": 5, "task_id": "t-fire-0920", "receipt_type": "sign_off", "person_id": "v-w",
             "occurred_at": "2026-09-20T01:40:00Z", "payload": {}},
        ])

        # 月末周期：先生成可复算草案，再复算一次，输入不变应命中原草案。
        clock.current = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
        service.open_period("station1", "p-2026-09", "2026-09-01", "2026-09-30")
        draft = service.compose_settlement("station1", "p-2026-09")
        recomposed = service.compose_settlement("fin1", "p-2026-09")
        assert recomposed["replayed"] is True
        assert draft["totals"] == recomposed["totals"]

        lines = draft["lines"]
        line_by_task = {line["task_id"]: line for line in lines}
        assert line_by_task["t-fire-0910"]["total_amount_cny"] == "90.00"
        assert line_by_task["t-fire-0910"]["excused_amount_cny"] == "30.00"
        assert line_by_task["t-fire-rein-0911"]["total_amount_cny"] == "150.00"
        assert line_by_task["t-waste-0912"]["total_amount_cny"] == "80.00"
        assert line_by_task["t-wild-0913"]["total_amount_cny"] == "100.00"
        assert line_by_task["t-fire-0920"]["org_id"] == "vg-west"
        assert draft["totals"]["total_amount_cny"] == "510.00"

        # 保护站对野生动物上报件数提出争议：只冻结该任务金额。
        wild_line_id = line_by_task["t-wild-0913"]["line_id"]
        service.confirm_scope("station1", "p-2026-09", "station", [wild_line_id], "disputed", "其中一起与上周重复")
        undisputed = [line["line_id"] for line in lines if not line["frozen"] and line["line_id"] != wild_line_id]
        east_lines = [line["line_id"] for line in lines
                      if line["org_id"] == "vg-east" and line["line_id"] != wild_line_id]
        west_lines = [line["line_id"] for line in lines if line["org_id"] == "vg-west"]
        service.confirm_scope("station1", "p-2026-09", "station", undisputed, "confirmed", "事实核验通过")
        service.confirm_scope("head-east", "p-2026-09", "village_head", east_lines, "confirmed", "村组认可")
        service.confirm_scope("head-west", "p-2026-09", "village_head", west_lines, "confirmed", "村组认可")
        service.confirm_scope("fin1", "p-2026-09", "finance", undisputed, "confirmed", "金额复核通过")

        partial = service.finalize_payment("fin1", "p-2026-09")
        assert partial["new_payment_entries"] == 4
        assert sum(Decimal_value(entry["amount_cny"]) for entry in partial["payment_list"]) == 410

        # 解释一笔补偿：90 元由两个到达检查点与一个封路免责检查点构成。
        explained = service.explain_line("audit1", line_by_task["t-fire-0910"]["line_id"])
        assert [unit["state"] for unit in explained["units"]] == ["reached", "reached", "excused"]
        assert explained["excluded_receipts"]
        assert explained["excused_by_blockades"][0]["blockade_id"] == "b-0910"

        # 重启：关闭进程后重新打开同一数据库，未结争议必须能恢复。
        connection.close()
        restarted = ComanagementService(connect(database), clock)
        recovered = restarted.recover_open_disputes("audit1")
        assert len(recovered["open_disputes"]) == 1
        assert recovered["open_disputes"][0]["task_id"] == "t-wild-0913"
        # 草案依旧可复算且金额逐分一致。
        after_restart = restarted.compose_settlement("station1", "p-2026-09")
        assert after_restart["totals"]["total_amount_cny"] == "510.00"

        # 争议裁定以追加决定留痕：核减 20 元并写明原因，随后三方按新金额确认并支付。
        ruling = restarted.resolve_dispute(
            "station1", recovered["open_disputes"][0]["dispute_id"], "resolved", "-20.00",
            "经回放设备轨迹，第二起猕猴上报与邻村任务时间冲突，核减一件计酬",
        )
        assert ruling["total_amount_cny"] == "80.00"
        restarted.confirm_scope("station1", "p-2026-09", "station", [wild_line_id], "confirmed", "按核减后件数")
        restarted.confirm_scope("head-east", "p-2026-09", "village_head", [wild_line_id], "confirmed", "村组接受裁定")
        restarted.confirm_scope("fin1", "p-2026-09", "finance", [wild_line_id], "confirmed", "财务按 80 元支付")
        final = restarted.finalize_payment("fin1", "p-2026-09")
        assert final["state"] == "paid"
        paid_total = sum(Decimal_value(entry["amount_cny"]) for entry in final["payment_list"])
        assert paid_total == 490
        final_explain = restarted.explain_line("audit1", wild_line_id)
        assert len(final_explain["adjustment_decisions"]) == 1
        assert final_explain["adjustment_decisions"][0]["amount_delta_cny"] == "-20.00"

        audit = restarted.audit_chain("audit1")
        schema = inspect_schema(restarted.connection)
        restarted.connection.close()

        return {
            "status": "ok",
            "workspace": workspace.name,
            "schema_version": schema["schema_version"],
            "missing_tables": schema["missing_tables"],
            "ingest": {
                "accepted": len(upload1["accepted_receipt_ids"]),
                "replayed": replay["replayed_sequences"],
                "forked": [item["sequence_no"] for item in fork["forked_sequences"]],
                "gapped": gap["gapped_sequences"],
                "gap_filled": gap_fill["gap_filled_sequences"],
            },
            "task_versions": {
                "t-fire-0920": transfer["task_version"],
            },
            "draft_totals": draft["totals"],
            "recomputed": recomposed["replayed"],
            "partial_payment_entries": partial["new_payment_entries"],
            "recovered_open_disputes": len(recovered["open_disputes"]),
            "final_state": final["state"],
            "paid_total_cny": f"{paid_total:.2f}",
            "audit_valid": audit["valid"],
            "audit_events": audit["events"],
        }


def Decimal_value(text: str) -> float:  # 仅用于验收脚本中的金额求和展示
    from decimal import Decimal

    return float(Decimal(text))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行社区共管任务与补偿核算离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
