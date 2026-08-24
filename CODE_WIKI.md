# Shared Brain — Code Wiki

> 面向开放 Agent Harness 的**零 LLM、可自托管**共享记忆与 Skill 后端。
> 首发原生支持 Hermes（Python MemoryProvider）与 DeepSeek Harness（Cordis 插件）。
> 本文档基于源码静态分析生成，对应版本 `0.1.0`（Phase 1 MVP），状态机审计更新于 2026-08-24。

## 1. 项目概览

| 维度 | 说明 |
|---|---|
| 定位 | 共享记忆 / Skill 后端；各 Agent 通过**官方扩展点**接入，跨 Agent 受控写入、自动召回、可靠溯源 |
| 服务器 | Python 3.9+ · FastAPI · SQLite（WAL + FTS5 trigram）· 零 LLM |
| 客户端 | Python（`shared_brain.client`，供 Hermes 与 CLI 复用）；TypeScript（`@shared-agent-brain/dsh-plugin`，供 DeepSeek Harness） |
| 部署 | `docker compose up` 自托管，监听 `127.0.0.1:8787`，TLS 由运维侧反代负责 |
| 鉴权 | 单共享 `BRAIN_TOKEN`（SHA-256 哈希存储）+ 乐观锁（`expected_version` → 409）+ 幂等键 |
| 阶段 | Phase 1（共享记忆）已实现；Phase 1b（Handoff）/ Phase 2（Skill 共享）待续 |

### 1.1 核心设计取舍

- **原生扩展点优先**：Hermes 走 `MemoryProvider` ABC/插件目录发现；DeepSeek 走 Cordis 插件 + `ctx.tools`。会话 remember 只通过各自本机可读的会话接口提取文本，不跨 agent 猜测远端存储。
- **CLI 降级为工具**：`amm` 只做配置 / 诊断 / 人工维护，不承载运行时 I/O。
- **Memory = 不可信引用数据，Skill = 可执行指令**：注入层强制分界，召回内容永远不提升为系统指令。
- **服务器零 LLM**：只存 / 查 / 搜，弱机（树莓派级）可跑。

## 2. 项目架构

```
┌──────────────────────────────────────────────────────────────┐
│  自托管 Shared Brain 服务器（零 LLM, FastAPI + SQLite + FTS5） │
│  Phase 1 REST: /v1/memories/*；后续 /handoff /skill           │
└───────────▲──────────────────────────────────▲──────────────┘
            │ REST + Bearer Brain-Token         │ REST + Bearer Brain-Token
┌───────────┴──────────────┐      ┌────────────┴──────────────┐
│ Hermes  amm-memory-      │      │ DeepSeek Harness Cordis 插件│
│ provider (官方 ABC)       │      │ · 每轮前注入引用资料         │
│ · prefetch() 召回         │      │ · brain_search/remember/    │
│ · on_memory_write 镜像   │      │   update/forget             │
│ · 4 个显式写工具         │      │ · 离线队列重放                │
└───────────┬──────────────┘      └─────────────────────────────┘
            │ 配置/诊断/人工维护
┌───────────┴──────────────┐
│ amm CLI（仅工具）         │
│ config / doctor / queue / memory │
└──────────────────────────┘
```

### 2.1 请求与数据流

**写入路径**（以 Hermes `brain_remember` 为例）：
1. 插件 `handle_tool_call("brain_remember", ...)` → `SharedBrainClient.remember()`
2. 客户端生成 `Idempotency-Key`，`POST /v1/memories`
3. 服务器 `authenticate()` 校验 Bearer token → `BrainStore.create_memory(payload, op_key, req_hash)`
4. `_idempotent()` 在 `BEGIN IMMEDIATE` 事务内：查 `applied_ops` 去重 → 内容哈希去重 → 插 `memories` + `memory_versions` + `memory_change_log` → 写 `applied_ops`
5. 网络断开时客户端捕获 `httpx.TransportError`，写入本地 `OfflineQueue`，后续 `flush_queue()` 用**同一幂等键**重放；仅 transport/5xx 自动重试，409/其他 4xx 进入 failed，刷新业务状态后才能显式重试

