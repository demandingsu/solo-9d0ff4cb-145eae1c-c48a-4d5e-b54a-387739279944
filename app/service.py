"""导入、校时与查询的核心业务逻辑。

规则实现：
- 流水：按 txn_id 幂等 —— 同号同内容（含 terminal_id）跳过不重复计数，
  同号不同内容整批拒收；
- 终端校时：每个终端保存一个有符号秒偏移，校正时刻 = 原始时刻 + 偏移；
  原始 UTC 时刻永不修改。偏移更正会在同一事务内重算该终端全部历史流水的
  校正时刻，跨映射时段、跨 UTC 月份的归属与统计随之更新；未标终端的旧
  数据（terminal_id 为 NULL）不参与任何校时；
- 映射：按 (线路, 临时站号) 整组替换；组内时段重叠则整批拒收；
- 统计：按**校正时刻**命中映射时段 [from, to) 归属真实站点，按校正时刻的
  UTC 月份汇总；每次导入/校时后在同一事务内全量重算；
- 一致性：映射/流水/校时/统计在同一事务提交，查询只读已提交快照，
  失败即回滚，不留部分更改。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from .csvio import MappingRow, SwipeRow, fmt_utc
from .db import connect, write_txn

# 偏移量保护边界（±1 年）：防止误传毫秒等单位错误把校正时刻推出合理范围
MAX_OFFSET_SECONDS = 366 * 24 * 3600

_UTC_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class ConflictError(Exception):
    """业务冲突（HTTP 409）：整批拒收，数据库保持不变。"""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__(self.errors[0] if self.errors else "conflict")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _corrected_time(raw_time_utc: str, offset_seconds: int) -> str:
    """校正时刻 = 原始 UTC 时刻 + 有符号秒偏移，输出规范 UTC 文本。"""
    dt = datetime.strptime(raw_time_utc, _UTC_TS_FORMAT).replace(
        tzinfo=timezone.utc
    )
    return fmt_utc(dt + timedelta(seconds=offset_seconds))


def _recompute_stats(conn) -> None:
    """全量重算月度真实站点上车量（与导入/校时在同一事务内）。

    匹配条件：同线路 + 同临时站号，且**校正时刻**落在映射生效时段
    [valid_from_utc, valid_to_utc) 内。组内时段已保证不重叠，
    因此一条流水最多命中一个真实站点，不会重复计数。
    """
    conn.execute("DELETE FROM monthly_stats")
    conn.execute(
        """
        INSERT INTO monthly_stats (month, real_station, boardings)
        SELECT substr(s.corrected_time_utc, 1, 7) AS month,
               m.real_station,
               COUNT(*) AS boardings
        FROM swipes s
        JOIN mappings m
          ON m.line = s.line
         AND m.temp_station_id = s.temp_station_id
         AND s.corrected_time_utc >= m.valid_from_utc
         AND s.corrected_time_utc <  m.valid_to_utc
        GROUP BY month, m.real_station
        """
    )


# ---------------------------------------------------------------- 流水导入


def import_swipes(rows: list[SwipeRow], filename: str) -> dict:
    # 1) 文件内部：同号不同内容 -> 整批拒收；同号同内容视为重复行，去重后跳过
    by_id: dict[str, SwipeRow] = {}
    intra: set[str] = set()
    for r in rows:
        prev = by_id.get(r.txn_id)
        if prev is not None and prev != r:
            intra.add(f"流水号 {r.txn_id}: 同一文件内出现不同内容")
        else:
            by_id[r.txn_id] = r
    if intra:
        raise ConflictError(sorted(intra))

    with write_txn() as conn:
        # 当前各终端偏移；未登记终端按 0 处理（校正时刻 == 原始时刻）
        offsets = {
            r["terminal_id"]: r["offset_seconds"]
            for r in conn.execute(
                "SELECT terminal_id, offset_seconds FROM terminal_offsets"
            )
        }
        to_insert: list[SwipeRow] = []
        skipped = 0
        conflicts: list[str] = []
        for r in by_id.values():
            ex = conn.execute(
                "SELECT swipe_time_utc, line, temp_station_id, terminal_id "
                "FROM swipes WHERE txn_id = ?",
                (r.txn_id,),
            ).fetchone()
            if ex is None:
                to_insert.append(r)
            elif (
                ex["swipe_time_utc"],
                ex["line"],
                ex["temp_station_id"],
                ex["terminal_id"],
            ) == (
                r.swipe_time_utc,
                r.line,
                r.temp_station_id,
                r.terminal_id,
            ):
                # 同号同原始内容（含 terminal_id）：幂等跳过，不重复计数；
                # 判重只看原始内容，偏移变化不影响判重结果
                skipped += 1
            else:
                conflicts.append(
                    f"流水号 {r.txn_id}: 已存在不同内容 "
                    f"(库中 {ex['swipe_time_utc']} / {ex['line']} / "
                    f"{ex['temp_station_id']} / 终端 {ex['terminal_id'] or '无'}，"
                    f"本次 {r.swipe_time_utc} / {r.line} / {r.temp_station_id} / "
                    f"终端 {r.terminal_id or '无'})"
                )
        if conflicts:
            # 抛出后事务回滚：整批拒收，已有数据不变
            raise ConflictError(conflicts)

        batch_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO import_batches "
            "(batch_id, file_type, filename, rows_received, rows_applied, rows_skipped, note, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (batch_id, "swipes", filename, len(rows), len(to_insert), skipped, None, _now()),
        )
        conn.executemany(
            "INSERT INTO swipes (txn_id, swipe_time_utc, corrected_time_utc, "
            "line, temp_station_id, terminal_id, batch_id) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (
                    r.txn_id,
                    r.swipe_time_utc,
                    _corrected_time(r.swipe_time_utc, offsets.get(r.terminal_id, 0)),
                    r.line,
                    r.temp_station_id,
                    r.terminal_id,
                    batch_id,
                )
                for r in to_insert
            ],
        )
        _recompute_stats(conn)
        return {
            "batch_id": batch_id,
            "file_type": "swipes",
            "filename": filename,
            "rows_received": len(rows),
            "applied": len(to_insert),
            "skipped_duplicates": skipped,
            "note": None,
        }


# ---------------------------------------------------------------- 映射导入


def import_mappings(rows: list[MappingRow], filename: str) -> dict:
    # 按 (线路, 临时站号) 分组 —— 替换与重叠校验都以组为单位
    groups: dict[tuple[str, str], list[MappingRow]] = {}
    for r in rows:
        groups.setdefault((r.line, r.temp_station_id), []).append(r)

    # 组内时段重叠 -> 整批拒收（纯校验，先失败先返回，不写库）
    overlaps: list[str] = []
    for (line, ts), ivs in groups.items():
        ordered = sorted(ivs, key=lambda i: (i.valid_from_utc, i.valid_to_utc))
        for a, b in zip(ordered, ordered[1:]):
            if b.valid_from_utc < a.valid_to_utc:
                overlaps.append(
                    f"线路 {line} / 临时站号 {ts}: "
                    f"时段 [{a.valid_from_utc}, {a.valid_to_utc}) 与 "
                    f"[{b.valid_from_utc}, {b.valid_to_utc}) 重叠"
                )
    if overlaps:
        raise ConflictError(overlaps)

    with write_txn() as conn:
        batch_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO import_batches "
            "(batch_id, file_type, filename, rows_received, rows_applied, rows_skipped, note, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                batch_id,
                "mappings",
                filename,
                len(rows),
                len(rows),
                0,
                f"整组替换 {len(groups)} 个 (线路, 临时站号) 组",
                _now(),
            ),
        )
        for (line, ts), ivs in groups.items():
            # 整组替换：文件里出现的组整体覆盖旧记录；未出现的组不受影响
            conn.execute(
                "DELETE FROM mappings WHERE line = ? AND temp_station_id = ?",
                (line, ts),
            )
            conn.executemany(
                "INSERT INTO mappings "
                "(line, temp_station_id, valid_from_utc, valid_to_utc, real_station, batch_id) "
                "VALUES (?,?,?,?,?,?)",
                [
                    (line, ts, i.valid_from_utc, i.valid_to_utc, i.real_station, batch_id)
                    for i in ivs
                ],
            )
        _recompute_stats(conn)
        return {
            "batch_id": batch_id,
            "file_type": "mappings",
            "filename": filename,
            "rows_received": len(rows),
            "applied": len(rows),
            "skipped_duplicates": 0,
            "note": f"整组替换 {len(groups)} 个 (线路, 临时站号) 组",
        }


# ---------------------------------------------------------------- 终端校时


def set_terminal_offset(
    terminal_id: str, offset_seconds: int, note: str | None = None
) -> dict:
    """设置终端有符号秒偏移，并在同一事务内重算该终端全部历史流水。

    校正时刻 = 原始时刻 + 偏移。偏移与已登记值相同时为幂等空操作，
    不写审计、不动统计；否则更新偏移、重算校正时刻与月度统计并记审计，
    任一步失败整体回滚。未标终端（terminal_id IS NULL）的旧数据不受影响。
    """
    if not terminal_id or not terminal_id.strip():
        raise ValueError("terminal_id 不能为空")
    if not isinstance(offset_seconds, int) or isinstance(offset_seconds, bool):
        raise ValueError("offset_seconds 必须是整数秒")
    if abs(offset_seconds) > MAX_OFFSET_SECONDS:
        raise ValueError(
            f"offset_seconds 超出允许范围 ±{MAX_OFFSET_SECONDS} 秒（±366 天）"
        )
    terminal_id = terminal_id.strip()

    with write_txn() as conn:
        row = conn.execute(
            "SELECT offset_seconds FROM terminal_offsets WHERE terminal_id = ?",
            (terminal_id,),
        ).fetchone()
        old_offset = None if row is None else row["offset_seconds"]
        affected = conn.execute(
            "SELECT COUNT(*) AS c FROM swipes WHERE terminal_id = ?",
            (terminal_id,),
        ).fetchone()["c"]

        if old_offset == offset_seconds:
            # 幂等空操作：无审计行、无统计重算，已有数据保持原样
            return {
                "terminal_id": terminal_id,
                "old_offset_seconds": old_offset,
                "offset_seconds": offset_seconds,
                "changed": False,
                "affected_swipes": affected,
                "batch_id": None,
                "note": note,
            }

        conn.execute(
            "INSERT INTO terminal_offsets (terminal_id, offset_seconds, updated_at) "
            "VALUES (?,?,?) "
            "ON CONFLICT(terminal_id) DO UPDATE SET "
            "offset_seconds = excluded.offset_seconds, updated_at = excluded.updated_at",
            (terminal_id, offset_seconds, _now()),
        )
        # 重算该终端全部历史流水的校正时刻（跨映射时段、跨月份都随之更新）
        swipe_rows = conn.execute(
            "SELECT txn_id, swipe_time_utc FROM swipes WHERE terminal_id = ?",
            (terminal_id,),
        ).fetchall()
        for sr in swipe_rows:
            conn.execute(
                "UPDATE swipes SET corrected_time_utc = ? WHERE txn_id = ?",
                (_corrected_time(sr["swipe_time_utc"], offset_seconds), sr["txn_id"]),
            )

        batch_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO terminal_offset_changes "
            "(batch_id, terminal_id, old_offset_seconds, new_offset_seconds, "
            "affected_swipes, note, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                batch_id,
                terminal_id,
                old_offset,
                offset_seconds,
                len(swipe_rows),
                note,
                _now(),
            ),
        )
        _recompute_stats(conn)
        return {
            "terminal_id": terminal_id,
            "old_offset_seconds": old_offset,
            "offset_seconds": offset_seconds,
            "changed": True,
            "affected_swipes": len(swipe_rows),
            "batch_id": batch_id,
            "note": note,
        }


def list_terminal_offsets() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT terminal_id, offset_seconds, updated_at "
            "FROM terminal_offsets ORDER BY terminal_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_terminal_offset(terminal_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT terminal_id, offset_seconds, updated_at "
            "FROM terminal_offsets WHERE terminal_id = ?",
            (terminal_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_offset_changes(terminal_id: str | None = None) -> list[dict]:
    conn = connect()
    try:
        if terminal_id:
            rows = conn.execute(
                "SELECT batch_id, terminal_id, old_offset_seconds, "
                "new_offset_seconds, affected_swipes, note, created_at "
                "FROM terminal_offset_changes WHERE terminal_id = ? "
                "ORDER BY created_at DESC, batch_id",
                (terminal_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT batch_id, terminal_id, old_offset_seconds, "
                "new_offset_seconds, affected_swipes, note, created_at "
                "FROM terminal_offset_changes ORDER BY created_at DESC, batch_id"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------- 查询


def monthly_stats(month: str | None = None) -> list[dict]:
    conn = connect()
    try:
        sql = "SELECT month, real_station, boardings FROM monthly_stats"
        args: tuple = ()
        if month:
            sql += " WHERE month = ?"
            args = (month,)
        sql += " ORDER BY month, real_station"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def unattributed(limit: int, offset: int) -> dict:
    """未归属流水：校正时刻没有命中任何映射时段。"""
    not_covered = """
        NOT EXISTS (
            SELECT 1 FROM mappings m
            WHERE m.line = s.line AND m.temp_station_id = s.temp_station_id
              AND s.corrected_time_utc >= m.valid_from_utc
              AND s.corrected_time_utc <  m.valid_to_utc)
    """
    conn = connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM swipes s WHERE {not_covered}"
        ).fetchone()["c"]
        items = conn.execute(
            f"""
            SELECT s.txn_id, s.swipe_time_utc, s.corrected_time_utc,
                   s.line, s.temp_station_id, s.terminal_id,
                   CASE WHEN k.line IS NULL THEN '该线路+临时站号没有任何映射'
                        ELSE '校正时刻不在任何映射生效时段内' END AS reason
            FROM swipes s
            LEFT JOIN (SELECT DISTINCT line, temp_station_id FROM mappings) k
                   ON k.line = s.line AND k.temp_station_id = s.temp_station_id
            WHERE {not_covered}
            ORDER BY s.corrected_time_utc, s.txn_id
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return {"total": total, "items": [dict(r) for r in items]}
    finally:
        conn.close()


