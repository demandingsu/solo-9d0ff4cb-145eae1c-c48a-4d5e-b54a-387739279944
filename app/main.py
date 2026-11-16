"""FastAPI 入口：导入、校时、查询接口。

所有接口均为同步实现（Starlette 线程池执行），写路径由
app.db.write_txn 全局串行化，读路径走 WAL 快照，互不一致性问题。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, service
from .csvio import CsvError, parse_mappings, parse_swipes
from .service import MAX_OFFSET_SECONDS, ConflictError

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"

DESCRIPTION = """
把刷卡流水里的**临时站号**归属到**真实站点**，供月度客流分析；
并支持按终端校正设备时钟偏差。

## 两类 CSV

* **刷卡流水**：`txn_id, swipe_time_utc, line, temp_station_id[, terminal_id]`
  （流水号、原始 UTC 时间、线路、临时站号，可选终端号）
* **站号映射**：`line, temp_station_id, valid_from_utc, valid_to_utc, real_station`
  （线路、临时站号、生效起止 UTC、真实站点）

## 终端校时

* 设备时钟偏快/偏慢时，按终端设置**有符号秒偏移**：
  校正时刻 = 原始时刻 + 偏移（时钟偏快用负值拨慢，偏慢用正值拨快）；
* 原始 UTC 时刻**原样保留**，归站与月度客流一律按**校正时刻**计算；
* 偏移更正会在同一事务内重算该终端全部历史流水，
  **跨映射时段、跨 UTC 月份**的归属与统计同步更新；未标终端的旧数据不动；
* 校时、归站重算、月度统计**同一事务提交**，失败不留部分更改。

## 其余规则

* 同一临时站号可被不同线路复用 —— 映射按 **(线路, 临时站号)** 区分；
* 映射按 (线路, 临时站号) **整组替换**；组内时段重叠则 **整批拒收**（409）；
* 流水按流水号幂等：按**原始内容（含 terminal_id）**比对，
  **同号同内容**重传不重复计数（偏移变化不会误报冲突），
  **同号不同内容**整批拒收（409）；
* 映射晚到会**重算旧流水**；查询只见一致的已提交状态；
* 导入失败（校验/冲突）**不改变已有结果**。

## 快速体验