**召回路径**（以 DeepSeek 每轮前注入为例）：
1. `ctx.on('agent/pre-step')` 在 `step === 1` 提取用户文本 → `client.search(query, recallLimit)`
2. `GET /v1/memories/search?q=...&project_key=...`
3. `BrainStore.search_memories()`：≥3 字符走 FTS5 `MATCH` + `bm25()`；<3 字符降级 `LIKE`
4. 结果经 `render_untrusted_memories()` 包进 `<shared-memory-context trust="untrusted-reference-data">` 并 HTML 转义
5. 以 `shared-memory`/`reference` 来源消息注入到下游指令之前

## 3. 目录结构与模块职责

```
shared-agent-brain/
├── src/shared_brain/              # Python 服务端 + 客户端 + Hermes 插件
│   ├── __init__.py                # 包标识，__version__ = "0.1.0"
│   ├── api.py                     # FastAPI 应用工厂与路由
│   ├── cli.py                     # amm / amm-server 命令行入口
│   ├── client.py                  # 同步 HTTP 客户端 + 离线重试
│   ├── config.py                  # 客户端配置（0600 权限原子写）
│   ├── db.py                      # SQLite 持久化（BrainStore）+ schema
│   ├── errors.py                  # 领域异常层级
│   ├── models.py                  # Pydantic 请求模型与枚举
│   ├── queue.py                   # 客户端离线队列（SQLite）
│   ├── security.py                # token 哈希 / 内容哈希 / 幂等哈希 / 不可信渲染
│   └── hermes_plugin/__init__.py  # Hermes MemoryProvider 实现
├── integrations/deepseek-harness/ # DeepSeek Harness Cordis 插件（TS）
│   ├── src/{index,client,queue}.ts
│   ├── test/client.test.mjs
│   └── package.json / tsconfig.json
├── tests/                         # pytest 套件
├── pyproject.toml / Dockerfile / compose.yaml
├── README.md / IDEA.md / 共享大脑设计方案.md
└── CODE_WIKI.md / ASSESSMENT.md  # 本文档与评估报告
```

## 4. 核心模块详解

### 4.1 `api.py` — FastAPI 应用层

**职责**：路由定义、鉴权依赖、幂等键校验、领域异常 → HTTP 状态码映射。

| 函数 / 对象 | 说明 |
|---|---|
| `create_app(db_path=None, token=None) -> FastAPI` | 应用工厂。校验 `BRAIN_TOKEN`（≥24 字符）→ `BrainStore.initialize()` → 装配路由与异常处理。无 token 时**失败关闭** |
| `app_from_env()` | 从环境变量构造应用，供 uvicorn `--factory` |
| `authenticate(authorization)` | 依赖项：解析 `Bearer <token>`，`verify_token()` 常量时间比对 `users.token_hash`，失败 401 |
| `idempotency_key(value)` | 依赖项：校验 `Idempotency-Key` 头长度 8–200 |
| 路由 `POST /v1/memories` | 创建记忆，调 `store.create_memory` |
| 路由 `GET /v1/memories/search` | FTS5 检索，支持 scope/project_key/kind/source_agent/min_trust_level/limit 过滤 |
| 路由 `GET /v1/memories/changes` | 增量变更游标接口，返回 `next_cursor` |
| 路由 `GET/POST /v1/memories/{id}[/versions]` | 读取 / 新版本（乐观锁）/ 列版本 |
| 路由 `DELETE /v1/memories/{id}` | tombstone 删除（乐观锁） |
| 异常映射 | `NotFoundError→404` · `ConflictError→409` · `ValidationError→422` · `BrainError→400` |

模块级 `app`：若 `BRAIN_TOKEN` 未设，构造"未配置"应用，`/health` 返回 503。

### 4.2 `db.py` — `BrainStore` 持久化层

**职责**：所有 SQL、schema、事务、幂等、版本链、FTS5、变更日志。

