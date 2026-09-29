"""社区共管任务与补偿核算的领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
TASK_FAMILIES = {"fire_lookout", "waste_haul", "wildlife_conflict"}
TASK_KINDS = {"planned", "reinforcement"}
BASE_UNITS = {"shift", "checkpoint", "event", "km", "day"}
ROLES = {"villager", "village_head", "station", "finance", "auditor"}
RECEIPT_TYPES = {"sign_on", "checkpoint", "sign_off", "report"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


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


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


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


def _choice(value: object, field: str, choices: set[str]) -> str:
    result = required_text(value, field, 32)
    if result not in choices:
        raise ValidationFailed(f"{field} 必须是 {sorted(choices)} 之一")
    return result


@dataclass(frozen=True, slots=True)
class Organization:
    org_id: str
    name: str
    kind: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Organization":
        return cls(
            org_id=identifier(raw.get("org_id"), "org_id"),
            name=required_text(raw.get("name"), "name"),
            kind=_choice(raw.get("kind"), "kind", {"village_group", "station"}),
        )


@dataclass(frozen=True, slots=True)
class ServiceArea:
    service_area_id: str
    name: str
    geometry: list[Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ServiceArea":
        geometry = raw.get("geometry", [])
        if not isinstance(geometry, list) or not geometry:
            raise ValidationFailed("geometry 必须是非空数组")
        return cls(
            service_area_id=identifier(raw.get("service_area_id"), "service_area_id"),
            name=required_text(raw.get("name"), "name"),
            geometry=geometry,
        )


@dataclass(frozen=True, slots=True)
class Checkpoint:
    checkpoint_id: str
    service_area_id: str
    name: str
    position: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Checkpoint":
        position = raw.get("position", {})
        if not isinstance(position, dict):
            raise ValidationFailed("position 必须是对象")
        return cls(
            checkpoint_id=identifier(raw.get("checkpoint_id"), "checkpoint_id"),
            service_area_id=identifier(raw.get("service_area_id"), "service_area_id"),
            name=required_text(raw.get("name"), "name"),
            position=position,
        )


@dataclass(frozen=True, slots=True)
class Qualification:
    person_id: str
    task_family: str
    level: str
    valid_from: str
    valid_until: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Qualification":
        valid_from = date_text(raw.get("valid_from"), "valid_from")
        valid_until = None
        if raw.get("valid_until"):
            valid_until = date_text(raw.get("valid_until"), "valid_until")
            if valid_until < valid_from:
                raise ValidationFailed("valid_until 不能早于 valid_from")
        return cls(
            person_id=identifier(raw.get("person_id"), "person_id"),
            task_family=_choice(raw.get("task_family"), "task_family", TASK_FAMILIES),
            level=required_text(raw.get("level"), "level", 32),
            valid_from=valid_from,
            valid_until=valid_until,
        )


@dataclass(frozen=True, slots=True)
class PricingRuleInput:
    rule_id: str
    task_family: str
    task_kind: str
    base_unit: str
    base_rate: Decimal
    reinforcement_rate: Decimal
    repeat_within_minutes: int
    road_closure_excuse_code: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PricingRuleInput":
        repeat = raw.get("repeat_within_minutes", 30)
        if isinstance(repeat, bool) or not isinstance(repeat, int) or not 0 <= repeat <= 1440:
            raise ValidationFailed("repeat_within_minutes 必须是 0 到 1440 的整数")
        return cls(
            rule_id=identifier(raw.get("rule_id"), "rule_id"),
            task_family=_choice(raw.get("task_family"), "task_family", TASK_FAMILIES),
            task_kind=_choice(raw.get("task_kind"), "task_kind", TASK_KINDS),
            base_unit=_choice(raw.get("base_unit"), "base_unit", BASE_UNITS),
            base_rate=decimal_value(raw.get("base_rate_cny"), "base_rate_cny", minimum=Decimal("0")),
            reinforcement_rate=decimal_value(
                raw.get("reinforcement_rate_cny", "0"), "reinforcement_rate_cny", minimum=Decimal("0")
            ),
            repeat_within_minutes=repeat,
            road_closure_excuse_code=required_text(raw.get("road_closure_excuse_code", "ROAD_CLOSED"), "road_closure_excuse_code", 32),
        )


@dataclass(frozen=True, slots=True)
class TaskCheckpoint:
    checkpoint_id: str
    required: bool


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    task_id: str
    task_family: str
    task_kind: str
    service_area_id: str
    scheduled_for: str
    person_ids: tuple[str, ...]
    checkpoints: tuple[TaskCheckpoint, ...]
    pricing_rule_id: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TaskDefinition":
        task_family = _choice(raw.get("task_family"), "task_family", TASK_FAMILIES)
        person_ids = raw.get("person_ids", [])
        if not isinstance(person_ids, list) or not person_ids:
            raise ValidationFailed("person_ids 必须是非空数组")
        people = tuple(identifier(item, "person_ids[]") for item in person_ids)
        if len(set(people)) != len(people):
            raise ValidationFailed("person_ids 不能重复")
        checkpoint_raw = raw.get("checkpoints", [])
        if not isinstance(checkpoint_raw, list):
            raise ValidationFailed("checkpoints 必须是数组")
        checkpoints = tuple(
            TaskCheckpoint(
                checkpoint_id=identifier(item.get("checkpoint_id"), "checkpoints[].checkpoint_id"),
                required=bool(item.get("required", True)),
            )
            for item in checkpoint_raw
        )
        ids = [item.checkpoint_id for item in checkpoints]
        if len(set(ids)) != len(ids):
            raise ValidationFailed("checkpoints 不能重复")
        return cls(
            task_id=identifier(raw.get("task_id"), "task_id"),
            task_family=task_family,
            task_kind=_choice(raw.get("task_kind"), "task_kind", TASK_KINDS),
            service_area_id=identifier(raw.get("service_area_id"), "service_area_id"),
            scheduled_for=timestamp_text(raw.get("scheduled_for"), "scheduled_for"),
            person_ids=people,
            checkpoints=checkpoints,
            pricing_rule_id=identifier(raw.get("pricing_rule_id"), "pricing_rule_id"),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class DeviceRegistration:
    device_serial: str
    person_id: str
    model_name: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DeviceRegistration":
        return cls(
            device_serial=identifier(raw.get("device_serial"), "device_serial"),
            person_id=identifier(raw.get("person_id"), "person_id"),
            model_name=required_text(raw.get("model_name"), "model_name", 64),
        )


@dataclass(frozen=True, slots=True)
class ReceiptItem:
    sequence_no: int
    task_id: str
    receipt_type: str
    checkpoint_id: str | None
    person_id: str
    occurred_at: str
    payload: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReceiptItem":
        checkpoint_id = raw.get("checkpoint_id")
        payload = raw.get("payload", {})
        if not isinstance(payload, dict):
            raise ValidationFailed("payload 必须是对象")
        return cls(
            sequence_no=positive_integer(raw.get("sequence_no"), "sequence_no"),
            task_id=identifier(raw.get("task_id"), "task_id"),
            receipt_type=_choice(raw.get("receipt_type"), "receipt_type", RECEIPT_TYPES),
            checkpoint_id=None if not checkpoint_id else identifier(checkpoint_id, "checkpoint_id"),
            person_id=identifier(raw.get("person_id"), "person_id"),
            occurred_at=timestamp_text(raw.get("occurred_at"), "occurred_at"),
            payload=payload,
        )


@dataclass(frozen=True, slots=True)
class Blockade:
    blockade_id: str
    service_area_id: str
    checkpoint_id: str | None
    starts_at: str
    ends_at: str | None
    reason_code: str
    evidence: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Blockade":
        starts_at = timestamp_text(raw.get("starts_at"), "starts_at")
        ends_at = None
        if raw.get("ends_at"):
            ends_at = timestamp_text(raw.get("ends_at"), "ends_at")
            if ends_at <= starts_at:
                raise ValidationFailed("ends_at 必须晚于 starts_at")
        evidence = raw.get("evidence", {})
        if not isinstance(evidence, dict):
            raise ValidationFailed("evidence 必须是对象")
        checkpoint_id = raw.get("checkpoint_id")
        return cls(
            blockade_id=identifier(raw.get("blockade_id"), "blockade_id"),
            service_area_id=identifier(raw.get("service_area_id"), "service_area_id"),
            checkpoint_id=None if not checkpoint_id else identifier(checkpoint_id, "checkpoint_id"),
            starts_at=starts_at,
            ends_at=ends_at,
            reason_code=required_text(raw.get("reason_code"), "reason_code", 32),
            evidence=evidence,
        )
