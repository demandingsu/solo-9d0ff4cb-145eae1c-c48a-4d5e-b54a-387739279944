# 公交刷卡流水归站与终端校时 API

把刷卡流水里的**临时站号**归属到**真实站点**，供月度客流分析；并支持按终端
**校正设备时钟偏差**（时钟偏快导致流水落入错误的 UTC 月份或站点时段）。

- FastAPI + SQLite（WAL），单容器即可运行；
- 刷卡 CSV 新增**可选 `terminal_id`** 列；按终端设置**有符号秒偏移**，
  校正时刻 = 原始时刻 + 偏移（偏快传负、偏慢传正）；
- **原始 UTC 时间原样保留**，归站与月度客流一律按**校正时刻**计算；
- 偏移更正会在同一事务内**重算该终端全部历史流水**，跨映射时段、跨 UTC
  月份的归属与统计同步更新；未标终端的旧数据保持原样；
- 同号重传按**原始内容（含 terminal_id）**判重，偏移变化不会误报冲突；
- 校时、归站重算、月度统计在**同一事务提交**，失败回滚不留部分更改；
- 同一临时站号可被不同线路复用，映射按 **(线路, 临时站号)** 整组替换；
- 映射、流水、月度统计在同一事务提交，查询永远只见一致的已提交状态。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

服务端口：`8000`。SQLite 数据落在命名卷 `transit_data`（容器内 `/data/transit.db`）。

## CSV 格式

**刷卡流水**（`POST /api/imports/swipes`）

```csv
txn_id,swipe_time_utc,line,temp_station_id,terminal_id
S001,2026-01-05T08:15:00Z,L1,T100,TM-A
```

- `terminal_id` 为**可选列**：表头不含该列（旧格式文件）或单元格留空，
  都视为「未标终端」，其校正时刻恒等于原始时刻、不参与任何校时；
- 同一终端的流水使用相同 `terminal_id`，校时按该终端当前偏移计算校正时刻。

**站号映射**（`POST /api/imports/mappings`）

```csv
line,temp_station_id,valid_from_utc,valid_to_utc,real_station
L1,T100,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z,Central Station
```

- 时间为 ISO 8601；无显式时区时按 UTC 处理，带偏移（如 `+08:00`）会归一到 UTC；
- 映射生效时段为**左闭右开** `[valid_from_utc, valid_to_utc)`；
- 文件需 UTF-8 编码（容忍 BOM），表头列名必须与上表一致。

## 终端校时

```bash
# 设备 TM-A 时钟偏快 30 分钟 -> 负偏移把时刻拨慢 1800 秒
curl -X PUT http://localhost:8000/api/terminals/TM-A/offset \
  -H 'Content-Type: application/json' \
  -d '{"offset_seconds": -1800, "note": "现场核对发现偏快30分钟"}'
```

- 校正时刻 = 原始 UTC 时刻 + `offset_seconds`（有符号整数秒，范围 ±366 天）；
- 提交后同事务重算该终端**全部历史流水**的校正时刻，归站匹配与 UTC 归月
  全部基于校正时刻刷新 —— 跨映射时段（换真实站点）、跨月份（换统计月）都会更新；
- 原始 `swipe_time_utc` 永不修改；`GET /api/transactions/{txn_id}` 可同时核对
  原始时刻、终端偏移、校正时刻与归站结果；
- 偏移与当前值相同为幂等空操作（`changed=false`，不写审计、不动统计）；
- 每次实际更正写入 `terminal_offset_changes` 审计（旧值/新值/影响流水数）。

## 业务规则