| 成员 | 说明 |
|---|---|
| `SCHEMA` | 建表脚本：`users`（单行 id=1）/ `memories`（逻辑身份）/ `memories`（不可变版本行）/ `memory_versions_fts`（external-content + trigram）/ 3 个 AFTER 触发器同步 FTS / `applied_ops`（幂等）/ `memory_change_log`（游标） |
| `now_iso()` | UTC ISO8601 微秒 + `Z` 后缀 |
| `connect()` | `sqlite3.connect(timeout=10, isolation_level=None)` + `PRAGMA foreign_keys/busy_timeout` |
| `initialize(token)` | 建 schema；若 `users` 无行则插入 token 哈希；若已存在且 token 不符 → `RuntimeError`（防 token 漂移）；`chmod 0600` |
| `write_transaction()` | `BEGIN IMMEDIATE` → yield → `COMMIT`，异常 `ROLLBACK` |
| `_idempotent(op_key, req_hash, operation)` | 核心幂等：命中同 key 同请求 → 回放原结果；同 key 不同请求 → `ConflictError`；否则执行 + 记录 `applied_ops` |
| `create_memory()` | 内容哈希去重（命中返 200 `deduplicated=True`）；否则插 `memories`+`v1`+`change_log`，返 201 |
| `update_memory()` | 乐观锁：`current_version != expected_version` → `ConflictError`；插新版本行 + `supersedes_id` + 推进 `current_version` |
| `delete_memory()` | tombstone：已删则幂等返 200；乐观锁失败 → 409；否则写 `deleted_at`/`deleted_by_agent` |
| `search_memories()` | ≥3 字符：FTS5 `MATCH` 短语 + `bm25()` 排序；<3 字符：`LIKE` 降级；强制 `trust_level >= min`、project 隔离、排除 tombstone |
| `changes_since(cursor, project_key, limit)` | 基于 `memory_change_log.seq` 单调游标，join 当前版本快照，返回 tombstone 供离线对齐 |

**关键约束**：`memories` 表 `CHECK ((scope='project' AND project_key IS NOT NULL) OR scope!='project')`；`memory_versions` `UNIQUE(memory_id, version)`；`trust_level BETWEEN 0 AND 3`。

### 4.3 `models.py` — 请求模型

**职责**：Pydantic v2 校验，`str_strip_whitespace=True`。

| 类 | 关键字段 / 约束 |
|---|---|
| `MemoryScope` / `MemoryKind` | 枚举：global/user/project；fact/preference/decision/pitfall |
| `MemoryCreate` | scope/kind 必填；`project_key` ≤255；`title` 1–300；`content_text` 1–16384；`source_agent` 1–128；`trust_level` 0–3 |
| `MemoryUpdate` | `expected_version≥1`；`require_a_change` 校验器：title/content/kind/trust_level 至少一个非空 |
| `MemoryDelete` | `expected_version≥1` + `source_agent` |

### 4.4 `errors.py` — 领域异常

```
BrainError（基类，→400）
├── NotFoundError（→404）
├── ConflictError（→409，乐观锁 / 幂等冲突）
└── ValidationError（→422）
```

### 4.5 `security.py` — 安全与渲染

| 函数 | 说明 |
|---|---|
| `hash_token(token)` | `sha256(token)` 存储哈希 |
| `verify_token(token, expected_hash)` | `hmac.compare_digest` 常量时间比对，防时序攻击 |
| `content_hash(title, content_text)` | `sha256("title\0content")`，去重 / 变更检测 |
| `request_hash(method, path, payload)` | 规范化 JSON（`sort_keys` + 紧凑分隔符）后哈希，幂等键请求一致性校验 |
| `render_untrusted_memories(memories)` | **注入安全核心**：包进 `<shared-memory-context trust="untrusted-reference-data">` + 安全边界声明 + 逐条 HTML 转义（`html.escape`）的 metadata/title/content；空列表返 `""` |

### 4.6 `queue.py` — `OfflineQueue`（Python 客户端）

**职责**：SQLite 持久化离线写操作，供网络恢复后重放。

| 方法 | 说明 |
|---|---|
| `__init__(path)` | 建表 `pending_ops(op_key UNIQUE, method, path, payload_json, attempts, last_error)`；`chmod 0600` |
| `enqueue(method, path, payload, op_key?, error?)` | `INSERT OR IGNORE`（幂等入队） |
| `list(limit=100)` | 返回待重放列表（payload 反序列化） |
| `mark_failed(op_key, error)` | `attempts++`，`last_error` 截断 2000 字符 |
| `remove(op_key)` / `count()` | 重放成功后删除 / 计数 |

### 4.7 `client.py` — `SharedBrainClient`（Python）

**职责**：同步 HTTP 客户端，供 Hermes 插件与 CLI 共用；内置离线重试。

