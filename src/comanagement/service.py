"""社区共管任务与补偿核算的领域用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    Blockade,
    DeviceRegistration,
    Organization,
    PricingRuleInput,
    Qualification,
    ReceiptItem,
    ServiceArea,
    TaskDefinition,
    Checkpoint,
)
from .pricing import (
    LineFact,
    PricingRule,
    ReceiptFact,
    blockade_covers,
    canonical_json,
    compute_line,
    decimal_text,
    dedupe_checkins,
    digest,
    money,
    totalize,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS: Mapping[str, set[str]] = {
    "villager": {"receipt.upload"},
    "village_head": {"task.write", "transfer.propose", "transfer.decide", "confirmation.write"},
    "station": {
        "catalog.write", "qualification.write", "pricing.write", "task.write", "task.manage",
        "transfer.propose", "blockade.write", "settlement.compose", "confirmation.write",
        "report.read",
    },
    "finance": {
        "pricing.write", "settlement.compose", "confirmation.write", "payment.finalize",
        "report.read",
    },
    "auditor": {"report.read", "audit.read"},
}

CONFIRMATION_SCOPES = ("village_head", "station", "finance")
TASK_FINISHED_STATES = ("completed", "incomplete", "cancelled")


class ComanagementService:
    """在单个 SQLite 连接上提供全部社区共管业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM comanage_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _require_any(self, user_id: str, permissions: Sequence[str]) -> sqlite3.Row:
        user = self._user(user_id)
        if not any(permission in ROLE_PERMISSIONS[user["role"]] for permission in permissions):
            raise Forbidden(f"角色 {user['role']} 无权执行此操作（需要 {permissions} 之一）")
        return user

    def _can(self, user_id: str, permission: str) -> bool:
        user = self._user(user_id)
        return permission in ROLE_PERMISSIONS[user["role"]]

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
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
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

    def _idempotency(self, scope: str, key: str, raw: Mapping[str, Any]) -> dict[str, Any] | None:
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM comanage_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != request_digest:
            raise Conflict("幂等键对应不同的请求内容")
        return json.loads(stored["response_json"])

    def _save_idempotency(self, scope: str, key: str, raw: Mapping[str, Any], response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO comanage_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, digest(raw), canonical_json(response), self._now()),
        )

    def create_user(
        self, user_id: str, display_name: str, role: str, village_group_id: str | None = None
    ) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        if role in ("villager", "village_head"):
            if not village_group_id:
                raise ValidationFailed("村民和村组组长必须归属一个村组")
            if self.connection.execute(
                "SELECT 1 FROM organizations WHERE org_id=? AND kind='village_group'",
                (village_group_id,),
            ).fetchone() is None:
                raise ValidationFailed("归属村组不存在")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO comanage_users(user_id,display_name,role,village_group_id,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, village_group_id, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role, "village_group_id": village_group_id}

    # ------------------------------------------------------------ 组织与区域

    def create_organization(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        org = Organization.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO organizations(org_id,name,kind,created_by,created_at) VALUES(?,?,?,?,?)",
                    (org.org_id, org.name, org.kind, actor_id, self._now()),
                )
                self._audit("organization", org.org_id, "organization.created", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"组织编号已存在: {org.org_id}") from exc
        return {"org_id": org.org_id, "name": org.name, "kind": org.kind}

    def create_service_area(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        area = ServiceArea.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO service_areas(service_area_id,name,geometry_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (area.service_area_id, area.name, canonical_json(area.geometry), actor_id, self._now()),
                )
                self._audit("service_area", area.service_area_id, "service_area.created", actor_id, {"name": area.name})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"服务区域编号已存在: {area.service_area_id}") from exc
        return {"service_area_id": area.service_area_id, "name": area.name}

    def create_checkpoint(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        checkpoint = Checkpoint.from_dict(raw)
        if self.connection.execute(
            "SELECT 1 FROM service_areas WHERE service_area_id=?", (checkpoint.service_area_id,)
        ).fetchone() is None:
            raise NotFound("服务区域不存在")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO checkpoints(checkpoint_id,service_area_id,name,position_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        checkpoint.checkpoint_id, checkpoint.service_area_id, checkpoint.name,
                        canonical_json(checkpoint.position), actor_id, self._now(),
                    ),
                )
                self._audit("checkpoint", checkpoint.checkpoint_id, "checkpoint.created", actor_id,
                            {"service_area_id": checkpoint.service_area_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"检查点编号已存在: {checkpoint.checkpoint_id}") from exc
        return {"checkpoint_id": checkpoint.checkpoint_id, "service_area_id": checkpoint.service_area_id}

    # ---------------------------------------------------------------- 资格

    def grant_qualification(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "qualification.write")
        qualification = Qualification.from_dict(raw)
        self._user(qualification.person_id)
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO personnel_qualifications(person_id,task_family,level,valid_from,valid_until,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        qualification.person_id, qualification.task_family, qualification.level,
                        qualification.valid_from, qualification.valid_until, actor_id, self._now(),
                    ),
                )
                qualification_id = int(cursor.lastrowid)
                self._audit("qualification", str(qualification_id), "qualification.granted", actor_id,
                            {"person_id": qualification.person_id, "task_family": qualification.task_family})
        except sqlite3.IntegrityError as exc:
            raise Conflict("该人员同类资格起始日期重复") from exc
        return {"qualification_id": qualification_id, "person_id": qualification.person_id,
                "task_family": qualification.task_family, "level": qualification.level}

    def _qualified(self, person_id: str, task_family: str, on_date: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM personnel_qualifications WHERE person_id=? AND task_family=? AND state='active' "
            "AND valid_from<=? AND (valid_until IS NULL OR valid_until>=?) LIMIT 1",
            (person_id, task_family, on_date, on_date),
        ).fetchone()
        return row is not None

    # -------------------------------------------------------------- 计价规则

    def publish_pricing_rule(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "pricing.write")
        rule = PricingRuleInput.from_dict(raw)
        with transaction(self.connection, immediate=True):
            previous = self.connection.execute(
                "SELECT version FROM pricing_rules WHERE rule_id=? ORDER BY version DESC LIMIT 1",
                (rule.rule_id,),
            ).fetchone()
            version = 1 if previous is None else int(previous["version"]) + 1
            if previous is not None:
                self.connection.execute(
                    "UPDATE pricing_rules SET state='superseded' WHERE rule_id=? AND version=?",
                    (rule.rule_id, previous["version"]),
                )
            try:
                self.connection.execute(
                    "INSERT INTO pricing_rules(rule_id,version,task_family,task_kind,base_unit,base_rate_cny,"
                    "reinforcement_rate_cny,repeat_within_minutes,road_closure_excuse_code,created_by,created_at,"
                    "supersedes_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        rule.rule_id, version, rule.task_family, rule.task_kind, rule.base_unit,
                        decimal_text(rule.base_rate), decimal_text(rule.reinforcement_rate),
                        rule.repeat_within_minutes, rule.road_closure_excuse_code, actor_id, self._now(),
                        None if previous is None else int(previous["version"]),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("计价规则版本冲突") from exc
            self._audit("pricing_rule", rule.rule_id, "pricing_rule.published", actor_id,
                        {"version": version, "task_family": rule.task_family, "task_kind": rule.task_kind})
        return {"rule_id": rule.rule_id, "version": version, "state": "active"}

    def _active_rule(self, rule_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM pricing_rules WHERE rule_id=? ORDER BY version DESC LIMIT 1", (rule_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"计价规则不存在: {rule_id}")
        return row

    def _snapshot_rule(self, rule_id: str, version: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM pricing_rules WHERE rule_id=? AND version=?", (rule_id, version)
        ).fetchone()
        if row is None:
            raise NotFound("任务版本引用的计价规则快照不存在")
        return row

    # ---------------------------------------------------------------- 任务

    def _validate_people(self, person_ids: Sequence[str], task_family: str, org_id: str, on_date: str) -> None:
        for person_id in person_ids:
            person = self._user(person_id)
            if person["village_group_id"] != org_id:
                raise ValidationFailed(f"人员 {person_id} 不属于责任村组 {org_id}")
            if not self._qualified(person_id, task_family, on_date):
                raise ValidationFailed(f"人员 {person_id} 缺少 {task_family} 的有效资格")

    def _write_version(
        self,
        definition: TaskDefinition,
        version: int,
        responsible_org_id: str,
        supersedes_version: int | None,
        change_reason: str,
        actor_id: str,
        now: str,
    ) -> None:
        content = {
            "task_family": definition.task_family,
            "task_kind": definition.task_kind,
            "service_area_id": definition.service_area_id,
            "scheduled_for": definition.scheduled_for,
            "person_ids": list(definition.person_ids),
            "checkpoints": [
                {"checkpoint_id": item.checkpoint_id, "required": item.required}
                for item in definition.checkpoints
            ],
            "pricing_rule_id": definition.pricing_rule_id,
        }
        rule = self._active_rule(definition.pricing_rule_id)
        if rule["task_family"] != definition.task_family or rule["task_kind"] != definition.task_kind:
            raise ValidationFailed("计价规则与任务族或任务类型不匹配")
        if self.connection.execute(
            "SELECT 1 FROM service_areas WHERE service_area_id=?", (definition.service_area_id,)
        ).fetchone() is None:
            raise NotFound("服务区域不存在")
        area_checkpoints = {
            row["checkpoint_id"]
            for row in self.connection.execute(
                "SELECT checkpoint_id FROM checkpoints WHERE service_area_id=?",
                (definition.service_area_id,),
            )
        }
        for item in definition.checkpoints:
            if item.checkpoint_id not in area_checkpoints:
                raise ValidationFailed(f"检查点 {item.checkpoint_id} 不在服务区域内")
        self.connection.execute(
            "INSERT INTO task_versions(task_id,version,task_family,task_kind,service_area_id,pricing_rule_id,"
            "pricing_rule_version,definition_json,content_sha256,change_reason,created_by,created_at,supersedes_version) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                definition.task_id, version, definition.task_family, definition.task_kind,
                definition.service_area_id, definition.pricing_rule_id, int(rule["version"]),
                canonical_json(content), digest(content), change_reason, actor_id, now, supersedes_version,
            ),
        )
        for person_id in definition.person_ids:
            self.connection.execute(
                "INSERT INTO task_assignments(task_id,version,person_id,role,state,joined_at) "
                "VALUES(?,?,?,'patroller','active',?)",
                (definition.task_id, version, person_id, now),
            )
        for sequence, item in enumerate(definition.checkpoints, start=1):
            self.connection.execute(
                "INSERT INTO task_checkpoints(task_id,version,checkpoint_id,sequence_no,required) "
                "VALUES(?,?,?,?,?)",
                (definition.task_id, version, item.checkpoint_id, sequence, 1 if item.required else 0),
            )

    def create_task(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "task.write")
        definition = TaskDefinition.from_dict(raw)
        cached = self._idempotency("task_create", definition.idempotency_key, raw)
        if cached is not None:
            return cached
        actor = self._user(actor_id)
        responsible_org_id = actor["village_group_id"] if actor["role"] == "village_head" else raw.get("responsible_org_id")
        if not responsible_org_id:
            raise ValidationFailed("保护站代建任务必须提供 responsible_org_id")
        if self.connection.execute(
            "SELECT 1 FROM organizations WHERE org_id=? AND kind='village_group'", (responsible_org_id,)
        ).fetchone() is None:
            raise NotFound("责任村组不存在")
        scheduled_date = definition.scheduled_for[:10]
        self._validate_people(definition.person_ids, definition.task_family, responsible_org_id, scheduled_date)
        now = self._now()
        response: dict[str, Any]
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO tasks(task_id,current_version,state,responsible_org_id,scheduled_for,created_at) "
                    "VALUES(?,1,'assigned',?,?,?)",
                    (definition.task_id, responsible_org_id, definition.scheduled_for, now),
                )
                self._write_version(definition, 1, responsible_org_id, None, "created", actor_id, now)
                response = {
                    "task_id": definition.task_id, "version": 1, "state": "assigned",
                    "responsible_org_id": responsible_org_id,
                }
                self._save_idempotency("task_create", definition.idempotency_key, raw, response)
                self._audit("task", definition.task_id, "task.created", actor_id,
                            {"version": 1, "task_family": definition.task_family, "task_kind": definition.task_kind})
        except sqlite3.IntegrityError as exc:
            raise Conflict("任务编号或幂等键冲突") from exc
        return response

    def revise_task(
        self, actor_id: str, task_id: str, raw: Mapping[str, Any], reason: str, expected_version: int
    ) -> dict[str, Any]:
        """追加一个任务版本（计划调整）。任务一旦开始即禁止改派。"""

        self._require(actor_id, "task.write")
        if not reason.strip():
            raise ValidationFailed("调整任务必须填写 change_reason")
        task = self._task_row(task_id)
        if task["state"] != "assigned":
            raise InvalidState("任务已经开始或结束，不能再修改版本；如需换人请走跨村组转派且仅限未开始任务")
        if task["current_version"] != expected_version:
            raise Conflict("任务版本已变化，请基于最新版本修改")
        definition = TaskDefinition.from_dict({**raw, "task_id": task_id})
        scheduled_date = definition.scheduled_for[:10]
        self._validate_people(definition.person_ids, definition.task_family, task["responsible_org_id"], scheduled_date)
        new_version = expected_version + 1
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE tasks SET current_version=? WHERE task_id=? AND current_version=?",
                (new_version, task_id, expected_version),
            )
            self._write_version(definition, new_version, task["responsible_org_id"], expected_version, reason, actor_id, now)
            self._audit("task", task_id, "task.revised", actor_id,
                        {"version": new_version, "supersedes_version": expected_version, "reason": reason})
        return {"task_id": task_id, "version": new_version, "state": "assigned", "change_reason": reason}

    def task(self, task_id: str) -> dict[str, Any]:
        task = self._task_row(task_id)
        versions = self.connection.execute(
            "SELECT version,task_family,task_kind,service_area_id,pricing_rule_id,pricing_rule_version,"
            "change_reason,supersedes_version,created_by,created_at FROM task_versions WHERE task_id=? ORDER BY version",
            (task_id,),
        ).fetchall()
        assignments = self.connection.execute(
            "SELECT version,person_id,state FROM task_assignments WHERE task_id=? ORDER BY version,person_id",
            (task_id,),
        ).fetchall()
        return {
            "task": dict(task),
            "versions": [dict(row) for row in versions],
            "assignments": [dict(row) for row in assignments],
        }

    def _task_row(self, task_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFound(f"任务不存在: {task_id}")
        return row

    def start_task(self, actor_id: str, task_id: str) -> dict[str, Any]:
        self._require(actor_id, "task.manage")
        with transaction(self.connection, immediate=True):
            task = self._task_row(task_id)
            if task["state"] != "assigned":
                raise InvalidState("任务不是待开始状态")
            self.connection.execute(
                "UPDATE tasks SET state='in_progress',started_at=? WHERE task_id=?",
                (self._now(), task_id),
            )
            self._audit("task", task_id, "task.started", actor_id, {})
        return {"task_id": task_id, "state": "in_progress"}

    def _maybe_complete(self, task_id: str) -> None:
        """所有当值人员都完成签到与签退时自动完工。"""

        task = self.connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None or task["state"] != "in_progress":
            return
        version = task["current_version"]
        assigned = [
            row["person_id"]
            for row in self.connection.execute(
                "SELECT person_id FROM task_assignments WHERE task_id=? AND version=? AND state='active'",
                (task_id, version),
            )
        ]
        for person_id in assigned:
            types = {
                row["receipt_type"]
                for row in self.connection.execute(
                    "SELECT DISTINCT receipt_type FROM receipts WHERE task_id=? AND person_id=? "
                    "AND ingest_state IN ('accepted','gapped')",
                    (task_id, person_id),
                )
            }
            if "sign_on" not in types or "sign_off" not in types:
                return
        self.connection.execute(
            "UPDATE tasks SET state='completed',finished_at=? WHERE task_id=? AND state='in_progress'",
            (self._now(), task_id),
        )
        self._audit("task", task_id, "task.completed", "system", {"trigger": "all_signed_off"})

    def close_task(self, actor_id: str, task_id: str, state: str) -> dict[str, Any]:
        """保护站登记未完工任务（如封路未完成），状态本身不产生扣减，封路事实才免责。"""

        self._require(actor_id, "task.manage")
        if state not in ("completed", "incomplete", "cancelled"):
            raise ValidationFailed("state 必须是 completed、incomplete 或 cancelled")
        with transaction(self.connection, immediate=True):
            task = self._task_row(task_id)
            if task["state"] in TASK_FINISHED_STATES:
                raise InvalidState("任务已经结束")
            self.connection.execute(
                "UPDATE tasks SET state=?,finished_at=? WHERE task_id=?",
                (state, self._now(), task_id),
            )
            self._audit("task", task_id, "task.closed", actor_id, {"state": state})
        return {"task_id": task_id, "state": state}

    # ----------------------------------------------------------- 设备与回执

    def register_device(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        device = DeviceRegistration.from_dict(raw)
        self._user(device.person_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO field_devices(device_serial,person_id,model_name,registered_at) "
                    "VALUES(?,?,?,?)",
                    (device.device_serial, device.person_id, device.model_name, self._now()),
                )
                self.connection.execute(
                    "INSERT INTO device_streams(device_serial,last_sequence) VALUES(?,0)",
                    (device.device_serial,),
                )
                self._audit("device", device.device_serial, "device.registered", actor_id,
                            {"person_id": device.person_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"设备序号已登记: {device.device_serial}") from exc
        return {"device_serial": device.device_serial, "person_id": device.person_id}

    def upload_receipts(
        self, actor_id: str, device_serial: str, items: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """离线设备补传一批回执。

        按 (设备序号) 识别：
        - 同序号同内容：重放，幂等吸收，不重复计酬；
        - 同序号不同内容：分叉，拒绝新内容并留痕，原回执不变；
        - 序号跳跃：缺口，照常入账但标记 gapped，等待缺口序号补传。
        """

        self._require_any(actor_id, ("receipt.upload", "catalog.write"))
        device = self.connection.execute(
            "SELECT * FROM field_devices WHERE device_serial=?", (device_serial,)
        ).fetchone()
        if device is None:
            raise NotFound("设备未登记")
        if device["revoked"]:
            raise Forbidden("设备已注销")
        actor_row = self._user(actor_id)
        if actor_row["user_id"] != device["person_id"] and "catalog.write" not in ROLE_PERMISSIONS[actor_row["role"]]:
            raise Forbidden("只能由设备绑定人员本人或保护站补传该设备回执")
        parsed = [ReceiptItem.from_dict(item) for item in items]
        if not parsed:
            raise ValidationFailed("补传批次不能为空")
        sequences = [item.sequence_no for item in parsed]
        if len(set(sequences)) != len(sequences):
            raise Conflict("同一批次内设备序号不能重复")

        accepted: list[int] = []
        replayed: list[int] = []
        forked: list[dict[str, Any]] = []
        gapped: list[int] = []
        gap_filled: list[int] = []
        with transaction(self.connection, immediate=True):
            stream = self.connection.execute(
                "SELECT last_sequence FROM device_streams WHERE device_serial=?", (device_serial,)
            ).fetchone()
            last_sequence = int(stream["last_sequence"])
            for item, raw in zip(parsed, items):
                task = self._task_row(item.task_id)
                assignment = self.connection.execute(
                    "SELECT 1 FROM task_assignments WHERE task_id=? AND version=? AND person_id=? AND state='active'",
                    (item.task_id, task["current_version"], item.person_id),
                ).fetchone()
                if assignment is None:
                    raise ValidationFailed(f"序号 {item.sequence_no}：回执人员不是该任务当前版本的当值人员")
                if item.person_id != device["person_id"]:
                    raise ValidationFailed(f"序号 {item.sequence_no}：设备绑定人员与回执人员不一致")
                if item.checkpoint_id is not None:
                    if self.connection.execute(
                        "SELECT 1 FROM task_checkpoints WHERE task_id=? AND version=? AND checkpoint_id=?",
                        (item.task_id, task["current_version"], item.checkpoint_id),
                    ).fetchone() is None:
                        raise ValidationFailed(f"序号 {item.sequence_no}：检查点不属于该任务版本")
                content = {
                    "task_id": item.task_id,
                    "receipt_type": item.receipt_type,
                    "checkpoint_id": item.checkpoint_id,
                    "person_id": item.person_id,
                    "occurred_at": item.occurred_at,
                    "payload": item.payload,
                }
                content_hash = digest(content)
                existing = self.connection.execute(
                    "SELECT content_sha256 FROM receipts WHERE device_serial=? AND sequence_no=?",
                    (device_serial, item.sequence_no),
                ).fetchone()
                if existing is not None:
                    if existing["content_sha256"] == content_hash:
                        replayed.append(item.sequence_no)
                    else:
                        forked.append({
                            "sequence_no": item.sequence_no,
                            "existing_sha256": existing["content_sha256"],
                            "incoming_sha256": content_hash,
                        })
                        self._audit("receipt", f"{device_serial}:{item.sequence_no}", "receipt.fork_detected",
                                    actor_id, {"task_id": item.task_id})
                    continue
                if item.sequence_no == last_sequence + 1:
                    ingest_state = "accepted"
                elif item.sequence_no <= last_sequence:
                    # 填补之前标记的缺口。
                    ingest_state = "accepted"
                    gap_filled.append(item.sequence_no)
                else:
                    ingest_state = "gapped"
                    gapped.append(item.sequence_no)
                now = self._now()
                cursor = self.connection.execute(
                    "INSERT INTO receipts(device_serial,sequence_no,task_id,receipt_type,checkpoint_id,person_id,"
                    "occurred_at,received_at,payload_json,content_sha256,ingest_state) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        device_serial, item.sequence_no, item.task_id, item.receipt_type, item.checkpoint_id,
                        item.person_id, item.occurred_at, now, canonical_json(item.payload), content_hash,
                        ingest_state,
                    ),
                )
                receipt_id = int(cursor.lastrowid)
                accepted.append(receipt_id)
                if item.sequence_no > last_sequence:
                    last_sequence = item.sequence_no
                if item.receipt_type == "sign_on" and task["state"] == "assigned":
                    self.connection.execute(
                        "UPDATE tasks SET state='in_progress',started_at=COALESCE(started_at,?) WHERE task_id=?",
                        (item.occurred_at, item.task_id),
                    )
                    self._audit("task", item.task_id, "task.started", item.person_id, {"source_receipt_id": receipt_id})
            self.connection.execute(
                "UPDATE device_streams SET last_sequence=? WHERE device_serial=?",
                (last_sequence, device_serial),
            )
            for task_id in {item.task_id for item in parsed}:
                self._maybe_complete(task_id)
            self._audit("device", device_serial, "receipts.uploaded", actor_id, {
                "accepted": len(accepted), "replayed": replayed, "forked": forked,
                "gapped": gapped, "gap_filled": gap_filled,
            })
        return {
            "device_serial": device_serial,
            "accepted_receipt_ids": accepted,
            "replayed_sequences": replayed,
            "forked_sequences": forked,
            "gapped_sequences": gapped,
            "gap_filled_sequences": gap_filled,
        }

    # ---------------------------------------------------------------- 封路

    def register_blockade(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "blockade.write")
        blockade = Blockade.from_dict(raw)
        if self.connection.execute(
            "SELECT 1 FROM service_areas WHERE service_area_id=?", (blockade.service_area_id,)
        ).fetchone() is None:
            raise NotFound("服务区域不存在")
        if blockade.checkpoint_id and self.connection.execute(
            "SELECT 1 FROM checkpoints WHERE checkpoint_id=? AND service_area_id=?",
            (blockade.checkpoint_id, blockade.service_area_id),
        ).fetchone() is None:
            raise ValidationFailed("检查点不在该服务区域内")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO blockade_events(blockade_id,service_area_id,checkpoint_id,starts_at,ends_at,"
                    "reason_code,evidence_json,registered_by,registered_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        blockade.blockade_id, blockade.service_area_id, blockade.checkpoint_id,
                        blockade.starts_at, blockade.ends_at, blockade.reason_code,
                        canonical_json(blockade.evidence), actor_id, self._now(),
                    ),
                )
                self._audit("blockade", blockade.blockade_id, "blockade.registered", actor_id,
                            {"service_area_id": blockade.service_area_id, "reason_code": blockade.reason_code})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"封路事件编号已存在: {blockade.blockade_id}") from exc
        return {"blockade_id": blockade.blockade_id, "state": "registered"}

    # ------------------------------------------------------------- 跨组转派

    def propose_transfer(
        self, actor_id: str, task_id: str, to_org_id: str, proposed_person_ids: Sequence[str], note: str
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.propose")
        task = self._task_row(task_id)
        if task["state"] != "assigned":
            raise InvalidState("任务已经开始，不能跨村组转派（后台不得静默换人）")
        actor = self._user(actor_id)
        if actor["role"] == "village_head" and actor["village_group_id"] != task["responsible_org_id"]:
            raise Forbidden("只有责任村组组长或保护站可以发起转派")
        if self.connection.execute(
            "SELECT 1 FROM organizations WHERE org_id=? AND kind='village_group'", (to_org_id,)
        ).fetchone() is None:
            raise NotFound("接收村组不存在")
        if to_org_id == task["responsible_org_id"]:
            raise ValidationFailed("转派目标必须是其他村组")
        version_row = self.connection.execute(
            "SELECT task_family FROM task_versions WHERE task_id=? AND version=?",
            (task_id, task["current_version"]),
        ).fetchone()
        scheduled_date = task["scheduled_for"][:10]
        self._validate_people(proposed_person_ids, version_row["task_family"], to_org_id, scheduled_date)
        transfer_id = f"tr-{task_id}-{task['current_version']}"
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO transfer_requests(transfer_id,task_id,task_version,from_org_id,to_org_id,state,"
                    "proposed_people_json,note,proposed_by,proposed_at) VALUES(?,?,?,?,?,'proposed',?,?,?,?)",
                    (
                        transfer_id, task_id, task["current_version"], task["responsible_org_id"], to_org_id,
                        canonical_json(list(proposed_person_ids)), note, actor_id, self._now(),
                    ),
                )
                self._audit("transfer", transfer_id, "transfer.proposed", actor_id,
                            {"task_id": task_id, "to_org_id": to_org_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("该任务版本已有转派请求") from exc
        return {"transfer_id": transfer_id, "state": "proposed", "awaits": to_org_id}

    def decide_transfer(self, actor_id: str, transfer_id: str, accept: bool, note: str = "") -> dict[str, Any]:
        """接收村组组长确认（双方确认的第二方），拒绝则关闭转派。"""

        self._require(actor_id, "transfer.decide")
        transfer = self.connection.execute(
            "SELECT * FROM transfer_requests WHERE transfer_id=?", (transfer_id,)
        ).fetchone()
        if transfer is None:
            raise NotFound("转派请求不存在")
        if transfer["state"] != "proposed":
            raise InvalidState("转派请求已经处理")
        actor = self._user(actor_id)
        if actor["role"] != "village_head" or actor["village_group_id"] != transfer["to_org_id"]:
            raise Forbidden("必须由接收村组组长确认")
        task = self._task_row(transfer["task_id"])
        if task["state"] != "assigned":
            raise InvalidState("任务在确认期间已经开始，转派自动失效")
        with transaction(self.connection, immediate=True):
            if accept:
                version_row = self.connection.execute(
                    "SELECT * FROM task_versions WHERE task_id=? AND version=?",
                    (transfer["task_id"], transfer["task_version"]),
                ).fetchone()
                new_version = int(task["current_version"]) + 1
                if new_version != int(transfer["task_version"]) + 1:
                    raise Conflict("任务版本在转派期间发生变化")
                proposed_people = json.loads(transfer["proposed_people_json"])
                self.connection.execute(
                    "UPDATE tasks SET current_version=?,responsible_org_id=? WHERE task_id=? AND current_version=?",
                    (new_version, transfer["to_org_id"], transfer["task_id"], transfer["task_version"]),
                )
                self._append_transfer_version(transfer, version_row, proposed_people, new_version, actor_id)
                self.connection.execute(
                    "UPDATE transfer_requests SET state='accepted',decided_by=?,decided_at=? WHERE transfer_id=?",
                    (actor_id, self._now(), transfer_id),
                )
                self._audit("transfer", transfer_id, "transfer.accepted", actor_id,
                            {"task_id": transfer["task_id"], "new_version": new_version})
                result = {"transfer_id": transfer_id, "state": "accepted", "task_version": new_version}
            else:
                self.connection.execute(
                    "UPDATE transfer_requests SET state='rejected',decided_by=?,decided_at=? WHERE transfer_id=?",
                    (actor_id, self._now(), transfer_id),
                )
                self._audit("transfer", transfer_id, "transfer.rejected", actor_id, {"note": note})
                result = {"transfer_id": transfer_id, "state": "rejected"}
        return result

    def _append_transfer_version(
        self,
        transfer: sqlite3.Row,
        version_row: sqlite3.Row,
        proposed_people: Sequence[str],
        new_version: int,
        actor_id: str,
    ) -> None:
        definition_json = json.loads(version_row["definition_json"])
        definition_json["person_ids"] = list(proposed_people)
        content = {
            "task_family": version_row["task_family"],
            "task_kind": version_row["task_kind"],
            "service_area_id": version_row["service_area_id"],
            "scheduled_for": self.connection.execute(
                "SELECT scheduled_for FROM tasks WHERE task_id=?", (transfer["task_id"],)
            ).fetchone()["scheduled_for"],
            "person_ids": list(proposed_people),
            "checkpoints": definition_json["checkpoints"],
            "pricing_rule_id": version_row["pricing_rule_id"],
        }
        now = self._now()
        self.connection.execute(
            "INSERT INTO task_versions(task_id,version,task_family,task_kind,service_area_id,pricing_rule_id,"
            "pricing_rule_version,definition_json,content_sha256,change_reason,created_by,created_at,supersedes_version) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                transfer["task_id"], new_version, version_row["task_family"], version_row["task_kind"],
                version_row["service_area_id"], version_row["pricing_rule_id"],
                version_row["pricing_rule_version"], canonical_json(content), digest(content),
                f"transfer:{transfer['to_org_id']}", actor_id, now, int(transfer["task_version"]),
            ),
        )
        for person_id in proposed_people:
            self.connection.execute(
                "INSERT INTO task_assignments(task_id,version,person_id,role,state,joined_at) "
                "VALUES(?,?,?,'patroller','active',?)",
                (transfer["task_id"], new_version, person_id, now),
            )
        for sequence, checkpoint in enumerate(definition_json["checkpoints"], start=1):
            self.connection.execute(
                "INSERT INTO task_checkpoints(task_id,version,checkpoint_id,sequence_no,required) "
                "VALUES(?,?,?,?,?)",
                (transfer["task_id"], new_version, checkpoint["checkpoint_id"], sequence, 1 if checkpoint["required"] else 0),
            )

    # ------------------------------------------------------------- 周期与结算

    def open_period(self, actor_id: str, period_id: str, starts_on: str, ends_on: str) -> dict[str, Any]:
        self._require(actor_id, "settlement.compose")
        from .clock import date_text as _date_text

        start = _date_text(starts_on, "starts_on")
        end = _date_text(ends_on, "ends_on")
        if end < start:
            raise ValidationFailed("ends_on 不能早于 starts_on")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO settlement_periods(period_id,starts_on,ends_on,state,created_at) "
                    "VALUES(?,?,?,'open',?)",
                    (period_id, start, end, self._now()),
                )
                self._audit("period", period_id, "period.opened", actor_id, {"starts_on": start, "ends_on": end})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"结算周期已存在: {period_id}") from exc
        return {"period_id": period_id, "state": "open", "starts_on": start, "ends_on": end}

    def _task_window(self, task: sqlite3.Row, receipt_rows: Sequence[sqlite3.Row]) -> tuple[str, str]:
        start = task["scheduled_for"]
        if task["finished_at"]:
            end = task["finished_at"]
        elif receipt_rows:
            end = max(row["occurred_at"] for row in receipt_rows)
        else:
            end = utc_text(parse_utc(start) + timedelta(hours=24))
        return start, end

    def _blockade_excuses(
        self, service_area_id: str, window_start: str, window_end: str, excuse_code: str
    ) -> list[sqlite3.Row]:
        blockades = self.connection.execute(
            "SELECT * FROM blockade_events WHERE service_area_id=? AND reason_code=?",
            (service_area_id, excuse_code),
        ).fetchall()
        covering = []
        for blockade in blockades:
            if blockade_covers(blockade["starts_at"], blockade["ends_at"], window_start, window_end):
                covering.append(blockade)
        return covering

    def _gather_period_inputs(self, period: sqlite3.Row) -> tuple[list[dict[str, Any]], str]:
        """收集周期内全部任务的计价事实；输入哈希相同则草案必然复算一致。"""

        tasks = self.connection.execute(
            "SELECT * FROM tasks WHERE substr(scheduled_for,1,10) BETWEEN ? AND ? "
            "AND state IN ('completed','incomplete') ORDER BY task_id",
            (period["starts_on"], period["ends_on"]),
        ).fetchall()
        facts: list[dict[str, Any]] = []
        for task in tasks:
            version = task["current_version"]
            version_row = self.connection.execute(
                "SELECT * FROM task_versions WHERE task_id=? AND version=?", (task["task_id"], version)
            ).fetchone()
            rule_row = self._snapshot_rule(version_row["pricing_rule_id"], version_row["pricing_rule_version"])
            rule = PricingRule.from_row(rule_row)
            receipt_rows = self.connection.execute(
                "SELECT * FROM receipts WHERE task_id=? AND ingest_state IN ('accepted','gapped') "
                "ORDER BY occurred_at,receipt_id",
                (task["task_id"],),
            ).fetchall()
            window_start, window_end = self._task_window(task, receipt_rows)
            area_blockades = self._blockade_excuses(
                version_row["service_area_id"], window_start, window_end,
                rule.road_closure_excuse_code,
            )
            required_checkpoints = [
                row["checkpoint_id"]
                for row in self.connection.execute(
                    "SELECT checkpoint_id FROM task_checkpoints WHERE task_id=? AND version=? AND required=1 "
                    "ORDER BY sequence_no",
                    (task["task_id"], version),
                )
            ]
            assignments = self.connection.execute(
                "SELECT person_id FROM task_assignments WHERE task_id=? AND version=? AND state='active' ORDER BY person_id",
                (task["task_id"], version),
            ).fetchall()
            for assignment in assignments:
                person_id = assignment["person_id"]
                person_receipts = [row for row in receipt_rows if row["person_id"] == person_id]
                events = [
                    ReceiptFact(
                        receipt_id=row["receipt_id"], person_id=person_id, receipt_type=row["receipt_type"],
                        checkpoint_id=row["checkpoint_id"], occurred_at=row["occurred_at"],
                        fingerprint=(
                            digest(json.loads(row["payload_json"]))
                            if row["receipt_type"] == "report" else None
                        ),
                    )
                    for row in person_receipts
                ]
                valid_ids, duplicates = dedupe_checkins(events, rule.repeat_within_minutes)
                valid_rows = [row for row in person_receipts if row["receipt_id"] in set(valid_ids)]
                reached: dict[str, int] = {}
                for row in valid_rows:
                    if row["receipt_type"] == "checkpoint" and row["checkpoint_id"] in required_checkpoints:
                        reached.setdefault(row["checkpoint_id"], row["receipt_id"])
                valid_events = tuple(
                    row["receipt_id"] for row in valid_rows if row["receipt_type"] == "report"
                )
                signed_on = any(row["receipt_type"] == "sign_on" for row in valid_rows)
                signed_off = any(row["receipt_type"] == "sign_off" for row in valid_rows)
                excused_points: set[str] = set()
                excuse_blockade_ids: set[str] = set()
                for checkpoint_id in required_checkpoints:
                    if checkpoint_id in reached:
                        continue
                    for blockade in area_blockades:
                        if blockade["checkpoint_id"] is None or blockade["checkpoint_id"] == checkpoint_id:
                            excused_points.add(checkpoint_id)
                            excuse_blockade_ids.add(blockade["blockade_id"])
                            break
                shift_excused = False
                if not required_checkpoints and task["state"] == "incomplete" and signed_on:
                    shift_excused = any(
                        blockade["checkpoint_id"] is None for blockade in area_blockades
                    )
                fact = LineFact(
                    person_id=person_id,
                    task_state=task["state"],
                    required_checkpoints=tuple(required_checkpoints),
                    reached=reached,
                    excused=frozenset(excused_points),
                    signed_on=signed_on,
                    signed_off=signed_off,
                    shift_excused=shift_excused,
                    valid_events=valid_events,
                )
                computed = compute_line(rule, fact)
                facts.append({
                    "task_id": task["task_id"],
                    "task_version": version,
                    "org_id": task["responsible_org_id"],
                    "person_id": person_id,
                    "computed": computed,
                    "receipts": [
                        {
                            "receipt_id": row["receipt_id"],
                            "device_serial": row["device_serial"],
                            "sequence_no": row["sequence_no"],
                            "receipt_type": row["receipt_type"],
                            "checkpoint_id": row["checkpoint_id"],
                            "occurred_at": row["occurred_at"],
                            "ingest_state": row["ingest_state"],
                        }
                        for row in person_receipts
                    ],
                    "duplicates": list(duplicates),
                    "excuse_blockade_ids": sorted(excuse_blockade_ids),
                })
        return facts, digest({
            "period": {"period_id": period["period_id"], "starts_on": period["starts_on"], "ends_on": period["ends_on"]},
            "facts": facts,
        })

    def compose_settlement(self, actor_id: str, period_id: str) -> dict[str, Any]:
        """生成（或按相同输入复算）周期补偿草案。"""

        self._require(actor_id, "settlement.compose")
        period = self.connection.execute(
            "SELECT * FROM settlement_periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if period is None:
            raise NotFound("结算周期不存在")
        if period["state"] in ("paid", "closed"):
            raise InvalidState("周期已经支付关闭，不能重新生成草案")
        facts, input_hash = self._gather_period_inputs(period)
        if period["state"] in ("composed", "confirmed") and period["input_sha256"] == input_hash:
            return {**self.period_summary(actor_id, period_id), "replayed": True}
        with transaction(self.connection, immediate=True):
            for fact in facts:
                computed = fact["computed"]
                detail = {
                    "computed": computed,
                    "receipts": fact["receipts"],
                    "duplicates": fact["duplicates"],
                    "excuse_blockade_ids": fact["excuse_blockade_ids"],
                }
                existing = self.connection.execute(
                    "SELECT * FROM settlement_lines WHERE period_id=? AND task_id=? AND person_id=?",
                    (period_id, fact["task_id"], fact["person_id"]),
                ).fetchone()
                if existing is None:
                    self.connection.execute(
                        "INSERT INTO settlement_lines(period_id,task_id,task_version,org_id,person_id,task_family,"
                        "task_kind,base_amount_cny,reinforcement_amount_cny,deducted_amount_cny,excused_amount_cny,"
                        "total_amount_cny,detail_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            period_id, fact["task_id"], fact["task_version"], fact["org_id"], fact["person_id"],
                            computed["task_family"], computed["task_kind"], computed["base_amount_cny"],
                            computed["reinforcement_amount_cny"], computed["deducted_amount_cny"],
                            computed["excused_amount_cny"], computed["total_amount_cny"], canonical_json(detail),
                        ),
                    )
                elif self.connection.execute(
                    "SELECT 1 FROM payment_entries WHERE line_id=?", (existing["line_id"],)
                ).fetchone() is not None:
                    # 已进入支付清单的明细不可变：重算草案不得回改已支付金额。
                    continue
                elif int(existing["frozen"]) == 0:
                    delta_total = self.connection.execute(
                        "SELECT COALESCE(SUM(CAST(amount_delta_cny AS REAL)),0) delta FROM adjustment_decisions "
                        "WHERE line_id=?",
                        (existing["line_id"],),
                    ).fetchone()["delta"]
                    total = money(Decimal(computed["total_amount_cny"]) + Decimal(str(delta_total)))
                    confirmed_count = self.connection.execute(
                        "SELECT COUNT(*) c FROM settlement_confirmations WHERE line_id=? AND decision='confirmed'",
                        (existing["line_id"],),
                    ).fetchone()["c"]
                    if confirmed_count and total != Decimal(existing["total_amount_cny"]):
                        # 已确认行金额变化时清空确认，要求三方按新金额重新确认。
                        self.connection.execute(
                            "DELETE FROM settlement_confirmations WHERE line_id=?", (existing["line_id"],)
                        )
                    self.connection.execute(
                        "UPDATE settlement_lines SET task_version=?,base_amount_cny=?,reinforcement_amount_cny=?,"
                        "deducted_amount_cny=?,excused_amount_cny=?,total_amount_cny=?,detail_json=? "
                        "WHERE line_id=? AND frozen=0",
                        (
                            fact["task_version"], computed["base_amount_cny"], computed["reinforcement_amount_cny"],
                            computed["deducted_amount_cny"], computed["excused_amount_cny"],
                            decimal_text(total), canonical_json(detail), existing["line_id"],
                        ),
                    )
                # 冻结行原样保留：争议只影响该任务和金额。
            self.connection.execute(
                "UPDATE settlement_periods SET state='composed',composed_by=?,composed_at=?,input_sha256=? "
                "WHERE period_id=?",
                (actor_id, self._now(), input_hash, period_id),
            )
            self._audit("period", period_id, "settlement.composed", actor_id,
                        {"lines": len(facts), "input_sha256": input_hash})
        return {**self.period_summary(actor_id, period_id), "replayed": False}

    def confirm_scope(
        self,
        actor_id: str,
        period_id: str,
        scope: str,
        line_ids: Sequence[int] | None,
        decision: str,
        note: str = "",
    ) -> dict[str, Any]:
        """村组、保护站、财务分别确认自己负责的部分。"""

        self._require(actor_id, "confirmation.write")
        if scope not in CONFIRMATION_SCOPES:
            raise ValidationFailed(f"scope 必须是 {CONFIRMATION_SCOPES} 之一")
        if decision not in ("confirmed", "disputed"):
            raise ValidationFailed("decision 必须是 confirmed 或 disputed")
        period = self.connection.execute(
            "SELECT * FROM settlement_periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if period is None:
            raise NotFound("结算周期不存在")
        if period["state"] not in ("composed", "confirmed"):
            raise InvalidState("周期还没有可确认的草案")
        actor = self._user(actor_id)
        if scope == "village_head" and actor["role"] != "village_head":
            raise Forbidden("村组部分必须由村组组长确认")
        if scope == "station" and actor["role"] != "station":
            raise Forbidden("保护站部分必须由保护站确认")
        if scope == "finance" and actor["role"] != "finance":
            raise Forbidden("财务部分必须由财务确认")
        query = "SELECT * FROM settlement_lines WHERE period_id=?"
        params: list[Any] = [period_id]
        if line_ids:
            query += " AND line_id IN ({})".format(",".join("?" for _ in line_ids))
            params.extend(line_ids)
        rows = self.connection.execute(query, params).fetchall()
        if line_ids and len(rows) != len(set(line_ids)):
            raise NotFound("部分明细不存在")
        with transaction(self.connection, immediate=True):
            for row in rows:
                if scope == "village_head" and actor["village_group_id"] != row["org_id"]:
                    raise Forbidden(f"明细 {row['line_id']} 不属于你村组")
                if int(row["frozen"]) == 1 and decision == "confirmed":
                    raise InvalidState(f"明细 {row['line_id']} 处于争议冻结，不能确认")
                self.connection.execute(
                    "INSERT INTO settlement_confirmations(period_id,scope_role,org_id,person_id,line_id,"
                    "decision,note,decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(period_id,scope_role,line_id) DO UPDATE SET decision=excluded.decision,"
                    "note=excluded.note,decided_by=excluded.decided_by,decided_at=excluded.decided_at",
                    (
                        period_id, scope, row["org_id"], row["person_id"], row["line_id"], decision, note,
                        actor_id, self._now(),
                    ),
                )
                if decision == "disputed":
                    self._freeze_line(period, row, actor_id, note)
            self._refresh_period_state(period_id)
            self._audit("period", period_id, "settlement.confirmed", actor_id,
                        {"scope": scope, "decision": decision, "lines": len(rows)})
        return self.period_summary(actor_id, period_id)

    def _freeze_line(self, period: sqlite3.Row, row: sqlite3.Row, actor_id: str, reason: str) -> None:
        self.connection.execute(
            "UPDATE settlement_lines SET disputed=1,frozen=1 WHERE line_id=?", (row["line_id"],)
        )
        self.connection.execute(
            "INSERT INTO disputes(period_id,line_id,task_id,state,reason,raised_by,raised_at) "
            "VALUES(?,?,?, 'open',?,?,?) ON CONFLICT(period_id,line_id) DO UPDATE SET state='open',reason=excluded.reason",
            (period["period_id"], row["line_id"], row["task_id"], reason or "存在争议", actor_id, self._now()),
        )

    def _refresh_period_state(self, period_id: str) -> None:
        pending = self.connection.execute(
            "SELECT COUNT(*) c FROM settlement_lines WHERE period_id=? AND frozen=0", (period_id,)
        ).fetchone()["c"]
        fully_confirmed = self.connection.execute(
            "SELECT COUNT(*) c FROM settlement_lines l WHERE l.period_id=? AND l.frozen=0 AND "
            "(SELECT COUNT(*) FROM settlement_confirmations c WHERE c.line_id=l.line_id AND c.decision='confirmed')=3",
            (period_id,),
        ).fetchone()["c"]
        # 全部未冻结行三方确认齐备才进入 confirmed；任何一行缺确认（如争议改判后财务需重认）回退 composed。
        new_state = "confirmed" if pending and pending == fully_confirmed else "composed"
        self.connection.execute(
            "UPDATE settlement_periods SET state=? WHERE period_id=? AND state IN ('composed','confirmed')",
            (new_state, period_id),
        )

    def resolve_dispute(
        self, actor_id: str, dispute_id: int, resolution: str, amount_delta_cny: str, reason: str
    ) -> dict[str, Any]:
        """处理争议：任何金额调整都以追加决定留下原因，不覆盖历史。"""

        self._require(actor_id, "confirmation.write")
        if resolution not in ("resolved", "rejected"):
            raise ValidationFailed("resolution 必须是 resolved 或 rejected")
        if not reason.strip():
            raise ValidationFailed("追加决定必须填写原因")
        dispute = self.connection.execute(
            "SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)
        ).fetchone()
        if dispute is None:
            raise NotFound("争议不存在")
        if dispute["state"] != "open":
            raise InvalidState("争议已经处理")
        actor = self._user(actor_id)
        if actor["role"] not in ("station", "finance"):
            raise Forbidden("只有保护站或财务可以裁定争议")
        delta = Decimal(str(amount_delta_cny))
        if not delta.is_finite():
            raise ValidationFailed("amount_delta_cny 必须是有限数值")
        if resolution == "rejected" and delta != 0:
            raise ValidationFailed("驳回争议不能调整金额，amount_delta_cny 必须为 0")
        with transaction(self.connection, immediate=True):
            line = self.connection.execute(
                "SELECT * FROM settlement_lines WHERE line_id=?", (dispute["line_id"],)
            ).fetchone()
            if resolution == "resolved":
                new_total = money(Decimal(line["total_amount_cny"]) + delta)
                if new_total < 0:
                    raise ValidationFailed("调整后金额不能为负数")
                self.connection.execute(
                    "INSERT INTO adjustment_decisions(period_id,task_id,line_id,dispute_id,adjustment_type,"
                    "amount_delta_cny,reason,decided_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        dispute["period_id"], dispute["task_id"], dispute["line_id"], dispute_id,
                        "allowance" if delta > 0 else ("clawback" if delta < 0 else "correction"),
                        decimal_text(delta), reason, actor_id, self._now(),
                    ),
                )
                self.connection.execute(
                    "UPDATE settlement_lines SET total_amount_cny=? WHERE line_id=?",
                    (decimal_text(new_total), dispute["line_id"]),
                )
            else:
                new_total = Decimal(line["total_amount_cny"])
            self.connection.execute(
                "UPDATE settlement_lines SET frozen=0,disputed=0 WHERE line_id=?", (dispute["line_id"],)
            )
            self.connection.execute(
                "UPDATE disputes SET state=?,resolved_at=? WHERE dispute_id=?",
                (resolution, self._now(), dispute_id),
            )
            # 原争议确认作废：发起方按裁定结果重新确认；金额变化时财务也要按新金额重新确认。
            raiser = self.connection.execute(
                "SELECT role FROM comanage_users WHERE user_id=?", (dispute["raised_by"],)
            ).fetchone()
            scopes_to_reset = {raiser["role"]} if raiser["role"] in CONFIRMATION_SCOPES else set()
            if resolution == "resolved" and delta != 0:
                scopes_to_reset.add("finance")
            if scopes_to_reset:
                self.connection.execute(
                    "DELETE FROM settlement_confirmations WHERE line_id=? AND scope_role IN ({})".format(
                        ",".join("?" for _ in scopes_to_reset)
                    ),
                    (dispute["line_id"], *sorted(scopes_to_reset)),
                )
            self._refresh_period_state(dispute["period_id"])
            self._audit("dispute", str(dispute_id), "dispute.resolved", actor_id,
                        {"resolution": resolution, "amount_delta_cny": decimal_text(delta), "reason": reason})
        return {"dispute_id": dispute_id, "state": resolution, "total_amount_cny": decimal_text(new_total)}

    def recover_open_disputes(self, actor_id: str) -> dict[str, Any]:
        """重启后恢复全部未结争议（状态持久化在 SQLite 中）。"""

        self._require(actor_id, "report.read")
        rows = self.connection.execute(
            "SELECT d.dispute_id,d.period_id,d.line_id,d.task_id,d.reason,d.raised_by,d.raised_at,"
            "l.org_id,l.person_id,l.total_amount_cny FROM disputes d "
            "JOIN settlement_lines l ON l.line_id=d.line_id WHERE d.state='open' "
            "ORDER BY d.period_id,d.dispute_id"
        ).fetchall()
        return {"open_disputes": [dict(row) for row in rows]}

    def finalize_payment(self, actor_id: str, period_id: str) -> dict[str, Any]:
        """无争议、三方确认齐备的明细进入支付清单；争议部分继续冻结，互不阻塞。"""

        self._require(actor_id, "payment.finalize")
        period = self.connection.execute(
            "SELECT * FROM settlement_periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if period is None:
            raise NotFound("结算周期不存在")
        if period["state"] not in ("composed", "confirmed", "paid"):
            raise InvalidState("周期状态不能生成支付清单")
        with transaction(self.connection, immediate=True):
            payable = self.connection.execute(
                "SELECT l.* FROM settlement_lines l WHERE l.period_id=? AND l.frozen=0 AND "
                "(SELECT COUNT(*) FROM settlement_confirmations c WHERE c.line_id=l.line_id AND c.decision='confirmed')=3 "
                "AND NOT EXISTS (SELECT 1 FROM payment_entries p WHERE p.line_id=l.line_id) ORDER BY l.line_id",
                (period_id,),
            ).fetchall()
            for row in payable:
                self.connection.execute(
                    "INSERT INTO payment_entries(period_id,line_id,org_id,person_id,amount_cny,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (period_id, row["line_id"], row["org_id"], row["person_id"], row["total_amount_cny"], self._now()),
                )
            remaining_unfrozen = self.connection.execute(
                "SELECT COUNT(*) c FROM settlement_lines WHERE period_id=? AND frozen=0 AND line_id NOT IN "
                "(SELECT line_id FROM payment_entries WHERE period_id=?)",
                (period_id, period_id),
            ).fetchone()["c"]
            frozen = self.connection.execute(
                "SELECT COUNT(*) c FROM settlement_lines WHERE period_id=? AND frozen=1", (period_id,)
            ).fetchone()["c"]
            # 仍有冻结争议时保持部分支付状态；只有无争议、无遗留时才整体支付完成。
            if frozen == 0 and remaining_unfrozen == 0:
                new_state = "paid"
            elif remaining_unfrozen == 0:
                new_state = "confirmed"
            else:
                new_state = "composed"
            self.connection.execute(
                "UPDATE settlement_periods SET state=? WHERE period_id=?", (new_state, period_id)
            )
            self._audit("period", period_id, "payment.finalized", actor_id,
                        {"new_entries": len(payable), "still_frozen": frozen})
        return {
            "period_id": period_id,
            "state": new_state,
            "new_payment_entries": len(payable),
            "payment_list": self.payment_list(actor_id, period_id),
        }

    def payment_list(self, actor_id: str, period_id: str) -> list[dict[str, Any]]:
        self._require_any(actor_id, ("report.read", "payment.finalize"))
        rows = self.connection.execute(
            "SELECT payment_id,line_id,org_id,person_id,amount_cny FROM payment_entries "
            "WHERE period_id=? ORDER BY payment_id",
            (period_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # -------------------------------------------------------------- 查询解释

    def period_summary(self, actor_id: str, period_id: str) -> dict[str, Any]:
        self._require_any(actor_id, ("report.read", "payment.finalize", "confirmation.write"))
        period = self.connection.execute(
            "SELECT * FROM settlement_periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if period is None:
            raise NotFound("结算周期不存在")
        lines = self.connection.execute(
            "SELECT line_id,task_id,task_version,org_id,person_id,base_amount_cny,reinforcement_amount_cny,"
            "deducted_amount_cny,excused_amount_cny,total_amount_cny,disputed,frozen FROM settlement_lines "
            "WHERE period_id=? ORDER BY line_id",
            (period_id,),
        ).fetchall()
        line_rows = [dict(row) for row in lines]
        confirmations = self.connection.execute(
            "SELECT line_id,scope_role,decision FROM settlement_confirmations WHERE period_id=? ORDER BY line_id,scope_role",
            (period_id,),
        ).fetchall()
        confirmation_map: dict[int, dict[str, str]] = {}
        for row in confirmations:
            confirmation_map.setdefault(row["line_id"], {})[row["scope_role"]] = row["decision"]
        for line in line_rows:
            line["confirmations"] = confirmation_map.get(line["line_id"], {})
        payable = [line for line in line_rows if not line["frozen"]]
        frozen = [line for line in line_rows if line["frozen"]]
        return {
            "period_id": period_id,
            "state": period["state"],
            "input_sha256": period["input_sha256"],
            "lines": line_rows,
            "totals": totalize(line_rows),
            "payable_totals": totalize(payable),
            "frozen_totals": totalize(frozen),
            "frozen_line_ids": [line["line_id"] for line in frozen],
        }

    def explain_line(self, actor_id: str, line_id: int) -> dict[str, Any]:
        """逐事件解释一笔补偿由哪些有效回执、免责封路与追加决定构成。"""

        self._require_any(actor_id, ("report.read", "payment.finalize", "confirmation.write"))
        line = self.connection.execute(
            "SELECT * FROM settlement_lines WHERE line_id=?", (line_id,)
        ).fetchone()
        if line is None:
            raise NotFound("补偿明细不存在")
        detail = json.loads(line["detail_json"])
        excluded_ids = {
            item["receipt_id"] for item in detail.get("duplicates", [])
        }
        valid_receipts = [
            receipt for receipt in detail.get("receipts", [])
            if receipt["receipt_id"] not in excluded_ids
        ]
        adjustments = self.connection.execute(
            "SELECT decision_id,adjustment_type,amount_delta_cny,reason,decided_by,created_at "
            "FROM adjustment_decisions WHERE line_id=? ORDER BY decision_id",
            (line_id,),
        ).fetchall()
        blockades = []
        for blockade_id in detail.get("excuse_blockade_ids", []):
            row = self.connection.execute(
                "SELECT blockade_id,starts_at,ends_at,reason_code FROM blockade_events WHERE blockade_id=?",
                (blockade_id,),
            ).fetchone()
            if row is not None:
                blockades.append(dict(row))
        return {
            "line_id": line_id,
            "period_id": line["period_id"],
            "task_id": line["task_id"],
            "task_version": line["task_version"],
            "org_id": line["org_id"],
            "person_id": line["person_id"],
            "amounts": {
                "base_amount_cny": line["base_amount_cny"],
                "reinforcement_amount_cny": line["reinforcement_amount_cny"],
                "deducted_amount_cny": line["deducted_amount_cny"],
                "excused_amount_cny": line["excused_amount_cny"],
                "total_amount_cny": line["total_amount_cny"],
            },
            "pricing": {
                "rule_id": detail["computed"]["pricing_rule_id"],
                "rule_version": detail["computed"]["pricing_rule_version"],
                "base_unit": detail["computed"]["base_unit"],
            },
            "units": detail["computed"]["units"],
            "valid_receipts": sorted(valid_receipts, key=lambda item: item["receipt_id"]),
            "excluded_receipts": detail.get("duplicates", []),
            "excused_by_blockades": blockades,
            "adjustment_decisions": [dict(row) for row in adjustments],
            "frozen": bool(line["frozen"]),
            "disputed": bool(line["disputed"]),
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
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
