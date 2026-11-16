"""终端校时功能测试。

覆盖：
- 可选 terminal_id 列（含缺列旧格式、空值）；
- 校正时刻 = 原始时刻 + 有符号偏移，按校正时刻归站、按校正时刻归 UTC 月；
- 偏移更正重算历史流水，跨映射时段/跨月份统计同步更新；
- 未标终端旧数据保持原样；
- 同号重传按原始内容（含 terminal_id）判重，偏移变化不误报冲突；
- 校时与归站、统计同事务提交，失败不留部分更改；
- 单笔流水查询可核对原始/校正时刻。
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSIT_DB_PATH", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def upload(client, url, filename, content: bytes):
    return client.post(url, files={"file": (filename, content, "text/csv")})


def stats_map(client, month=None):
    params = {"month": month} if month else {}
    r = client.get("/api/stats/monthly", params=params)
    assert r.status_code == 200
    return {(i["month"], i["real_station"]): i["boardings"] for i in r.json()}


# 样例配套映射：覆盖 1-3 月，(L1,T100) 与 (L1,T200) 各有月份边界
MAPPINGS = (
    b"line,temp_station_id,valid_from_utc,valid_to_utc,real_station\n"
    b"L1,T100,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z,Central Station\n"
    b"L1,T100,2026-02-01T00:00:00Z,2026-03-01T00:00:00Z,Central Station North Gate\n"
    b"L1,T100,2026-03-01T00:00:00Z,2026-04-01T00:00:00Z,Central Station East Gate\n"
    b"L1,T200,2026-01-01T00:00:00Z,2026-02-15T00:00:00Z,Museum\n"
)

SWIPES = (SAMPLES / "swipes_4_clock_skew.csv").read_bytes()


def _setup(client):
    assert upload(client, "/api/imports/mappings", "m.csv", MAPPINGS).status_code == 200
    r = upload(client, "/api/imports/swipes", "s.csv", SWIPES)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- 基础口径


def test_terminal_id_optional_column_and_default_corrected(client):
    """无 terminal_id 列的旧格式照常导入；未标终端校正时刻=原始时刻。"""
    legacy = (
        b"txn_id,swipe_time_utc,line,temp_station_id\n"
        b"L1,2026-01-05T08:00:00Z,L1,T100\n"
    )
    assert upload(client, "/api/imports/swipes", "legacy.csv", legacy).status_code == 200
    detail = client.get("/api/transactions/L1").json()
    assert detail["terminal_id"] is None
    assert detail["offset_seconds"] is None
    assert detail["swipe_time_utc"] == detail["corrected_time_utc"]
    assert detail["real_station"] is None  # 还没映射


def test_empty_terminal_cell_treated_as_unlabeled(client):
    """terminal_id 空单元格与缺列等价，不参与校时。"""
    _setup(client)
    detail = client.get("/api/transactions/C005").json()
    assert detail["terminal_id"] is None
    assert detail["corrected_time_utc"] == detail["swipe_time_utc"]


def test_corrected_time_drives_attribution_and_month(client):
    """校时后跨月、跨时段、新归属、边界全覆盖。"""
    _setup(client)
    # 校时前（偏移为 0）：按原始时刻归属
    before = stats_map(client)
    assert before[("2026-01", "Central Station")] == 1          # C001
    assert before[("2026-02", "Central Station North Gate")] == 1  # C002
    assert before[("2026-03", "Central Station East Gate")] == 1  # C003
    assert before[("2026-02", "Museum")] == 1                  # C004
    una = {i["txn_id"] for i in client.get("/api/transactions/unattributed").json()["items"]}
    assert {"C005", "C006"} <= una  # 无映射 / 超出 3 月窗口

    # TM-A 时钟偏快 30 分钟：C001 跨年边界回到 2025-12，C002 回 1 月
    r = client.put("/api/terminals/TM-A/offset", json={"offset_seconds": -1800})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True and body["old_offset_seconds"] is None
    assert body["affected_swipes"] == 2
    c1 = client.get("/api/transactions/C001").json()
    assert c1["swipe_time_utc"] == "2026-01-01T00:20:00.000000Z"  # 原始保留
    assert c1["corrected_time_utc"] == "2025-12-31T23:50:00.000000Z"
    assert c1["offset_seconds"] == -1800 and c1["real_station"] is None  # 12 月无映射
    c2 = client.get("/api/transactions/C002").json()
    assert c2["corrected_time_utc"] == "2026-01-31T23:40:00.000000Z"
    assert c2["real_station"] == "Central Station"  # 从 North Gate 跨时段改挂 Central

    # TM-B 时钟偏快 1 小时：C003 回 2 月换站，C004 回 2/15 左闭边界内仍为 Museum
    r = client.put("/api/terminals/TM-B/offset", json={"offset_seconds": -3600})
    assert r.status_code == 200
    assert r.json()["affected_swipes"] == 2
    c3 = client.get("/api/transactions/C003").json()
    assert c3["corrected_time_utc"] == "2026-02-28T23:30:00.000000Z"
    assert c3["real_station"] == "Central Station North Gate"
    c4 = client.get("/api/transactions/C004").json()
    assert c4["corrected_time_utc"] == "2026-02-14T22:50:00.000000Z"
    assert c4["real_station"] == "Museum"

    after = stats_map(client)
    # 跨月更新：2026-03 的旧计数消失，2026-01/02 变化，C001 落 2025-12 未归属
    assert ("2026-03", "Central Station East Gate") not in after
    assert after[("2026-01", "Central Station")] == 1   # 仅 C002 改挂至此
    assert after[("2026-02", "Central Station North Gate")] == 1  # 仅 C003
    assert after[("2026-02", "Museum")] == 1            # C004 仍在
    una_after = {
        i["txn_id"] for i in client.get("/api/transactions/unattributed").json()["items"]
    }
    assert "C001" in una_after  # 校正到 2025-12，无映射
    assert {"C005", "C006"} <= una_after


def test_positive_offset_moves_swipe_into_coverage(client):
    """时钟偏慢（正偏移）把落在窗口外的流水向前推进映射窗口并完成归属。"""
    _setup(client)
    # P1 原始时刻在 2025-12，早于全部映射窗口 -> 未归属
    s = (
        b"txn_id,swipe_time_utc,line,temp_station_id,terminal_id\n"
        b"P1,2025-12-31T23:40:00Z,L1,T100,TM-C\n"
    )
    assert upload(client, "/api/imports/swipes", "p.csv", s).status_code == 200
    assert client.get("/api/transactions/P1").json()["real_station"] is None
    r = client.put("/api/terminals/TM-C/offset", json={"offset_seconds": 1800})
    assert r.status_code == 200
    detail = client.get("/api/transactions/P1").json()
    assert detail["corrected_time_utc"] == "2026-01-01T00:10:00.000000Z"
    assert detail["real_station"] == "Central Station"
    assert stats_map(client, "2026-01")[("2026-01", "Central Station")] >= 2


def test_unlabeled_legacy_swipes_never_change(client):
    """未标终端的流水在任何校时后保持原始时刻与归属。"""
    _setup(client)
    before_c5 = client.get("/api/transactions/C005").json()
    before_c6 = client.get("/api/transactions/C006").json()
    # 给一个不存在于任何流水的终端设偏移，再给已有终端反复改偏移
    client.put("/api/terminals/TM-X/offset", json={"offset_seconds": -7200})
    client.put("/api/terminals/TM-A/offset", json={"offset_seconds": -900})
    assert client.get("/api/transactions/C005").json() == before_c5
    assert client.get("/api/transactions/C006").json() == before_c6
    ov = client.get("/api/overview").json()
    assert ov["swipes_without_terminal"] == 2  # C005, C006


def test_offset_change_recomputes_existing_and_new_import_uses_current(client):
    """先校时后导入：新流水按当前偏移落校正时刻；再次改偏移全部重算。"""
    _setup(client)
    client.put("/api/terminals/TM-A/offset", json={"offset_seconds": -1800})
    s = (
        b"txn_id,swipe_time_utc,line,temp_station_id,terminal_id\n"
        b"N1,2026-01-10T00:10:00Z,L1,T100,TM-A\n"
    )
    assert upload(client, "/api/imports/swipes", "n.csv", s).status_code == 200
    n1 = client.get("/api/transactions/N1").json()
    assert n1["corrected_time_utc"] == "2026-01-09T23:40:00.000000Z"
    # 偏移从 -1800 改为 -3600：N1 与历史 C001/C002 一起重算
    client.put("/api/terminals/TM-A/offset", json={"offset_seconds": -3600})
    n1 = client.get("/api/transactions/N1").json()
    assert n1["corrected_time_utc"] == "2026-01-09T23:10:00.000000Z"
    changes = client.get("/api/terminals/offset-changes", params={"terminal_id": "TM-A"}).json()
    assert [c["new_offset_seconds"] for c in changes] == [-3600, -1800]  # 倒序
    # 首次校时时 N1 尚未导入（影响 C001/C002 两条）；第二次含 N1 共三条
    assert changes[1]["affected_swipes"] == 2
    assert changes[0]["affected_swipes"] == 3


def test_idempotent_offset_is_noop(client):
    """相同偏移重复提交：不写审计、不动统计。"""
    _setup(client)
    client.put("/api/terminals/TM-A/offset", json={"offset_seconds": -1800})
    stats_before = stats_map(client)
    r = client.put("/api/terminals/TM-A/offset", json={"offset_seconds": -1800})
    assert r.status_code == 200
    assert r.json()["changed"] is False and r.json()["batch_id"] is None
    assert len(client.get("/api/terminals/offset-changes").json()) == 1
    assert stats_map(client) == stats_before
    ov = client.get("/api/overview").json()
    assert ov["terminal_offset_changes"] == 1


def test_retry_dedup_uses_raw_content_including_terminal(client):
    """同号重传按原始内容判重：偏移变化不误报，terminal_id 不同则算冲突。"""
    _setup(client)
    client.put("/api/terminals/TM-A/offset", json={"offset_seconds": -1800})
    # 同内容重传（此时偏移已变）：仍判为重复跳过，不报冲突
    r = upload(
        client,
        "/api/imports/swipes",
        "retry.csv",
        (SAMPLES / "swipes_5_offset_retry.csv").read_bytes(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == 1 and r.json()["skipped_duplicates"] == 1
    # 同号但 terminal_id 不同：409 整批拒收
    conflict = (
        b"txn_id,swipe_time_utc,line,temp_station_id,terminal_id\n"
        b"C001,2026-01-01T00:20:00Z,L1,T100,TM-OTHER\n"
    )
    r = upload(client, "/api/imports/swipes", "conflict.csv", conflict)
    assert r.status_code == 409
    # 样例 6 条 + 重传文件新增的 C007 一条 = 7；冲突批次整批回滚不新增
    assert client.get("/api/overview").json()["swipes"] == 7


def test_offset_validation_and_404(client):
    """偏移越界 422；未设置偏移的终端查询 404。"""
    r = client.put("/api/terminals/TM-A/offset", json={"offset_seconds": 999999999})
    assert r.status_code == 422
    r = client.put("/api/terminals/TM-A/offset", json={"offset_seconds": "30x"})
    assert r.status_code == 422
    assert client.get("/api/terminals/TM-A/offset").status_code == 404
    r = client.put("/api/terminals/  /offset", json={"offset_seconds": -1})
    assert r.status_code == 422
    # 失败不留痕
    assert client.get("/api/overview").json()["terminals_with_offset"] == 0


def test_transaction_detail_404(client):
    assert client.get("/api/transactions/NOPE").status_code == 404


def test_offsets_listing_and_audit(client):
    _setup(client)
    client.put("/api/terminals/TM-A/offset", json={"offset_seconds": -1800, "note": "拨慢半小时"})
    client.put("/api/terminals/TM-B/offset", json={"offset_seconds": 60})
    rows = client.get("/api/terminals/offsets").json()
    assert {r["terminal_id"]: r["offset_seconds"] for r in rows} == {"TM-A": -1800, "TM-B": 60}
    one = client.get("/api/terminals/TM-A/offset").json()
    assert one["offset_seconds"] == -1800 and "updated_at" in one
    audit = client.get("/api/terminals/offset-changes").json()
    assert len(audit) == 2
    assert audit[0]["old_offset_seconds"] is None and audit[0]["affected_swipes"] >= 1


def test_mapping_late_correction_uses_corrected_time(client):
    """映射晚到重算也以校正时刻为准。"""
    _setup(client)
    client.put("/api/terminals/TM-A/offset", json={"offset_seconds": -1800})
    # C001 校正到 2025-12-31T23:50，当前无 2025 年映射 -> 未归属
    assert client.get("/api/transactions/C001").json()["real_station"] is None
    m = (
        b"line,temp_station_id,valid_from_utc,valid_to_utc,real_station\n"
        b"L1,T100,2025-12-01T00:00:00Z,2026-01-01T00:00:00Z,Old Year Station\n"
    )
    assert upload(client, "/api/imports/mappings", "late.csv", m).status_code == 200
    detail = client.get("/api/transactions/C001").json()
    assert detail["real_station"] == "Old Year Station"
    assert stats_map(client, "2025-12") == {("2025-12", "Old Year Station"): 1}


def test_full_story_samples_fixture_files_present(client):
    """样例文件本身可走通：校时样例 + 重传样例。"""
    _setup(client)
    r = upload(
        client,
        "/api/imports/swipes",
        "retry.csv",
        (SAMPLES / "swipes_5_offset_retry.csv").read_bytes(),
    )
    assert r.status_code == 200 and r.json()["applied"] == 1
