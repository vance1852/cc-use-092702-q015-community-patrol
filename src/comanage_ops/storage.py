"""共管任务与补偿核算服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS comanage_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('station','village','finance','auditor')),
    village_group_id TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    device_serial TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    owner_village_group_id TEXT,
    registered_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
    created_at TEXT NOT NULL
);

-- 每台离线设备的本地事件序号水位：补传按 (device_serial, seq) 识别重放。
CREATE TABLE IF NOT EXISTS device_event_streams (
    device_serial TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK(seq > 0),
    event_uuid TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY(device_serial, seq),
    UNIQUE(device_serial, event_uuid)
);

CREATE TABLE IF NOT EXISTS service_areas (
    area_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    village_group_id TEXT NOT NULL,
    geometry_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qualifications (
    qualification_id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_qualifications_person
ON qualifications(person_id, kind, active);

CREATE TABLE IF NOT EXISTS pricing_rules (
    rule_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    basis TEXT NOT NULL,
    base_amount_cny TEXT NOT NULL,
    unit_amount_cny TEXT NOT NULL,
    reinforcement_multiplier TEXT NOT NULL,
    absolved_ratio TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pricing_rules_lookup
ON pricing_rules(kind, source, valid_from, valid_until);

-- 任务为版本化聚合：每次调整或转派写入新版本，supersedes 指向上一版本。
-- 版本行保存当时的定义与受托人快照，只追加不更新；运行态见 task_runtime。
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('planned','reinforcement')),
    area_id TEXT NOT NULL REFERENCES service_areas(area_id),
    title TEXT NOT NULL,
    planned_start TEXT NOT NULL,
    planned_end TEXT,
    required_qualification TEXT NOT NULL,
    checkpoints_json TEXT NOT NULL,
    assignee_person_id TEXT,
    assignee_village_group_id TEXT,
    supersedes_version INTEGER,
    change_reason TEXT,
    created_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(task_id, version)
);

-- 任务运行态（当前版本、派单确认状态、生命周期），与版本定义分离。
CREATE TABLE IF NOT EXISTS task_runtime (
    task_id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL,
    assignment_status TEXT NOT NULL DEFAULT 'unassigned'
        CHECK(assignment_status IN ('unassigned','proposed','confirmed','cancelled')),
    proposed_person_id TEXT,
    proposed_village_group_id TEXT,
    lifecycle_state TEXT NOT NULL DEFAULT 'assigned'
        CHECK(lifecycle_state IN ('assigned','in_progress','completed','absolved','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_kind
ON tasks(kind, source);

-- 跨村组转派的双方确认链：from/to 两组各自显式确认后才生效。
CREATE TABLE IF NOT EXISTS task_transfers (
    transfer_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    task_version INTEGER NOT NULL,
    from_village_group_id TEXT NOT NULL,
    to_village_group_id TEXT NOT NULL,
    to_person_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK(status IN ('proposed','accepted','rejected','cancelled')),
    reason TEXT NOT NULL,
    proposed_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    proposed_at TEXT NOT NULL,
    from_confirmed_by TEXT REFERENCES comanage_users(user_id),
    from_confirmed_at TEXT,
    to_confirmed_by TEXT REFERENCES comanage_users(user_id),
    to_confirmed_at TEXT,
    decided_by TEXT REFERENCES comanage_users(user_id),
    decided_at TEXT,
    decision_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_transfers_task
ON task_transfers(task_id, status);

-- 有效事件回执（同一设备序号/UUID 的重复补传不会产生第二条）。
CREATE TABLE IF NOT EXISTS task_events (
    event_uuid TEXT PRIMARY KEY,
    device_serial TEXT NOT NULL REFERENCES devices(device_serial),
    task_id TEXT NOT NULL,
    task_version INTEGER NOT NULL,
    person_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('assigned','started','checkpoint','completed','absolved')),
    client_clock TEXT NOT NULL,
    received_at TEXT NOT NULL,
    checkpoint_id TEXT,
    quantity TEXT NOT NULL DEFAULT '0',
    evidence_ref TEXT,
    absolved_reason TEXT,
    note TEXT,
    validity_state TEXT NOT NULL DEFAULT 'accepted'
        CHECK(validity_state IN ('accepted','rejected','voided')),
    invalid_reason TEXT,
    FOREIGN KEY(task_id, task_version) REFERENCES tasks(task_id, version)
);

CREATE INDEX IF NOT EXISTS idx_task_events_task
ON task_events(task_id, event_type, client_clock);

-- 追加式决定记录：调整、作废、争议结论都只追加，不覆盖。
CREATE TABLE IF NOT EXISTS event_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uuid TEXT NOT NULL REFERENCES task_events(event_uuid),
    action TEXT NOT NULL CHECK(action IN ('accept','reject','void','reinstate')),
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_decisions_event
ON event_decisions(event_uuid, decision_id);

CREATE TABLE IF NOT EXISTS settlements (
    settlement_id TEXT PRIMARY KEY,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK(state IN ('draft','partially_confirmed','confirmed','paid','frozen')),
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    generated_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    generated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_settlements_period
ON settlements(period_start, period_end);

-- 草案中每个任务一行，冻结/确认/支付都只针对具体任务行。
-- ready_at 是该行最近一次进入待确认（草案生成或争议解冻）的时间，
-- 只有时间晚于它的三方确认才能把该行推进支付清单。
CREATE TABLE IF NOT EXISTS settlement_items (
    settlement_id TEXT NOT NULL REFERENCES settlements(settlement_id),
    task_id TEXT NOT NULL,
    task_version INTEGER NOT NULL,
    village_group_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL,
    source TEXT NOT NULL,
    amount_cny TEXT NOT NULL,
    component_json TEXT NOT NULL,
    item_state TEXT NOT NULL DEFAULT 'draft'
        CHECK(item_state IN ('draft','frozen','confirmed','paid')),
    frozen_reason TEXT,
    ready_at TEXT NOT NULL,
    PRIMARY KEY(settlement_id, task_id)
);

CREATE INDEX IF NOT EXISTS idx_settlement_items_group
ON settlement_items(settlement_id, village_group_id, item_state);

-- 追加式确认历史：争议解决后三方就重算行再次确认时追加新行，旧意见保留。
CREATE TABLE IF NOT EXISTS settlement_confirmations (
    confirmation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id TEXT NOT NULL REFERENCES settlements(settlement_id),
    party TEXT NOT NULL CHECK(party IN ('village','station','finance')),
    village_group_id TEXT,
    status TEXT NOT NULL DEFAULT 'confirmed' CHECK(status IN ('confirmed','withdrawn')),
    confirmed_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_settlement_confirmations_lookup
ON settlement_confirmations(settlement_id, party, village_group_id, confirmation_id);

-- 结算过程中的一切调整只追加决定，不覆盖历史。
CREATE TABLE IF NOT EXISTS settlement_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id TEXT NOT NULL REFERENCES settlements(settlement_id),
    decision_type TEXT NOT NULL,
    note TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_settlement_decisions_settlement
ON settlement_decisions(settlement_id, decision_id);

CREATE TABLE IF NOT EXISTS disputes (
    dispute_id TEXT PRIMARY KEY,
    settlement_id TEXT NOT NULL REFERENCES settlements(settlement_id),
    task_id TEXT NOT NULL,
    raised_by_party TEXT NOT NULL CHECK(raised_by_party IN ('village','station','finance')),
    raised_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved','withdrawn')),
    resolution_note TEXT,
    resolved_by TEXT REFERENCES comanage_users(user_id),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(settlement_id, task_id, raised_by_party)
);

CREATE TABLE IF NOT EXISTS dispute_events (
    dispute_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispute_id TEXT NOT NULL REFERENCES disputes(dispute_id),
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES comanage_users(user_id),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dispute_events_dispute
ON dispute_events(dispute_id, dispute_event_id);

CREATE TABLE IF NOT EXISTS payment_list_items (
    settlement_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    village_group_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    amount_cny TEXT NOT NULL,
    listed_at TEXT NOT NULL,
    PRIMARY KEY(settlement_id, task_id)
);

CREATE TABLE IF NOT EXISTS comanage_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_comanage_audit_entity
ON comanage_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