| 方法 | 说明 |
|---|---|
| `__init__(server_url, token, agent_id, project_key?, queue_path?, timeout=5.0, transport?)` | base_url + Bearer 头；`transport` 供测试注入 `httpx.MockTransport` |
| `_write(method, path, payload, op_key?, queue_on_failure=True)` | 捕获 `httpx.TransportError` → `queue.enqueue` 返 `{queued:True}`；4xx+ → `BrainClientError` |
| `remember()` / `update()` / `forget()` | 封装三个写端点，自动填 `source_agent`、`project_key` |
| `search()` | `GET /v1/memories/search`，返回 `items` 列表 |
| `prefetch(query, limit=5, min_trust_level=0)` | 召回 + `render_untrusted_memories()` 一体化，供 Hermes `prefetch()` 直接返回注入字符串 |
| `flush_queue(limit=100)` | 重放队列：成功删除、失败 `mark_failed` 并 break（保序） |

### 4.8 `config.py` — 客户端配置

| 函数 | 说明 |
|---|---|
| `default_config_path()` | `$AMM_CONFIG` 或 `~/.amm/config.json` |
| `load_config(path?)` | 读 JSON，不存在返 `{}` |
| `save_config(config, path?)` | 写 `.tmp` → `chmod 0600` → `replace` 原子替换 → 再 `chmod 0600` |

### 4.9 `cli.py` — `amm` / `amm-server`

**`amm`**（管理 / 诊断 / 维护）：
- `config <server_url> <agent_id> [--project-key] [--token]`：token 取自 `--token` / `BRAIN_TOKEN` / 隐藏输入；校验 ≥24 字符
- `doctor`：`health()` + agent_id + project_key + pending_ops 计数
- `queue {list|flush}`：查看 / 重放离线队列
- `memory {search|remember|update|forget}`：手工记忆操作

**`amm-server`**：`--host/--port/--db/--token`；无 token 时生成一次性 `secrets.token_urlsafe(32)` 并提示保存；`uvicorn.run(..., factory=True)`

### 4.10 `hermes_plugin/__init__.py` — `SharedBrainMemoryProvider`

实现 Hermes `MemoryProvider` ABC，由 entry point `hermes_agent.memory_providers` = `shared-brain` 发布，`register(ctx)` 注册。

| 钩子 | 实现 |
|---|---|
| `is_available()` | 检查 `server_url` 与 `token` 存在 |
| `initialize(session_id, **kwargs)` | 读配置 → 构造 `SharedBrainClient` → 静默 `flush_queue()` |
| `system_prompt_block()` | 明确声明召回为不可信引用数据 |
| `prefetch(query, session_id)` | 调 `client.prefetch()`；异常静默返 `""` |
| `sync_turn(...)` | **默认关闭**，不上传完整对话 |
| `get_tool_schemas()` | `brain_search` / `brain_remember` / `brain_update` / `brain_forget`（后两者 `required` 含 `expected_version`） |
| `handle_tool_call(name, args, **kwargs)` | 分派到 client 方法，错误返 JSON `{"error":...}` |
| `on_memory_write(action, target, content)` | 尽力镜像：`add/replace` → `remember()`；`remove` → 按内容匹配 + `forget()`；异常静默（不阻断 Hermes 内置写） |
| `save_config(values, hermes_home)` | 落盘 `$HERMES_HOME/shared-brain.json`（剔除 token），`0600` |
| `shutdown()` | 静默 `flush_queue()` + `close()` |

## 5. 集成模块：DeepSeek Harness 插件（TypeScript）

包名 `@shared-agent-brain/dsh-plugin`，ESM，Node ≥20，`strict` + `noUncheckedIndexedAccess`。

### 5.1 `src/index.ts` — Cordis 插件入口

- `Config`（schemastery）：`serverUrl` / `tokenEnv=BRAIN_TOKEN` / `agentId=deepseek-harness` / `projectKey` / `recallLimit=5` / `requestTimeoutMs=5000` / `queuePath`
- 注册 4 个 `defineTool`：`brain_search`（并发安全）/ `brain_remember` / `brain_update`（校验至少一个可变字段）/ `brain_forget`，输出统一为 JSON 字符串
- `ctx.on('agent/pre-step')`：仅在 `step === 1` 且下游未 reject 时，提取用户文本 → `client.search` → `renderUntrustedMemories` → 以 `shared-memory`/`reference` 来源 `UserMessage` 注入到下游指令**之前**；`AbortSignal` 可取消；失败仅 `logger.warn` 不阻断
- `ctx.on('session/event')`：`turn/end` 时异步 `flushQueue()`

### 5.2 `src/client.ts` — TS 客户端

