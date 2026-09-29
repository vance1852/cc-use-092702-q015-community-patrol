"""社区共管任务与补偿核算服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comanage_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('villager','village_head','station','finance','auditor')),
    village_group_id TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 村组与保护站等组织，跨村组转派与三方确认都引用此处。
CREATE TABLE IF NOT EXISTS organizations (
    org_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('village_group','station')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    created_at TEXT NOT NULL
);

-- 人员资格：巡护员必须持有任务族对应的有效资格才能被安排并计酬。
CREATE TABLE IF NOT EXISTS personnel_qualifications (
    qualification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL REFERENCES comanage_users(user_id),
    task_family TEXT NOT NULL,
    level TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK(state IN ('active','revoked')),
    created_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(person_id, task_family, valid_from)
);

-- 计价规则版本化：compose 时快照规则版本，旧周期永远按旧版本复算。
CREATE TABLE IF NOT EXISTS pricing_rules (
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    task_family TEXT NOT NULL,
    task_kind TEXT NOT NULL,
    base_unit TEXT NOT NULL CHECK(base_unit IN ('shift','checkpoint','event','km','day')),
    base_rate_cny TEXT NOT NULL,
    reinforcement_rate_cny TEXT NOT NULL,
    repeat_within_minutes INTEGER NOT NULL,
    road_closure_excuse_code TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK(state IN ('active','superseded','retired')),
    created_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    created_at TEXT NOT NULL,
    supersedes_version INTEGER,
    PRIMARY KEY(rule_id, version),
    UNIQUE(task_family, task_kind, version)
);

-- 任务主表保存当前版本指针；全部历史版本落在 task_versions。
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL CHECK(current_version > 0),
    state TEXT NOT NULL
        CHECK(state IN ('assigned','in_progress','completed','incomplete','cancelled')),
    responsible_org_id TEXT NOT NULL REFERENCES organizations(org_id),
    scheduled_for TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_schedule
ON tasks(scheduled_for, task_id);

CREATE TABLE IF NOT EXISTS task_versions (
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    version INTEGER NOT NULL CHECK(version > 0),
    task_family TEXT NOT NULL
        CHECK(task_family IN ('fire_lookout','waste_haul','wildlife_conflict')),
    task_kind TEXT NOT NULL CHECK(task_kind IN ('planned','reinforcement')),
    service_area_id TEXT NOT NULL,
    pricing_rule_id TEXT NOT NULL,
    pricing_rule_version INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    created_at TEXT NOT NULL,
    supersedes_version INTEGER,
    PRIMARY KEY(task_id, version),
    FOREIGN KEY(pricing_rule_id, pricing_rule_version)
        REFERENCES pricing_rules(rule_id, version)
);

CREATE INDEX IF NOT EXISTS idx_task_versions_area
ON task_versions(service_area_id, task_family);

-- 任务分配的人员。started 后只允许经双方确认的交接修改，后台无法静默换人。
CREATE TABLE IF NOT EXISTS task_assignments (
    task_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    person_id TEXT NOT NULL REFERENCES comanage_users(user_id),
    role TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK(state IN ('active','relieved')),
    joined_at TEXT NOT NULL,
    relieved_at TEXT,
    PRIMARY KEY(task_id, version, person_id, role),
    FOREIGN KEY(task_id, version) REFERENCES task_versions(task_id, version)
);

CREATE TABLE IF NOT EXISTS service_areas (
    service_area_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    geometry_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    service_area_id TEXT NOT NULL REFERENCES service_areas(service_area_id),
    name TEXT NOT NULL,
    position_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_checkpoints (
    task_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    checkpoint_id TEXT NOT NULL REFERENCES checkpoints(checkpoint_id),
    sequence_no INTEGER NOT NULL CHECK(sequence_no > 0),
    required INTEGER NOT NULL CHECK(required IN (0,1)),
    PRIMARY KEY(task_id, version, checkpoint_id),
    FOREIGN KEY(task_id, version) REFERENCES task_versions(task_id, version)
);

-- 离线设备登记；设备序号用于补传时识别重放与分叉。
CREATE TABLE IF NOT EXISTS field_devices (
    device_serial TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES comanage_users(user_id),
    model_name TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1))
);

-- 每台设备维护一个严格递增的序号游标，重放与缺口在此识别。
CREATE TABLE IF NOT EXISTS device_streams (
    device_serial TEXT PRIMARY KEY REFERENCES field_devices(device_serial),
    last_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_sequence >= 0)
);

-- 回执（签到/检查点/完工/上报）。UNIQUE 约束天然吸收重放。
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_serial TEXT NOT NULL REFERENCES field_devices(device_serial),
    sequence_no INTEGER NOT NULL CHECK(sequence_no > 0),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    receipt_type TEXT NOT NULL
        CHECK(receipt_type IN ('sign_on','checkpoint','sign_off','report')),
    checkpoint_id TEXT REFERENCES checkpoints(checkpoint_id),
    person_id TEXT NOT NULL REFERENCES comanage_users(user_id),
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    -- replay：同序号重复送达；fork：同序号但内容不同；gap：曾检测到序号缺口
    ingest_state TEXT NOT NULL
        CHECK(ingest_state IN ('accepted','replayed','forked','gapped')),
    UNIQUE(device_serial, sequence_no)
);

CREATE INDEX IF NOT EXISTS idx_receipts_task
ON receipts(task_id, occurred_at, receipt_id);

-- 封路等阻断事件，登记后任务可以合理免责而不按缺勤扣减。
CREATE TABLE IF NOT EXISTS blockade_events (
    blockade_id TEXT PRIMARY KEY,
    service_area_id TEXT NOT NULL REFERENCES service_areas(service_area_id),
    checkpoint_id TEXT REFERENCES checkpoints(checkpoint_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    reason_code TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    registered_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    registered_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_blockades_area_time
ON blockade_events(service_area_id, starts_at, ends_at);

-- 跨村组转派：必须双方村组确认，任务开始后禁止发起。
CREATE TABLE IF NOT EXISTS transfer_requests (
    transfer_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    task_version INTEGER NOT NULL,
    from_org_id TEXT NOT NULL REFERENCES organizations(org_id),
    to_org_id TEXT NOT NULL REFERENCES organizations(org_id),
    state TEXT NOT NULL
        CHECK(state IN ('proposed','accepted','rejected','cancelled')),
    proposed_people_json TEXT NOT NULL,
    note TEXT NOT NULL,
    proposed_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    proposed_at TEXT NOT NULL,
    decided_by TEXT REFERENCES comanage_users(user_id),
    decided_at TEXT,
    CHECK(from_org_id <> to_org_id)
);

-- 结算周期与可复算草案。
CREATE TABLE IF NOT EXISTS settlement_periods (
    period_id TEXT PRIMARY KEY,
    starts_on TEXT NOT NULL,
    ends_on TEXT NOT NULL,
    state TEXT NOT NULL
        CHECK(state IN ('open','composed','confirmed','paid','closed')),
    composed_by TEXT REFERENCES comanage_users(user_id),
    composed_at TEXT,
    input_sha256 TEXT,
    created_at TEXT NOT NULL,
    CHECK(ends_on >= starts_on)
);

CREATE TABLE IF NOT EXISTS settlement_lines (
    line_id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id TEXT NOT NULL REFERENCES settlement_periods(period_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    task_version INTEGER NOT NULL,
    org_id TEXT NOT NULL REFERENCES organizations(org_id),
    person_id TEXT NOT NULL REFERENCES comanage_users(user_id),
    task_family TEXT NOT NULL,
    task_kind TEXT NOT NULL,
    base_amount_cny TEXT NOT NULL,
    reinforcement_amount_cny TEXT NOT NULL,
    deducted_amount_cny TEXT NOT NULL,
    excused_amount_cny TEXT NOT NULL,
    total_amount_cny TEXT NOT NULL,
    disputed INTEGER NOT NULL DEFAULT 0 CHECK(disputed IN (0,1)),
    frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0,1)),
    detail_json TEXT NOT NULL,
    UNIQUE(period_id, task_id, person_id)
);

CREATE INDEX IF NOT EXISTS idx_settlement_lines_org
ON settlement_lines(period_id, org_id, person_id);

-- 村组、保护站、财务分别确认自己负责的部分；每条明细只允许被一个角色确认。
CREATE TABLE IF NOT EXISTS settlement_confirmations (
    confirmation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id TEXT NOT NULL,
    scope_role TEXT NOT NULL
        CHECK(scope_role IN ('village_head','station','finance')),
    org_id TEXT REFERENCES organizations(org_id),
    person_id TEXT REFERENCES comanage_users(user_id),
    line_id INTEGER REFERENCES settlement_lines(line_id),
    decision TEXT NOT NULL CHECK(decision IN ('confirmed','disputed')),
    note TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    decided_at TEXT NOT NULL,
    UNIQUE(period_id, scope_role, line_id),
    CHECK(line_id IS NOT NULL OR org_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_confirmations_period
ON settlement_confirmations(period_id, scope_role);

-- 争议：按行冻结；只有受影响任务和金额被冻结，无争议部分继续支付。
CREATE TABLE IF NOT EXISTS disputes (
    dispute_id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id TEXT NOT NULL REFERENCES settlement_periods(period_id),
    line_id INTEGER NOT NULL REFERENCES settlement_lines(line_id),
    task_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open','resolved','rejected')),
    reason TEXT NOT NULL,
    raised_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    raised_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(period_id, line_id)
);

-- 支付清单只收集无争议、三方确认完成的行。
CREATE TABLE IF NOT EXISTS payment_entries (
    payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id TEXT NOT NULL REFERENCES settlement_periods(period_id),
    line_id INTEGER NOT NULL UNIQUE REFERENCES settlement_lines(line_id),
    org_id TEXT NOT NULL REFERENCES organizations(org_id),
    person_id TEXT NOT NULL REFERENCES comanage_users(user_id),
    amount_cny TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 追加决定：任何调整都只追加一行原因，从不覆盖历史。
CREATE TABLE IF NOT EXISTS adjustment_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id TEXT NOT NULL REFERENCES settlement_periods(period_id),
    task_id TEXT,
    line_id INTEGER REFERENCES settlement_lines(line_id),
    dispute_id INTEGER REFERENCES disputes(dispute_id),
    adjustment_type TEXT NOT NULL,
    amount_delta_cny TEXT NOT NULL,
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES comanage_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_adjustments_period
ON adjustment_decisions(period_id, task_id);

CREATE TABLE IF NOT EXISTS comanage_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

-- 与既有模块一致的哈希链审计。
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


REQUIRED_TABLES = frozenset({
    "schema_meta", "comanage_users", "organizations", "personnel_qualifications",
    "pricing_rules", "tasks", "task_versions", "task_assignments", "service_areas",
    "checkpoints", "task_checkpoints", "field_devices", "device_streams", "receipts",
    "blockade_events", "transfer_requests", "settlement_periods", "settlement_lines",
    "settlement_confirmations", "disputes", "payment_entries", "adjustment_decisions",
    "comanage_idempotency", "comanage_audit_events",
})


def connect(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """打开连接并启用外键、WAL 与忙等待。

    HTTP 服务在工作线程中共享启动时创建的连接，传入 check_same_thread=False；
    所有写入都走 BEGIN IMMEDIATE 事务并配合 busy_timeout 串行化。
    """

    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=check_same_thread
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化全部表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
