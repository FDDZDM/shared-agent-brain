# Shared Brain — 项目评估报告

> 评估时间：2026-08-21 · 评估对象：`shared-agent-brain@0.1.0`（Phase 1 MVP）
> 评估方法：源码静态审查 + 全量测试执行（Python pytest + TS check/build/test）+ 覆盖率采集

## 1. 测试结果

### 1.1 Python 套件

```
.venv/bin/python -m pytest
============================== 12 passed in 0.18s ==============================
```

全部通过，无失败 / 跳过。

### 1.2 TypeScript 套件

```
npm run check   # tsc --noEmit        ✅ 0 错误
npm run build   # tsc                 ✅ 产物生成
npm test        # node --test         ✅ 2 passed / 0 fail
```

`strict` + `noUncheckedIndexedAccess` 下类型检查零错误。

### 1.3 覆盖率（Python）

| 模块 | Stmts | Cover | 说明 |
|---|---|---|---|
| `errors.py` / `models.py` / `__init__.py` | — | 100% | 充分 |
| `security.py` | 29 | 97% | 仅空列表早返分支未覆盖 |
| `queue.py` | 39 | 95% | `mark_failed` 路径未覆盖 |
| `api.py` | 87 | 92% | 未配置 app、unconfigured_health 等边界 |
| `db.py` | 193 | 90% | 部分搜索分支与异常路径 |
| `client.py` | 77 | 64% | `search/update/forget/prefetch` 等线上方法未直测（经 API 间接覆盖） |
| `config.py` | 21 | 38% | 配置读写经 CLI 路径，CLI 未测 |
| `hermes_plugin` | 102 | 36% | 仅 schema/system_prompt 被测；运行时钩子未测（依赖 Hermes 运行时） |
| `cli.py` | 91 | 0% | 完全未测 |
| **TOTAL** | 684 | **67%** | — |

**结论**：核心服务器（api/db/security/queue/models）覆盖良好（90%+）；CLI 与 Hermes 插件运行时是覆盖盲区，建议补足。

## 2. 架构评估

### 2.1 优点

- **定位收敛、职责清晰**：服务器（零 LLM 存/查/搜）+ 原生插件（运行时 I/O）+ CLI（配置/诊断/维护）三层分离，无职责混淆。比初版"通用 CLI + 目录嗅探"方案显著更稳健。
- **数据模型设计扎实**：`memories`（逻辑身份）+ `memory_versions`（不可变版本行 + `supersedes_id` 链）+ tombstone + 单调游标 `memory_change_log`，支撑版本溯源、离线对齐、增量同步。
- **并发与一致性严谨**：`BEGIN IMMEDIATE` + 乐观锁 `expected_version→409` + 持久化幂等键（同 key 同请求回放 / 同 key 不同请求 409）+ 内容哈希去重，构成完整并发安全闭环。
- **安全边界是真正的核心亮点**：Python/TS 两端 `render_untrusted_memories` 实现一致，召回内容强制 HTML 转义 + 显式 `untrusted-reference-data` 标记 + 安全边界声明，并有专门测试验证 `<system>` 注入被阻断。Memory=引用 data、Skill=可执行指令 的分界是本项目的最关键取舍。
- **跨平台一致**：Python 端 `OfflineQueue`（SQLite）与 TS 端 `JsonOperationQueue`（JSON 原子写）语义对齐，均保留原幂等键重放。
- **部署门槛低**：单 SQLite + FastAPI + docker compose，弱机可跑；Dockerfile 用非 root uid 10001，最小镜像。

### 2.2 不足 / 风险

| 项 | 说明 | 严重度 |
|---|---|---|
| **单共享 token 鉴权** | MVP 单用户模型；`source_agent` 仅是配置身份（血缘溯源），非鉴权手段。多 agent / 多用户场景需按 agent 拆 token 权限（设计文档已列入后续） | 中（MVP 可接受） |
| **无速率限制** | `/v1/*` 无 rate limit / 请求体大小上限仅靠 Pydantic 字段长度。token 泄露后无额外缓解 | 中 |
| **CLI 0% 测试覆盖** | `cli.py` 完全未测；`config.py` 38%。配置写入与 `doctor/queue/memory` 子命令无回归保护 | 中 |
| **Hermes 运行时未测** | `hermes_plugin` 36%——仅 schema/prompt 被测，`prefetch/handle_tool_call/on_memory_write/shutdown` 未测（需 Hermes 运行时，可注入 mock 补足） | 中 |
| **`on_memory_write` remove 镜像脆弱** | 按内容全文相等匹配 + `forget`，任意空白差异即漏；设计文档已明确 `add` 可靠、replace/remove 需走显式工具 | 低（已记录限制） |
| **`applied_ops` 无清理** | `result_json` 全量留存，长期运行可能膨胀；无 TTL / 清理任务 | 低 |
| **无 CI 配置** | 仓库未见 `.github/workflows` 等 CI 文件，测试仅本地手动跑 | 低 |
| **FTS5 短语查询语义** | `search_memories` 将整 query 包成 `"..."` 短语 MATCH，多词查询按精确短语匹配而非 OR/AND，可能漏召回 | 低（可后续改 |
| **`requires-python>=3.9` vs Dockerfile 3.12** | 开发环境为 3.9.6（本机测试），Docker 用 3.12；`str | None` 等 3.10+ 语法未使用（用 `Optional`），兼容性 OK | 信息项 |

