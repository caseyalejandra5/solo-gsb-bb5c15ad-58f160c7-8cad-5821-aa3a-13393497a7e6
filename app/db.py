"""SQLite 连接与建表。

数据库路径通过环境变量 ``CIDR_DB_PATH`` 配置，默认 ``./cidr.db``。
所有连接使用 ``isolation_level=None``（自动提交），写操作由
``app.main.write_tx`` 显式开启 ``BEGIN IMMEDIATE`` 事务。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = "./cidr.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pools (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    cidr        TEXT NOT NULL,
    version     INTEGER NOT NULL CHECK (version IN (4, 6)),
    prefix_len  INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS unavailable (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_id     INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    cidr        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (pool_id, cidr)
);

CREATE TABLE IF NOT EXISTS allocations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_id     INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    cidr        TEXT NOT NULL,
    prefix_len  INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (pool_id, cidr)
);

CREATE TABLE IF NOT EXISTS plans (
    id           TEXT PRIMARY KEY,
    pool_id      INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    cidrs        TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'committed')),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at   TEXT NOT NULL,
    committed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_plans_pool_id ON plans(pool_id);

-- 池快照：创建时的不可变状态副本，删除地址池时随之外键级联删除。
-- 快照一经写入不提供任何修改入口（无 UPDATE / DELETE 路由）。
CREATE TABLE IF NOT EXISTS snapshots (
    id          TEXT PRIMARY KEY,
    pool_id     INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_snapshots_pool_id ON snapshots(pool_id);

CREATE TABLE IF NOT EXISTS snapshot_allocations (
    snapshot_id  TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    cidr         TEXT NOT NULL,
    prefix_len   INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, position)
);

CREATE TABLE IF NOT EXISTS snapshot_unavailable (
    snapshot_id  TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    cidr         TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, position)
);
"""


def db_path() -> str:
    return os.environ.get("CIDR_DB_PATH", DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    path = db_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