def transaction_detail(txn_id: str) -> dict | None:
    """单笔流水核对：原始时刻、终端偏移、校正时刻与归站结果一次给出。"""
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT s.txn_id, s.swipe_time_utc, s.corrected_time_utc,
                   s.line, s.temp_station_id, s.terminal_id,
                   o.offset_seconds, m.real_station
            FROM swipes s
            LEFT JOIN terminal_offsets o ON o.terminal_id = s.terminal_id
            LEFT JOIN mappings m
                   ON m.line = s.line
                  AND m.temp_station_id = s.temp_station_id
                  AND s.corrected_time_utc >= m.valid_from_utc
                  AND s.corrected_time_utc <  m.valid_to_utc
            WHERE s.txn_id = ?
            """,
            (txn_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_mappings(line: str | None = None, temp_station_id: str | None = None) -> list[dict]:
    conn = connect()
    try:
        sql = (
            "SELECT line, temp_station_id, valid_from_utc, valid_to_utc, "
            "real_station, batch_id FROM mappings"
        )
        clauses, args = [], []
        if line:
            clauses.append("line = ?")
            args.append(line)
        if temp_station_id:
            clauses.append("temp_station_id = ?")
            args.append(temp_station_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY line, temp_station_id, valid_from_utc"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def list_batches() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT batch_id, file_type, filename, rows_received, rows_applied, "
            "rows_skipped, note, created_at FROM import_batches "
            "ORDER BY created_at DESC, batch_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def overview() -> dict:
    conn = connect()
    try:
        swipes = conn.execute("SELECT COUNT(*) AS c FROM swipes").fetchone()["c"]
        mappings = conn.execute("SELECT COUNT(*) AS c FROM mappings").fetchone()["c"]
        groups = conn.execute(
            "SELECT COUNT(*) AS c FROM (SELECT DISTINCT line, temp_station_id FROM mappings)"
        ).fetchone()["c"]
        batches = conn.execute("SELECT COUNT(*) AS c FROM import_batches").fetchone()["c"]
        stats_rows = conn.execute("SELECT COUNT(*) AS c FROM monthly_stats").fetchone()["c"]
        boardings = conn.execute(
            "SELECT COALESCE(SUM(boardings), 0) AS s FROM monthly_stats"
        ).fetchone()["s"]
        terminals = conn.execute(
            "SELECT COUNT(*) AS c FROM terminal_offsets"
        ).fetchone()["c"]
        offset_changes = conn.execute(
            "SELECT COUNT(*) AS c FROM terminal_offset_changes"
        ).fetchone()["c"]
        unlabeled = conn.execute(
            "SELECT COUNT(*) AS c FROM swipes WHERE terminal_id IS NULL"
        ).fetchone()["c"]
        return {
            "swipes": swipes,
            "mapping_intervals": mappings,
            "mapping_groups": groups,
            "import_batches": batches,
            "monthly_stat_rows": stats_rows,
            "attributed_boardings": boardings,
            "terminals_with_offset": terminals,
            "terminal_offset_changes": offset_changes,
            "swipes_without_terminal": unlabeled,
        }
    finally:
        conn.close()
