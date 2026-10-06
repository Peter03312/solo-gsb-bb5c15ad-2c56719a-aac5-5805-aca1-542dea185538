# 数据集派生许可判定服务

为研究团队提供「数据集派生许可」的登记与判定能力。基于 **Python 3.11 + FastAPI + SQLite**，单容器即可运行，数据持久化在 Docker Volume 中。

## 判定规则

- **版本登记**：登记一个数据集版本时，可固定引用多个**已经存在**的父版本；父版本引用在创建之后不可修改（服务不提供任何修改/删除父引用的接口，重复登记同一版本号返回 `409`）。父引用构成一个不可变的 DAG。
- **许可变更**：可对任一已登记版本提交许可变更，记录其 **按 UTC 时刻生效** 的允许用途全集（后一次变更整体覆盖前一次，不做用途增量合并）。
- **判定口径**：查询「给定版本 + 用途 + UTC 时刻」时——
  1. 取该版本**自身及全部祖先**（沿固定父引用展开、自动去重，支持菱形继承）；
  2. 对其中每个版本，取**不晚于查询时刻的最近一次**许可变更作为该版本当时的有效许可；
  3. 对各版本的允许用途取**交集**；
  4. **任一版本在该时刻没有有效许可，或其有效许可不包含所申请用途，即拒绝**；
  5. 响应返回：`allowed` 可否使用、`basis` 各版本所依据的变更、`blockers` 所有阻断版本（含原因）、`allowed_uses_intersection` 交集。
- **区间分段判定（排期）**：查询「给定版本 + 用途 + 带时区的起止时刻（开始严格早于结束）」时，服务在**一次一致数据视图**中展开自身及全部祖先，把半开区间 **[start, end)** 切成连续、无重叠的片段：分段点取自闭包内各版本变更的生效时刻，**起点恰逢变更立即采用新许可、终点上的变更不计入**；无有效许可的时段持续拒绝；菱形祖先只算一次；**仅在判定依据或阻断信息变化时分段**，相邻相同片段自动合并。每段返回起止、`allowed`、`basis`、全部 `blockers`（含原因）与用途交集，任一参数非法（未知版本、无时区、区间倒置或为空）都明确报错且不返回部分结果。
- **变更预演（不写入）**：提交变更前可经 `POST /license-changes/preview` 预演其对目标版本及全部后代的影响；同一数据快照下分别计算现状与假设提交后的判定，仅返回允许状态翻转的版本（含前后判定与阻断原因），菱形继承的后代只计一次；查询早于生效时刻、或已有更晚有效变更覆盖时结果为空且绝不误报；预演不写入任何数据，冲突/幂等语义与提交接口一致。
- **许可变更作废**：业务人员发现变更登记有误时，可凭**原变更标识、唯一作废标识和原因**经 `POST /license-changes/revocations` 作废，返回含**原标识、作废标识、原因、登记时间**的回执（`registered_at` 由服务按 UTC 生成）。作废一经提交即对**所有查询时刻**生效：
  - **原变更仍可按原接口查询**：`GET /license-changes/{change_id}` 与版本详情照常返回该变更，且附带作废回执（`revocation` 字段；未作废为 `null`）；回执也可经 `GET /license-changes/revocations/{revocation_id}` 追溯。
  - **单时点判定**忽略已作废变更，改取该版本截至查询时刻最近的**未作废**变更；若没有，则按无有效许可（`no_valid_license`）阻断。
  - **区间判定**仅以未作废变更划分片段，半开区间 `[start, end)` 口径与全部响应字段保持不变。
  - **变更预演**基于同一份「作废后」快照计算（已作废变更不参与现状判定），且同样不落库。
  - 原许可变更的**同标识重放**与**同版本同生效时刻冲突**规则不变；重放一条已作废变更只会返回原记录（仍标记为已作废），**不能恢复**它；与已作废变更同版本同生效时刻的新提交/预演仍报 `409`。
- **用途判定凭证（可追溯）**：研究审批可把一次用途判定**固化为凭证**（`POST /permissions/vouchers`）。请求携带**唯一凭证标识、已登记版本、用途、带时区的判定时刻**；首次请求在**同一一致数据视图**中沿用现有判定规则，把服务按 UTC 生成的**登记时间**连同**完整祖先闭包、依据（basis）、阻断原因（blockers）与用途交集**一次性持久保存——**允许与拒绝都留痕**。判定与保存在**单个写事务内原子完成，失败不留半成品**。
  - 凭证是**不可变历史**：`GET /permissions/vouchers/{voucher_id}` 始终返回**首次保存**的结果；之后的许可变更或作废只影响实时判定与预演，**不能改写历史凭证**。
  - **幂等与冲突**：相同凭证标识、相同版本、**去首尾空格后相同的用途**、**换算为 UTC 后相同的判定时刻**重试 → `200` 且 `replayed=true`，返回原凭证；同一标识复用到不同输入（版本/用途/时刻任一不同）→ `409 conflict`；未知凭证 → `404 not_found`；未知版本 → `404`；空用途或无时区时刻 → `422`，且**均不留凭证**。
- **许可结果订阅与翻转事件流**：研究团队可对某已登记版本的「用途 + 带时区的固定判定时刻」建立订阅（`POST /permissions/subscriptions`），携带**唯一订阅标识**。建立订阅时在**单个 `BEGIN IMMEDIATE` 写事务**内沿用现有判定规则，把**当时的允许状态原子保存为不可变基线**（`baseline_allowed` 与全部阻断版本/原因，允许与拒绝都固化）。此后每当**真实提交许可变更或作废**，服务在**同一事务**内按现有「祖先闭包、不晚于时刻的最近有效变更、作废忽略」规则**重新判定受影响订阅**（目标版本自身及其全部后代；菱形后代只计一次）：**仅当允许状态真正翻转**时才追加一条持久事件，事件记录**订阅内从 1 起递增的序号、触发操作类型与标识（变更/作废）、关联变更与生效时刻、前后允许状态与前后阻断原因**；状态未翻转（变更晚于固定时刻、被更晚变更覆盖、仅依据变化而允许状态不变）一律不追加。
  - **按订阅标识 + 序号游标读取事件**（`GET /permissions/subscriptions/{id}/events?after_seq=&limit=`）：严格按序号升序稳定返回，`next_cursor` 给出下一游标（空页回显当前游标），`has_more` 提示是否还有后续；可从任意已见游标续读，顺序不重不漏。
  - **幂等与并发**：变更/作废的同标识重试返回原记录，**绝不产生重复事件**；订阅与变更/作废并发时，进程级锁 + 单写事务 + 每订阅 `(subscription_id, seq)` 唯一约束共同保证「翻转与触发同进同退」，一次翻转**既不漏计也不重复计入**。
  - 基线不可变：订阅后许可如何变化，`GET /permissions/subscriptions/{id}` 与同输入重试都恒返回建立时的基线；事件流才是之后翻转的留痕。**幂等/冲突/错误口径与凭证一致**：同标识 + 同版本 + 去空格用途 + UTC 归一化同时刻重试返回原订阅（`200 replayed=true`）；标识复用到不同输入 → `409`；未知订阅 → `404`；未知版本 → `404`；空用途或无时区时刻 → `422`，且**均不留新订阅**。
- **批量许可变更导入（同一事务、逐条可见真实翻转）**：研究团队可经 `POST /license-changes/batches` 一次导入**非空、有序**的多条许可变更，请求携带**唯一批次标识**；每条沿用现有单条变更的字段（变更标识、已登记版本、带时区生效时刻、允许用途全集）与归一化规则（用途去空格/去重/顺序无关、时刻换算 UTC）。
  - **整批原子校验**：批内变更标识重复、与已存记录冲突（同标识内容不同，或同版本同生效时刻存在不同变更——含批内与已存两种情形）、存在未知版本、或任一字段非法（空列表、无时区时刻、空白用途项）时**整批拒绝**：变更、批次记录与翻转事件**均不落库**。
  - **同一事务按请求顺序逐条应用**：每条应用后以**该条之后的快照**（已含本批先前各条）按订阅的**固定判定时刻与祖先闭包**规则比较该条前后允许状态；**仅真实翻转**才以原事件字段（`license_change` / 变更标识 / 生效时刻 / 前后状态与阻断原因）与**订阅内连续序号**追加事件——**即使同批后续变更把状态翻回，两次事件都保留**。
  - **成功响应**返回批次标识与服务生成的首次提交时间、逐条变更（请求序号、该条是否重放、变更详情）、**每条新增事件数**与整批新增事件总数。批内允许混入**与已存记录完全相同的重放条目**：该条 `replayed=true`、`new_event_count=0`，其余新条目照常应用与翻转。
  - **幂等与冲突**：同批次标识 + **规范化后完全相同**的有序列表重试 → `200` 且 `replayed=true`，返回**首次冻结结果**（逐字段一致，之后即使其中变更被作废也不变），**不重新应用、不重放任何事件**；同批次标识对应不同内容 → `409`，且**不得重放事件**。批次结果可经 `GET /license-changes/batches/{batch_id}` 追溯；未知批次 → `404`。
  - 批量落库的变更与单条提交完全互通：可按 `GET /license-changes/{change_id}` 查询、按单条接口重放、按作废接口作废；事件游标 `GET /permissions/subscriptions/{id}/events` 读到的批量事件与单条事件序号连续、形状一致。
