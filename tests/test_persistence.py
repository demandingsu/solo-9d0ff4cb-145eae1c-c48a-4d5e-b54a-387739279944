"""持久化层保障：旧库迁移、校时事务原子回滚。"""
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSIT_DB_PATH", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_legacy_schema_migrates_and_backfills(tmp_path, monkeypatch):
    """旧版本库（无 terminal_id/corrected_time_utc/终端表）可平滑升级。"""
    db_file = tmp_path / "legacy.db"
    monkeypatch.setenv("TRANSIT_DB_PATH", str(db_file))
    raw = sqlite3.connect(db_file)
    raw.executescript(
        """
        CREATE TABLE import_batches (
          batch_id TEXT PRIMARY KEY, file_type TEXT NOT NULL, filename TEXT NOT NULL,
          rows_received INTEGER NOT NULL, rows_applied INTEGER NOT NULL,
          rows_skipped INTEGER NOT NULL DEFAULT 0, note TEXT, created_at TEXT NOT NULL);
        CREATE TABLE swipes (
          txn_id TEXT PRIMARY KEY, swipe_time_utc TEXT NOT NULL, line TEXT NOT NULL,
          temp_station_id TEXT NOT NULL, batch_id TEXT NOT NULL);
        CREATE TABLE mappings (
          id INTEGER PRIMARY KEY AUTOINCREMENT, line TEXT NOT NULL,
          temp_station_id TEXT NOT NULL, valid_from_utc TEXT NOT NULL,
          valid_to_utc TEXT NOT NULL, real_station TEXT NOT NULL, batch_id TEXT NOT NULL);
        CREATE TABLE monthly_stats (month TEXT NOT NULL, real_station TEXT NOT NULL,
          boardings INTEGER NOT NULL, PRIMARY KEY (month, real_station));
        -- 旧版本索引建在原始时刻上，迁移后必须重建为校正时刻口径
        CREATE INDEX idx_swipes_lookup
          ON swipes (line, temp_station_id, swipe_time_utc);
        INSERT INTO import_batches VALUES
          ('b1','swipes','old.csv',1,1,0,NULL,'2026-01-01T00:00:00Z');
        INSERT INTO swipes VALUES
          ('OLD1','2026-01-05T08:00:00.000000Z','L1','T100','b1');
        """
    )
    raw.commit()
    raw.close()

    from app import db, service

    db.init_db()
    db.init_db()  # 迁移必须幂等可重复执行
    conn = db.connect()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(swipes)")}
        assert {"terminal_id", "corrected_time_utc"} <= cols
        row = conn.execute(
            "SELECT swipe_time_utc, corrected_time_utc, terminal_id FROM swipes"
        ).fetchone()
        # 旧数据回填：校正时刻 = 原始时刻，终端号为空
        assert row[0] == row[1] and row[2] is None
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"terminal_offsets", "terminal_offset_changes"} <= tables
        # 旧索引已重建为校正时刻口径，且终端索引存在
        index_sql = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' "
                "AND name IN ('idx_swipes_lookup','idx_swipes_terminal')"
            )
        }
        assert "corrected_time_utc" in index_sql["idx_swipes_lookup"]
        assert "swipe_time_utc" not in index_sql["idx_swipes_lookup"]
        assert index_sql["idx_swipes_terminal"] is not None
    finally:
        conn.close()

    # 迁移后功能正常，且旧流水不受任何校时影响
    service.set_terminal_offset("TM-A", -60)
    detail = service.transaction_detail("OLD1")
    assert detail["terminal_id"] is None
    assert detail["corrected_time_utc"] == detail["swipe_time_utc"]


def test_offset_update_rolls_back_atomically(client, monkeypatch):
    """校时事务内统计重算失败：偏移、审计、校正时刻全部回滚，不留部分更改。"""
    from app import service

    csv1 = (
        b"txn_id,swipe_time_utc,line,temp_station_id,terminal_id\n"
        b"T1,2026-01-05T08:00:00Z,L1,T100,TM-A\n"
    )
    r = client.post(
        "/api/imports/swipes", files={"file": ("s.csv", csv1, "text/csv")}
    )
    assert r.status_code == 200

    orig = service._recompute_stats

    def boom(conn):
        raise RuntimeError("simulated stats failure")

    monkeypatch.setattr(service, "_recompute_stats", boom)
    with pytest.raises(RuntimeError):
        service.set_terminal_offset("TM-A", -3600)
    monkeypatch.setattr(service, "_recompute_stats", orig)

    # 没有偏移、没有审计、校正时刻未被修改
    assert service.list_terminal_offsets() == []
    assert service.list_offset_changes() == []
    detail = service.transaction_detail("T1")
    assert detail["corrected_time_utc"] == "2026-01-05T08:00:00.000000Z"
    assert detail["offset_seconds"] is None

    # 服务恢复后可正常校时
    ok = service.set_terminal_offset("TM-A", -3600)
    assert ok["changed"] is True
    assert service.transaction_detail("T1")["corrected_time_utc"] == (
        "2026-01-05T07:00:00.000000Z"
    )
