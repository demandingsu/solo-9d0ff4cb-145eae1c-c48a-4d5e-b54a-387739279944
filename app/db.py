"""SQLite 连接与事务管理。

并发与一致性设计：
- WAL 模式：读查询拿到一致的"已提交"快照，导入（写）不阻塞查询（读）；
- 全局写锁 + BEGIN IMMEDIATE：同一时刻只允许一个导入/校时事务；
- 映射、流水、终端校时、月度统计在同一个事务里提交 —— 查询端永远只看到
  同属一个已提交状态的数据，不会看到"偏移已改、统计未算"的中间态；
- 写事务内任何失败都会 ROLLBACK，已有结果不变。

Schema 演进：终端校时功能为 swipes 增加 terminal_id / corrected_time_utc
两列，并新增 terminal_offsets、terminal_offset_changes 两张表。
对已存在的旧库用幂等 ALTER TABLE 迁移并回填校正时刻。
"""
from __future__ import annotations

import os
import sqlite3
import threading

_DEFAULT_DB_PATH = "/data/transit.db"

_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batches (
    batch_id      TEXT PRIMARY KEY,
    file_type     TEXT NOT NULL CHECK (file_type IN ('swipes', 'mappings')),
    filename      TEXT NOT NULL,
    rows_received INTEGER NOT NULL,
    rows_applied  INTEGER NOT NULL,
    rows_skipped  INTEGER NOT NULL DEFAULT 0,
    note          TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS swipes (
    txn_id             TEXT PRIMARY KEY,
    swipe_time_utc     TEXT NOT NULL,   -- 设备上报的原始 UTC 时刻，永不修改
    corrected_time_utc TEXT NOT NULL,   -- 原始时刻 + 终端偏移后的校正时刻（归站/统计口径）
    line               TEXT NOT NULL,
    temp_station_id    TEXT NOT NULL,
    terminal_id        TEXT,            -- 可空：未标终端的旧数据为 NULL，不参与校时
    batch_id           TEXT NOT NULL REFERENCES import_batches (batch_id)
);
CREATE INDEX IF NOT EXISTS idx_swipes_lookup
    ON swipes (line, temp_station_id, corrected_time_utc);
CREATE INDEX IF NOT EXISTS idx_swipes_terminal
    ON swipes (terminal_id);

CREATE TABLE IF NOT EXISTS mappings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    line            TEXT NOT NULL,
    temp_station_id TEXT NOT NULL,
    valid_from_utc  TEXT NOT NULL,
    valid_to_utc    TEXT NOT NULL,
    real_station    TEXT NOT NULL,
    batch_id        TEXT NOT NULL REFERENCES import_batches (batch_id),
    CHECK (valid_from_utc < valid_to_utc)
);
CREATE INDEX IF NOT EXISTS idx_mappings_lookup
    ON mappings (line, temp_station_id);

-- 每个终端当前生效的有符号秒偏移（时钟偏快为负、偏慢为正）
CREATE TABLE IF NOT EXISTS terminal_offsets (
    terminal_id    TEXT PRIMARY KEY,
    offset_seconds INTEGER NOT NULL,
    updated_at     TEXT NOT NULL
);

-- 终端校时审计：每次偏移更正一行（含首次设置、重算影响流水数）
CREATE TABLE IF NOT EXISTS terminal_offset_changes (
    batch_id           TEXT PRIMARY KEY,
    terminal_id        TEXT NOT NULL,
    old_offset_seconds INTEGER,          -- NULL 表示该终端此前没有偏移
    new_offset_seconds INTEGER NOT NULL,
    affected_swipes    INTEGER NOT NULL, -- 本次重算的该终端流水条数
    note               TEXT,
    created_at         TEXT NOT NULL
);

-- 物化统计：与导入/校时同事务重算、同事务提交
CREATE TABLE IF NOT EXISTS monthly_stats (
    month        TEXT NOT NULL,   -- 校正时刻的 UTC 月份，格式 YYYY-MM
    real_station TEXT NOT NULL,
    boardings    INTEGER NOT NULL,
    PRIMARY KEY (month, real_station)
);
"""


def db_path() -> str:
    return os.environ.get("TRANSIT_DB_PATH", _DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path(), isolation_level=None, check_same_thread=False, timeout=30
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _migrate_swipes(conn: sqlite3.Connection) -> None:
    """旧库升级：补齐校时功能新增列并回填校正时刻（幂等）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(swipes)")}
    if not cols:
        return  # 全新库：SCHEMA 里已建好新表
    if "terminal_id" not in cols:
        conn.execute("ALTER TABLE swipes ADD COLUMN terminal_id TEXT")
    if "corrected_time_utc" not in cols:
        # 旧库没有校正列：先加可空列，再以原始时刻回填（旧数据均无终端，偏移为 0）
        conn.execute("ALTER TABLE swipes ADD COLUMN corrected_time_utc TEXT")
        conn.execute(
            "UPDATE swipes SET corrected_time_utc = swipe_time_utc "
            "WHERE corrected_time_utc IS NULL"
        )
    # 旧库的同名索引建在 swipe_time_utc 上：先删除再按校正时刻重建，
    # 否则 IF NOT EXISTS 会因索引名已存在而保留过期索引
    conn.execute("DROP INDEX IF EXISTS idx_swipes_lookup")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_swipes_lookup "
        "ON swipes (line, temp_station_id, corrected_time_utc)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_swipes_terminal ON swipes (terminal_id)"
    )


def init_db() -> None:
    path = db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = connect()
    try:
        # 旧库先补列（SCHEMA 中的新索引依赖 corrected_time_utc），再建新表/索引
        _migrate_swipes(conn)
        conn.executescript(SCHEMA)
        _migrate_swipes(conn)
    finally:
        conn.close()


class write_txn:
    """序列化写事务：进入时取全局写锁并 BEGIN IMMEDIATE；
    正常退出 COMMIT，异常退出 ROLLBACK（导入/校时失败不改变已有结果）。"""

    def __enter__(self) -> sqlite3.Connection:
        _write_lock.acquire()
        try:
            self._conn = connect()
            self._conn.execute("BEGIN IMMEDIATE")
        except Exception:
            _write_lock.release()
            raise
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self._conn.execute("ROLLBACK" if exc_type is not None else "COMMIT")
        finally:
            self._conn.close()
            _write_lock.release()
        return False