- **按版本槽统一发布与历史时刻复现**：研究团队可把已登记数据集版本按「版本槽」统一发布（`POST /releases`），供之后按历史时刻复现。一次发布携带**唯一发布标识、带时区生效时刻**与**非空槽位清单**；每个槽位给出**槽名、目标版本、该槽预期修订号**。槽名**去首尾空格**后必须非空且在同一次发布内**不可重复**，每个目标版本都必须已登记。
  - **逐槽推进规则**：槽位**首次发布的预期修订号必须为 0**，发布后该槽修订为 0；此后每次发布的目标必须是该槽**上次发布版本的严格后代**（不能是自己或旁支），本次**生效时刻必须严格晚于**该槽上次发布的生效时刻，且**预期修订号必须等于该槽当前修订号 + 1**（漏发/跳号/陈旧预期都拒绝）。
  - **整次原子**：任一槽位校验失败（槽名重复、未知版本、首版预期非 0、修订不匹配、目标非严格后代、时刻不晚于上次），**整次发布不生效**——不插入发布记录、不留下任何槽位条目、任何槽的修订都不前进。全部校验通过后，才在**同一个 `BEGIN IMMEDIATE` 写事务**内写入不可变发布记录、逐槽不可变条目并推进各槽修订指针。
  - **幂等与冲突**：相同发布标识 + **规范化后相同内容**（槽名 trim、时刻换算 UTC、清单按请求顺序）重试 → `200` 且 `replayed=true`，返回**首次冻结结果**且不再推进任何修订；同标识异内容（含槽位换序）→ `409`。并发发布由**进程级锁 + 单写事务 + 修订比较更新（CAS）+ 数据库级 `(槽名, 修订号)` 唯一约束**共同保证：修订号从 0 起**稠密递增、绝不跳过、绝不重复**，失败发布不留部分记录。
  - **按历史时刻解析**：`GET /slot-resolutions/{slot}?at=` 取该槽**生效时刻不晚于 `at` 的最近一次发布**（恰在生效时刻即采用新发布），返回当时目标版本、命中发布标识与修订号；**槽从未发布或在该时刻之前尚无生效发布时明确报 `404 slot_not_published`**（`details.reason` 区分 `slot_never_published` / `no_release_effective_at_or_before`），绝不猜测版本。解析所得版本可用 `GET /slot-permissions/{slot}?use=&at=` 或直接交给既有判定接口，**完全沿用现有规则**（祖先闭包、最近未作废变更、用途交集、作废忽略）判定该时刻的指定用途。
  - 发布记录与槽位条目均为**不可变历史**：`GET /releases` / `GET /releases/{release_id}` 恒返回首次冻结内容；`GET /slots` / `GET /slots/by-name/{slot}` 查看各槽当前修订指针。
- **交付窗口整单准入**：研究团队交付数据前可经 `POST /delivery-admissions` 用**唯一交付号、带时区半开窗口 `[start, end)` 与非空有序条目清单**申请整单准入；每条引用**已登记版本或版本槽**并给出用途。槽条目在**同一数据库快照**中随窗口内发布切换解析目标版本（恰在 `start` 生效立即采用、恰在 `end` 不计入；**槽尚未发布也算失败**），解析版本再按既有祖先许可与作废规则判定。
  - 仅当**全部条目在整个窗口内始终允许**时，才在**同一事务原子固化不可变准入记录**，保存各条**全窗口分段的解析版本与判定依据**，并可按交付号 `GET /delivery-admissions/{delivery_id}` 追溯；否则返回 **403**，给出**每条未通过条目的首个失败时段与阻断原因**（`no_valid_license` / `use_not_allowed` / `slot_not_published`），**不留准入记录**。
  - **幂等与冲突**：同交付号 + 去空格引用/用途、时刻换算 UTC 后相同且**顺序一致**的重试返回**首次记录**（`200 replayed=true`），之后目录变化（新发布、许可变更、作废）**不改写**它；异输入（窗口、任一条目或顺序不同）→ `409`。未知版本 → `404`；无时区、空/倒置窗口、空白用途/引用、空清单 → `422`，均**整单不落库**。详见下文第 11 节。
- **离线审计证明包（签名固定）**：已签发凭证可按标识导出自包含证明包（`POST /permissions/vouchers/{id}/proof`），内含**首次判定完整快照、签名算法（Ed25519/JWS EdDSA）、密钥标识、签名与明确验签编码规则**；签名覆盖版本、用途、判定时刻、登记时刻、祖先、依据、阻断原因与用途交集（顺序敏感），**篡改任一字段均无法通过验签**。首次导出**原子固化**，重复或并发导出返回同一内容；历史凭证同样可导出，未知凭证明确报错。签名密钥为**本地持久密钥**（`SIGNING_KEYS_DIR`），**轮换活跃密钥后旧证明包与旧公钥仍可核验、旧包绝不重签**，密钥不可用时明确报 `503`。详见下文第 12–14 节。
- **幂等与冲突**：
  - 相同**变更标识**且语义内容（版本、生效时刻、允许用途集合；用途顺序/重复不影响）完全相同的重复提交，返回**同一结果**（HTTP `200`，`replayed=true`）；
  - 标识相同而内容不同 → `409 conflict`；
  - 同一版本在同一生效时刻出现不同变更 → `409 conflict`；
  - 作废接口：同一**作废标识**且内容（原变更、原因）完全相同的重试返回**原回执**（HTTP `200`，`replayed=true`）；复用同一作废标识指向不同内容（不同原变更或不同原因）→ `409`；以**另一作废标识重复作废同一变更** → `409`；目标变更不存在 → `404 not_found`；
  - 所有时间必须**带时区**（如 `...Z`、`+08:00`），服务统一按 UTC 归一化；传入无时区时间返回 `422`。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与异常映射
│   ├── service.py     # 核心业务逻辑（登记 / 变更 / 判定 / 凭证 / 订阅与翻转事件流 / 证明包导出 / 幂等冲突）
│   ├── signing.py     # 本地持久 Ed25519 密钥、密钥轮换与 JWS(EdDSA) 证明包签名/验签
│   ├── database.py    # SQLite 连接与建表
│   ├── schemas.py     # 请求/响应模型
│   ├── seed.py        # 幂等的示例数据
│   └── config.py      # 环境变量配置
├── examples/
│   ├── curl-examples.sh            # HTTP API 全流程调用示例（含证明包/密钥轮换）
│   └── offline-verify-proof.py     # 离线验签示例（仅依赖 cryptography）
├── tests/test_api.py    # 端到端 API 测试（pytest + TestClient）
├── tests/test_releases.py # 版本槽发布与历史复现的端到端测试（含并发修订稠密性）
├── tests/test_signing.py # 签名密钥与证明包编码的单元测试
├── docker/entrypoint.sh
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

前置：已安装 Docker 与 Compose 插件（`docker compose version` 可用）。

```bash
# 构建镜像、初始化依赖与数据库（首次启动自动建表并写入示例数据）并后台启动
docker compose up -d --build

# 查看日志
docker compose logs -f

# 停止
docker compose down            # 保留数据卷
docker compose down -v         # 同时删除数据卷（清空数据）
```

启动完成后（默认约 1–2 秒）：

| 入口 | 地址 |
| --- | --- |
| 服务 Base URL | <http://localhost:8000> |
| 健康检查 | <http://localhost:8000/health> |
| Swagger UI（可在线调用） | <http://localhost:8000/docs> |
| OpenAPI 文档 | <http://localhost:8000/openapi.json> |

> 宿主机端口由环境变量 `APP_PORT` 控制（默认 `8000`），容器内固定监听 `8000`。数据存放在命名卷 `license-data`（容器内 `/data`）。

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_HOST` | `0.0.0.0` | uvicorn 监听地址，一般无需修改 |
| `APP_PORT` | `8000` | **宿主机映射端口**（同时作为容器内监听端口）；改为如 `8080` 后访问 <http://localhost:8080> |
| `DB_PATH` | `/data/licenses.db` | SQLite 数据库文件路径（位于持久化卷内） |
| `SIGNING_KEYS_DIR` | `<DB 所在目录>/signing-keys`（容器内即 `/data/signing-keys`） | 证明包 Ed25519 签名密钥目录（位于持久化卷内；轮换后旧密钥仍保留以供旧证明包验签） |
| `INIT_DB` | `true` | 启动时自动建表（`CREATE TABLE IF NOT EXISTS`，幂等） |
| `SEED_DEMO` | `true` | 数据库为空时写入示例数据；对已有数据的数据库自动跳过。生产环境可设为 `false` |
| `INIT_SIGNING_KEY` | `true` | 启动时确保存在活跃签名密钥：无密钥则生成首个密钥、仅有旧密钥而缺指针则自动修复；密钥目录不可用时只记录错误（证明包导出返回 503），不影响其他接口 |
| `CONFIG_ECHO` | `true` | 启动时在日志打印解析后的配置 |

临时改端口或关闭示例数据：

```bash
APP_PORT=8080 SEED_DEMO=false docker compose up -d --build
```

## HTTP API

### 1. 登记版本（固定父版本引用，不可修改）

`POST /versions`

```bash
curl -X POST http://localhost:8000/versions \
  -H 'Content-Type: application/json' \
  -d '{
    "version_id": "dataset-c-v1",
    "parent_ids": ["demo-root-a", "demo-root-b"],
    "note": "由两个上游数据集派生"
  }'
```

- `parent_ids` 中每个版本必须已存在，否则 `404`；
- `version_id` / `change_id` 省略时服务自动生成 UUID；
- 父引用创建后不可修改：再次提交相同 `version_id` 返回 `409`，且不存在任何编辑接口。

### 2. 提交许可变更（幂等）

`POST /license-changes`

```bash
curl -X POST http://localhost:8000/license-changes \
  -H 'Content-Type: application/json' \
  -d '{
    "change_id": "chg-c-1",
    "version_id": "dataset-c-v1",
    "effective_at": "2026-03-01T00:00:00Z",
    "allowed_uses": ["research", "review"]
  }'
