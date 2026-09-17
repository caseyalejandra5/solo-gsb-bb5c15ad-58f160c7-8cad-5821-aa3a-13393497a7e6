# CIDR 地址池分配服务（cidr-pool-api）

基于 **FastAPI + SQLite** 的本地 CIDR 地址池分配 API，支持 IPv4 与 IPv6。

## 功能特性

- 创建 IPv4 / IPv6 地址池，并登记池内不可用子网
- 严格校验：拒绝**越界**、**地址族（版本）不符**及**任意重叠**（池与池、不可用段、已分配段之间）
- 按前缀长度申请：自动从当前空闲空间选择**网络地址最小的对齐子网**
- 批量申请：同一事务内全部成功，否则整体回滚、不产生任何分配
- 两阶段分配计划：先创建计划（预选子网、10 分钟有效期、不占用地址），确认无误后再一次性提交落库
- 池快照与差异比较：为任意时刻的 allocations / unavailable 建立**不可变快照**，随时比较当前状态，输出新增 / 移除的 CIDR 及已分配、剩余地址数的有符号增量
- 释放后相邻空闲段自动重新合并
- 并发安全：写操作经 `BEGIN IMMEDIATE` 事务串行化，并发申请不会拿到重叠网段
- 地址池状态接口：列出已分配项、不可用段，以及按**最小 CIDR 集合**归并的剩余空间

## 快速启动（一条命令）

```bash
docker compose up --build
```

服务监听 **8000** 端口：

- API 地址：http://localhost:8000
- 交互式文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：`GET /healthz`

数据持久化在 named volume `cidr-data`（容器内 `/data/cidr.db`），容器重建后数据保留。

## 调用示例

```bash
# 1. 创建 IPv4 地址池
curl -s -X POST http://localhost:8000/pools \
  -H 'Content-Type: application/json' \
  -d '{"name": "eng", "cidr": "10.0.0.0/24"}'

# 2. 登记不可用子网（越界 / 版本不符 / 重叠会被拒绝）
curl -s -X POST http://localhost:8000/pools/eng/unavailable \
  -H 'Content-Type: application/json' \
  -d '{"cidr": "10.0.0.0/25"}'

# 3. 申请一个 /26 —— 自动选择最小网络地址的对齐子网，返回 10.0.0.128/26
curl -s -X POST http://localhost:8000/pools/eng/allocations \
  -H 'Content-Type: application/json' \
  -d '{"requests": [{"prefix_len": 26}]}'

# 4. 批量申请（同一事务，要么全部成功，要么全部回滚）
curl -s -X POST http://localhost:8000/pools/eng/allocations \
  -H 'Content-Type: application/json' \
  -d '{"requests": [{"prefix_len": 28, "count": 2}, {"prefix_len": 27}]}'

# 5. 查看地址池状态（已分配 / 不可用 / 最小 CIDR 集合归并的剩余空间）
curl -s http://localhost:8000/pools/eng

# 6. 释放网段（相邻空闲段自动合并）
curl -s -X POST http://localhost:8000/pools/eng/release \
  -H 'Content-Type: application/json' \
  -d '{"cidr": "10.0.0.128/26"}'

# 7. IPv6 地址池
curl -s -X POST http://localhost:8000/pools \
  -H 'Content-Type: application/json' \
  -d '{"name": "v6", "cidr": "2001:db8::/32"}'
curl -s -X POST http://localhost:8000/pools/v6/allocations \
  -H 'Content-Type: application/json' \
  -d '{"requests": [{"prefix_len": 48}]}'

# 8. 两阶段分配：先创建计划（预选子网但不占用地址，10 分钟内有效）
curl -s -X POST http://localhost:8000/pools/eng/plans \
  -H 'Content-Type: application/json' \
  -d '{"requests": [{"prefix_len": 26, "count": 2}]}'
# => {"pool": "eng", "plan_id": "…", "cidrs": ["10.0.0.0/26", "10.0.0.64/26"],
#     "expires_at": "2026-09-17T00:10:00.000Z"}

# 9. 提交计划：仅当计划未过期、未提交且池状态（分配项与不可用段）自创建后
#    未变化时原子落库，否则返回 409；每个计划只能成功提交一次
curl -s -X POST http://localhost:8000/pools/eng/plans/<plan_id>/commit

# 10. 创建池快照（保存此刻按 CIDR 排序的 allocations 与 unavailable，快照不可变）
curl -s -X POST http://localhost:8000/pools/eng/snapshots
# => {"pool": "eng", "snapshot_id": "…", "created_at": "…",
#     "allocations": [...], "unavailable": [...]}

# 11. 比较快照与当前状态：added/removed 为相对快照的变化；
#     同一 CIDR 由分配转为不可用时同时计入 removed 与 added；
#     *_addresses.delta 为有符号增量（当前 − 快照）
curl -s http://localhost:8000/pools/eng/snapshots/<snapshot_id>/diff
```

状态接口返回示例：

```json
{
  "name": "eng",
  "cidr": "10.0.0.0/24",
  "version": 4,
  "prefix_len": 24,
  "total_addresses": 256,
  "unavailable": ["10.0.0.0/25"],
  "allocations": [{"cidr": "10.0.0.128/26", "created_at": "2026-09-17T00:00:00.000Z"}],
  "allocated_addresses": 64,
  "remaining": ["10.0.0.192/26"],
  "remaining_addresses": 64
}
```

快照差异返回示例（快照后新增一个 /26 分配、释放原分配）：