`/samples` 下有成组样例文件，按 README 的顺序在下方接口里上传/调用即可
演示「终端校时跨月跨时段重算」「迟到改站重算」「幂等重传」「冲突拒收」。
"""


class ImportResult(BaseModel):
    batch_id: str
    file_type: str
    filename: str
    rows_received: int
    applied: int
    skipped_duplicates: int
    note: str | None = None


class MonthlyStat(BaseModel):
    month: str
    real_station: str
    boardings: int


class UnattributedItem(BaseModel):
    txn_id: str
    swipe_time_utc: str
    corrected_time_utc: str
    line: str
    temp_station_id: str
    terminal_id: str | None
    reason: str


class UnattributedPage(BaseModel):
    total: int
    items: list[UnattributedItem]


class TransactionDetail(BaseModel):
    txn_id: str
    swipe_time_utc: str
    corrected_time_utc: str
    line: str
    temp_station_id: str
    terminal_id: str | None
    offset_seconds: int | None
    real_station: str | None


class MappingItem(BaseModel):
    line: str
    temp_station_id: str
    valid_from_utc: str
    valid_to_utc: str
    real_station: str
    batch_id: str


class BatchItem(BaseModel):
    batch_id: str
    file_type: str
    filename: str
    rows_received: int
    rows_applied: int
    rows_skipped: int
    note: str | None
    created_at: str


class TerminalOffsetItem(BaseModel):
    terminal_id: str
    offset_seconds: int
    updated_at: str


class OffsetChangeItem(BaseModel):
    batch_id: str
    terminal_id: str
    old_offset_seconds: int | None
    new_offset_seconds: int
    affected_swipes: int
    note: str | None
    created_at: str


class OffsetApply(BaseModel):
    offset_seconds: int = Field(
        ...,
        ge=-MAX_OFFSET_SECONDS,
        le=MAX_OFFSET_SECONDS,
        description="有符号秒偏移；校正时刻 = 原始时刻 + 偏移。"
        "设备时钟偏快传负值，偏慢传正值。范围 ±366 天。",
        examples=[-3600],
    )
    note: str | None = Field(default=None, description="备注（可选）")


class OffsetApplyResult(BaseModel):
    terminal_id: str
    old_offset_seconds: int | None
    offset_seconds: int
    changed: bool
    affected_swipes: int
    batch_id: str | None
    note: str | None


class Overview(BaseModel):
    swipes: int
    mapping_intervals: int
    mapping_groups: int
    import_batches: int
    monthly_stat_rows: int
    attributed_boardings: int
    terminals_with_offset: int
    terminal_offset_changes: int
    swipes_without_terminal: int


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="公交刷卡流水归站与终端校时 API",
    description=DESCRIPTION,
    version="1.1.0",
    lifespan=lifespan,
)

if SAMPLES_DIR.is_dir():
    app.mount("/samples", StaticFiles(directory=str(SAMPLES_DIR)), name="samples")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


# ---------------------------------------------------------------- 导入


def _read_upload(file: UploadFile) -> bytes:
    data = file.file.read()
    if not data:
        raise HTTPException(status_code=422, detail="上传文件为空")
    return data


@app.post(
    "/api/imports/swipes",
    response_model=ImportResult,
    tags=["导入"],
    responses={
        409: {"description": "同号不同内容，整批拒收（已有数据不变）"},
        422: {"description": "CSV 结构或内容非法，整批拒收"},
    },
)
def import_swipe_file(
    file: UploadFile = File(
        ...,
        description=(
            "刷卡流水 CSV：txn_id, swipe_time_utc, line, temp_station_id"
            "[, terminal_id]；terminal_id 为可选列，缺列/空值表示未标终端"
        ),
    ),
):
    """导入刷卡流水 CSV。

    - 表头含 **terminal_id** 时逐行记录终端号（空值视为未标终端）；
      不含该列的旧格式文件也可正常导入；
    - 流水号已存在且**原始内容完全相同（含 terminal_id）**：跳过，不重复计数
      （可安全重传，判重不受校时偏移影响）；
    - 流水号已存在但**内容不同**：整批拒收，返回 409 与冲突明细；
    - 新流水按该终端当前偏移计算校正时刻；导入成功后同事务重算月度统计。
    """
    try:
        rows = parse_swipes(_read_upload(file))
    except CsvError as e:
        raise HTTPException(
            status_code=422,
            detail={"message": "CSV 校验失败，整批拒收", "errors": e.errors},
        )
    try:
        return service.import_swipes(rows, file.filename or "upload.csv")
    except ConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "同号不同内容，整批拒收；已有数据未改变",
                "conflicts": e.errors,
            },
        )


@app.post(
    "/api/imports/mappings",
    response_model=ImportResult,
    tags=["导入"],
    responses={
        409: {"description": "组内时段重叠，整批拒收（已有数据不变）"},
        422: {"description": "CSV 结构或内容非法，整批拒收"},
    },
)
def import_mapping_file(
    file: UploadFile = File(..., description="站号映射 CSV：line, temp_station_id, valid_from_utc, valid_to_utc, real_station"),
):
    """导入站号映射 CSV。

    - 按 (线路, 临时站号) **整组替换**：文件里出现的组整体覆盖旧记录，
      未出现的组不受影响；
    - 组内生效时段重叠：整批拒收，返回 409 与重叠明细；
    - 映射晚到时，旧流水按校正时刻在新统计中自动重新归属。
    """
    try:
        rows = parse_mappings(_read_upload(file))
    except CsvError as e:
        raise HTTPException(
            status_code=422,
            detail={"message": "CSV 校验失败，整批拒收", "errors": e.errors},
        )
    try:
        return service.import_mappings(rows, file.filename or "upload.csv")
    except ConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "组内时段重叠，整批拒收；已有数据未改变",
                "conflicts": e.errors,
            },
        )


# ---------------------------------------------------------------- 终端校时


@app.put(
    "/api/terminals/{terminal_id}/offset",
    response_model=OffsetApplyResult,
    tags=["终端校时"],
    responses={422: {"description": "参数非法（空终端号 / 偏移超出范围）"}},
)
def apply_terminal_offset(terminal_id: str, body: OffsetApply):
    """按终端设置有符号秒偏移并校时。

    - 校正时刻 = 原始 UTC 时刻 + offset_seconds（时钟偏快传负值，偏慢传正值）；
    - 同事务重算该终端**全部历史流水**的校正时刻，跨映射时段、跨月份的
      归站与月度统计一并更新；未标终端的旧数据不受影响；
    - 偏移与当前值相同则为幂等空操作（changed=false，不写审计、不动统计）；
    - 校时、重算、统计同一事务提交，任何失败整体回滚，不留部分更改。
    """
    try:
        return service.set_terminal_offset(
            terminal_id, body.offset_seconds, body.note
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get(
    "/api/terminals/offsets",
    response_model=list[TerminalOffsetItem],
    tags=["终端校时"],
)
def list_terminal_offsets():
    """所有终端当前生效的有符号秒偏移。"""
    return service.list_terminal_offsets()


@app.get(
    "/api/terminals/{terminal_id}/offset",
    response_model=TerminalOffsetItem,
    tags=["终端校时"],
    responses={404: {"description": "该终端尚未设置偏移"}},
)
def get_terminal_offset(terminal_id: str):
    """查询单个终端的当前偏移。"""
    row = service.get_terminal_offset(terminal_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"终端 {terminal_id} 尚未设置偏移"
        )
    return row


@app.get(
    "/api/terminals/offset-changes",
    response_model=list[OffsetChangeItem],
    tags=["终端校时"],
)
def list_offset_changes(
    terminal_id: str | None = Query(default=None, description="按终端号过滤审计"),
):
    """终端校时审计：每次偏移更正一行（可按终端过滤）。"""
    return service.list_offset_changes(terminal_id)


# ---------------------------------------------------------------- 查询


@app.get("/api/stats/monthly", response_model=list[MonthlyStat], tags=["查询"])
def get_monthly_stats(
    month: str | None = Query(
        default=None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description="按校正时刻的 UTC 月份过滤，如 2026-01；缺省返回全部月份",
        examples=["2026-01"],
    ),
):
    """真实站点的 UTC 月上车量（按校正时刻归属与归月）。"""
    return service.monthly_stats(month)


@app.get(
    "/api/transactions/unattributed",
    response_model=UnattributedPage,
    tags=["查询"],
)
def get_unattributed(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """未归属流水：校正时刻没有命中任何映射时段，附原因说明。"""
    return service.unattributed(limit, offset)


@app.get(
    "/api/transactions/{txn_id}",
    response_model=TransactionDetail,
    tags=["查询"],
    responses={404: {"description": "流水号不存在"}},
)
def get_transaction(txn_id: str):
    """单笔流水核对：原始 UTC 时刻、终端偏移、校正时刻、归站结果一次给出。"""
    row = service.transaction_detail(txn_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"流水号 {txn_id} 不存在")
    return row


@app.get("/api/mappings", response_model=list[MappingItem], tags=["查询"])
def get_mappings(
    line: str | None = Query(default=None, description="按线路过滤"),
    temp_station_id: str | None = Query(default=None, description="按临时站号过滤"),
):
    """当前生效的站号映射（整组替换后的最新状态）。"""
    return service.list_mappings(line, temp_station_id)


@app.get("/api/imports", response_model=list[BatchItem], tags=["查询"])
def get_import_batches():
    """已成功提交的导入批次审计（失败的导入不留痕、不改数据）。"""
    return service.list_batches()


@app.get("/api/overview", response_model=Overview, tags=["查询"])
def get_overview():
    """库内数据总览。"""
    return service.overview()