```

- 首次创建返回 `201`；相同 `change_id` + 相同内容重放返回 `200` 且 `replayed=true`，`change` 内容与首次完全一致；
- 同标识不同内容、或同版本同生效时刻的不同变更返回 `409`；
- `allowed_uses` 是该时刻起允许用途的**全集**（覆盖语义）；用途去重、顺序无关。

### 3. 判定可否使用

`GET /permissions/check?version_id=...&use=...&at=...`

```bash
curl -s "http://localhost:8000/permissions/check?version_id=demo-derived-1&use=research&at=2026-03-01T00:00:00Z"
```

允许时：

```json
{
  "version_id": "demo-derived-1",
  "use": "research",
  "at": "2026-03-01T00:00:00Z",
  "allowed": true,
  "closure": ["demo-derived-1", "demo-root-a", "demo-root-b"],
  "basis": [ { "version_id": "...", "change_id": "...", "effective_at": "...", "allowed_uses": ["..."] } ],
  "blockers": [],
  "allowed_uses_intersection": ["research"]
}
```

拒绝时 `allowed=false`，`blockers` 列出**全部**阻断版本，`reason` 取值：

- `no_valid_license`：该版本在查询时刻没有任何不晚于该时刻的变更（包括尚未登记任何变更、或所有变更都在未来生效）；
- `use_not_allowed`：该版本的最近有效变更不包含所申请用途（同时回传所依据的 `change_id`、`effective_at` 与当时的 `allowed_uses`）。

被祖先拒绝的示例：

```bash
# demo-root-a 在 2026-06-01 起仅允许 research，故 7 月对 demo-derived-1 申请 review 被阻断
curl -s "http://localhost:8000/permissions/check?version_id=demo-derived-1&use=review&at=2026-07-01T00:00:00Z"
# -> allowed=false, blockers 含 demo-root-a / use_not_allowed
```

### 4. 区间分段判定（本轮入口，排期用）

`GET /permissions/timeline?version_id=...&use=...&start=...&end=...`

回答「某用途在一段时期内**何时可用、为何中断**」：服务在一次一致数据视图中沿固定父引用展开自身及全部祖先（菱形去重），按各版本当时最近生效的许可变更与用途交集，把半开区间 **[start, end)** 切成连续、无重叠的片段。分段点只取自闭包内各版本变更的生效时刻；**仅在判定依据（basis）或阻断信息（blockers）变化时分段**，相邻相同片段自动合并。

- 起点恰好有变更：立即采用新许可（与单时刻判定 `effective_at <= at` 同口径）；
- 终点上的变更**不计入**（半开区间）；
- 某时段内任一祖先无有效许可 → 该片段持续拒绝，`blockers` 给出 `no_valid_license`；
- 每个片段返回 `start` / `end`、`allowed`、`basis`（各版本依据的变更）、`blockers`（全部阻断版本与原因）、`allowed_uses_intersection`。

```bash
curl -s "http://localhost:8000/permissions/timeline?version_id=demo-derived-1&use=review&start=2026-01-15T00:00:00Z&end=2026-07-15T00:00:00Z"
```

基于内置示例数据的响应（`basis` 字段从略，形状同单时刻判定）：

```json
{
  "version_id": "demo-derived-1",
  "use": "review",
  "start": "2026-01-15T00:00:00Z",
  "end": "2026-07-15T00:00:00Z",
  "closure": ["demo-derived-1", "demo-root-a", "demo-root-b"],
  "segments": [
    {
      "start": "2026-01-15T00:00:00Z",
      "end": "2026-02-01T00:00:00Z",
      "allowed": false,
      "blockers": [
        { "version_id": "demo-derived-1", "reason": "no_valid_license" },
        { "version_id": "demo-root-b", "reason": "use_not_allowed", "change_id": "demo-chg-b-1" }
      ],
      "allowed_uses_intersection": []
    },
    {
      "start": "2026-02-01T00:00:00Z",
      "end": "2026-06-01T00:00:00Z",
      "allowed": false,
      "blockers": [
        { "version_id": "demo-root-b", "reason": "use_not_allowed", "change_id": "demo-chg-b-1" }
      ],
      "allowed_uses_intersection": ["research"]
    },
    {
      "start": "2026-06-01T00:00:00Z",
      "end": "2026-07-15T00:00:00Z",
      "allowed": false,
      "blockers": [
        { "version_id": "demo-root-a", "reason": "use_not_allowed", "change_id": "demo-chg-a-2" },
        { "version_id": "demo-root-b", "reason": "use_not_allowed", "change_id": "demo-chg-b-1" }
      ],
      "allowed_uses_intersection": ["research"]
    }
  ]
}
```

即：review 在 2 月 1 日前因派生版本尚无许可且 root-b 不含 review 被拒；2 月 1 日起仅剩 root-b 阻断；6 月 1 日 root-a 收紧后两个根版本共同阻断。换成 `use=research` 则第 2、3 段均为 `allowed=true`（6 月 1 日 root-a 的依据变更仍会切分片段，因为判定依据变了）。

错误语义（一律不返回部分结果）：

| 情形 | 结果 |
| --- | --- |
| 版本不存在 | `404 not_found` |
| `start` / `end` 无时区或无法解析 | `422 invalid_time` |
| `start >= end`（倒置或空区间；同一时刻的两种时区写法也算空区间） | `422 invalid_interval` |

### 5. 预演许可变更（提交前影响分析，不写入）

`POST /license-changes/preview`

在**真正提交变更之前**，传入与提交接口相同的变更字段（变更标识、已登记版本、带时区的生效时刻、允许用途全集）外加待判定用途 `use` 与带时区的查询时刻 `at`，服务对同一份数据快照分别计算「现状」与「假设该变更已提交」两种判定，仅返回目标版本及其全部后代中**允许状态发生翻转**的版本。

```bash
curl -s -X POST http://localhost:8000/license-changes/preview \
  -H 'Content-Type: application/json' \
  -d '{
    "change_id": "preview-a-commercial",
    "version_id": "demo-root-a",
    "effective_at": "2026-04-01T00:00:00Z",
    "allowed_uses": ["research", "review", "commercial"],
    "use": "commercial",
    "at": "2026-05-01T00:00:00Z"
  }'
```

上例预演「2026-04 起给 `demo-root-a` 放开 commercial」对 5 月判定的影响，`impacted_versions` 会同时包含 `demo-root-a` 与后代 `demo-derived-1`，每条给出：

- `before_allowed` / `after_allowed`：提交前、假设提交后的允许状态；
- `before_blockers` / `after_blockers`：两个时点的**全部**阻断版本与原因（`no_valid_license` / `use_not_allowed`，字段口径同判定接口）；
- `before_basis` / `after_basis`、`before/after_allowed_uses_intersection`：所依据的最近变更与祖先许可交集；
- `closure`：该版本判定时展开的自身及全部祖先。

顶层 `scope` 为本次计算覆盖的「目标版本 + 全部后代」（广度优先、去重，**菱形继承的后代只计一次**）；预演只做只读计算，绝不写入变更（可用 `GET /license-changes/{change_id}` 验证预演标识仍为 `404`）。

不误报的两种情形（`impacted_versions` 为空数组）：

- **查询时刻早于生效时刻**：变更尚未生效，前后判定一致；
- **已有更晚（且不晚于查询时刻）的已登记变更覆盖该时刻**：按「最近有效变更」规则，预演变更不会成为依据。

错误与幂等语义与 `POST /license-changes` 完全一致：

| 情形 | 结果 |
| --- | --- |
| 目标版本不存在 | `404 not_found` |
| 相同 `change_id` 已存在但内容不同 | `409 conflict` |
| 同版本同生效时刻已存在**不同**变更 | `409 conflict` |
| 相同标识与语义内容的重放 | `200` 且 `replayed=true`，`impacted_versions` 恒为空（现状与「重放后」本就相同，无影响） |
| `effective_at` / `at` 无时区 | `422` |

并发说明：预演在单个 SQLite WAL 只读快照事务内、并在进程级锁保护下一次性读取版本、父引用与变更，评估过程中其他请求的提交不会混入，不会出现「版本来自旧快照、变更来自新快照」的拼接结果。

### 6. 作废许可变更（登记有误时撤回，幂等）

`POST /license-changes/revocations`

业务人员发现许可变更登记有误时，凭**原变更标识、唯一作废标识和原因**作废一条已登记变更。作废立即对所有查询时刻生效（单时点判定、区间分段、变更预演均忽略该变更），但**不删除原记录**。

```bash
curl -X POST http://localhost:8000/license-changes/revocations \
  -H 'Content-Type: application/json' \
  -d '{
    "change_id": "chg-c-1",
    "revocation_id": "rev-chg-c-1-001",
    "reason": "允许用途登记有误，撤回重报"
  }'
```

首次作废返回 `201`：

```json
{
  "replayed": false,
  "receipt": {
    "revocation_id": "rev-chg-c-1-001",
    "change_id": "chg-c-1",
    "reason": "允许用途登记有误，撤回重报",
    "registered_at": "2026-10-04T09:00:00.000000Z"
  }
}
```

- 回执含**原标识** `change_id`、**作废标识** `revocation_id`、**原因** `reason`、**登记时间** `registered_at`（服务按 UTC 生成）；`revocation_id` 省略时自动生成 UUID；
- 同一作废标识 + 同一原变更 + 同一原因重试 → `200` 且 `replayed=true`，返回**同一份原回执**（含首次登记时间）；
- 复用同一作废标识但内容不同（指向另一原变更，或原因不同）→ `409 conflict`；
- 以另一作废标识重复作废同一变更 → `409 conflict`（错误详情回传已存在的 `existing_revocation_id`）；
- 目标变更不存在 → `404 not_found`；原因为空白 → `422`。

作废后的查询行为：

```bash
# 原变更仍可按原接口查询，附带 revocation 回执
curl -s http://localhost:8000/license-changes/chg-c-1

# 回执可按作废标识追溯
curl -s http://localhost:8000/license-changes/revocations/rev-chg-c-1-001

# 单时点判定忽略它：若无其他未作废变更，按 no_valid_license 阻断
curl -s "http://localhost:8000/permissions/check?version_id=dataset-c-v1&use=research&at=2026-07-01T00:00:00Z"
```

原变更的同标识重放规则不变——重放只会取回原记录（仍带作废标记），**不会恢复**为有效变更：

```bash
curl -X POST http://localhost:8000/license-changes -H 'Content-Type: application/json' \
  -d '{"change_id":"chg-c-1","version_id":"dataset-c-v1","effective_at":"2026-03-01T00:00:00Z","allowed_uses":["research","review"]}'
# -> 200 replayed=true，change.revocation 仍为原作废回执
```

### 7. 用途判定凭证（可追溯，判定与保存原子完成）

`POST /permissions/vouchers` · `GET /permissions/vouchers/{voucher_id}`

把一次用途判定固化为研究审批可追溯的凭证。首次请求在**单个 `BEGIN IMMEDIATE` 写事务**内、以同一一致快照完成「版本校验 → 祖先展开 → 沿用现有判定规则评估 → 写入凭证」，任一步失败全部回滚，**不留半成品凭证**。响应中的 `registered_at` 是服务按 UTC 生成的登记时间；`closure` / `basis` / `blockers` / `allowed_uses_intersection` 与 `GET /permissions/check` 的字段口径完全一致（`blockers` 为空即允许）。

```bash
curl -s -X POST http://localhost:8000/permissions/vouchers \
  -H 'Content-Type: application/json' \
  -d '{
    "voucher_id": "approval-2026-03-research-001",
    "version_id": "demo-derived-1",
    "use": " research ",
    "at": "2026-03-01T08:00:00+08:00"
  }'