```json
{
  "pool": "eng",
  "snapshot_id": "9f1c…",
  "snapshot_created_at": "2026-09-17T00:00:00.000Z",
  "changes": {
    "allocations": {"added": ["10.0.0.192/26"], "removed": ["10.0.0.128/26"]},
    "unavailable": {"added": [], "removed": []}
  },
  "allocated_addresses": {"snapshot": 64, "current": 64, "delta": 0},
  "remaining_addresses": {"snapshot": 64, "current": 64, "delta": 0}
}
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/pools` | 创建地址池 `{name, cidr}`（重名或与同族已有池重叠返回 409） |
| `GET` | `/pools` | 列出全部地址池及用量摘要 |
| `GET` | `/pools/{name}` | 地址池状态：已分配项、不可用段、最小 CIDR 集合归并的剩余空间 |
| `DELETE` | `/pools/{name}` | 删除地址池（级联删除其不可用段与分配记录） |
| `POST` | `/pools/{name}/unavailable` | 登记不可用子网 `{cidr}` |
| `POST` | `/pools/{name}/allocations` | 申请子网 `{requests: [{prefix_len, count?}]}`，批量原子生效 |
| `POST` | `/pools/{name}/plans` | 创建分配计划（请求体同上）：预选子网并返回 `{plan_id, cidrs, expires_at}`，不占用地址 |
| `POST` | `/pools/{name}/plans/{plan_id}/commit` | 提交计划：未过期、未提交且池状态未变时原子落库，否则 409 |
| `POST` | `/pools/{name}/snapshots` | 创建不可变快照：在一个 `BEGIN IMMEDIATE` 事务内保存按 CIDR 排序的 allocations 与 unavailable，返回 `snapshot_id` |
| `GET` | `/pools/{name}/snapshots/{snapshot_id}/diff` | 在单个读事务中比较快照与当前状态：`changes.allocations.{added,removed}`、`changes.unavailable.{added,removed}` 及已分配 / 剩余地址数的有符号增量 |
| `POST` | `/pools/{name}/release` | 释放已分配网段 `{cidr}` |
| `GET` | `/healthz` | 健康检查 |

## 错误语义

| 状态码 | 场景 |
| --- | --- |
| `400` | CIDR 格式非法、越界、地址族（版本）不符、前缀长度非法 |
| `404` | 地址池不存在、释放的网段不在已分配列表中、计划不存在，或快照不存在（含快照归属其他地址池） |
| `409` | 任意重叠（池 / 不可用段 / 已分配段）、名称冲突、剩余空间不足（批量申请整体回滚）；计划已过期、已提交或创建后池状态已变化 |
| `422` | 请求体结构不合法（字段缺失、类型错误等） |

## 本地开发（不用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 启动服务（默认数据库文件 ./cidr.db，可用 CIDR_DB_PATH 覆盖）
uvicorn app.main:app --reload --port 8000

# 运行测试（含并发不重叠的集成测试）
pytest
```

## 设计说明

- **分配算法**：空闲空间 = 地址池 − 不可用段 − 已分配段，以整数闭区间表示；按区间起点升序找到第一个能容纳指定前缀长度的对齐位置，保证分配结果是"网络地址最小的对齐子网"。
- **事务与并发**：所有写操作在 `BEGIN IMMEDIATE` 事务中执行，SQLite 将并发写串行化；批量申请中任何一步失败都会回滚整个事务，因此要么全部落库，要么不产生任何分配，并发申请也不会得到重叠网段。
- **两阶段计划**：`POST /pools/{name}/plans` 用与直接分配完全相同的选路规则预选子网，把结果（CIDR 列表、10 分钟 `expires_at`、创建时刻的池状态指纹）持久化到 `plans` 表，但不写入 `allocations`，因此计划本身不占用地址，也不影响直接分配。`POST /pools/{name}/plans/{plan_id}/commit` 在同一 `BEGIN IMMEDIATE` 事务内依次校验：计划未提交、未过期、池状态指纹与创建时一致（即分配项与不可用段均未变化），全部满足才按计划批量插入并标记 `committed`；任一不满足返回 409 且不产生任何分配，每个计划只能成功提交一次。计划有效期默认 600 秒，可用环境变量 `CIDR_PLAN_TTL_SECONDS` 调整。
- **释放合并**：空闲空间由占用集合动态计算，状态接口用 `ipaddress.collapse_addresses` 输出最小 CIDR 集合，释放后相邻空闲段自然重新合并，无需额外维护。
- **池快照与差异**：`POST /pools/{name}/snapshots` 在单个 `BEGIN IMMEDIATE` 事务内读取并分别保存按 CIDR（网络地址升序）排序的 allocations 与 unavailable 到 `snapshots` / `snapshot_allocations` / `snapshot_unavailable` 三张表；快照只有创建入口、没有任何修改或删除入口，因此不可变，并经外键 `ON DELETE CASCADE` 归属地址池——删除池时快照级联删除，同名新池不继承旧快照。`GET /pools/{name}/snapshots/{snapshot_id}/diff` 在单个读事务（一次 `BEGIN`，WAL 下读到一致快照）内取快照与当前状态并做集合差：`added` 为当前有、快照无，`removed` 为快照有、当前无；同一 CIDR 由分配转为不可用（或反向）时在两个集合中身份不同，因此会同时记入对应类别的 `removed` 与 `added`。已分配地址数为各网段 `num_addresses` 之和，剩余地址数由"地址池 − 不可用段 − 已分配段"的空闲闭区间求和，二者均同时给出 `snapshot`、`current` 与有符号增量 `delta = current − snapshot`（新增占用时剩余增量为负）。快照只记录状态、不参与分配或计划校验，不改变现有分配与计划语义。
- **存储**：SQLite（WAL 模式），路径由环境变量 `CIDR_DB_PATH` 指定，默认 `./cidr.db`，Docker 中为 `/data/cidr.db`。表结构通过 `CREATE TABLE IF NOT EXISTS` 幂等创建，启动时自动补齐快照相关表，旧数据库无需迁移。
