"""确定性的补偿计价：由计价规则、任务版本和有效事件推导金额与组成。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from .clock import parse_utc


CENT = Decimal("0.01")


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True, slots=True)
class EffectiveEvent:
    event_uuid: str
    event_type: str
    client_clock: str
    checkpoint_id: str | None
    quantity: Decimal


def _event_units(basis: str, events: Sequence[EffectiveEvent]) -> tuple[Decimal, list[str]]:
    """按计价口径统计有效工作量。"""
    if basis == "per_checkpoint":
        counted = [event for event in events if event.event_type == "checkpoint"]
        return Decimal(len(counted)), [event.event_uuid for event in counted]
    if basis in {"per_kilometer", "per_kilogram"}:
        # 以完成回执申报的最终工作量为准；尚未完成时才回退到检查点累计。
        completed = [event for event in events if event.event_type == "completed" and event.quantity > 0]
        source = completed if completed else [
            event for event in events if event.event_type == "checkpoint" and event.quantity > 0
        ]
        total = sum((event.quantity for event in source), Decimal("0"))
        return total, [event.event_uuid for event in source]
    if basis == "per_hour":
        started = [event for event in events if event.event_type == "started"]
        completed = [event for event in events if event.event_type == "completed"]
        if started and completed:
            seconds = (
                parse_utc(completed[0].client_clock) - parse_utc(started[0].client_clock)
            ).total_seconds()
            hours = Decimal(max(seconds, 0)) / Decimal(3600)
            return hours, [started[0].event_uuid, completed[0].event_uuid]
        return Decimal("0"), []
    return Decimal("0"), []


def value_task(
    rule: Mapping[str, Any],
    task: Mapping[str, Any],
    events: Sequence[EffectiveEvent],
) -> dict[str, Any]:
    """返回单笔补偿的可复算组成。

    无有效事件的任务金额为 0；封路免责按 base_amount × absolved_ratio 计。
    """
    basis = rule["basis"]
    base = Decimal(str(rule["base_amount_cny"]))
    unit = Decimal(str(rule["unit_amount_cny"]))
    multiplier = Decimal(str(rule["reinforcement_multiplier"]))
    if task["source"] == "planned":
        multiplier = Decimal("1")
    absolved = task["lifecycle_state"] == "absolved"

    contributing: list[str] = []
    subtotal = Decimal("0")

    if absolved:
        subtotal = base * Decimal(str(rule["absolved_ratio"])) * multiplier
        contributing = [event.event_uuid for event in events if event.event_type == "absolved"]
        units = Decimal("0")
    elif basis == "fixed":
        has_work = any(event.event_type in {"started", "completed", "checkpoint"} for event in events)
        if has_work:
            subtotal = base * multiplier
            units = Decimal("1")
            contributing = sorted(
                event.event_uuid
                for event in events
                if event.event_type in {"started", "completed", "checkpoint"}
            )
        else:
            units = Decimal("0")
    else:
        units, contributing = _event_units(basis, events)
        if units > 0:
            subtotal = (base + unit * units) * multiplier

    amount = quantize_money(subtotal)
    return {
        "rule_id": rule["rule_id"],
        "kind": task["kind"],
        "source": task["source"],
        "basis": basis,
        "lifecycle_state": task["lifecycle_state"],
        "base_amount_cny": decimal_text(base),
        "unit_amount_cny": decimal_text(unit),
        "multiplier": decimal_text(multiplier),
        "units": decimal_text(units),
        "amount_cny": decimal_text(amount),
        "effective_event_uuids": contributing,
    }