- `SharedBrainClient`：基于 `globalThis.fetch` + `AbortSignal.timeout`；`BrainHttpError` 区分 4xx
- `write()`：非 `BrainHttpError` 的失败 → `queue.enqueue` 返 `{queued:true}`；`BrainHttpError` 直接抛（业务错误不入队）
- `search` / `remember` / `update` / `forget` / `flushQueue` 与 Python 端语义对齐
- `renderUntrustedMemories()`：XML 转义（`escapeXml`）+ 安全边界声明，与 Python 版一致

### 5.3 `src/queue.ts` — `JsonOperationQueue`

- JSON 文件持久化（`.tmp` → `rename` 原子写，`mode 0o600`）
- `enqueue`（opKey 去重）/ `list` / `remove` / `fail`

## 6. 数据模型

### 6.1 `memories`（逻辑身份）

| 列 | 类型 / 约束 |
|---|---|
| id | TEXT PK（uuidv4） |
| scope | global/user/project |
| kind | fact/preference/decision/pitfall |
| project_key | scope=project 时必填（CHECK） |
| current_version | INTEGER，乐观锁基准 |
| deleted_at / deleted_by_agent | tombstone |
| created_at / updated_at | ISO8601 |

### 6.2 `memory_versions`（不可变版本行）

自增 `rowid`（FTS5 external-content 映射）+ `id`（version uuid）+ `UNIQUE(memory_id, version)` + `supersedes_id` 版本链 + `content_hash` 去重 + `trust_level 0..3`。**更新 = 插入新 version + 推进 `current_version`**，旧版本保留供溯源。

### 6.3 `memory_versions_fts`

`fts5(content='memory_versions', content_rowid='rowid', tokenize='trigram')` + 3 触发器（AFTER INSERT/UPDATE/DELETE）。CJK 用 trigram 避免 `unicode61` 把连续中文当一词导致检索失效。

### 6.4 `applied_ops` / `memory_change_log`

- `applied_ops(op_key PK, request_hash, response_status, result_json, created_at)`：幂等回放
- `memory_change_log(seq INTEGER PK AUTOINCREMENT, memory_id, action, source_agent, changed_at)`：单调游标，离线同步依据

## 7. 依赖关系

### 7.1 Python 运行依赖

| 包 | 用途 |
|---|---|
| `fastapi>=0.116,<1` | Web 框架 |
| `httpx>=0.28,<1` | 客户端 HTTP（含 `MockTransport` 供测试） |
| `pydantic>=2.8,<3` | 请求模型校验 |
| `uvicorn[standard]>=0.35,<1` | ASGI server |

### 7.2 Python 开发依赖

`pytest>=8.3,<9`、`pytest-cov>=6,<7`

### 7.3 TS 依赖

- peer：`@deepseek-ai/cordis ^4.0.1`、`@deepseek-ai/dsh-{agent,llm,session,tools} ^0.1.0-rc.8`
- 运行：`@deepseek-ai/schemastery ^3.18.1`
- 开发：`typescript ^5.9.0`、`@types/node ^24`

### 7.4 内部依赖图

```
api.py ─┬─ db.py ── security.py, errors.py
        ├─ models.py
        └─ security.py
client.py ── queue.py, security.py
cli.py ── client.py, config.py
hermes_plugin ── client.py, config.py
dsh/index.ts ── client.ts, queue.ts
```

### 7.5 Entry Points（pyproject.toml）

- `[project.scripts]`：`amm = shared_brain.cli:main`、`amm-server = shared_brain.cli:server_main`
- `[project.entry-points."hermes_agent.memory_providers"]`：`shared-brain = shared_brain.hermes_plugin:register`

## 8. 项目运行方式

### 8.1 Docker（推荐自托管）

```bash
export BRAIN_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose up --build
# 监听 127.0.0.1:8787；healthcheck 命中 /health；数据卷 brain-data:/data
```