| 规则 | 行为 |
| --- | --- |
| 终端时钟偏差 | 按终端设有符号秒偏移，校正时刻 = 原始 + 偏移；原始 UTC 保留 |
| 偏移更正 | 同事务重算该终端全部历史流水，跨时段/跨月份归站与统计同步更新 |
| 未标终端旧数据 | `terminal_id` 为空，校正时刻恒等于原始时刻，任何校时都不影响 |
| 流水号重复 + 原始内容相同（含 terminal_id） | 跳过，不重复计数（安全重传，与偏移无关） |
| 流水号重复 + 内容不同 | **整批拒收**，HTTP 409，已有流水不变 |
| 临时站号复用 | 映射键是 (线路, 临时站号)，不同线路互不影响 |
| 映射导入 | 按 (线路, 临时站号) **整组替换**；文件未涉及的组不受影响 |
| 组内时段重叠 | **整批拒收**，HTTP 409，已有映射不变 |
| 映射晚到 | 旧流水按校正时刻在新统计中自动重新归属 |
| 统计口径 | 按**校正时刻**落入的映射时段归属真实站点，按校正时刻的 UTC 月份汇总 |
| 校时/导入一致性 | 校时、归站、统计同一事务提交；失败整体回滚，不留部分更改 |
| 并发写入 | 写事务串行；查询走 WAL 快照，只见同一已提交状态 |

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/imports/swipes` | 导入刷卡流水 CSV（terminal_id 可选） |
| POST | `/api/imports/mappings` | 导入站号映射 CSV |
| PUT | `/api/terminals/{terminal_id}/offset` | 按终端设置有符号秒偏移并同事务校时重算 |
| GET | `/api/terminals/offsets` | 所有终端当前偏移 |
| GET | `/api/terminals/{terminal_id}/offset` | 单个终端当前偏移（未设置返回 404） |
| GET | `/api/terminals/offset-changes` | 校时审计（可按 terminal_id 过滤） |
| GET | `/api/transactions/{txn_id}` | 单笔流水核对：原始/校正时刻、偏移、归站 |
| GET | `/api/stats/monthly?month=YYYY-MM` | 真实站点 UTC 月上车量（按校正时刻，month 可省） |
| GET | `/api/transactions/unattributed` | 未归属流水（含原因、分页） |
| GET | `/api/mappings` | 当前生效映射（可按线路/站号过滤） |
| GET | `/api/imports` | 已提交导入批次审计 |
| GET | `/api/overview` | 数据总览 |
| GET | `/samples/{文件名}` | 下载内置样例 CSV |

## 在 /docs 里演示

### A. 终端校时（跨月、跨时段重算）

样例 `swipes_4_clock_skew.csv` 含 6 条流水（C001–C006，部分带终端号、
C005/C006 未标终端），配合 `mappings_1_initial.csv`：

1. 先 `POST /api/imports/mappings` 上传 `mappings_1_initial.csv`，
   再 `POST /api/imports/swipes` 上传 `swipes_4_clock_skew.csv`；
2. `PUT /api/terminals/TM-A/offset`，body `{"offset_seconds": -1800}`：
   C001（原始 2026-01-01T00:20）校正到 **2025-12-31T23:50**，跨出 1 月映射，
   变成未归属；C002 从 2 月 North Gate 跨时段改挂 1 月 Central Station；
3. `PUT /api/terminals/TM-B/offset`，body `{"offset_seconds": -3600}`：
   C003 从 3 月跨月回到 2 月并换站；C004 回到映射左闭边界内仍归 Museum；
4. `GET /api/transactions/C001` 同时看到原始时刻、偏移与校正时刻；
   C005/C006 未标终端，校正时刻始终等于原始时刻；
5. 再次上传 `swipes_5_offset_retry.csv`：C001 同号同原始内容 →
   `skipped_duplicates=1`，偏移已变也不误报冲突。

### B. 迟到改站、幂等与冲突（原有故事线）

1. **导流水**：上传 `swipes_1.csv`（10 条），此时全部未归属；
2. **导映射**：上传 `mappings_1_initial.csv`，未归属剩 3 条；
3. **迟到改站**：上传 `mappings_2_late_correction.csv`，S007 被重新归属；
4. **幂等重传**：上传 `swipes_2_more.csv` → `applied=2, skipped_duplicates=1`；
5. **冲突拒收**：上传 `swipes_3_conflict.csv` → HTTP 409 整批拒收；
6. **重叠拒收**：上传 `mappings_3_overlap.csv` → HTTP 409 整批拒收。

命令行等价演示：`docker compose up -d` 后执行 `bash scripts/demo.sh`。

## 一致性设计

- **单写事务**：所有导入与校时经全局写锁串行化，`BEGIN IMMEDIATE` 开启事务；
  终端偏移更新、历史流水校正时刻重算、月度统计全量重算在**同一事务**内完成并提交；
- **快照读**：SQLite WAL 模式，查询连接读到的是某一已提交快照，
  不会看到校时/导入进行中的中间态；
- **失败即回滚**：校验失败（422）在写事务之前返回；业务冲突（409）或事务内
  任何异常都触发回滚 —— 偏移、流水校正时刻、统计、审计要么全部生效，要么全部不变；
- **幂等键**：流水以 `txn_id` 为主键，按原始字段（含 terminal_id）逐字段比对，
  同内容跳过、异内容拒收；判重与校正时刻解耦；
- **旧库升级**：启动时自动幂等迁移（补 `terminal_id`/`corrected_time_utc`
  列并以原始时刻回填、新建终端偏移与审计表），旧数据行为不变。

## 本地开发与测试

```bash
pip install -r requirements.txt
pip install pytest httpx
TRANSIT_DB_PATH=/tmp/transit.db uvicorn app.main:app --reload
python -m pytest tests/ -v
```

## 目录结构

```
app/
  main.py     # FastAPI 入口与接口定义（导入 / 终端校时 / 查询）
  service.py  # 导入/校时/重算/查询核心业务逻辑
  csvio.py    # CSV 解析（terminal_id 可选列）与 UTC 时间规范化
  db.py       # SQLite 连接、schema、旧库迁移、写事务（WAL + 串行化）
samples/      # 成组样例 CSV（/samples 可下载），含校时场景
scripts/demo.sh
tests/        # 原故事线 + 校时专题 + 迁移/回滚持久化测试
Dockerfile / docker-compose.yml
```