```

首次签发返回 `201`（`replayed=false`），拒绝同样返回 `201` 并在 `blockers` 中给出全部阻断版本与原因；用途与时刻在服务侧归一化（去首尾空格、换算 UTC）：

```json
{
  "replayed": false,
  "voucher": {
    "voucher_id": "approval-2026-03-research-001",
    "version_id": "demo-derived-1",
    "use": "research",
    "at": "2026-03-01T00:00:00Z",
    "registered_at": "2026-10-05T01:23:45.123456Z",
    "allowed": true,
    "closure": ["demo-derived-1", "demo-root-a", "demo-root-b"],
    "basis": [ { "version_id": "...", "change_id": "...", "effective_at": "...", "allowed_uses": ["..."] } ],
    "blockers": [],
    "allowed_uses_intersection": ["research"]
  }
}
```

- **重试幂等**：相同凭证标识 + 相同版本 + 去空格后相同用途 + UTC 归一化后相同时刻（如 `+08:00` 与 `Z` 两种写法指向同一瞬间）→ `200` 且 `replayed=true`，凭证内容与首次**逐字段一致**（含首次的 `registered_at`）；
- **历史不可改写**：签发后即使原变更被收紧、作废，凭证查询与同输入重试都恒返回首次结果；实时接口 `GET /permissions/check` 的判定照常随当前数据变化，不受影响；
- **标识复用冲突**：同一 `voucher_id` 提交不同版本/用途/时刻 → `409 conflict`（错误详情回传已存输入与本次输入），原凭证保持不变；
- `voucher_id` 省略时服务自动生成 UUID（`p-` 前缀）。

| 情形 | 结果 |
| --- | --- |
| 首次签发（允许或拒绝） | `201`，`replayed=false` |
| 同标识 + 同版本 + 同用途（去空格）+ 同时刻（UTC 归一化）重试 | `200`，`replayed=true`，返回原凭证 |
| 标识复用到不同输入 | `409 conflict` |
| 凭证标识不存在（GET） | `404 not_found` |
| 版本不存在 | `404 not_found`，不留凭证 |
| 用途为空白 | `422`，不留凭证 |
| `at` 无时区或无法解析 | `422 invalid_time`，不留凭证 |

```bash
# 按凭证标识追溯（恒为首次保存的结果）
curl -s http://localhost:8000/permissions/vouchers/approval-2026-03-research-001

# 同输入重试：+08:00 写法与 Z 指向同一 UTC 瞬间 -> 200 replayed=true
curl -s -X POST http://localhost:8000/permissions/vouchers \
  -H 'Content-Type: application/json' \
  -d '{"voucher_id":"approval-2026-03-research-001","version_id":"demo-derived-1","use":"research","at":"2026-03-01T00:00:00Z"}'

# 复用标识指向不同用途 -> 409
curl -s -X POST http://localhost:8000/permissions/vouchers \
  -H 'Content-Type: application/json' \
  -d '{"voucher_id":"approval-2026-03-research-001","version_id":"demo-derived-1","use":"commercial","at":"2026-03-01T00:00:00Z"}'
```

### 8. 许可结果订阅与翻转事件流（基线原子固化，翻转同事务留痕）

`POST /permissions/subscriptions` · `GET /permissions/subscriptions/{subscription_id}` · `GET /permissions/subscriptions/{subscription_id}/events`

研究团队可对某个**已登记版本**的「**用途 + 带时区的固定判定时刻**」建立长期关注。首次请求在**单个 `BEGIN IMMEDIATE` 写事务**内、以同一一致快照完成「版本校验 → 祖先展开 → 沿用现有判定规则评估 → 固化基线」，把**建立当时的允许状态**（`baseline_allowed` 与全部阻断版本/原因，允许与拒绝都留痕）原子保存；任一步失败全部回滚，**不留半成品订阅**。此后真实的许可变更/作废在各自的**同一事务**内重新判定受影响订阅，只对**允许状态真正翻转**的订阅追加事件。

```bash
# 建立订阅（示例版本 sub-demo-v1 在 2026-09 仅允许 research，关注 review -> 基线拒绝）
curl -s -X POST http://localhost:8000/permissions/subscriptions \
  -H 'Content-Type: application/json' \
  -d '{
    "subscription_id": "approval-watch-review-2026-09",
    "version_id": "sub-demo-v1",
    "use": " review ",
    "at": "2026-09-01T08:00:00+08:00"
  }'
```

首次建立返回 `201`（`replayed=false`）；用途与时刻在服务侧归一化（去首尾空格、换算 UTC）：

```json
{
  "replayed": false,
  "subscription": {
    "subscription_id": "approval-watch-review-2026-09",
    "version_id": "sub-demo-v1",
    "use": "review",
    "at": "2026-09-01T00:00:00Z",
    "created_at": "2026-10-05T10:00:00.000000Z",
    "baseline_allowed": false,
    "baseline_blockers": [
      {
        "version_id": "sub-demo-v1",
        "reason": "use_not_allowed",
        "change_id": "sub-demo-c1",
        "effective_at": "2026-03-01T00:00:00Z",
        "allowed_uses": ["research"]
      }
    ]
  }
}
```

**翻转事件如何产生**：此后每条真实变更/作废提交后，服务沿固定父引用取**目标版本自身及全部后代**（菱形去重），用**现有规则**（各版本不晚于固定时刻的最近**未作废**变更、用途交集、无有效许可即 `no_valid_license` 阻断）在触发操作的**同一写事务**内重新判定每个候选订阅；**仅当允许状态与上一状态不同**才追加事件，记录：

- `seq`：**该订阅内从 1 起严格递增**的事件序号；
- `trigger_kind` / `trigger_id`：触发操作类型（`license_change` 变更 / `revocation` 作废）与对应标识（`change_id` / `revocation_id`）；
- `change_id` / `effective_at`：本次涉及的原许可变更与其生效时刻（作废事件记录原变更的生效时刻，作废对所有时刻生效）；
- `before_allowed` / `after_allowed`：翻转前后允许状态；
- `before_blockers` / `after_blockers`：翻转前后的**全部阻断版本与原因**（字段口径同判定接口；允许时为空数组）；
- `created_at`：事件追加时间（UTC，服务生成）。

```bash
# 6 月起放开 review -> 为订阅追加 seq=1（false -> true）
curl -s -X POST http://localhost:8000/license-changes -H 'Content-Type: application/json' \
  -d '{"change_id":"sub-demo-c2","version_id":"sub-demo-v1","effective_at":"2026-06-01T00:00:00Z","allowed_uses":["research","review"]}'

# 7 月再收紧回 research -> seq=2（true -> false）
curl -s -X POST http://localhost:8000/license-changes -H 'Content-Type: application/json' \
  -d '{"change_id":"sub-demo-c3","version_id":"sub-demo-v1","effective_at":"2026-07-01T00:00:00Z","allowed_uses":["research"]}'

# 作废收紧的那条 -> seq=3（false -> true，重新露出 sub-demo-c2）
curl -s -X POST http://localhost:8000/license-changes/revocations -H 'Content-Type: application/json' \
  -d '{"change_id":"sub-demo-c3","revocation_id":"sub-demo-rev-c3","reason":"收紧登记有误，撤回"}'
```

**按订阅标识与序号游标读取事件**（稳定顺序、可续读）：

```bash
# 从头读，每页最多 2 条
curl -s "http://localhost:8000/permissions/subscriptions/approval-watch-review-2026-09/events?after_seq=0&limit=2"
```

```json
{
  "subscription_id": "approval-watch-review-2026-09",
  "after_seq": 0,
  "limit": 2,
  "events": [
    {
      "seq": 1,
      "trigger_kind": "license_change",
      "trigger_id": "sub-demo-c2",
      "change_id": "sub-demo-c2",
      "effective_at": "2026-06-01T00:00:00Z",
      "before_allowed": false,
      "after_allowed": true,
      "before_blockers": [ { "version_id": "sub-demo-v1", "reason": "use_not_allowed", "change_id": "sub-demo-c1" } ],
      "after_blockers": [],
      "created_at": "2026-10-05T10:01:00.000000Z"
    },
    { "seq": 2, "trigger_kind": "license_change", "trigger_id": "sub-demo-c3", "change_id": "sub-demo-c3", "effective_at": "2026-07-01T00:00:00Z", "before_allowed": true, "after_allowed": false, "before_blockers": [], "after_blockers": [ { "version_id": "sub-demo-v1", "reason": "use_not_allowed", "change_id": "sub-demo-c3" } ], "created_at": "..." }
  ],
  "next_cursor": 2,
  "has_more": true
}
```

- 用返回的 `next_cursor` 作为下一次的 `after_seq` 续读（上例再取 `after_seq=2` 得到 seq=3）；无更多事件时 `events=[]`、`has_more=false`、`next_cursor` 回显当前游标；
- 顺序恒为序号升序，重复翻页不会重放或跳过事件；`limit` 范围 1–1000（默认 100）。

**基线不可变、只追加事件**：订阅建立后许可如何变化，`GET /permissions/subscriptions/{id}` 与同输入重试都恒返回**建立时**的基线；之后的变化只体现在事件流里，且事件行同样是**不可变历史**（任何接口都不更新/删除）。订阅晚于某次变更建立时，**不会回填**该变更——事件只从建立之后的真实触发开始累积。

| 情形 | 结果 |
| --- | --- |
| 首次建立订阅（基线允许或拒绝） | `201`，`replayed=false`，基线原子固化 |
| 同标识 + 同版本 + 同用途（去空格）+ 同时刻（UTC 归一化）重试 | `200`，`replayed=true`，返回原订阅（含首次 `created_at` 与基线） |
| 标识复用到不同输入（版本/用途/时刻任一不同） | `409 conflict`，原订阅与事件流不变 |
| 订阅标识不存在（GET 订阅 / GET 事件流） | `404 not_found` |
| 版本不存在 | `404 not_found`，不留订阅 |
| 用途为空白 | `422`，不留订阅 |
| `at` 无时区或无法解析 | `422 invalid_time`，不留订阅 |
| 变更/作废的同标识重试 | 返回原记录（`200 replayed=true`），**不追加任何事件** |
| 变更未翻转某订阅（晚于固定时刻生效/被更晚变更覆盖/仅依据变化而允许状态不变） | 不追加事件 |
| 游标 `after_seq < 0` 或 `limit < 1` | `422` |

**并发保证**：建立订阅、提交变更、作废都在进程级锁与单个写事务内完成；订阅的「基线固化」与变更/作废的「翻转事件追加」不可能交错。多个订阅与一次变更并发时，每个订阅要么在基线中已看到新状态（无事件），要么固化旧状态后在同事务恰得一条翻转事件；多个作废标识并发作废同一变更时恰一个成功，也恰追加一次翻转。事件主键 `(subscription_id, seq)` 与每订阅的序号分配都在同一写事务内，**一次翻转不漏计、不重复计入**。

```bash
# 按订阅标识追溯基线（恒为建立当时结果）
curl -s http://localhost:8000/permissions/subscriptions/approval-watch-review-2026-09