### 8.2 本地开发

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
BRAIN_TOKEN="$BRAIN_TOKEN" .venv/bin/amm-server --host 127.0.0.1
```

### 8.3 CLI 配置与诊断

```bash
amm config http://127.0.0.1:8787 hermes --project-key my-project
amm doctor
amm memory remember "Runtime" "This project uses Python 3.12"
amm memory search "Python"
```

### 8.4 Hermes 集成

安装本 Python 包到 Hermes 同环境（自动发布 entry point），或通过环境变量配置：

```bash
export BRAIN_URL=http://127.0.0.1:8787
export BRAIN_TOKEN=...
export BRAIN_AGENT_ID=hermes
export BRAIN_PROJECT_KEY=my-project
```

在 Hermes 选 `shared-brain` 为唯一激活的外部 memory provider（内置 `MEMORY.md`/`USER.md` 不受影响）。

### 8.5 DeepSeek Harness 集成

```bash
cd integrations/deepseek-harness && npm install && npm run check && npm run build
```

加入 Harness composition：

```yaml
- package: "@shared-agent-brain/dsh-plugin"
  config:
    serverUrl: "http://127.0.0.1:8787"
    tokenEnv: "BRAIN_TOKEN"
    agentId: "deepseek-harness"
    projectKey: "my-project"
    recallLimit: 5
    requestTimeoutMs: 5000
```

## 9. API 概览

所有 `/v1` 路由需 `Authorization: Bearer <token>`；写操作另需 `Idempotency-Key`（8–200 字符）。OpenAPI 在 `/docs`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 无需鉴权，返 `{status, version}`；未配置时 503 |
| POST | `/v1/memories` | 创建（内容去重 200 / 新建 201） |
| GET | `/v1/memories/search` | FTS5/LIKE 检索，多过滤 |
| GET | `/v1/memories/changes` | 游标增量，含 tombstone，返 `next_cursor` |
| GET | `/v1/memories/{id}` | 读取（默认排除 tombstone） |
| GET | `/v1/memories/{id}/versions` | 版本链（version DESC） |
| POST | `/v1/memories/{id}/versions` | 新版本（乐观锁） |
| DELETE | `/v1/memories/{id}` | tombstone（乐观锁） |

## 10. 测试

### 10.1 Python（pytest）

```bash
.venv/bin/python -m pytest
```

| 文件 | 覆盖点 |
|---|---|
| `test_api.py` | 鉴权必需、创建/检索/项目隔离、幂等+内容去重、乐观版本+tombstone+变更游标、project_key 校验、空更新校验、纯空白拒绝 |
| `test_store.py` | token 不符失败关闭、DB/队列文件 0600 |
| `test_security.py` | 恶意记忆被转义、安全边界标记、无 `<system>` 泄漏 |
| `test_client_queue.py` | 离线入队 + 同一幂等键在线重放 |
| `test_hermes_plugin.py` | 4 个版本安全工具 schema + system prompt 含"untrusted" |

### 10.2 TypeScript

```bash
cd integrations/deepseek-harness
npm run check   # tsc --noEmit
npm run build
npm test        # node --test
```

`client.test.mjs`：不可信标记转义、离线入队 + 原幂等键重放。

## 11. 安全边界与设计取舍（要点）

1. **注入分界**：召回内容永远是 HTML 转义的引用 data，绝不提升为系统指令；`<system>` 等标签被转义；Python/TS 两端实现一致并有专门测试。
2. **鉴权**：共享 token 哈希存储，常量时间比对；token 不符则拒绝启动；传输 TLS 交给反代。
3. **并发**：乐观锁 `expected_version` → 409；`BEGIN IMMEDIATE` 事务；幂等键防重复（同 key 同请求回放，同 key 不同请求 409）。
4. **删除**：tombstone 可同步给离线客户端；已删再删幂等返 200。
5. **离线**：客户端 SQLite/JSON 队列，重放保留原幂等键；DeepSeek 每 `turn/end` 触发 flush。
6. **文件权限**：DB / 队列 / 配置均 `0600`。
7. **明确不做**：raw 会话上传、平台目录嗅探、SSH 自动装机、Postgres 双后端、MCP 目录导入。

## 12. 关键决策日志（摘自设计方案）

| 决策 | 选择 | 理由 |
|---|---|---|
| 接入方式 | 原生扩展点 | 稳定、官方支持，不猜格式 |
| CLI 角色 | 配置/诊断/维护 | 运行逻辑交给原生插件 |
| 内容存储 | 内联明文 + FTS5(trigram) | 弱机可搜、CJK 可检索 |
| 冲突 | 乐观锁 version→409 | 跨机真实并发 |
| 鉴权 | 共享 token + 配置身份 | MVP 单用户 |
| 删除 | tombstone | 可同步离线 |
| 注入安全 | memory=引用 data、skill=指令 | 防注入/供应链 |
| 服务器 LLM | 零 | 弱机约束 |
| 部署 | docker compose 自托管 | 不做 SSH 自动装机 |