## 3. 代码质量

- **风格一致**：`from __future__ import annotations`、类型注解完整、Pydantic v2 `model_dump(mode="json")`、SQL 用参数化（无注入）、`html.escape`/`escapeXml` 双端对齐。
- **事务正确**：`isolation_level=None` + 显式 `BEGIN IMMEDIATE`，写事务异常即 `ROLLBACK`，`_idempotent` 的幂等记录与业务在同一事务内（冲突 / notfound 异常会回滚 applied_ops，不缓存错误响应——可重试，语义正确）。
- **TS 严格模式**：`strict` + `noUncheckedIndexedAccess`，`PeerDependencies` 与 `devDependencies` 一致，避免幽灵依赖。
- **可读性高**：单文件职责单一，`SCHEMA` 集中、`_current_select()` 复用、`_row_to_memory()` 统一映射。

## 4. 安全评估

| 维度 | 状态 |
|---|---|
| 注入防护（prompt injection via memory） | ✅ 强：双重转义 + 边界声明 + 专项测试 |
| 鉴权 | ✅ token 哈希存储 + `hmac.compare_digest` 常量时间 + 失败关闭 + 重启 token 校验 |
| 传输安全 | ⚠️ 服务器自身无 TLS，文档明确交由反代；需运维侧落实 |
| 并发安全 | ✅ 乐观锁 + 幂等键 + IMMEDIATE 事务 |
| 文件权限 | ✅ DB / 队列 / 配置均 0600；容器非 root |
| 密钥管理 | ✅ token 不入 shell 参数 / 版本控制；CLI 隐藏输入；`save_config` 剔除 token 落盘 |
| 速率限制 / 滥用 | ❌ 无 |

## 5. 符合设计文档验收标准（Phase 1）

| 验收点 | 实测 |
|---|---|
| Hermes 写入能被 DeepSeek 搜到 | ✅ `test_api.py::test_create_search_and_project_isolation` |
| DeepSeek 记忆能被 prefetch 注入 | ✅ `client.prefetch` + `render_untrusted_memories`（TS `agent/pre-step` 注入路径已实现） |
| 不同项目不串库 | ✅ project_key 隔离测试 |
| 乐观锁：旧客户端不能覆盖新版本 | ✅ `stale-update → 409` 测试 |
| 删除用 tombstone 可同步离线 | ✅ `changes` 返回 tombstone |
| 恶意记忆只能作引用数据 | ✅ `test_security` + TS 测试 |
| 网络断开本地排队、恢复幂等重试 | ✅ Python + TS 离线重放测试 |

Phase 1 最小闭环验收标准**全部达成**。

## 6. 改进建议（按优先级）

1. **补 CLI 测试**：用 `argparse` 解析 + `monkeypatch` 注入 mock client，覆盖 `config/doctor/queue/memory` 子命令与 token 长度校验。
2. **补 Hermes 插件运行时测试**：注入 mock `SharedBrainClient` 测 `prefetch/handle_tool_call/on_memory_write`（含 remove 漏匹配场景）。
3. **加 CI**：GitHub Actions 跑 `pytest` + `npm run check/build/test`，防止回归。
4. **速率限制 / 请求体上限**：在 `authenticate` 依赖加 per-token rate limit（如 `slowapi`）。
5. **`applied_ops` 清理**：定时或按行数阈值清理过旧幂等记录。
6. **FTS 多词召回**：多词 query 改为 `term1 OR term2` 或拆分后 `bm25` 聚合，提升召回率。
7. **多 agent 鉴权前置**：为 Phase 1b/2 设计按 agent 的 token 与权限矩阵。

## 7. 总体评分

| 维度 | 评分 | 说明 |
|---|---|---|
| 架构设计 | 9 / 10 | 定位收敛、职责清晰、扩展点选择正确、数据模型扎实 |
| 代码质量 | 8 / 10 | 风格一致、类型完整、事务正确；个别边界注释可加强 |
| 安全设计 | 8.5 / 10 | 注入分界与幂等是亮点；缺速率限制与服务器 TLS |
| 测试覆盖 | 6.5 / 10 | 核心路径 90%+，但 CLI/Hermes 运行时盲区明显 |
| 文档完整性 | 8 / 10 | README + 设计方案清晰；缺 API 字段级示例 |
| 工程化 | 6 / 10 | 无 CI、无清理任务；Docker/compose 完备 |
| **综合** | **8 / 10** | 与设计文档自评一致；Phase 1 MVP 完成度高，主路径可投入自托管试用，需补测试与 CI 后再扩展 |

**一句话结论**：一个定位清晰、安全边界严谨、最小闭环可跑通的高质量 MVP；核心服务器与跨平台客户端实现扎实，主要短板在 CLI / 插件运行时的测试覆盖与工程化（CI、限流、清理），均属可在 Phase 1b 前补齐的可控风险。
