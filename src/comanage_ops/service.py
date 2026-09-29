"""共管任务与补偿核算的事务用例。

设计要点：
- 任务定义版本化，只追加；运行态与版本分离；
- 跨村组转派需转出、转入双方确认，已开始任务禁止转派；
- 离线回执按设备序号与事件 UUID 识别重放与分叉；
- 周期结算先落可复算草案，村组/保护站/财务分别确认，
  争议只冻结对应任务行，其余照常进入支付清单；
- 一切调整只追加决定（event_decisions / settlement_decisions），
  金额组成可逐笔解释，重启后未结争议从 SQLite 恢复。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    EventReceipt,
    PricingRule,
    Qualification,
    ServiceArea,
    TaskDefinition,
)
from .storage import initialize, transaction
from .valuation import EffectiveEvent, decimal_text, quantize_money, value_task


ROLE_PERMISSIONS = {
    "station": {
        "area.write",
        "qualification.write",
        "device.write",
        "task.write",
        "assignment.propose",
        "event.upload",
        "event.decide",
        "settlement.generate",
        "settlement.confirm",
        "dispute.raise",
        "report.read",
        "audit.read",
    },
    "village": {
        "assignment.confirm",
        "transfer.propose",
        "transfer.confirm",
        "event.upload",
        "settlement.confirm",
        "dispute.raise",
        "report.read",
    },
    "finance": {
        "pricing.write",
        "settlement.confirm",
        "payment.write",
        "dispute.raise",
        "report.read",
    },
    "auditor": {"report.read", "audit.read"},
}

PARTY_BY_ROLE = {"station": "station", "village": "village", "finance": "finance"}


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class ComanageService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM comanage_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM comanage_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = digest(body)
        self.connection.execute(
            "INSERT INTO comanage_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    # ------------------------------------------------------------------ 用户

    def create_user(
        self, user_id: str, display_name: str, role: str, village_group_id: str | None = None
    ) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        if role == "village" and not village_group_id:
            raise ValidationFailed("村组用户必须归属一个村组")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO comanage_users(user_id,display_name,role,village_group_id,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, village_group_id, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role, "village_group_id": village_group_id}

    # ------------------------------------------------------------------ 基础数据

    def register_device(
        self, actor_id: str, device_serial: str, model_name: str, owner_village_group_id: str | None
    ) -> dict[str, Any]:
        self._require(actor_id, "device.write")
        if not device_serial.strip() or not model_name.strip():
            raise ValidationFailed("设备序号和型号不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO devices(device_serial,model_name,owner_village_group_id,registered_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (device_serial.strip(), model_name.strip(), owner_village_group_id, actor_id, self._now()),
                )
                self._audit("device", device_serial, "device.registered", actor_id, {"model_name": model_name})
        except sqlite3.IntegrityError as exc:
            raise Conflict("设备序号已经登记") from exc
        return {"device_serial": device_serial.strip(), "revoked": False}

    def revoke_device(self, actor_id: str, device_serial: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "device.write")
        if not reason.strip():
            raise ValidationFailed("吊销原因不能为空")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE devices SET revoked=1 WHERE device_serial=? AND revoked=0", (device_serial,)
            )
            if cursor.rowcount != 1:
                raise InvalidState("设备不存在或已吊销")
            self._audit("device", device_serial, "device.revoked", actor_id, {"reason": reason})
        return {"device_serial": device_serial, "revoked": True}

    def create_area(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "area.write")
        area = ServiceArea.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO service_areas(area_id,name,village_group_id,geometry_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        area.area_id,
                        area.name,
                        area.village_group_id,
                        canonical_json([dict(point) for point in area.geometry]),
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("service_area", area.area_id, "area.created", actor_id, {"village_group_id": area.village_group_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("服务区域编号已经存在") from exc
        return {"area_id": area.area_id, "village_group_id": area.village_group_id, "revision": 1}

    def register_qualification(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "qualification.write")
        qualification = Qualification.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO qualifications(qualification_id,person_id,kind,valid_from,valid_until,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        qualification.qualification_id,
                        qualification.person_id,
                        qualification.kind,
                        qualification.valid_from,
                        qualification.valid_until,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "qualification",
                    qualification.qualification_id,
                    "qualification.registered",
                    actor_id,
                    {"person_id": qualification.person_id, "kind": qualification.kind},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("资格编号已经存在") from exc
        return {
            "qualification_id": qualification.qualification_id,
            "person_id": qualification.person_id,
            "kind": qualification.kind,
        }

    def _qualified(self, person_id: str, kind: str, on_date: str) -> None:
        row = self.connection.execute(
            "SELECT 1 FROM qualifications WHERE person_id=? AND kind=? AND active=1 "
            "AND valid_from<=? AND (valid_until IS NULL OR valid_until>=?) LIMIT 1",
            (person_id, kind, on_date, on_date),
        ).fetchone()
        if row is None:
            raise Forbidden(f"人员 {person_id} 在 {on_date} 不具备 {kind} 资格")

    def create_pricing_rule(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "pricing.write")
        rule = PricingRule.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO pricing_rules(rule_id,kind,source,basis,base_amount_cny,unit_amount_cny,"
                    "reinforcement_multiplier,absolved_ratio,valid_from,valid_until,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        rule.rule_id,
                        rule.kind,
                        rule.source,
                        rule.basis,
                        decimal_text(rule.base_amount_cny),
                        decimal_text(rule.unit_amount_cny),
                        decimal_text(rule.reinforcement_multiplier),
                        decimal_text(rule.absolved_ratio),
                        rule.valid_from,
                        rule.valid_until,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("pricing_rule", rule.rule_id, "pricing_rule.created", actor_id, {"kind": rule.kind, "source": rule.source})
        except sqlite3.IntegrityError as exc:
            raise Conflict("计价规则编号已经存在") from exc
        return {"rule_id": rule.rule_id, "state": "active", "revision": 1}

    def retire_pricing_rule(self, actor_id: str, rule_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "pricing.write")
        if not reason.strip():
            raise ValidationFailed("停用原因不能为空")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE pricing_rules SET state='retired',revision=revision+1 WHERE rule_id=? AND state='active'",
                (rule_id,),
            )
            if cursor.rowcount != 1:
                raise InvalidState("计价规则不存在或已停用")
            self._audit("pricing_rule", rule_id, "pricing_rule.retired", actor_id, {"reason": reason})
        return {"rule_id": rule_id, "state": "retired"}

    def _applicable_rule(self, kind: str, source: str, on_date: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM pricing_rules WHERE kind=? AND source=? AND state='active' "
            "AND valid_from<=? AND (valid_until IS NULL OR valid_until>=?) "
            "ORDER BY valid_from DESC, rule_id ASC LIMIT 1",
            (kind, source, on_date, on_date),
        ).fetchone()
        if row is None:
            raise InvalidState(f"{on_date} 没有适用于 {kind}/{source} 的计价规则")
        return row

    # ------------------------------------------------------------------ 任务与版本

    def create_task(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "task.write")
        task = TaskDefinition.from_dict(raw)
        if self.connection.execute("SELECT 1 FROM service_areas WHERE area_id=?", (task.area_id,)).fetchone() is None:
            raise NotFound("服务区域不存在")
        area = self.connection.execute(
            "SELECT geometry_json FROM service_areas WHERE area_id=?", (task.area_id,)
        ).fetchone()
        point_ids = {point["point_id"] for point in json.loads(area["geometry_json"])}
        for checkpoint in task.checkpoints:
            if checkpoint.point_id not in point_ids:
                raise ValidationFailed(f"检查点 {checkpoint.checkpoint_id} 的点位不在服务区域内")
        checkpoints_json = canonical_json(
            [
                {
                    "checkpoint_id": item.checkpoint_id,
                    "label": item.label,
                    "point_id": item.point_id,
                    "ordinal": item.ordinal,
                }
                for item in sorted(task.checkpoints, key=lambda item: item.ordinal)
            ]
        )
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO tasks(task_id,version,kind,source,area_id,title,planned_start,planned_end,"
                    "required_qualification,checkpoints_json,assignee_person_id,assignee_village_group_id,"
                    "supersedes_version,change_reason,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        task.task_id,
                        1,
                        task.kind,
                        task.source,
                        task.area_id,
                        task.title,
                        task.planned_start,
                        task.planned_end,
                        task.required_qualification,
                        checkpoints_json,
                        None,
                        None,
                        None,
                        None,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO task_runtime(task_id,current_version,assignment_status,lifecycle_state,updated_at) "
                    "VALUES(?,1,'unassigned','assigned',?)",
                    (task.task_id, self._now()),
                )
                self._audit("task", task.task_id, "task.created", actor_id, {"kind": task.kind, "source": task.source})
        except sqlite3.IntegrityError as exc:
            raise Conflict("任务编号已经存在") from exc
        return {"task_id": task.task_id, "version": 1, "assignment_status": "unassigned"}

    def _task_row(self, task_id: str, version: int | None = None) -> sqlite3.Row:
        if version is None:
            row = self.connection.execute(
                "SELECT t.* FROM tasks t JOIN task_runtime r ON r.task_id=t.task_id "
                "WHERE t.task_id=? AND t.version=r.current_version",
                (task_id,),
            ).fetchone()
        else:
            row = self.connection.execute("SELECT * FROM tasks WHERE task_id=? AND version=?", (task_id, version)).fetchone()
        if row is None:
            raise NotFound("任务版本不存在")
        return row

    def _runtime(self, task_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM task_runtime WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFound("任务不存在")
        return row

    def task(self, task_id: str) -> dict[str, Any]:
        runtime = self._runtime(task_id)
        row = self._task_row(task_id)
        return {
            **{key: row[key] for key in row.keys() if key not in {"checkpoints_json"}},
            "checkpoints": json.loads(row["checkpoints_json"]),
            "assignment_status": runtime["assignment_status"],
            "lifecycle_state": runtime["lifecycle_state"],
            "revision": runtime["revision"],
        }

    def task_versions(self, task_id: str) -> list[dict[str, Any]]:
        self._runtime(task_id)
        rows = self.connection.execute(
            "SELECT task_id,version,kind,source,title,assignee_person_id,assignee_village_group_id,"
            "supersedes_version,change_reason,created_by,created_at FROM tasks WHERE task_id=? ORDER BY version",
            (task_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def revise_task_definition(self, actor_id: str, task_id: str, raw: Mapping[str, Any], reason: str) -> dict[str, Any]:
        """任务定义调整以新版本追加，留下原因；已完成/免责任务不可改。"""
        self._require(actor_id, "task.write")
        if not reason.strip():
            raise ValidationFailed("调整原因不能为空")
        runtime = self._runtime(task_id)
        if runtime["lifecycle_state"] in {"completed", "absolved", "cancelled"}:
            raise InvalidState("任务已结束，定义不能再调整")
        current = self._task_row(task_id)
        revision_payload = dict(raw)
        revision_payload.setdefault("task_id", task_id)
        revision_payload.setdefault("kind", current["kind"])
        revision_payload.setdefault("source", current["source"])
        revision_payload.setdefault("area_id", current["area_id"])
        revision_payload.setdefault("title", current["title"])
        revision_payload.setdefault("planned_start", current["planned_start"])
        revision_payload.setdefault("planned_end", current["planned_end"])
        revision_payload.setdefault("required_qualification", current["required_qualification"])
        revision_payload.setdefault("checkpoints", json.loads(current["checkpoints_json"]))
        task = TaskDefinition.from_dict(revision_payload)
        area = self.connection.execute("SELECT geometry_json FROM service_areas WHERE area_id=?", (task.area_id,)).fetchone()
        if area is None:
            raise NotFound("服务区域不存在")
        point_ids = {point["point_id"] for point in json.loads(area["geometry_json"])}
        checkpoints = sorted(task.checkpoints, key=lambda item: item.ordinal)
        for checkpoint in checkpoints:
            if checkpoint.point_id not in point_ids:
                raise ValidationFailed(f"检查点 {checkpoint.checkpoint_id} 的点位不在服务区域内")
        new_version = current["version"] + 1
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO tasks(task_id,version,kind,source,area_id,title,planned_start,planned_end,"
                "required_qualification,checkpoints_json,assignee_person_id,assignee_village_group_id,"
                "supersedes_version,change_reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    new_version,
                    task.kind,
                    task.source,
                    task.area_id,
                    task.title,
                    task.planned_start,
                    task.planned_end,
                    task.required_qualification,
                    canonical_json(
                        [
                            {"checkpoint_id": c.checkpoint_id, "label": c.label, "point_id": c.point_id, "ordinal": c.ordinal}
                            for c in checkpoints
                        ]
                    ),
                    current["assignee_person_id"],
                    current["assignee_village_group_id"],
                    current["version"],
                    reason,
                    actor_id,
                    self._now(),
                ),
            )
            self.connection.execute(
                "UPDATE task_runtime SET current_version=?,revision=revision+1,updated_at=? WHERE task_id=?",
                (new_version, self._now(), task_id),
            )
            self._audit("task", task_id, "task.revised", actor_id, {"version": new_version, "reason": reason})
        return {"task_id": task_id, "version": new_version, "supersedes_version": current["version"]}

    # ------------------------------------------------------------------ 派单与转派

    def propose_assignment(
        self, actor_id: str, task_id: str, person_id: str, village_group_id: str, note: str = ""
    ) -> dict[str, Any]:
        self._require(actor_id, "assignment.propose")
        runtime = self._runtime(task_id)
        if runtime["assignment_status"] != "unassigned":
            raise InvalidState("任务已有派单结果，不能重复提议")
        self._qualified(person_id, self._task_row(task_id)["required_qualification"], self._now()[:10])
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE task_runtime SET assignment_status='proposed',proposed_person_id=?,"
                "proposed_village_group_id=?,revision=revision+1,updated_at=? WHERE task_id=?",
                (person_id, village_group_id, self._now(), task_id),
            )
            self._audit("task", task_id, "assignment.proposed", actor_id, {"person_id": person_id, "village_group_id": village_group_id, "note": note})
        return {"task_id": task_id, "assignment_status": "proposed", "person_id": person_id, "village_group_id": village_group_id}

    def confirm_assignment(self, actor_id: str, task_id: str) -> dict[str, Any]:
        user = self._require(actor_id, "assignment.confirm")
        runtime = self._runtime(task_id)
        if runtime["assignment_status"] != "proposed":
            raise InvalidState("任务没有待确认的派单提议")
        if user["village_group_id"] != runtime["proposed_village_group_id"]:
            raise Forbidden("只有受托村组可以确认派单")
        with transaction(self.connection, immediate=True):
            # 村组确认后受托人才落到任务新版本上；被拒绝的提议不留版本。
            new_version = self._append_task_version_from_current(
                task_id,
                person_id=runtime["proposed_person_id"],
                village_group_id=runtime["proposed_village_group_id"],
                reason="受托村组确认派单",
                actor_id=actor_id,
            )
            self.connection.execute(
                "UPDATE task_runtime SET assignment_status='confirmed',proposed_person_id=NULL,"
                "proposed_village_group_id=NULL,revision=revision+1,updated_at=? WHERE task_id=?",
                (self._now(), task_id),
            )
            self._audit("task", task_id, "assignment.confirmed", actor_id, {
                "village_group_id": user["village_group_id"], "version": new_version,
            })
        return {"task_id": task_id, "assignment_status": "confirmed", "task_version": new_version}

    def reject_assignment(self, actor_id: str, task_id: str, note: str) -> dict[str, Any]:
        user = self._require(actor_id, "assignment.confirm")
        runtime = self._runtime(task_id)
        if runtime["assignment_status"] != "proposed":
            raise InvalidState("任务没有待确认的派单提议")
        if user["village_group_id"] != runtime["proposed_village_group_id"]:
            raise Forbidden("只有受托村组可以拒绝派单")
        if not note.strip():
            raise ValidationFailed("拒绝原因不能为空")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE task_runtime SET assignment_status='unassigned',proposed_person_id=NULL,"
                "proposed_village_group_id=NULL,revision=revision+1,updated_at=? WHERE task_id=?",
                (self._now(), task_id),
            )
            self._audit("task", task_id, "assignment.rejected", actor_id, {"note": note})
        return {"task_id": task_id, "assignment_status": "unassigned"}

    def _append_task_version_from_current(
        self,
        task_id: str,
        *,
        person_id: str | None,
        village_group_id: str | None,
        reason: str,
        actor_id: str,
    ) -> int:
        current = self._task_row(task_id)
        new_version = current["version"] + 1
        self.connection.execute(
            "INSERT INTO tasks(task_id,version,kind,source,area_id,title,planned_start,planned_end,"
            "required_qualification,checkpoints_json,assignee_person_id,assignee_village_group_id,"
            "supersedes_version,change_reason,created_by,created_at) "
            "SELECT ?,version+1,kind,source,area_id,title,planned_start,planned_end,required_qualification,"
            "checkpoints_json,?,?,?,?,?,? FROM tasks WHERE task_id=? AND version=?",
            (
                task_id,
                person_id,
                village_group_id,
                current["version"],
                reason,
                actor_id,
                self._now(),
                task_id,
                current["version"],
            ),
        )
        self.connection.execute(
            "UPDATE task_runtime SET current_version=? WHERE task_id=?", (new_version, task_id)
        )
        return new_version

    def propose_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        task_id: str,
        to_village_group_id: str,
        to_person_id: str,
        reason: str,
    ) -> dict[str, Any]:
        user = self._require(actor_id, "transfer.propose")
        runtime = self._runtime(task_id)
        current = self._task_row(task_id)
        if runtime["lifecycle_state"] != "assigned":
            raise InvalidState("任务已经开始或结束，不能转派")
        if runtime["assignment_status"] != "confirmed":
            raise InvalidState("任务尚未确认派单，不能转派")
        from_group = current["assignee_village_group_id"]
        if user["village_group_id"] != from_group:
            raise Forbidden("只有受托村组可以发起转出")
        if to_village_group_id == from_group:
            raise ValidationFailed("转入村组必须与转出村组不同")
        if not reason.strip():
            raise ValidationFailed("转派原因不能为空")
        self._qualified(to_person_id, current["required_qualification"], self._now()[:10])
        pending = self.connection.execute(
            "SELECT 1 FROM task_transfers WHERE task_id=? AND status='proposed' LIMIT 1", (task_id,)
        ).fetchone()
        if pending is not None:
            raise Conflict("该任务已有待双方确认的转派")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO task_transfers(transfer_id,task_id,task_version,from_village_group_id,"
                    "to_village_group_id,to_person_id,reason,proposed_by,proposed_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        transfer_id,
                        task_id,
                        current["version"],
                        from_group,
                        to_village_group_id,
                        to_person_id,
                        reason,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("task", task_id, "transfer.proposed", actor_id, {
                    "transfer_id": transfer_id,
                    "from": from_group,
                    "to": to_village_group_id,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("转派单编号冲突") from exc
        return {"transfer_id": transfer_id, "status": "proposed", "from": from_group, "to": to_village_group_id}

    def confirm_transfer(self, actor_id: str, transfer_id: str, side: str) -> dict[str, Any]:
        """side='from' 或 'to'；双方都确认后才追加新版本并生效。"""
        user = self._require(actor_id, "transfer.confirm")
        if side not in {"from", "to"}:
            raise ValidationFailed("side 必须是 from 或 to")
        transfer = self.connection.execute("SELECT * FROM task_transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
        if transfer is None:
            raise NotFound("转派单不存在")
        if transfer["status"] != "proposed":
            raise InvalidState("转派单已关闭")
        expected_group = transfer["from_village_group_id"] if side == "from" else transfer["to_village_group_id"]
        if user["role"] != "village" or user["village_group_id"] != expected_group:
            raise Forbidden(f"只有{('转出', '转入')[side == 'to']}村组本人可以确认")
        with transaction(self.connection, immediate=True):
            if side == "from":
                if transfer["from_confirmed_at"] is not None:
                    raise Conflict("转出方已经确认过")
                self.connection.execute(
                    "UPDATE task_transfers SET from_confirmed_by=?,from_confirmed_at=? WHERE transfer_id=?",
                    (actor_id, self._now(), transfer_id),
                )
            else:
                if transfer["to_confirmed_at"] is not None:
                    raise Conflict("转入方已经确认过")
                self.connection.execute(
                    "UPDATE task_transfers SET to_confirmed_by=?,to_confirmed_at=? WHERE transfer_id=?",
                    (actor_id, self._now(), transfer_id),
                )
            self._audit("task", transfer["task_id"], f"transfer.{side}_confirmed", actor_id, {"transfer_id": transfer_id})
            row = self.connection.execute(
                "SELECT * FROM task_transfers WHERE transfer_id=?", (transfer_id,)
            ).fetchone()
            if row["from_confirmed_at"] is not None and row["to_confirmed_at"] is not None:
                runtime = self._runtime(row["task_id"])
                if runtime["lifecycle_state"] != "assigned":
                    raise InvalidState("任务已经开始，不能换人")
                new_version = self._append_task_version_from_current(
                    row["task_id"],
                    person_id=row["to_person_id"],
                    village_group_id=row["to_village_group_id"],
                    reason=f"跨村组转派双方确认：{row['reason']}",
                    actor_id=actor_id,
                )
                self.connection.execute(
                    "UPDATE task_transfers SET status='accepted',decided_by=?,decided_at=? WHERE transfer_id=?",
                    (actor_id, self._now(), transfer_id),
                )
                self.connection.execute(
                    "UPDATE task_runtime SET revision=revision+1,updated_at=? WHERE task_id=?",
                    (self._now(), row["task_id"]),
                )
                self._audit("task", row["task_id"], "transfer.accepted", actor_id, {
                    "transfer_id": transfer_id, "version": new_version,
                    "person_id": row["to_person_id"], "village_group_id": row["to_village_group_id"],
                })
                return {"transfer_id": transfer_id, "status": "accepted", "task_version": new_version}
        return {"transfer_id": transfer_id, "status": "proposed", "awaiting": "to" if side == "from" else "from"}

    def reject_transfer(self, actor_id: str, transfer_id: str, note: str) -> dict[str, Any]:
        user = self._require(actor_id, "transfer.confirm")
        if not note.strip():
            raise ValidationFailed("拒绝原因不能为空")
        transfer = self.connection.execute("SELECT * FROM task_transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
        if transfer is None:
            raise NotFound("转派单不存在")
        if transfer["status"] != "proposed":
            raise InvalidState("转派单已关闭")
        if user["village_group_id"] not in {transfer["from_village_group_id"], transfer["to_village_group_id"]}:
            raise Forbidden("只有转出或转入村组可以拒绝转派")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE task_transfers SET status='rejected',decided_by=?,decided_at=?,decision_note=? WHERE transfer_id=?",
                (actor_id, self._now(), note, transfer_id),
            )
            self._audit("task", transfer["task_id"], "transfer.rejected", actor_id, {"transfer_id": transfer_id})
        return {"transfer_id": transfer_id, "status": "rejected"}

    # ------------------------------------------------------------------ 离线事件回执

    def upload_events(self, actor_id: str, device_serial: str, raw_receipts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        self._require(actor_id, "event.upload")
        device = self.connection.execute("SELECT * FROM devices WHERE device_serial=?", (device_serial,)).fetchone()
        if device is None:
            raise NotFound("设备未登记")
        if device["revoked"]:
            raise Forbidden("设备已吊销，回执不再接收")
        receipts = [EventReceipt.from_dict(dict(item, device_serial=device_serial)) for item in raw_receipts]
        if not receipts:
            raise ValidationFailed("补传事件不能为空")
        accepted: list[str] = []
        duplicates: list[str] = []
        rejected: list[dict[str, str]] = []
        with transaction(self.connection, immediate=True):
            for receipt in receipts:
                # 重放：同一设备同一 UUID（或同一序号且 UUID 相同）已见过。
                existing_uuid = self.connection.execute(
                    "SELECT event_uuid FROM device_event_streams WHERE device_serial=? AND event_uuid=?",
                    (device_serial, receipt.event_uuid),
                ).fetchone()
                if existing_uuid is not None:
                    duplicates.append(receipt.event_uuid)
                    continue
                # 分叉：同一设备本地序号对应了不同 UUID。
                existing_seq = self.connection.execute(
                    "SELECT event_uuid FROM device_event_streams WHERE device_serial=? AND seq=?",
                    (device_serial, receipt.seq),
                ).fetchone()
                if existing_seq is not None and existing_seq["event_uuid"] != receipt.event_uuid:
                    raise Conflict(
                        f"设备 {device_serial} 序号 {receipt.seq} 出现分叉："
                        f"已有事件 {existing_seq['event_uuid']}，又收到 {receipt.event_uuid}"
                    )
                # 未知任务：只登记设备流水（仍可识别重放/分叉），不写任务事件表。
                runtime_row = self.connection.execute(
                    "SELECT current_version FROM task_runtime WHERE task_id=?", (receipt.task_id,)
                ).fetchone()
                if runtime_row is None:
                    self.connection.execute(
                        "INSERT INTO device_event_streams(device_serial,seq,event_uuid,received_at) VALUES(?,?,?,?)",
                        (device_serial, receipt.seq, receipt.event_uuid, self._now()),
                    )
                    rejected.append({"event_uuid": receipt.event_uuid, "reason": f"任务不存在：{receipt.task_id}"})
                    self._audit("task_event", receipt.event_uuid, f"event.{receipt.event_type}.rejected", actor_id, {
                        "task_id": receipt.task_id, "device_serial": device_serial, "seq": receipt.seq,
                        "reason": "task_not_found",
                    })
                    continue
                try:
                    state, invalid_reason = self._accept_receipt(receipt)
                except (Forbidden, NotFound, ValidationFailed) as exc:
                    state, invalid_reason = "rejected", str(exc)
                self.connection.execute(
                    "INSERT INTO device_event_streams(device_serial,seq,event_uuid,received_at) VALUES(?,?,?,?)",
                    (device_serial, receipt.seq, receipt.event_uuid, self._now()),
                )
                self.connection.execute(
                    "INSERT INTO task_events(event_uuid,device_serial,task_id,task_version,person_id,event_type,"
                    "client_clock,received_at,checkpoint_id,quantity,evidence_ref,absolved_reason,note,"
                    "validity_state,invalid_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt.event_uuid,
                        device_serial,
                        receipt.task_id,
                        runtime_row["current_version"],
                        receipt.person_id,
                        receipt.event_type,
                        receipt.client_clock,
                        self._now(),
                        receipt.checkpoint_id,
                        decimal_text(receipt.quantity),
                        receipt.evidence_ref,
                        receipt.absolved_reason,
                        receipt.note,
                        state,
                        invalid_reason,
                    ),
                )
                if state == "rejected":
                    self.connection.execute(
                        "INSERT INTO event_decisions(event_uuid,action,reason,decided_by,created_at) VALUES(?,?,?,?,?)",
                        (receipt.event_uuid, "reject", invalid_reason, actor_id, self._now()),
                    )
                    rejected.append({"event_uuid": receipt.event_uuid, "reason": invalid_reason})
                else:
                    accepted.append(receipt.event_uuid)
                self._audit("task_event", receipt.event_uuid, f"event.{receipt.event_type}.{state}", actor_id, {
                    "task_id": receipt.task_id, "device_serial": device_serial, "seq": receipt.seq,
                })
        return {
            "device_serial": device_serial,
            "accepted": accepted,
            "duplicates": duplicates,
            "rejected": rejected,
            "accepted_count": len(accepted),
            "duplicate_count": len(duplicates),
            "rejected_count": len(rejected),
        }

    def _accept_receipt(self, receipt: EventReceipt) -> tuple[str, str | None]:
        runtime = self._runtime(receipt.task_id)
        current = self._task_row(receipt.task_id)
        if runtime["assignment_status"] != "confirmed":
            return "rejected", "任务未经受托村组确认"
        if receipt.person_id != current["assignee_person_id"]:
            return "rejected", "回执人员不是任务当前受托人"
        self._qualified(receipt.person_id, current["required_qualification"], receipt.client_clock[:10])
        if receipt.event_type == "assigned":
            return "accepted", None
        lifecycle = runtime["lifecycle_state"]
        if receipt.event_type == "started":
            if lifecycle != "assigned":
                return "rejected", f"任务处于 {lifecycle}，不能开始"
            self.connection.execute(
                "UPDATE task_runtime SET lifecycle_state='in_progress',revision=revision+1,updated_at=? WHERE task_id=?",
                (self._now(), receipt.task_id),
            )
            return "accepted", None
        if receipt.event_type == "checkpoint":
            if lifecycle != "in_progress":
                return "rejected", f"任务处于 {lifecycle}，不能提交检查点"
            checkpoint_ids = {item["checkpoint_id"] for item in json.loads(current["checkpoints_json"])}
            if receipt.checkpoint_id not in checkpoint_ids:
                return "rejected", "检查点不属于当前任务版本"
            duplicate = self.connection.execute(
                "SELECT 1 FROM task_events WHERE task_id=? AND checkpoint_id=? AND validity_state='accepted' LIMIT 1",
                (receipt.task_id, receipt.checkpoint_id),
            ).fetchone()
            if duplicate is not None:
                return "rejected", "duplicate_checkin"
            return "accepted", None
        if receipt.event_type == "completed":
            if lifecycle != "in_progress":
                return "rejected", f"任务处于 {lifecycle}，不能完成"
            self.connection.execute(
                "UPDATE task_runtime SET lifecycle_state='completed',revision=revision+1,updated_at=? WHERE task_id=?",
                (self._now(), receipt.task_id),
            )
            return "accepted", None
        if receipt.event_type == "absolved":
            if lifecycle not in {"assigned", "in_progress"}:
                return "rejected", f"任务处于 {lifecycle}，不能登记免责"
            self.connection.execute(
                "UPDATE task_runtime SET lifecycle_state='absolved',revision=revision+1,updated_at=? WHERE task_id=?",
                (self._now(), receipt.task_id),
            )
            return "accepted", None
        return "rejected", "未知事件类型"

    def decide_event(self, actor_id: str, event_uuid: str, action: str, reason: str) -> dict[str, Any]:
        """对回执的任何调整只追加决定，不删除原始事件。"""
        self._require(actor_id, "event.decide")
        if action not in {"reject", "void", "reinstate"}:
            raise ValidationFailed("action 必须是 reject、void 或 reinstate")
        if not reason.strip():
            raise ValidationFailed("调整原因不能为空")
        event = self.connection.execute("SELECT * FROM task_events WHERE event_uuid=?", (event_uuid,)).fetchone()
        if event is None:
            raise NotFound("事件回执不存在")
        target_state = {"reject": "rejected", "void": "voided", "reinstate": "accepted"}[action]
        if event["validity_state"] == target_state:
            raise InvalidState(f"事件已经是 {target_state} 状态")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO event_decisions(event_uuid,action,reason,decided_by,created_at) VALUES(?,?,?,?,?)",
                (event_uuid, action, reason, actor_id, self._now()),
            )
            self.connection.execute(
                "UPDATE task_events SET validity_state=?,invalid_reason=? WHERE event_uuid=?",
                (target_state, None if action == "reinstate" else reason, event_uuid),
            )
            self._audit("task_event", event_uuid, f"event.{action}", actor_id, {"reason": reason})
        return {"event_uuid": event_uuid, "validity_state": target_state}

    def _effective_events(self, task_id: str) -> list[EffectiveEvent]:
        rows = self.connection.execute(
            "SELECT event_uuid,event_type,client_clock,checkpoint_id,quantity FROM task_events "
            "WHERE task_id=? AND validity_state='accepted' ORDER BY client_clock,event_uuid",
            (task_id,),
        ).fetchall()
        return [
            EffectiveEvent(
                event_uuid=row["event_uuid"],
                event_type=row["event_type"],
                client_clock=row["client_clock"],
                checkpoint_id=row["checkpoint_id"],
                quantity=Decimal(row["quantity"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ 周期结算

    def generate_settlement(
        self, actor_id: str, settlement_id: str, period_start: str, period_end: str
    ) -> dict[str, Any]:
        self._require(actor_id, "settlement.generate")
        if period_end < period_start:
            raise ValidationFailed("period_end 不能早于 period_start")
        existing = self.connection.execute(
            "SELECT settlement_id,state FROM settlements WHERE period_start=? AND period_end=?",
            (period_start, period_end),
        ).fetchone()
        if existing is not None:
            return {"settlement_id": existing["settlement_id"], "state": existing["state"], "replayed": True}
        # 结算候选：周期内有有效完成/免责事件落账的任务。
        terminal_rows = self.connection.execute(
            "SELECT e.task_id, max(e.client_clock) AS last_terminal "
            "FROM task_events e JOIN task_runtime r ON r.task_id=e.task_id "
            "WHERE e.validity_state='accepted' AND e.event_type IN ('completed','absolved') "
            "AND r.lifecycle_state IN ('completed','absolved') "
            "AND substr(e.client_clock,1,10) BETWEEN ? AND ? "
            "GROUP BY e.task_id",
            (period_start, period_end),
        ).fetchall()
        items: list[dict[str, Any]] = []
        snapshot_events: list[dict[str, Any]] = []
        snapshot_rules: list[dict[str, Any]] = []
        seen_rules: set[str] = set()
        for cand in terminal_rows:
            task_id = cand["task_id"]
            runtime = self._runtime(task_id)
            row = self._task_row(task_id)
            rule = self._applicable_rule(row["kind"], row["source"], cand["last_terminal"][:10])
            events = self._effective_events(task_id)
            task_snapshot = {
                "task_id": task_id,
                "version": row["version"],
                "kind": row["kind"],
                "source": row["source"],
                "lifecycle_state": runtime["lifecycle_state"],
            }
            component = value_task(rule, task_snapshot, events)
            items.append({
                "task_id": task_id,
                "task_version": row["version"],
                "village_group_id": row["assignee_village_group_id"],
                "person_id": row["assignee_person_id"],
                "lifecycle_state": runtime["lifecycle_state"],
                "source": row["source"],
                "amount_cny": component["amount_cny"],
                "component": component,
            })
            snapshot_events.extend(
                {
                    "event_uuid": e.event_uuid,
                    "event_type": e.event_type,
                    "client_clock": e.client_clock,
                    "checkpoint_id": e.checkpoint_id,
                    "quantity": decimal_text(e.quantity),
                }
                for e in events
            )
            if rule["rule_id"] not in seen_rules:
                seen_rules.add(rule["rule_id"])
                snapshot_rules.append({key: rule[key] for key in rule.keys()})
        items.sort(key=lambda item: item["task_id"])
        generated_at = self._now()
        result = {"period_start": period_start, "period_end": period_end, "items": items}
        input_sha256 = digest({
            "rules": sorted(snapshot_rules, key=lambda r: r["rule_id"]),
            "events": sorted(snapshot_events, key=lambda e: e["event_uuid"]),
        })
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO settlements(settlement_id,period_start,period_end,state,input_sha256,result_json,"
                "generated_by,generated_at) VALUES(?,?,?,'draft',?,?,?,?)",
                (settlement_id, period_start, period_end, input_sha256, canonical_json(result), actor_id, generated_at),
            )
            for item in items:
                self.connection.execute(
                    "INSERT INTO settlement_items(settlement_id,task_id,task_version,village_group_id,person_id,"
                    "lifecycle_state,source,amount_cny,component_json,item_state,ready_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'draft',?)",
                    (
                        settlement_id,
                        item["task_id"],
                        item["task_version"],
                        item["village_group_id"],
                        item["person_id"],
                        item["lifecycle_state"],
                        item["source"],
                        item["amount_cny"],
                        canonical_json(item["component"]),
                        generated_at,
                    ),
                )
            self.connection.execute(
                "INSERT INTO settlement_decisions(settlement_id,decision_type,note,created_by,created_at) "
                "VALUES(?,?,?,?,?)",
                (settlement_id, "draft_generated", "生成可复算草案", actor_id, generated_at),
            )
            self._audit("settlement", settlement_id, "settlement.generated", actor_id, {
                "period": [period_start, period_end], "items": len(items), "input_sha256": input_sha256,
            })
        return {
            "settlement_id": settlement_id,
            "state": "draft",
            "items": len(items),
            "total_amount_cny": decimal_text(sum((Decimal(i["amount_cny"]) for i in items), Decimal("0"))),
            "input_sha256": input_sha256,
            "replayed": False,
        }

    def _settlement(self, settlement_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM settlements WHERE settlement_id=?", (settlement_id,)).fetchone()
        if row is None:
            raise NotFound("结算单不存在")
        return row

    def settlement(self, actor_id: str, settlement_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self._settlement(settlement_id)
        items = self.connection.execute(
            "SELECT task_id,task_version,village_group_id,person_id,lifecycle_state,source,amount_cny,"
            "component_json,item_state,frozen_reason FROM settlement_items WHERE settlement_id=? ORDER BY task_id",
            (settlement_id,),
        ).fetchall()
        confirmations = self.connection.execute(
            "SELECT party,village_group_id,status,confirmed_by,created_at FROM settlement_confirmations "
            "WHERE settlement_id=? ORDER BY party,village_group_id",
            (settlement_id,),
        ).fetchall()
        open_disputes = self.connection.execute(
            "SELECT dispute_id,task_id,raised_by_party,reason,status,created_at FROM disputes "
            "WHERE settlement_id=? AND status='open' ORDER BY dispute_id",
            (settlement_id,),
        ).fetchall()
        return {
            "settlement_id": settlement_id,
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "state": self._settlement_state(settlement_id),
            "input_sha256": row["input_sha256"],
            "revision": row["revision"],
            "items": [
                {
                    "task_id": item["task_id"],
                    "task_version": item["task_version"],
                    "village_group_id": item["village_group_id"],
                    "person_id": item["person_id"],
                    "lifecycle_state": item["lifecycle_state"],
                    "source": item["source"],
                    "amount_cny": item["amount_cny"],
                    "item_state": item["item_state"],
                    "frozen_reason": item["frozen_reason"],
                    "component": json.loads(item["component_json"]),
                }
                for item in items
            ],
            "confirmations": [dict(item) for item in confirmations],
            "open_disputes": [dict(item) for item in open_disputes],
        }

    def _settlement_state(self, settlement_id: str) -> str:
        row = self.connection.execute(
            "SELECT count(*) total, "
            "sum(CASE WHEN item_state='frozen' THEN 1 ELSE 0 END) frozen, "
            "sum(CASE WHEN item_state='confirmed' THEN 1 ELSE 0 END) confirmed, "
            "sum(CASE WHEN item_state='paid' THEN 1 ELSE 0 END) paid, "
            "sum(CASE WHEN item_state='draft' THEN 1 ELSE 0 END) draft "
            "FROM settlement_items WHERE settlement_id=?",
            (settlement_id,),
        ).fetchone()
        total = row["total"] or 0
        if total == 0:
            return "draft"
        if row["paid"] == total:
            return "paid"
        if row["frozen"]:
            return "frozen" if (row["confirmed"] or 0) + (row["paid"] or 0) == 0 else "partially_confirmed"
        if row["confirmed"] + (row["paid"] or 0) == total:
            return "confirmed"
        if row["confirmed"] or row["paid"]:
            return "partially_confirmed"
        return "draft"

    def confirm_settlement(
        self, actor_id: str, settlement_id: str, party: str, note: str = ""
    ) -> dict[str, Any]:
        user = self._require(actor_id, "settlement.confirm")
        self._settlement(settlement_id)
        if party not in PARTY_BY_ROLE.values():
            raise ValidationFailed("party 必须是 village、station 或 finance")
        if PARTY_BY_ROLE[user["role"]] != party:
            raise Forbidden(f"角色 {user['role']} 只能代表 {PARTY_BY_ROLE[user['role']]} 确认")
        village_group_id = user["village_group_id"] if party == "village" else None
        if party == "village":
            owns = self.connection.execute(
                "SELECT 1 FROM settlement_items WHERE settlement_id=? AND village_group_id=? LIMIT 1",
                (settlement_id, village_group_id),
            ).fetchone()
            if owns is None:
                raise InvalidState("该村组在本结算单中没有补偿行")
        with transaction(self.connection, immediate=True):
            confirmed_at = self._now()
            self.connection.execute(
                "INSERT INTO settlement_confirmations(settlement_id,party,village_group_id,status,confirmed_by,note,created_at) "
                "VALUES(?,?,?,'confirmed',?,?,?)",
                (settlement_id, party, village_group_id, actor_id, note or None, confirmed_at),
            )
            promoted = self._promote_items(settlement_id)
            state = self._settlement_state(settlement_id)
            self.connection.execute(
                "UPDATE settlements SET state=?,revision=revision+1 WHERE settlement_id=?", (state, settlement_id)
            )
            self.connection.execute(
                "INSERT INTO settlement_decisions(settlement_id,decision_type,note,created_by,created_at) VALUES(?,?,?,?,?)",
                (settlement_id, f"confirm.{party}", note or f"{party} 确认", actor_id, confirmed_at),
            )
            self._audit("settlement", settlement_id, "settlement.confirmed", actor_id, {
                "party": party, "village_group_id": village_group_id, "promoted": promoted,
            })
        return {"settlement_id": settlement_id, "state": self._settlement_state(settlement_id), "promoted": promoted}

    def _party_confirmed_at(self, settlement_id: str, party: str, village_group_id: str | None) -> str | None:
        row = self.connection.execute(
            "SELECT max(created_at) AS confirmed_at FROM settlement_confirmations "
            "WHERE settlement_id=? AND party=? AND status='confirmed'"
            + (" AND village_group_id=?" if village_group_id is not None else " AND village_group_id IS NULL"),
            (settlement_id, party, village_group_id) if village_group_id is not None else (settlement_id, party),
        ).fetchone()
        return row["confirmed_at"]

    def _promote_items(self, settlement_id: str) -> list[str]:
        """每行独立判定：村组、保护站、财务三方在该行 ready_at 之后均确认才进入支付清单。

        争议解冻会刷新该行 ready_at，因此旧确认不会让它静默重新支付，
        而其它无争议行的确认与支付不受影响。
        """
        station_at = self._party_confirmed_at(settlement_id, "station", None)
        finance_at = self._party_confirmed_at(settlement_id, "finance", None)
        promoted: list[str] = []
        if station_at is None or finance_at is None:
            return promoted
        rows = self.connection.execute(
            "SELECT task_id,village_group_id,ready_at FROM settlement_items "
            "WHERE settlement_id=? AND item_state='draft' ORDER BY task_id",
            (settlement_id,),
        ).fetchall()
        for row in rows:
            village_at = self._party_confirmed_at(settlement_id, "village", row["village_group_id"])
            if village_at is None:
                continue
            if min(station_at, finance_at, village_at) < row["ready_at"]:
                continue
            self.connection.execute(
                "UPDATE settlement_items SET item_state='confirmed' WHERE settlement_id=? AND task_id=?",
                (settlement_id, row["task_id"]),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO payment_list_items(settlement_id,task_id,village_group_id,person_id,amount_cny,listed_at) "
                "SELECT ?,task_id,village_group_id,person_id,amount_cny,? FROM settlement_items "
                "WHERE settlement_id=? AND task_id=?",
                (settlement_id, self._now(), settlement_id, row["task_id"]),
            )
            promoted.append(row["task_id"])
        return promoted

    # ------------------------------------------------------------------ 争议

    def raise_dispute(
        self, actor_id: str, settlement_id: str, task_id: str, reason: str
    ) -> dict[str, Any]:
        user = self._require(actor_id, "dispute.raise")
        self._settlement(settlement_id)
        if not reason.strip():
            raise ValidationFailed("争议原因不能为空")
        item = self.connection.execute(
            "SELECT * FROM settlement_items WHERE settlement_id=? AND task_id=?",
            (settlement_id, task_id),
        ).fetchone()
        if item is None:
            raise NotFound("结算任务行不存在")
        if item["item_state"] == "paid":
            raise InvalidState("已支付任务不能再争议")
        party = PARTY_BY_ROLE[user["role"]]
        dispute_id = f"disp-{settlement_id}-{task_id}-{party}"
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO disputes(dispute_id,settlement_id,task_id,raised_by_party,raised_by,reason,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (dispute_id, settlement_id, task_id, party, actor_id, reason, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该方已对此任务提出争议") from exc
            self.connection.execute(
                "UPDATE settlement_items SET item_state='frozen',frozen_reason=? WHERE settlement_id=? AND task_id=?",
                (reason, settlement_id, task_id),
            )
            self.connection.execute(
                "DELETE FROM payment_list_items WHERE settlement_id=? AND task_id=?",
                (settlement_id, task_id),
            )
            self.connection.execute(
                "INSERT INTO dispute_events(dispute_id,event_type,actor_id,payload_json,created_at) VALUES(?,?,?,?,?)",
                (dispute_id, "dispute.raised", actor_id, canonical_json({"reason": reason}), self._now()),
            )
            self.connection.execute(
                "INSERT INTO settlement_decisions(settlement_id,decision_type,note,created_by,created_at) VALUES(?,?,?,?,?)",
                (settlement_id, "item.frozen", f"{task_id} 因争议冻结：{reason}", actor_id, self._now()),
            )
            self.connection.execute(
                "UPDATE settlements SET state=?,revision=revision+1 WHERE settlement_id=?",
                (self._settlement_state(settlement_id), settlement_id),
            )
            self._audit("dispute", dispute_id, "dispute.raised", actor_id, {"task_id": task_id})
        return {"dispute_id": dispute_id, "task_id": task_id, "item_state": "frozen"}

    def resolve_dispute(
        self, actor_id: str, dispute_id: str, resolution_note: str
    ) -> dict[str, Any]:
        """解决争议：按当前有效事件重算该任务金额（追加决定），解冻回草案。"""
        self._require(actor_id, "dispute.raise")
        if not resolution_note.strip():
            raise ValidationFailed("解决说明不能为空")
        dispute = self.connection.execute("SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)).fetchone()
        if dispute is None:
            raise NotFound("争议不存在")
        if dispute["status"] != "open":
            raise InvalidState("争议已关闭")
        settlement_id = dispute["settlement_id"]
        task_id = dispute["task_id"]
        item = self.connection.execute(
            "SELECT * FROM settlement_items WHERE settlement_id=? AND task_id=?",
            (settlement_id, task_id),
        ).fetchone()
        with transaction(self.connection, immediate=True):
            row = self._task_row(task_id, item["task_version"])
            runtime = self._runtime(task_id)
            finish = self.connection.execute(
                "SELECT client_clock FROM task_events WHERE task_id=? AND event_type IN ('completed','absolved') "
                "AND validity_state='accepted' ORDER BY client_clock DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if finish is None:
                raise InvalidState("任务已无有效完成/免责事件，无法重算")
            rule = self._applicable_rule(row["kind"], row["source"], finish["client_clock"][:10])
            component = value_task(
                rule,
                {"kind": row["kind"], "source": row["source"], "lifecycle_state": runtime["lifecycle_state"]},
                self._effective_events(task_id),
            )
            before_amount = item["amount_cny"]
            after_amount = component["amount_cny"]
            resolved_at = self._now()
            # 仅解冻受影响任务行：回到草案并刷新 ready_at，旧确认早于 ready_at，
            # 三方必须就重算结果重新确认；无争议行的确认和支付保持不变。
            self.connection.execute(
                "UPDATE settlement_items SET amount_cny=?,component_json=?,item_state='draft',"
                "frozen_reason=NULL,ready_at=? WHERE settlement_id=? AND task_id=?",
                (after_amount, canonical_json(component), resolved_at, settlement_id, task_id),
            )
            self.connection.execute(
                "UPDATE disputes SET status='resolved',resolution_note=?,resolved_by=?,resolved_at=? WHERE dispute_id=?",
                (resolution_note, actor_id, resolved_at, dispute_id),
            )
            self.connection.execute(
                "INSERT INTO dispute_events(dispute_id,event_type,actor_id,payload_json,created_at) VALUES(?,?,?,?,?)",
                (dispute_id, "dispute.resolved", actor_id,
                 canonical_json({"note": resolution_note, "before_amount_cny": before_amount, "after_amount_cny": after_amount}),
                 resolved_at),
            )
            self.connection.execute(
                "INSERT INTO settlement_decisions(settlement_id,decision_type,note,created_by,created_at) VALUES(?,?,?,?,?)",
                (settlement_id, "item.adjusted",
                 f"{task_id} 争议解决并重算：{before_amount} -> {after_amount}；{resolution_note}",
                 actor_id, resolved_at),
            )
            self.connection.execute(
                "UPDATE settlements SET state=?,revision=revision+1 WHERE settlement_id=?",
                (self._settlement_state(settlement_id), settlement_id),
            )
            self._refresh_result_json(settlement_id)
            self._audit("dispute", dispute_id, "dispute.resolved", actor_id, {
                "task_id": task_id, "before_amount_cny": before_amount, "after_amount_cny": after_amount,
            })
        return {"dispute_id": dispute_id, "status": "resolved", "amount_cny": after_amount}

    def open_disputes(self, actor_id: str) -> dict[str, Any]:
        """重启后恢复未结争议的入口。"""
        self._require(actor_id, "report.read")
        rows = self.connection.execute(
            "SELECT d.dispute_id,d.settlement_id,d.task_id,d.raised_by_party,d.reason,d.created_at,"
            "s.period_start,s.period_end,i.amount_cny,i.item_state "
            "FROM disputes d JOIN settlements s ON s.settlement_id=d.settlement_id "
            "JOIN settlement_items i ON i.settlement_id=d.settlement_id AND i.task_id=d.task_id "
            "WHERE d.status='open' ORDER BY d.created_at,d.dispute_id"
        ).fetchall()
        return {"open_disputes": [dict(row) for row in rows]}

    def _refresh_result_json(self, settlement_id: str) -> None:
        items = self.connection.execute(
            "SELECT task_id,task_version,village_group_id,person_id,lifecycle_state,source,amount_cny,component_json "
            "FROM settlement_items WHERE settlement_id=? ORDER BY task_id",
            (settlement_id,),
        ).fetchall()
        settlement = self._settlement(settlement_id)
        result = {
            "period_start": settlement["period_start"],
            "period_end": settlement["period_end"],
            "items": [
                {
                    "task_id": row["task_id"],
                    "task_version": row["task_version"],
                    "village_group_id": row["village_group_id"],
                    "person_id": row["person_id"],
                    "lifecycle_state": row["lifecycle_state"],
                    "source": row["source"],
                    "amount_cny": row["amount_cny"],
                    "component": json.loads(row["component_json"]),
                }
                for row in items
            ],
        }
        self.connection.execute(
            "UPDATE settlements SET result_json=? WHERE settlement_id=?",
            (canonical_json(result), settlement_id),
        )

    # ------------------------------------------------------------------ 支付与解释

    def payment_list(self, actor_id: str, settlement_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        self._settlement(settlement_id)
        rows = self.connection.execute(
            "SELECT task_id,village_group_id,person_id,amount_cny,listed_at FROM payment_list_items "
            "WHERE settlement_id=? ORDER BY task_id",
            (settlement_id,),
        ).fetchall()
        return {
            "settlement_id": settlement_id,
            "items": [dict(row) for row in rows],
            "total_amount_cny": decimal_text(
                sum((Decimal(row["amount_cny"]) for row in rows), Decimal("0"))
            ),
        }

    def mark_paid(self, actor_id: str, settlement_id: str) -> dict[str, Any]:
        self._require(actor_id, "payment.write")
        self._settlement(settlement_id)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE settlement_items SET item_state='paid' WHERE settlement_id=? AND item_state='confirmed'",
                (settlement_id,),
            )
            paid = cursor.rowcount
            if paid == 0:
                raise InvalidState("没有已确认待支付的任务行")
            self.connection.execute(
                "INSERT INTO settlement_decisions(settlement_id,decision_type,note,created_by,created_at) VALUES(?,?,?,?,?)",
                (settlement_id, "payment.marked", f"财务标记 {paid} 行已支付", actor_id, self._now()),
            )
            state = self._settlement_state(settlement_id)
            self.connection.execute("UPDATE settlements SET state=?,revision=revision+1 WHERE settlement_id=?", (state, settlement_id))
            self._audit("settlement", settlement_id, "settlement.paid", actor_id, {"paid_rows": paid})
        return {"settlement_id": settlement_id, "state": self._settlement_state(settlement_id), "paid_rows": paid}

    def explain_compensation(self, actor_id: str, settlement_id: str, task_id: str) -> dict[str, Any]:
        """逐笔解释补偿由哪些有效事件组成，并现场复算与草案比对。"""
        self._require(actor_id, "report.read")
        item = self.connection.execute(
            "SELECT * FROM settlement_items WHERE settlement_id=? AND task_id=?",
            (settlement_id, task_id),
        ).fetchone()
        if item is None:
            raise NotFound("结算任务行不存在")
        row = self._task_row(task_id, item["task_version"])
        settlement = self._settlement(settlement_id)
        events = self.connection.execute(
            "SELECT event_uuid,device_serial,person_id,event_type,client_clock,checkpoint_id,quantity,"
            "evidence_ref,validity_state,invalid_reason FROM task_events WHERE task_id=? ORDER BY client_clock,event_uuid",
            (task_id,),
        ).fetchall()
        finish = self.connection.execute(
            "SELECT client_clock FROM task_events WHERE task_id=? AND event_type IN ('completed','absolved') "
            "AND validity_state='accepted' ORDER BY client_clock DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        recomputed = None
        rule_row = None
        if finish is not None:
            rule_row = self._applicable_rule(row["kind"], row["source"], finish["client_clock"][:10])
            recomputed = value_task(
                rule_row,
                {"kind": row["kind"], "source": row["source"], "lifecycle_state": item["lifecycle_state"]},
                self._effective_events(task_id),
            )
        decisions = self.connection.execute(
            "SELECT d.action,d.reason,d.decided_by,d.created_at FROM event_decisions d "
            "JOIN task_events e ON e.event_uuid=d.event_uuid WHERE e.task_id=? ORDER BY d.decision_id",
            (task_id,),
        ).fetchall()
        return {
            "settlement_id": settlement_id,
            "task_id": task_id,
            "task_version": item["task_version"],
            "period": [settlement["period_start"], settlement["period_end"]],
            "draft_amount_cny": item["amount_cny"],
            "item_state": item["item_state"],
            "component": json.loads(item["component_json"]),
            "recomputed": recomputed,
            "recomputable": recomputed is not None,
            "matches_draft": recomputed is not None and recomputed["amount_cny"] == item["amount_cny"],
            "pricing_rule": None if rule_row is None else {key: rule_row[key] for key in rule_row.keys()},
            "events": [dict(event) for event in events],
            "decisions": [dict(decision) for decision in decisions],
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM comanage_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            if row["previous_hash"] != previous_hash or row["event_hash"] != digest(body):
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
