"""社区共管补偿的确定性计价规则。

计价全部是纯函数：给定同样的任务版本、有效回执、检查点与封路事实，
任何时候重算都得到逐分一致的金额，因此周期草案可以在重启后复算，
接口也能逐事件解释每笔补偿由哪些有效回执构成。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping, Sequence


ZERO = Decimal("0")
MONEY_QUANTUM = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PricingRule:
    rule_id: str
    version: int
    task_family: str
    task_kind: str
    base_unit: str
    base_rate: Decimal
    reinforcement_rate: Decimal
    repeat_within_minutes: int
    road_closure_excuse_code: str

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> "PricingRule":
        return cls(
            rule_id=str(row["rule_id"]),
            version=int(row["version"]),
            task_family=str(row["task_family"]),
            task_kind=str(row["task_kind"]),
            base_unit=str(row["base_unit"]),
            base_rate=Decimal(str(row["base_rate_cny"])),
            reinforcement_rate=Decimal(str(row["reinforcement_rate_cny"])),
            repeat_within_minutes=int(row["repeat_within_minutes"]),
            road_closure_excuse_code=str(row["road_closure_excuse_code"]),
        )


@dataclass(frozen=True, slots=True)
class ReceiptFact:
    """服务层从 receipts 表整理出的、用于计价的单条回执事实。"""

    receipt_id: int
    person_id: str
    receipt_type: str
    checkpoint_id: str | None
    occurred_at: str
    # report 类回执的内容指纹（规范化负载）；签到类为 None。
    fingerprint: str | None = None


def dedupe_checkins(
    events: Sequence[ReceiptFact],
    repeat_within_minutes: int,
) -> tuple[tuple[int, ...], tuple[dict[str, object], ...]]:
    """剔除不能重复计酬的回执。

    - 签到/检查点：重复签到窗口内，同人同检查点（或同签到类型）只保留首条；
    - 上报事件：同内容指纹只计一次（设备重复补传同一事件），不同内容各自有效。

    返回 (有效回执编号, 被判重的回执说明)。事件按发生时间稳定排序。
    """

    from .clock import parse_utc

    window_seconds = repeat_within_minutes * 60
    accepted: list[ReceiptFact] = []
    accepted_fingerprints: set[str] = set()
    duplicates: list[dict[str, object]] = []
    for event in sorted(events, key=lambda item: (item.occurred_at, item.receipt_id)):
        key = (event.person_id, event.receipt_type, event.checkpoint_id or "")
        duplicate_of = None
        if event.receipt_type == "report":
            if event.fingerprint is not None and event.fingerprint in accepted_fingerprints:
                duplicate_of = next(
                    item.receipt_id for item in accepted
                    if item.receipt_type == "report" and item.fingerprint == event.fingerprint
                )
        else:
            event_time = parse_utc(event.occurred_at, "occurred_at")
            for earlier in accepted:
                earlier_key = (earlier.person_id, earlier.receipt_type, earlier.checkpoint_id or "")
                if earlier_key != key:
                    continue
                delta = (event_time - parse_utc(earlier.occurred_at, "occurred_at")).total_seconds()
                if 0 <= delta <= window_seconds:
                    duplicate_of = earlier.receipt_id
                    break
        if duplicate_of is None:
            accepted.append(event)
            if event.fingerprint is not None:
                accepted_fingerprints.add(event.fingerprint)
        else:
            duplicates.append({
                "receipt_id": event.receipt_id,
                "reason": "duplicate_checkin",
                "duplicate_of_receipt_id": duplicate_of,
            })
    return tuple(item.receipt_id for item in accepted), tuple(duplicates)


@dataclass(frozen=True, slots=True)
class LineFact:
    """服务层为某个任务上的某个人汇总的计价事实。"""

    person_id: str
    task_state: str
    required_checkpoints: tuple[str, ...] = ()
    # checkpoint_id -> 佐证它达成的有效回执编号
    reached: Mapping[str, int] = field(default_factory=dict)
    # 因封路等合理免责而未到达的检查点
    excused: frozenset[str] = frozenset()
    signed_on: bool = False
    signed_off: bool = False
    shift_excused: bool = False
    # 计件类任务（野生动物冲突上报）的有效上报回执编号
    valid_events: tuple[int, ...] = ()


def compute_line(rule: PricingRule, fact: LineFact) -> dict[str, object]:
    """根据计价规则与事实计算一条补偿明细的全部金额构成。"""

    required = list(fact.required_checkpoints)
    reached_map = dict(fact.reached)
    reached = [point for point in required if point in reached_map]
    excused = [point for point in required if point in fact.excused and point not in reached_map]
    missing = [point for point in required if point not in reached_map and point not in fact.excused]
    is_reinforcement = rule.task_kind == "reinforcement"

    units: list[dict[str, object]] = []
    if rule.base_unit == "event":
        # 野生动物冲突上报：按有效上报事件计件，重复回执已在上游剔除。
        for receipt_id in fact.valid_events:
            units.append({
                "unit": "valid_event",
                "receipt_id": receipt_id,
                "state": "reached",
                "rate_cny": decimal_text(rule.base_rate),
            })
        earned = money(rule.base_rate * len(units))
        excused_amount = ZERO
        deducted = ZERO
        paid_unit_count = len(units)
    elif required:
        # 检查点单价制：到达即计单价；封路免责按单价照付；缺失扣减单价。
        for point in required:
            if point in reached_map:
                state = "reached"
                receipt_id: int | None = reached_map[point]
            elif point in fact.excused:
                state = "excused"
                receipt_id = None
            else:
                state = "missing"
                receipt_id = None
            units.append({
                "unit": "checkpoint",
                "checkpoint_id": point,
                "receipt_id": receipt_id,
                "state": state,
                "rate_cny": decimal_text(rule.base_rate),
            })
        earned = money(rule.base_rate * len(reached))
        excused_amount = money(rule.base_rate * len(excused))
        deducted = money(rule.base_rate * len(missing))
        paid_unit_count = len(reached) + len(excused)
    else:
        completed = fact.task_state == "completed" and fact.signed_on and fact.signed_off
        fully_excused = fact.shift_excused and fact.signed_on
        if completed:
            state = "completed"
        elif fully_excused:
            state = "excused"
        else:
            state = "missing"
        units.append({
            "unit": "shift",
            "state": state,
            "rate_cny": decimal_text(rule.base_rate),
        })
        earned = money(rule.base_rate) if completed else ZERO
        excused_amount = money(rule.base_rate) if fully_excused else ZERO
        deducted = money(rule.base_rate) if state == "missing" else ZERO
        paid_unit_count = 0 if state == "missing" else 1

    # 总额 = 实做基准 + 合理免责 + 增援加价；扣减额仅列示，不计入应付。
    base_amount = money(earned)
    excused_amount = money(excused_amount)
    reinforcement_amount = money(rule.reinforcement_rate * paid_unit_count) if is_reinforcement else money(ZERO)
    deducted_amount = money(deducted)
    total_amount = money(base_amount + excused_amount + reinforcement_amount)

    return {
        "person_id": fact.person_id,
        "task_family": rule.task_family,
        "task_kind": rule.task_kind,
        "base_unit": rule.base_unit,
        "pricing_rule_id": rule.rule_id,
        "pricing_rule_version": rule.version,
        "base_amount_cny": decimal_text(base_amount),
        "reinforcement_amount_cny": decimal_text(reinforcement_amount),
        "deducted_amount_cny": decimal_text(deducted_amount),
        "excused_amount_cny": decimal_text(excused_amount),
        "total_amount_cny": decimal_text(total_amount),
        "units": units,
        "checkpoints": {
            "required": required,
            "reached": reached,
            "excused": excused,
            "missing": missing,
        },
        "valid_event_receipt_ids": list(fact.valid_events),
    }


def blockade_covers(
    starts_at: str,
    ends_at: str | None,
    window_start: str,
    window_end: str,
) -> bool:
    """判断封路区间是否与任务/检查点的时间窗相交（闭区间，含端点）。"""

    from .clock import parse_utc

    start = parse_utc(starts_at, "blockade.starts_at")
    finish = None if ends_at is None else parse_utc(ends_at, "blockade.ends_at")
    left = parse_utc(window_start, "window_start")
    right = parse_utc(window_end, "window_end")
    if finish is not None and finish < start:
        raise ValueError("封路结束时间不能早于开始时间")
    if finish is None:
        return start <= right
    return start <= right and finish >= left


def totalize(lines: Iterable[Mapping[str, object]]) -> dict[str, str]:
    """汇总一批明细的金额，供支付清单与解释接口使用。"""

    base = reinforcement = deducted = excused = total = ZERO
    for line in lines:
        base += Decimal(str(line["base_amount_cny"]))
        reinforcement += Decimal(str(line["reinforcement_amount_cny"]))
        deducted += Decimal(str(line["deducted_amount_cny"]))
        excused += Decimal(str(line["excused_amount_cny"]))
        total += Decimal(str(line["total_amount_cny"]))
    return {
        "base_amount_cny": decimal_text(money(base)),
        "reinforcement_amount_cny": decimal_text(money(reinforcement)),
        "deducted_amount_cny": decimal_text(money(deducted)),
        "excused_amount_cny": decimal_text(money(excused)),
        "total_amount_cny": decimal_text(money(total)),
    }
