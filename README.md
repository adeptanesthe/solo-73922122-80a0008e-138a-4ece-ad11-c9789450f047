# 野外调查观测更正汇交 API

多台离线设备对同一观测的更正在此汇交，供生态分析取数。FastAPI + SQLite，
两个 API 进程共库运行，任一端写入、两端查询结论一致，重启保留。

## 数据模型与状态机

每条更正是观测修订 DAG 中的一个节点：

| 字段 | 含义 |
|---|---|
| `observation_id` | 观测号 |
| `revision_id` | 全局唯一修订号 |
| `parents` | 直接父修订号列表（空数组 = 根修订） |
| `species` + `count` | 改值：物种与数量 |
| `delete: true` | 删除标记（与物种/数量互斥） |

每个观测由已到达的修订自动重算出三种状态：

| 状态 | 含义 | 有效值 |
|---|---|---|
| `pending` | 祖先未齐（有父修订尚未到达，允许子先父后） | 不输出 |
| `valid` | 祖先齐全且只剩一个无后继修订（当前分支唯一） | 顶端修订的载荷（删除也是合法结论） |
| `conflict` | 祖先齐全但有多个无后继修订（多分支；改值与删除并存同属此类） | 不输出 |

- **乱序到达**：子先父后时观测标 `pending` 并列出 `missing_ancestors`，父补齐后自动重算。
- **人工裁决**：`POST /observations/{id}/resolutions`，以裁决时所见全部分支（`tips`）为父新建修订；
  提交时分支已变则返回 **409** 及当前分支；裁决之后迟到的并发分支会重新形成冲突，不会被吞没。
- **幂等**：同号同内容重传原样接受（记入 `duplicates`，状态不变）；同号异内容整包拒绝。
- **整包拒绝（422）不留部分记录**：同号异内容、循环引用（含自引用）、跨观测引用
  （含父修订后到但观测号不符的反向情形）。
- **原子性**：一个包可含多条修订、多个观测，任一非法则整体回滚。
- **观测变更流**：`GET /changes`，自启用起每次结论变化都在同一事务中按提交次序
  记录完整快照，供生态分析库持续同步、撤回失效值（见下文）。

## 观测变更流（GET /changes）

生态分析库据此持续同步野外观测：离线补传改变有效值、并发分支撤回旧值时，
都能从流中领到变更。

- **记录时机**：自启用起，上报或裁决使观测的**状态、当前分支、未齐祖先或有效值**
  发生变化时，在**同一 SQLite 事务**中按**提交次序**追加一条记录，内容为当时的
  **完整结论**（`status` / `tips` / `missing_ancestors` / `effective`，含删除标记
  `deleted`）。被拒绝的请求（422/409/404）与纯重传（同号同内容）不产生记录。
- **不可变**：记录一旦落库不再修改；翻页、双进程交错写入或重启后，
  旧记录仍保留原结论。有效值被取代、转入冲突/待补齐或被删除时，
  分析库可对照新记录撤回已同步的失效值。
- **领取方式**：`GET /changes?since=<游标>&limit=<页幅>`（`since` 默认 0，
  `limit` 默认 100、最大 1000），返回 `seq` 严格大于 `since` 的记录，按 `seq` 升序。
  响应中的 `next_cursor` 等于本页最后一条记录的 `seq`（空页保持原游标），
  作为下一次的 `since` 即可——下一游标只越过已返回的记录，**不重读、不漏读**。
- **次序保证**：写事务一律 `BEGIN IMMEDIATE` 串行提交，`seq` 的分配次序即提交次序；
  两个共库进程交错写入时，任一快照看到 `seq=N` 必已包含 `seq<N` 的全部记录。

```bash
curl 'http://localhost:8001/changes?since=0&limit=100'
# → {"changes": [{"seq": 1, "observation_id": "OBS-1", "status": "valid",
#                  "tips": ["r2"], "missing_ancestors": [],
#                  "effective": {"revision_id": "r2", "species": "Cervus elaphus",
#                                "count": 3, "deleted": false},
#                  "version": 2, "recorded_at": "..."}, ...],
#     "next_cursor": 1}
```

## 运行（Docker Compose）

```bash
docker compose up --build
```

- api-a: http://localhost:8001/docs （Swagger UI，可上报、查冲突、裁决、领取变更）
- api-b: http://localhost:8002/docs （与 api-a 共库，结论一致）
- 数据存于命名卷 `survey-data`（SQLite 文件），`docker compose down` 后重启保留；
  `docker compose down -v` 才会清空。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/packages` | 上报 JSON 包：`{"revisions": [...]}`，整体原子 |
| `GET` | `/observations` | 列出全部观测状态，可 `?status=pending\|valid\|conflict` 过滤 |
| `GET` | `/observations/{id}` | 单个观测：状态、当前分支、未齐祖先、有效值 |
| `GET` | `/observations/{id}/revisions` | 观测的全部修订（DAG 节点） |
| `GET` | `/conflicts` | 查冲突：全部多分支观测及其当前分支 |
| `POST` | `/observations/{id}/resolutions` | 人工裁决（201；幂等重放 200；分支已变 409） |
| `GET` | `/changes` | 观测变更流：`?since=<游标>&limit=<页幅>` 按提交次序限量领取 |
| `GET` | `/health` | 健康检查 |

### 上报示例

```bash
curl -X POST http://localhost:8001/packages -H 'Content-Type: application/json' -d '{
  "revisions": [
    {"observation_id": "OBS-1", "revision_id": "r1", "parents": [],
     "species": "Cervus elaphus", "count": 3},
    {"observation_id": "OBS-1", "revision_id": "r2", "parents": ["r1"],
     "delete": true}
  ]
}'
```

### 裁决示例

```bash
curl -X POST http://localhost:8001/observations/OBS-1/resolutions \
  -H 'Content-Type: application/json' -d '{
    "revision_id": "r3", "parents": ["r2", "rX"],   # 必须等于当前 tips，否则 409
    "species": "Cervus elaphus", "count": 5
  }'
```

## 样例脚本

启动 Compose 后直接执行（默认打 8001/8002，可用 `API_A`/`API_B` 覆盖）：

```bash
bash samples/01_out_of_order.sh              # 乱序：子先父后 → 待补齐 → 自动重算
bash samples/02_edit_delete_conflict.sh      # 改删并发 → 冲突 → 裁决 → 迟到分支 → 409
bash samples/03_idempotency_and_rejection.sh # 幂等重传；异内容/循环/跨观测整包拒绝
bash samples/04_change_stream.sh             # 变更流：补传改值/并发撤回 → 游标翻页领取
```

## 一致性与并发设计

- 全部状态（修订、父子边、观测结论、变更流）都在同一个 SQLite 库文件中，两进程共享；
  重算与变更记录都在写入事务内同步完成，因此两端查询的分支、结论与变更流必然一致。
- SQLite **WAL** 模式：读写互不阻塞；写事务一律 `BEGIN IMMEDIATE`，
  跨进程串行化写入，`busy_timeout` + 重试消除瞬时锁冲突；
  变更序号 `seq` 在写锁内分配，分配次序即提交次序，游标翻页不重读、不漏读。
- 库文件在命名卷上，进程重启、容器重建均保留。

## 本地开发与测试

```bash
pip install -r requirements.txt
pip install pytest httpx
python -m pytest tests/ -q          # 27 个行为测试
DB_PATH=./data/dev.db uvicorn app.main:app --port 8000
```