# 续读翻转事件
curl -s "http://localhost:8000/permissions/subscriptions/approval-watch-review-2026-09/events?after_seq=2&limit=10"
```

### 9. 批量许可变更导入（同一事务，逐条可见真实翻转）

`POST /license-changes/batches` · `GET /license-changes/batches/{batch_id}`

研究团队一次导入多条许可变更时，需要看见**每条变更造成的真实订阅翻转**。请求携带**唯一批次标识**与**非空有序变更列表**，每条沿用现有单条提交的字段（`change_id`、`version_id`、带时区 `effective_at`、允许用途全集）与归一化口径。所有变更在**单个 `BEGIN IMMEDIATE` 写事务**内按请求顺序逐条应用，每条都以"该条应用之后、已含本批先前各条"的快照，按各订阅的**固定判定时刻与祖先闭包**规则比较该条前后允许状态。

```bash
curl -s -X POST http://localhost:8000/license-changes/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_id": "batch-2026-q3-review-policy",
    "changes": [
      {
        "change_id": "chg-open-review",
        "version_id": "sub-demo-v1",
        "effective_at": "2026-06-01T00:00:00Z",
        "allowed_uses": ["research", "review"]
      },
      {
        "change_id": "chg-close-review",
        "version_id": "sub-demo-v1",
        "effective_at": "2026-08-01T00:00:00Z",
        "allowed_uses": ["research"]
      }
    ]
  }'
```

首次提交返回 `201`（`replayed=false`）。上例中 6 月放开使关注 review 的订阅翻转一次（拒绝 → 允许），8 月收紧再翻转一次（允许 → 拒绝）——**即使同批后续变更把状态翻回，两次事件都保留**：

```json
{
  "replayed": false,
  "batch": {
    "batch_id": "batch-2026-q3-review-policy",
    "created_at": "2026-10-06T02:00:00.000000Z",
    "change_count": 2,
    "new_event_count": 2,
    "changes": [
      {
        "index": 0,
        "replayed": false,
        "change": {
          "change_id": "chg-open-review",
          "version_id": "sub-demo-v1",
          "effective_at": "2026-06-01T00:00:00Z",
          "allowed_uses": ["research", "review"],
          "content_hash": "…",
          "created_at": "2026-10-06T02:00:00.000000Z",
          "revocation": null
        },
        "new_event_count": 1
      },
      {
        "index": 1,
        "replayed": false,
        "change": { "change_id": "chg-close-review", "…": "…" },
        "new_event_count": 1
      }
    ]
  }
}
```

- **逐条快照**：第 N 条比较的是"第 N-1 条之后"与"第 N 条之后"的状态，因此批内放开后再收紧会产生两条连续序号的事件（每条事件字段与单条提交完全一致：`trigger_kind=license_change`、`trigger_id`/`change_id` 为该条标识、前后允许状态与前后阻断原因）；某条未造成翻转（晚于订阅固定时刻、被更晚变更覆盖、仅依据变化）则该条 `new_event_count=0`。
- **批内重放条目**：列表中的某条若与已存变更**标识与内容完全相同**，记为 `replayed=true`、`new_event_count=0`（不写入、不产生事件），同批其余新条目照常应用；批内不允许出现两条新变更落在同一版本同一生效时刻。
- **整批原子拒绝**：批内变更标识重复、与已存记录冲突（同标识内容不同，或同版本同时刻存在不同变更）、未知版本、空列表、无时区时刻、空白用途项等任一情形 → 整批 `4xx`，**变更与事件均不落库**（即使非法条目之前的条目本身合法）。

| 情形 | 结果 |
| --- | --- |
| 批次首次提交成功 | `201`，`replayed=false`，变更与翻转事件同一事务落库 |
| 同批次标识 + 规范化后完全相同的有序列表重试 | `200`，`replayed=true`，返回**首次冻结结果**（逐字段一致），不重新应用、**不重放事件** |
| 同批次标识对应不同内容（任一条不同或顺序不同） | `409 conflict`，不应用、不重放事件 |
| 批内变更标识重复 | `409 conflict`，整批拒绝 |
| 批内同版本同生效时刻出现不同条目 | `409 conflict`，整批拒绝 |
| 某条与已存变更冲突（同标识不同内容 / 不同标识占同版本同时刻） | `409 conflict`，整批拒绝 |
| 任一条的版本未知 | `404 not_found`（`details.missing_version_ids`），整批拒绝 |
| `changes` 为空、无时区时刻、空白用途项等字段非法 | `422`，整批拒绝 |
| 批次标识不存在（GET） | `404 not_found` |

```bash
# 幂等重试：用途换序/重复、同一 UTC 瞬间的 +08:00 写法都视为规范化后相同 -> 200 replayed=true
curl -s -X POST http://localhost:8000/license-changes/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_id": "batch-2026-q3-review-policy",
    "changes": [
      {"change_id":"chg-open-review","version_id":"sub-demo-v1","effective_at":"2026-06-01T08:00:00+08:00","allowed_uses":["review","research","review"]},
      {"change_id":"chg-close-review","version_id":"sub-demo-v1","effective_at":"2026-08-01T00:00:00Z","allowed_uses":["research"]}
    ]
  }'

# 同批次标识不同内容 -> 409（事件数不增加）
curl -s -X POST http://localhost:8000/license-changes/batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_id":"batch-2026-q3-review-policy","changes":[{"change_id":"other","version_id":"sub-demo-v1","effective_at":"2026-09-01T00:00:00Z","allowed_uses":["research"]}]}'

# 按批次标识取回首次冻结结果（恒为首次内容；其中变更之后被作废也不变）
curl -s http://localhost:8000/license-changes/batches/batch-2026-q3-review-policy

# 批量产生的事件与单条事件共用同一事件流，序号连续
curl -s "http://localhost:8000/permissions/subscriptions/example-sub-1/events?after_seq=0&limit=20"
```

批量提交的变更与现有语义完全互通：可经 `GET /license-changes/{change_id}` 查询、经单条 `POST /license-changes` 同标识重放、经 `POST /license-changes/revocations` 作废；单条提交、作废、订阅建立、事件游标与历史凭证的语义均保持不变。`batch_id` / 条目中的 `change_id` 省略时由服务生成（分别为 `b-` / `c-` 前缀；自动生成批次标识无法用于重试幂等）。

### 10. 按版本槽统一发布与历史时刻复现

把已登记版本按**版本槽（slot）**统一发布：一次发布原子推进多个槽的修订并留下不可变记录；之后可按「槽名 + 带时区时刻」解析当时的目标版本，再用既有规则判定用途，实现按历史时刻复现。

#### 10.1 发布：`POST /releases`

请求体：

```json
{
  "release_id": "release-2026-09-01",
  "effective_at": "2026-09-01T00:00:00Z",
  "slots": [
    {"slot": "prod", "version_id": "v2026-09", "expected_revision": 1},
    {"slot": "canary", "version_id": "v2026-10-rc", "expected_revision": 4}
  ]
}
```

- `release_id`：唯一发布标识（省略时自动生成 `rel-…`，但自动标识无法用于重试幂等）。
- `effective_at`：**必须带时区**的本次发布生效时刻，服务统一换算 UTC。
- `slots`：**非空**清单，按请求顺序冻结；每项为 `slot`（去首尾空格后非空、同次发布内唯一）、`version_id`（必须已登记）、`expected_revision`（非负整数）。槽名同时用于查询接口的路径参数（`/slots/by-name/{slot}`、`/slot-resolutions/{slot}`、`/slot-permissions/{slot}`），建议使用字母、数字、`-`、`_`、`.`，避免包含 `/`（百分号编码的 `/` 在常见 ASGI 部署中不会被路由到槽名）。

逐槽规则：

| 情形 | 要求 | 违反时 |
| --- | --- | --- |
| 槽首次发布 | `expected_revision` 必须为 `0`；发布后该槽修订 = 0 | `409` |
| 槽再次发布 | `expected_revision` 必须等于该槽**当前修订 + 1**（当前修订可由 `GET /slots/by-name/{slot}` 查询） | `409` |
| 目标版本 | 必须是该槽**上次发布版本的严格后代**（沿父引用的传递后代；不能是自身或旁支） | `409` |
| 生效时刻 | 必须**严格晚于**该槽上次发布的生效时刻 | `409` |

任一槽位不满足（含目标版本不存在 `404`、槽名空白/重复或无时区 `422`），**整次发布不生效**：无发布记录、无槽位条目、无修订前进。全部通过后在**单个写事务**内写入不可变发布记录与逐槽条目、推进各槽修订指针（0 → 1 → 2 …）。

```bash
# 首次发布：prod、canary 两个槽，预期修订号均为 0（槽名两侧空格会被 trim）
curl -s -X POST http://localhost:8000/releases -H 'Content-Type: application/json' -d '{
  "release_id": "release-2026-08-01",
  "effective_at": "2026-08-01T00:00:00Z",
  "slots": [
    {"slot": " prod ", "version_id": "release-demo-v1", "expected_revision": 0},
    {"slot": "canary", "version_id": "release-demo-root", "expected_revision": 0}
  ]
}'
# -> 201 replayed=false；两个槽 revision 均为 0

# 相同发布标识 + 规范化后相同内容重试（+08:00 同一 UTC 瞬间、槽名空格差异都算相同）
curl -s -X POST http://localhost:8000/releases -H 'Content-Type: application/json' -d '{
  "release_id": "release-2026-08-01",
  "effective_at": "2026-08-01T08:00:00+08:00",
  "slots": [
    {"slot": "prod", "version_id": "release-demo-v1", "expected_revision": 0},
    {"slot": " canary ", "version_id": "release-demo-root", "expected_revision": 0}
  ]
}'
# -> 200 replayed=true，返回首次冻结结果，修订不再推进

