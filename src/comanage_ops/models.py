"""共管任务领域输入契约。

任务分为三类：防火瞭望（fire_watch）、垃圾清运（waste_haul）和
野生动物冲突上报（wildlife_report），对应计划任务与临时增援两种来源。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
TASK_KINDS = {"fire_watch", "waste_haul", "wildlife_report"}
TASK_SOURCES = {"planned", "reinforcement"}
QUALIFICATION_KINDS = {
    "fire_watch",
    "waste_haul",
    "wildlife_report",
    "wildlife_response",
    "first_aid",
}
EVENT_TYPES = {
    "assigned",
    "started",
    "checkpoint",
    "completed",
    "absolved",
}
ABSOLUTION_REASONS = {"road_closed", "weather", "stand_down", "other"}
PRICING_BASIS = {"fixed", "per_checkpoint", "per_hour", "per_kilometer", "per_kilogram"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def optional_text(value: object, field: str, maximum: int = 512) -> str | None:
    if value is None:
        return None
    return required_text(value, field, maximum)


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def timestamp_text(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        parse_utc(text, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return text


def choice(value: object, field: str, choices: set[str]) -> str:
    result = required_text(value, field, 32)
    if result not in choices:
        raise ValidationFailed(f"{field} 必须是 {sorted(choices)} 之一")
    return result


@dataclass(frozen=True, slots=True)
class ServiceArea:
    area_id: str
    name: str
    village_group_id: str
    geometry: tuple[Mapping[str, Any], ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ServiceArea":
        geometry = raw.get("geometry", [])
        if not isinstance(geometry, list) or not geometry:
            raise ValidationFailed("geometry 必须是非空点位数组")
        points: list[Mapping[str, Any]] = []
        for index, point in enumerate(geometry):
            if not isinstance(point, Mapping) or not isinstance(point.get("point_id"), str) or not point["point_id"].strip():
                raise ValidationFailed(f"geometry[{index}].point_id 不能为空")
            points.append({"point_id": point["point_id"].strip()})
        return cls(
            area_id=identifier(raw.get("area_id"), "area_id"),
            name=required_text(raw.get("name"), "name"),
            village_group_id=identifier(raw.get("village_group_id"), "village_group_id"),
            geometry=tuple(points),
        )


@dataclass(frozen=True, slots=True)
class Qualification:
    qualification_id: str
    person_id: str
    kind: str
    valid_from: str
    valid_until: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Qualification":
        valid_from = date_text(raw.get("valid_from"), "valid_from")
        valid_until = raw.get("valid_until")
        if valid_until is not None:
            valid_until = date_text(valid_until, "valid_until")
            if valid_until < valid_from:
                raise ValidationFailed("valid_until 不能早于 valid_from")
        return cls(
            qualification_id=identifier(raw.get("qualification_id"), "qualification_id"),
            person_id=identifier(raw.get("person_id"), "person_id"),
            kind=choice(raw.get("kind"), "kind", QUALIFICATION_KINDS),
            valid_from=valid_from,
            valid_until=valid_until,
        )


@dataclass(frozen=True, slots=True)
class CheckpointSpec:
    checkpoint_id: str
    label: str
    point_id: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    task_id: str
    kind: str
    source: str
    area_id: str
    title: str
    planned_start: str
    planned_end: str | None
    required_qualification: str
    checkpoints: tuple[CheckpointSpec, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TaskDefinition":
        kind = choice(raw.get("kind"), "kind", TASK_KINDS)
        planned_start = timestamp_text(raw.get("planned_start"), "planned_start")
        planned_end = raw.get("planned_end")
        if planned_end is not None:
            planned_end = timestamp_text(planned_end, "planned_end")
            if planned_end <= planned_start:
                raise ValidationFailed("planned_end 必须晚于 planned_start")
        checkpoint_raw = raw.get("checkpoints", [])
        if not isinstance(checkpoint_raw, list) or not checkpoint_raw:
            raise ValidationFailed("checkpoints 必须是非空检查点数组")
        checkpoints: list[CheckpointSpec] = []
        seen: set[str] = set()
        for index, item in enumerate(checkpoint_raw):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"checkpoints[{index}] 必须是对象")
            checkpoint_id = identifier(item.get("checkpoint_id"), f"checkpoints[{index}].checkpoint_id")
            if checkpoint_id in seen:
                raise ValidationFailed(f"检查点编号重复: {checkpoint_id}")
            seen.add(checkpoint_id)
            ordinal = item.get("ordinal", index + 1)
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0:
                raise ValidationFailed(f"checkpoints[{index}].ordinal 必须是正整数")
            checkpoints.append(
                CheckpointSpec(
                    checkpoint_id=checkpoint_id,
                    label=required_text(item.get("label"), f"checkpoints[{index}].label", 64),
                    point_id=identifier(item.get("point_id"), f"checkpoints[{index}].point_id"),
                    ordinal=ordinal,
                )
            )
        return cls(
            task_id=identifier(raw.get("task_id"), "task_id"),
            kind=kind,
            source=choice(raw.get("source", "planned"), "source", TASK_SOURCES),
            area_id=identifier(raw.get("area_id"), "area_id"),
            title=required_text(raw.get("title"), "title"),
            planned_start=planned_start,
            planned_end=planned_end,
            required_qualification=choice(raw.get("required_qualification", kind), "required_qualification", QUALIFICATION_KINDS),
            checkpoints=tuple(checkpoints),
        )


@dataclass(frozen=True, slots=True)
class PricingRule:
    rule_id: str
    kind: str
    source: str
    basis: str
    base_amount_cny: Decimal
    unit_amount_cny: Decimal
    reinforcement_multiplier: Decimal
    absolved_ratio: Decimal
    valid_from: str
    valid_until: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PricingRule":
        kind = choice(raw.get("kind"), "kind", TASK_KINDS)
        source = choice(raw.get("source", "planned"), "source", TASK_SOURCES)
        basis = choice(raw.get("basis", "fixed"), "basis", PRICING_BASIS)
        valid_from = date_text(raw.get("valid_from"), "valid_from")
        valid_until = raw.get("valid_until")
        if valid_until is not None:
            valid_until = date_text(valid_until, "valid_until")
            if valid_until < valid_from:
                raise ValidationFailed("valid_until 不能早于 valid_from")
        return cls(
            rule_id=identifier(raw.get("rule_id"), "rule_id"),
            kind=kind,
            source=source,
            basis=basis,
            base_amount_cny=decimal_value(raw.get("base_amount_cny", 0), "base_amount_cny", minimum=Decimal("0")),
            unit_amount_cny=decimal_value(raw.get("unit_amount_cny", 0), "unit_amount_cny", minimum=Decimal("0")),
            reinforcement_multiplier=decimal_value(
                raw.get("reinforcement_multiplier", 1),
                "reinforcement_multiplier",
                minimum=Decimal("0"),
            ),
            absolved_ratio=decimal_value(
                raw.get("absolved_ratio", 0),
                "absolved_ratio",
                minimum=Decimal("0"),
                maximum=Decimal("1"),
            ),
            valid_from=valid_from,
            valid_until=valid_until,
        )


@dataclass(frozen=True, slots=True)
class EventReceipt:
    """离线设备补传的单条事件回执。

    device_serial 是设备出厂序号；seq 是设备本地单调递增的事件序号；
    event_uuid 是设备本地生成的去重标识。三者共同识别：
    - 重放：同 (device_serial, event_uuid) 或同 (device_serial, seq) 再次补传；
    - 分叉：同 (device_serial, seq) 对应不同 event_uuid（设备本地日志分叉）。
    client_clock 是设备本地事件时间。
    """

    event_uuid: str
    device_serial: str
    seq: int
    task_id: str
    person_id: str
    event_type: str
    client_clock: str
    checkpoint_id: str | None
    quantity: Decimal
    evidence_ref: str | None
    absolved_reason: str | None
    note: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EventReceipt":
        event_type = choice(raw.get("event_type"), "event_type", EVENT_TYPES)
        seq = raw.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
            raise ValidationFailed("seq 必须是正整数（设备本地事件序号）")
        checkpoint_id = raw.get("checkpoint_id")
        if checkpoint_id is not None:
            checkpoint_id = identifier(checkpoint_id, "checkpoint_id")
        absolved_reason = raw.get("absolved_reason")
        if absolved_reason is not None:
            absolved_reason = choice(absolved_reason, "absolved_reason", ABSOLUTION_REASONS)
        if event_type == "absolved" and absolved_reason is None:
            raise ValidationFailed("absolved 事件必须提供 absolved_reason")
        return cls(
            event_uuid=identifier(raw.get("event_uuid"), "event_uuid"),
            device_serial=identifier(raw.get("device_serial"), "device_serial"),
            seq=seq,
            task_id=identifier(raw.get("task_id"), "task_id"),
            person_id=identifier(raw.get("person_id"), "person_id"),
            event_type=event_type,
            client_clock=timestamp_text(raw.get("client_clock"), "client_clock"),
            checkpoint_id=checkpoint_id,
            quantity=decimal_value(raw.get("quantity", 0), "quantity", minimum=Decimal("0")),
            evidence_ref=optional_text(raw.get("evidence_ref"), "evidence_ref", 256),
            absolved_reason=absolved_reason,
            note=optional_text(raw.get("note"), "note", 512),
        )