# prod 推进到严格后代 release-demo-v2：预期修订 1（= 旧修订 0 + 1）
curl -s -X POST http://localhost:8000/releases -H 'Content-Type: application/json' -d '{
  "release_id": "release-2026-09-01",
  "effective_at": "2026-09-01T00:00:00Z",
  "slots": [{"slot": "prod", "version_id": "release-demo-v2", "expected_revision": 1}]
}'
# -> 201；prod revision=1

# 典型失败（均为整次拒绝，不留任何记录）：
#  - 预期修订号不匹配（跳号/陈旧）-> 409，details 含 current_revision/required_revision
#  - 目标不是上次版本的严格后代（自身/旁支）-> 409
#  - 生效时刻不严格晚于该槽上次发布 -> 409
#  - 同一发布标识换内容（含槽位换序）-> 409
#  - 目标版本不存在 -> 404；槽名空白/同次重复/无时区/空清单 -> 422
```

并发语义：进程级锁 + `BEGIN IMMEDIATE` + 修订比较更新（仅当库里修订仍等于旧修订才推进）+ 数据库级 `UNIQUE(槽名, 修订号)` 四重保证——并发发布同一槽时恰一个成功，其余得到 `409`，**修订号稠密递增、不跳过、不重复，失败发布不留部分记录**。

#### 10.2 查询发布记录与槽位指针

```bash
curl -s http://localhost:8000/releases                       # 全部不可变发布（按首次发布时间）
curl -s http://localhost:8000/releases/release-2026-08-01    # 按发布标识取回首次冻结结果；不存在 404
curl -s http://localhost:8000/slots                          # 全部槽当前修订指针
curl -s http://localhost:8000/slots/by-name/prod             # 单槽当前指针；从未发布 404
```

冻结结果字段：`release_id`、`effective_at`（UTC）、`created_at`（服务 UTC 生成）、`slot_count`、`slots[]`（每项含 `position` 请求序号、`slot`、`version_id`、`expected_revision` 客户端断言、`revision` 实际得到的修订号）。发布记录与槽位条目**只增不改**。

#### 10.3 按历史时刻解析：`GET /slot-resolutions/{slot}?at=`

取该槽**生效时刻不晚于 `at` 的最近一次发布**（半开口径：恰在生效时刻立即采用新发布）：

```bash
# 2026-08-15 介于两次发布之间 -> 命中 release-2026-08-01，prod 当时为 release-demo-v1 修订 0
curl -s "http://localhost:8000/slot-resolutions/prod?at=2026-08-15T00:00:00Z"
# 2026-09-15 -> 命中 release-2026-09-01，release-demo-v2 修订 1（+08:00 同一 UTC 瞬间等价）
curl -s "http://localhost:8000/slot-resolutions/prod?at=2026-09-15T08:00:00%2B08:00"
```

返回 `slot`、`at`（UTC）、`version_id`（当时目标版本）、`release_id`（命中发布）、`revision`（命中修订，0 起）、`effective_at`（命中发布的生效时刻）。尚未发布必须明确报错，不猜测版本：

- 槽从未发布 → `404 {"error":"slot_not_published", ..., "details":{"reason":"slot_never_published"}}`
- 槽有发布但首次发布晚于 `at` → `404 slot_not_published`，`reason=no_release_effective_at_or_before`
- 时刻无时区 → `422 invalid_time`

#### 10.4 用解析所得版本复现判定：`GET /slot-permissions/{slot}?use=&at=`

先按 10.3 解析槽在 `at` 当时的目标版本，再对**该版本**按现有规则（祖先闭包、不晚于时刻的最近未作废变更、用途交集）判定 `use`；响应在常规判定字段外额外携带 `resolution`（与 10.3 同形）。槽在该时刻尚未发布时同样返回 `404 slot_not_published`。

```bash
# 同一用途在不同历史时刻可能解析到不同版本、得到不同结论
curl -s "http://localhost:8000/slot-permissions/prod?use=research&at=2026-08-15T00:00:00Z"
curl -s "http://localhost:8000/slot-permissions/prod?use=research&at=2026-09-15T00:00:00Z"
```

也可以先调 10.3 拿到 `version_id`，再直接调用既有 `GET /permissions/check`、`/permissions/timeline`、凭证与订阅等接口——解析与判定完全解耦，判定口径不变。

### 11. 交付窗口整单准入（本轮新增）

`POST /delivery-admissions` · `GET /delivery-admissions/{delivery_id}`

研究团队交付数据前，用**一个唯一交付号**、一个**带时区的半开使用窗口 `[start, end)`** 与一份**非空有序条目清单**申请整单准入：每条要么引用**已登记版本**（`kind="version"`），要么引用**版本槽**（`kind="slot"`，随槽发布在窗口内切换解析目标版本），并声明一个去首尾空格的**用途**。

判定口径：

- 所有解析与许可判定都取自**同一数据库快照**：槽按不晚于当前时刻的最近一次发布解析目标版本（恰在 `start` 生效的发布立即采用，恰在 `end` 生效的发布不计入；槽在窗口某段**尚未发布即判该条失败**，不猜测版本），再对解析版本按**既有祖先闭包/最近未作废变更/用途交集**规则判定；
- 许可变更同样是恰在 `start` 生效即计入、恰在 `end` 不计入，已作废变更全窗口忽略；
- 仅当**全部条目在整个窗口内始终允许**时，才在同一事务原子固化一条**不可变准入记录**，保存各条全窗口**分段的解析版本与判定依据**（closure/basis/blockers/用途交集），可按交付号追溯；
- 任一条目在任一子区间不允许，则返回 **403 `admission_denied`**，`failures` 按请求顺序给出**每条未通过条目的首个失败半开时段**（`failure_start`/`failure_end`）与该时段内**全部阻断原因**（`reason` ∈ `no_valid_license` / `use_not_allowed` / `slot_not_published`），**不留任何准入记录**；
- 未知版本为 **404**（整单不落库）；条目为空、时刻无时区、空/倒置窗口、空白用途或空白引用为 **422**，同样整单不落库。

```bash
# 版本引用：r0 在 2026 全年研究用途持续允许 -> 201，原子固化不可变记录
curl -s -X POST http://localhost:8000/delivery-admissions \
  -H 'Content-Type: application/json' \
  -d '{
    "delivery_id": "delivery-2026-Q3-001",
    "start": "2026-07-01T00:00:00Z",
    "end":   "2026-10-01T00:00:00Z",
    "entries": [
      {"kind": "version", "ref": "demo-root-a", "use": "research"}
    ]
  }'

# 槽引用：prod 随发布在窗口内从 release-demo-v1 切换到 release-demo-v2，
# 每个分段分别保存解析版本（release_id/revision）与该版本的判定依据；
# 窗口起点前槽未发布会整单 403
curl -s -X POST http://localhost:8000/delivery-admissions \
  -H 'Content-Type: application/json' \
  -d '{
    "delivery_id": "delivery-2026-slot-001",
    "start": "2026-08-01T00:00:00Z",
    "end":   "2026-12-01T00:00:00Z",
    "entries": [
      {"kind": "slot", "ref": "prod", "use": "research"}
    ]
  }'

# 全窗口未持续获准 -> 403 admission_denied（示例：review 自 2026-06-01 起被收回）
# failures[0] 给出首个失败时段及其 use_not_allowed 阻断依据；
# 该交付号下不留记录（GET 为 404），之后补齐许可可用同一交付号重新申请
curl -s -X POST http://localhost:8000/delivery-admissions \
  -H 'Content-Type: application/json' \
  -d '{
    "delivery_id": "delivery-2026-Q3-deny",
    "start": "2026-07-01T00:00:00Z",
    "end":   "2026-10-01T00:00:00Z",
    "entries": [
      {"kind": "version", "ref": "demo-root-a", "use": "review"}
    ]
  }'
```

响应要点（201 首次固化 / 200 同输入重试）：

```json
{
  "replayed": false,
  "admission": {
    "delivery_id": "delivery-2026-slot-001",
    "created_at": "2026-10-06T00:00:00.000000Z",
    "start": "2026-08-01T00:00:00Z",
    "end": "2026-12-01T00:00:00Z",
    "entry_count": 1,
    "entries": [
      {
        "position": 0, "kind": "slot", "ref": "prod", "use": "research",
        "resolution": {"kind": "slot", "ref": "prod", "version_id": "release-demo-v1",
                       "release_id": "example-release-1", "revision": 0,
                       "effective_at": "2026-08-01T00:00:00Z"},
        "segments": [
          {"start": "2026-08-01T00:00:00Z", "end": "2026-09-01T00:00:00Z", "allowed": true,
           "resolution": {"version_id": "release-demo-v1", "release_id": "example-release-1", "revision": 0},
           "version_id": "release-demo-v1", "closure": ["..."], "basis": [{"...": "..."}],
           "blockers": [], "allowed_uses_intersection": ["research"]},
          {"start": "2026-09-01T00:00:00Z", "end": "2026-12-01T00:00:00Z", "allowed": true,
           "resolution": {"version_id": "release-demo-v2", "release_id": "example-release-2", "revision": 1},
           "...": "..."}
        ]
      }
    ]
  }
}
```

**幂等与冲突**：同交付号、窗口与条目在去首尾空格、时刻换算 UTC 后相同且**顺序一致**的重试，返回首次冻结记录（`200 replayed=true`）；固化之后目录再变化（新发布、许可变更、作废）**不改写**它（GET 恒为首次内容）。同交付号但窗口/任一条目（含顺序）不同为 **409**。被 403 拒绝的交付号没有记录，修正目录后用同号再次申请会正常固化。

```bash
# 同输入重试：+08:00 同一 UTC 瞬间、槽名/用途空格差异、顺序一致 -> 200 replayed=true
# 按交付号追溯：恒为首次固化内容；拒绝过的交付号返回 404
curl -s http://localhost:8000/delivery-admissions/delivery-2026-Q3-001
```

### 12. 导出用途判定凭证的离线审计证明包（签名固定）

`POST /permissions/vouchers/{voucher_id}/proof` · `GET /permissions/vouchers/{voucher_id}/proof`

研究审批把已签发的用途判定凭证交给**离线审计方**核验时，按凭证标识导出一个自包含的**证明包**：内含首次判定的完整快照、签名算法、密钥标识与签名，以及明确的验签编码规则；包内同时携带签发公钥（SPKI PEM 与 RFC 8037 JWK），审计方完全离线即可验签，也可按下述公钥接口从服务方独立取公钥交叉核对。

```bash
# 首次导出：201，证明包签名并原子固化
curl -s -X POST http://localhost:8000/permissions/vouchers/approval-2026-03-research-001/proof
# 重复/并发导出：200，replayed=true，返回同一份已固化证明包（逐字段一致）
# 只读取回已固化的证明包（不触发签名、不重签）
curl -s http://localhost:8000/permissions/vouchers/approval-2026-03-research-001/proof
```

证明包结构（字段说明见下；完整 JSON 可经 `examples/curl-examples.sh` 查看）：

```json
{
  "replayed": false,
  "proof": {
    "format_version": 1,
    "voucher_id": "approval-2026-03-research-001",
    "payload": {
      "format_version": 1,
      "voucher_id": "...",
      "version_id": "demo-derived-1",
      "use": "research",
      "at": "2026-03-01T00:00:00.000000Z",
      "registered_at": "2026-10-05T01:23:45.123456Z",
      "allowed": true,
      "closure": ["demo-derived-1", "demo-root-a", "demo-root-b"],
      "basis": [ { "version_id": "...", "change_id": "...", "effective_at": "...", "allowed_uses": ["..."] } ],
      "blockers": [],
      "allowed_uses_intersection": ["research"]
    },
    "signature": {
      "algorithm": "Ed25519",
      "jws_alg": "EdDSA",
      "key_id": "key-20261005t012345z-1a2b3c4d",
      "protected_header": { "alg": "EdDSA", "kid": "key-..." },
      "protected": "BASE64URL(UTF8(保护头 JSON))",
      "payload": "BASE64URL(规范 JSON 载荷)",
      "signature": "BASE64URL(Ed25519 签名)",
      "compact_serialization": "<protected>.<payload>.<signature>",
      "public_key_pem": "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n",
      "public_key_jwk": { "kty": "OKP", "crv": "Ed25519", "x": "...", "kid": "key-..." }
    },
    "verification": {
      "encoding": { "format": "jws-eddsa-ed25519-v1", "...": "明确的验签编码规则（见下）", "covered_fields": ["..."] },
      "public_key_url": "/signing/keys/key-.../public",
      "public_keys_url": "/signing/keys"
    },
    "exported_at": "2026-10-05T01:24:00.000000Z"
  }
}
```

**签名覆盖范围（篡改任一字段均无法通过验签）**：`format_version`、`voucher_id`、`version_id`、`use`、`at`（判定时刻）、`registered_at`（凭证登记时刻）、`allowed`、`closure`（祖先闭包）、`basis`（依据）、`blockers`（阻断原因）、`allowed_uses_intersection`（用途交集）。其中各列表的**顺序同样参与签名**（祖先顺序、依据顺序、阻断顺序、用途顺序被调换也会验签失败）。`exported_at` 只是首次导出的固化元数据，**不在**签名载荷内，因此轮换密钥后重放旧包不会因时间元数据而需要重签。

**明确的验签编码规则**（与 `verification.encoding` 一致，审计方照此重算即可）：

1. 签名方案：**Ed25519（RFC 8032）**，JWS 紧凑序列化（RFC 7515 §7.1），JWS 头 `alg=EdDSA`（RFC 8037）；
2. 保护头：UTF-8 JSON `{"alg":"EdDSA","kid":"<key_id>"}`，键按字典序、紧凑分隔符；
3. 载荷：UTF-8 JSON，**键按字典序排序、紧凑分隔符（`','` 与 `':'`）、非 ASCII 原样输出（`ensure_ascii=false`）**；时间统一为 UTC 文本 `YYYY-MM-DDTHH:MM:SS.ffffffZ`；
4. Base64URL：RFC 4648 §5，**无填充**；
5. 签名输入：`ASCII(BASE64URL(保护头 JSON)) + "." + BASE64URL(载荷 JSON 的 UTF-8 字节)`，对其做 Ed25519 签名；
6. 验签时把包内 `payload` 对象按第 3 条重新编码，必须与 `signature.payload` 解码后的字节**逐字节一致**（任何字段被改动都会在此或在签名校验处失败）。

**原子性、幂等与历史语义**：

- 首次导出在服务进程级锁与单个写事务内「读取不可变凭证快照 → 以活跃密钥签名 → 整体落库」，失败不留半成品；重复或**并发**导出都返回**同一份内容**（恰一个 `201`，其余 `200 replayed=true`，签名唯一）；
- **已有历史凭证随时可导出**（包括导出前签发、之后许可发生变更/作废的凭证）；导出的是凭证首次判定快照，不受之后数据影响；
- 证明包与凭证一样是**不可变历史**：任何接口都不会更新或重签它。

| 情形 | 结果 |
| --- | --- |
| 首次导出（允许或拒绝凭证均可） | `201`，`replayed=false` |
| 重复导出 / 并发首次导出 | `200`，`replayed=true`，返回同一证明包 |
| 凭证不存在（POST 或 GET） | `404 not_found`，不留证明行 |
| 凭证存在但从未导出（GET） | `404 not_found`，`details.reason=proof_not_exported` |
| 签名密钥缺失/损坏/密钥目录不可写 | `503 signing_key_unavailable`（明确报错；其他接口不受影响） |

### 13. 签发公钥与本地持久密钥轮换

`GET /signing/keys` · `GET /signing/keys/{key_id}/public` · `POST /signing/keys/rotate`

服务使用**本地持久密钥**：Ed25519 私钥以 PKCS#8 PEM 保存在 `SIGNING_KEYS_DIR`（默认与数据库同在持久卷内，容器内 `/data/signing-keys`），每个密钥一个 `<key_id>.json`（文件权限 `600`、目录 `700`，原子写入），`active` 单行文件指向当前活跃密钥。首次启动若目录为空会自动生成首个密钥（`INIT_SIGNING_KEY=true`）。

```bash
# 列出全部保留公钥（含轮换前旧公钥）与当前活跃密钥
curl -s http://localhost:8000/signing/keys

# 按密钥标识取对应公钥（轮换后旧公钥仍可取回）
curl -s http://localhost:8000/signing/keys/key-20261005t012345z-1a2b3c4d/public

# 轮换：生成并启用新密钥（201），旧密钥文件全部保留
curl -s -X POST http://localhost:8000/signing/keys/rotate
# -> { "previous_key_id": "key-...旧", "key": { "key_id": "key-...新", "active": true, "public_key_pem": "...", "public_key_jwk": {...} } }
```

轮换后的核验语义：

- **旧证明包仍可核验**：它的 `kid` 与签名永不改变，用 `GET /signing/keys/{旧 key_id}/public` 取回的旧公钥即可验签（该公钥标记为 `"active": false`）；**旧包不会被重签**——重复导出始终返回原内容；
- 轮换**之后**首次导出的证明包由新活跃密钥签名；新旧包互不通用公钥（用错公钥验签必然失败）；
- 若活跃指针指向的密钥文件丢失、密钥文件损坏或密钥目录不可创建/写入，密钥初始化/轮换/证明包导出都会返回 `503 signing_key_unavailable`（含原因与目录），服务其余功能（登记、签发、查询、实时判定）保持正常。

> 备份时请同时备份数据库与 `SIGNING_KEYS_DIR`；**私钥丢失后历史证明包无法由本服务重新出具**（已分发的证明包仍可用已取得的公钥核验）。轮换端点无内建鉴权，暴露到不可信网络时应放在管理网络/反向代理鉴权之后。

### 14. 离线验签示例（无需服务在线）

`examples/offline-verify-proof.py` 只依赖 `cryptography`（`pip install cryptography`），不导入本服务任何代码，可在审计方离线环境运行，接受**裸证明包**或 `POST` 导出的 `{replayed, proof}` 包装：

```bash
# 导出并保存（POST 返回 {replayed, proof}）
curl -s -X POST http://localhost:8000/permissions/vouchers/approval-2026-03-research-001/proof > proof.json

# 1) 完全离线：使用包内自带公钥验签，并打印判定快照关键字段
python3 examples/offline-verify-proof.py proof.json

# 2) 用独立渠道取得的公钥（SPKI PEM）交叉核验
KID=$(python3 -c "import json;print(json.load(open('proof.json'))['proof']['signature']['key_id'])")
curl -s http://localhost:8000/signing/keys/$KID/public \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['public_key_pem'])" > key.pem
python3 examples/offline-verify-proof.py proof.json --public-key key.pem

# 3) 直接从运行中的服务拉取证明包（审计侧只需出网 HTTP，无需其他依赖）
python3 examples/offline-verify-proof.py \
  --server http://localhost:8000 --voucher-id approval-2026-03-research-001
```

验签通过退出码为 `0`，失败（字段被篡改、公钥不匹配、包格式非法）为非零并给出原因，例如：

```text
✅ 验签通过（EdDSA/Ed25519，密钥 key-20261005t012345z-1a2b3c4d）
❌ payload 重编码与已签名 payload 不一致（字段可能被篡改）
❌ 验签失败：签名与载荷/公钥不匹配（字段可能被篡改）
```

### 15. 查询接口

| 方法与路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查 |
| `GET /versions` | 列出全部版本（含父引用与变更；已作废变更带 `revocation` 回执） |
| `GET /versions/{version_id}` | 版本详情，不存在返回 `404` |
| `GET /license-changes/{change_id}` | 按标识查询变更（已作废仍可查，附 `revocation` 字段） |
| `POST /license-changes/batches` | 同一事务按序批量导入多条许可变更，逐条返回重放标记与新增事件数（首次 `201` / 同批次同内容重试 `200` / 批内重复或冲突或未知版本整批 `409`/`404` / 字段非法 `422`） |
| `GET /license-changes/batches/{batch_id}` | 按批次标识取回首次冻结的批次结果，不存在返回 `404` |
| `POST /releases` | 按版本槽统一发布多个已登记版本，同一事务推进各槽修订并保存不可变发布记录（首次 `201` / 同标识规范化同内容重试 `200` / 槽名重复或修订不匹配或目标非严格后代或时刻不晚于上次或标识复用异内容 `409` / 未知目标版本 `404` / 空清单或空白槽名或无时区 `422`） |
| `GET /releases` | 列出全部不可变发布记录（按首次发布时间排序） |
| `GET /releases/{release_id}` | 按发布标识取回首次冻结的发布记录，不存在返回 `404` |
| `GET /slots` | 列出全部版本槽的当前修订指针 |
| `GET /slots/by-name/{slot}` | 按槽名查询当前最新发布（修订/目标版本/命中发布），从未发布返回 `404` |
| `GET /slot-resolutions/{slot}?at=` | 按槽名与带时区时刻解析当时最近的已发布目标版本；槽从未发布或该时刻前无生效发布返回 `404 slot_not_published`，无时区 `422` |
| `GET /slot-permissions/{slot}?use=&at=` | 先解析槽当时版本再按现有规则判定用途（响应含 `resolution`）；该时刻槽尚未发布返回 `404` |
| `GET /license-changes/revocations/{revocation_id}` | 按作废标识追溯作废回执，不存在返回 `404` |
| `POST /permissions/vouchers` | 把一次用途判定原子签发为可追溯凭证（首次 `201` / 同输入重试 `200` / 输入冲突 `409`） |
| `GET /permissions/vouchers/{voucher_id}` | 追溯凭证，恒返回首次保存的判定；不存在返回 `404` |
| `POST /permissions/subscriptions` | 建立许可结果订阅并原子固化基线（首次 `201` / 同输入重试 `200` / 输入冲突 `409` / 未知版本 `404` / 空用途或无时区 `422`） |
| `GET /permissions/subscriptions/{subscription_id}` | 按订阅标识取回不可变基线；不存在返回 `404` |
| `GET /permissions/subscriptions/{subscription_id}/events` | 按订阅标识与序号游标（`after_seq`/`limit`）读取翻转事件，稳定升序并返回 `next_cursor`/`has_more`；未知订阅 `404` |
| `POST /permissions/vouchers/{voucher_id}/proof` | 按凭证标识导出固定签名证明包（首次 `201` / 重复或并发 `200` / 未知凭证 `404` / 密钥不可用 `503`） |
| `GET /permissions/vouchers/{voucher_id}/proof` | 取回已固化的证明包（不签名、不重签）；未导出过 `404 proof_not_exported` |
| `GET /signing/keys` | 列出全部保留的签发公钥（含轮换前旧公钥）与当前活跃密钥标识 |
| `GET /signing/keys/{key_id}/public` | 按密钥标识取签发公钥（SPKI PEM + JWK），未知标识 `404` |
| `POST /signing/keys/rotate` | 轮换活跃签名密钥（`201`；旧密钥保留，旧证明包不重签），密钥不可用 `503` |

错误响应统一形如：

```json
{ "error": "conflict", "message": "……", "details": { } }
```

## 内置示例数据（SEED_DEMO=true 时）

| 版本 | 父版本 | 变更 |
| --- | --- | --- |
| `demo-root-a` | — | 2026-01-01 起允许 `research, review`；2026-06-01 起仅 `research` |
| `demo-root-b` | — | 2026-01-01 起允许 `research, commercial` |
| `demo-derived-1` | a, b | 2026-02-01 起允许 `research, review, commercial`（实际许可 = 三者交集，仅 `research`） |
| `demo-no-license` | — | 无任何变更，始终以 `no_valid_license` 阻断 |

一键体验（启动后）：

```bash
bash examples/curl-examples.sh
```

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 初始化依赖即数据库建表在应用启动时自动完成（默认 DB_PATH=/data/licenses.db，
# 本地运行建议指向可写路径；签名密钥默认落在数据库同级的 signing-keys 目录，
# 可用 SIGNING_KEYS_DIR 单独指定）：
DB_PATH=./data/licenses.db SEED_DEMO=true uvicorn app.main:app --reload --port 8000

# 运行测试
pip install -r requirements-dev.txt
pytest -q
```

测试覆盖：父引用存在性与不可变、祖先展开（含菱形去重）、用途交集、无有效许可阻断、最近变更生效、幂等重放、两类 409 冲突、时区校验、示例数据初始化，变更预演（目标与后代翻转、查询早于生效时刻/更晚变更覆盖不误报、重放无影响、冲突与 404 错误语义、菱形后代只计一次、预演不写入、并发提交下快照不混用），区间分段判定（变更边界切分、起点变更立即生效/终点变更不计入、无有效许可持续拒绝、菱形闭包去重、每段全部阻断版本与原因、与单时刻判定逐点一致、未知版本 404、无时区与倒置/空区间 422），许可变更作废（回执字段与追溯、同标识同内容重试返回原回执、标识复用内容不同/另一标识重复作废两类 409、目标不存在 404、原变更仍可查且带作废标记、单时点与区间判定对所有时刻忽略已作废变更、作废唯一变更后无有效许可阻断、祖先作废阻断后代、重放不能恢复、预演基于作废后快照且不落库、并发同标识重试恰一次创建、示例数据端到端），以及用途判定凭证（允许与拒绝都留痕、凭证内容与实时判定逐字段一致、去空格用途与 UTC 时刻归一化后同输入重试返回原凭证、标识复用到不同输入三类 409、未知凭证/未知版本 404、空用途/无时区 422 且不留凭证、省略标识自动生成、变更收紧与作废后历史凭证不可改写、并发同标识恰一次签发、示例数据端到端），和许可结果订阅与翻转事件流（建立时基线允许/拒绝均原子固化且含全部阻断原因、同版本+去空格用途+UTC 同时刻重试返回原订阅、标识复用到不同版本/用途/时刻 409、未知订阅/未知版本 404、空用途/无时区 422 且不留订阅、变更与作废在同一事务内对自身及全部后代重判且仅状态翻转追加事件、祖先阻断与菱形后代只计一次、事件序号每订阅从 1 递增且记录触发操作/前后状态/前后阻断原因、变更晚于固定时刻或被更晚变更覆盖或仅依据变化时不追加、作废后重新露出更早变更的双向翻转链、变更与作废重试不产生重复事件、游标分页稳定升序且 next_cursor/has_more 续读不重不漏、晚建立的订阅不回填历史、省略标识自动生成、订阅与变更并发每订阅恰零或一条翻转、同标识订阅并发恰一次建立、多标识并发作废恰一次成功且恰一次翻转、示例数据端到端），以及批量许可变更导入（同一事务按请求顺序逐条应用且每条按该条之后的快照/固定时刻/祖先闭包比较前后状态、仅真实翻转追加事件且逐条与整批返回新增事件数、同批后续翻回仍保留两次连续序号事件、未翻转与晚于固定时刻的条目新增事件为 0、祖先收紧时菱形后代每订阅只翻转一次、批内标识重复/批内同版本同时刻/与已存标识内容冲突/占已存同版本同时刻/未知版本均整批拒绝且变更与事件不落库（非法条目之前的合法条目同样不落库）、空列表/无时区/空白用途 422 不落库、同批次标识与规范化后完全相同的有序列表重试返回首次冻结结果且逐字段一致、事件不重放、用途换序去重与不同时区写法按规范化判定、同批次标识不同内容（含换序）409 且不应用不重放、批内已存同内容条目记重放且零事件并与新条目混排、省略批次/变更标识自动生成、批次 GET 追溯与 404、冻结结果在其中变更后被作废时保持不变、批量变更与单条查询/重放/作废及事件游标互通、并发同批次标识恰一次提交其余重放、示例数据端到端），和离线审计证明包与签名密钥（首次导出 201 且包内含完整快照/算法/密钥标识/签名/验签规则、独立公钥（端点 PEM 与包内 PEM）验签通过、重复与并发导出逐字段同一内容且恰一次固化、未知凭证 POST/GET 均 404 且不留行、未导出 GET 为 404 proof_not_exported、历史凭证在许可变更/作废后仍导出首次快照、篡改版本/用途/判定时刻/登记时刻/允许结果/祖先顺序/依据/阻断原因/用途交集/签名字节均验签失败、轮换后旧包不重签且用旧公钥仍可验签、用新公钥交叉验签失败、轮换后新包用新密钥、旧公钥标记非活跃且仍可取回、密钥目录不可用时导出/轮换明确 503 且其他接口照常、密钥文件丢失/损坏/指针缺失的启动自愈与报错、证明包与密钥跨重启持久、规范 JSON 与 base64url 编码确定性、示例数据端到端），以及版本槽统一发布与历史复现（多槽首版均得修订 0、严格后代链逐次 +1、间接/菱形后代允许、同次多槽独立推进、槽名 trim、时刻时区归一化、省略标识自动生成；空清单/空白槽名/trim 后重复槽名/无时区/负修订号 422、未知目标版本 404 且整次不落库；首版预期非 0、陈旧预期与跳号、目标为自身或旁支、生效时刻等于或早于该槽上次、按槽比较的时刻不满足 均 409；任一槽非法整次拒绝且不留发布/条目/指针；同标识规范化同内容（trim、UTC 等价、同序）重试返回首次冻结结果且不推进、同标识异内容与槽位换序 409；发布记录不可变且按时间列出/单取 404；历史解析取不晚于时刻的最近发布、恰在生效时刻采用新版、偏移时区等价；从未发布与首次发布晚于时刻两类 404 slot_not_published 区分 reason、无时区 422；解析版本经 slot-permissions 按既有交集规则复现判定；并发首版恰一成功、并发链式发布修订稠密不跳过不重复、同标识并发恰一提交其余重放），以及交付窗口整单准入（版本/槽两类条目全窗口分段、槽随发布切换解析版本并逐段保存解析指针与判定依据、发布/许可变更恰在起点生效与恰在终点不计入、槽未发布前缀/从未发布/首版晚于窗口均判 slot_not_published 失败、无有效许可与用途不允许的首个失败时段及全部阻断原因、祖先阻断、作废忽略、多失败条目按序列出、允许/依据变化两类分段保留；全部条目全窗口允许才 201 原子固化且 GET 可追溯、任一失败 403 且整单不留记录、拒绝号补齐目录后可再固化；未知版本 404 整单不落库；空清单/无时区/空倒置窗口/空白用途或引用/未知 kind 422；去空格与 UTC 归一化后同顺序同输入重试 200 返回首次记录且逐字段一致、之后发布/收紧/作废不改写已固化记录、异窗口/异条目/换序 409、未知交付号 GET 404；同号并发恰一次固化其余重放、单快照一致性；示例数据端到端）。
