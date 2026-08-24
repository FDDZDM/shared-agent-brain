# Shared Brain Remediation Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 按整改清单先完成四项最影响可用性的 P0：对话输出通道、自然语言召回、会话项目隔离、会话同步 revision；随后补齐离线复合同步、会话可访问性、doctor 鉴权与可靠性基础设施。

**Architecture:** 服务器继续保持零 LLM；客户端负责本机会话读取与摘要生成，服务器负责结构化记忆、会话元数据、权限和同步状态。所有用户可见命令结果使用平台正式消息注入接口；stdout/stderr 仅用于诊断。会话以 `project_key + agent_id + device_id/client_instance_id + session_id` 隔离，revision/hash 作为同步真相而不是时间戳。

**Tech Stack:** Python 3.11、FastAPI、SQLite/FTS5、httpx、pytest；TypeScript、DSH Cordis 插件、Node test runner。

---

## 当前基线与约束

- 当前工作区有 4 个未提交文件：`README.md`、`integrations/deepseek-harness/src/index.ts`、`integrations/deepseek-harness/test/ux.test.mjs`、`src/shared_brain/hermes_plugin/__init__.py`。实施时不得覆盖或回退这些用户改动。
- 当前验证已实测通过：Python `env -u PYTHONPATH .venv/bin/python -m pytest -q`（24 passed）；DSH `npm run check && npm run build && npm test`（13 passed）。
- 现有服务器已经有 `project_key`（记忆级别）和 `sessions` 表，但 sessions 只有 `(agent_id, session_id)` 主键与 `synced_at`，不满足项目隔离和 revision 同步。
- 现有 DSH 命令结果已经尝试使用 `agent.steer(createUserMessage(... source.kind=plugin, plugin=shared-brain, form=notice))`；Hermes 已有 `inject_message` 包装，但仍需以测试证明没有重复默认渲染，并清理文档/实现矛盾。
- 不提交、不 push；每个小任务完成后只运行测试和报告差异，提交由用户决定。

## 实施批次

### Batch 0：保护基线与输出通道（P0）

1. **盘点并锁定消息契约**
   - 检查 `src/shared_brain/hermes_plugin/__init__.py`、`integrations/deepseek-harness/src/index.ts`、相关测试和 README。
   - 定义统一来源字段：`source.kind=plugin`、`source.plugin=shared-brain`、`source.form=notice`，另加稳定 command/source summary；业务错误进入会话，异常详情仅日志。
   - 明确“写入会话后是否进入模型上下文”的配置开关，默认保持当前可回看行为，关闭时仅展示 UI notice 或平台等价通道。

2. **DSH 命令消息回归测试（先 RED，再实现）**
   - Test: `integrations/deepseek-harness/test/ux.test.mjs`。
   - 覆盖 `/brain search|remember|update|forget|test|help` 成功和失败；断言 `steer` 的消息结构、无 stdout 业务结果、取消是 silent success、注入成功后 handler 不重复返回。
   - 关注多个插件共同处理 `agent/pre-step` 时不得把命令 notice 当召回资料重复注入。

3. **Hermes 命令消息回归测试与实现**
   - Test: `tests/test_hermes_plugin.py`，实现：`src/shared_brain/hermes_plugin/__init__.py`。
   - 用 fake context 验证 `inject_message` 成功时返回空值、失败时返回降级文本；验证 help、成功、错误、取消的 source/role；日志不包含 token 或完整敏感内容。
   - 删除“结果不进入模型历史”与“结果写入会话”的冲突文档，统一在 `README.md` 和设计文档中说明开关语义。

4. **结构化 ToolResult 边界**
   - 检查 Hermes 工具注册适配层与 DSH 工具注册返回类型；工具结果使用结构化对象（items/count/metadata/error code），仅在平台渲染层生成文本。
   - Test: `tests/test_hermes_plugin.py`、DSH client/plugin tests；不得改变现有 `brain_*` 工具名。

**Batch 0 验收：** 两端命令结果只出现在正式会话消息流；重开/回放可见；终端无重复业务结果；错误可见但敏感详情只在日志；普通/Plan/新会话路径均有测试。

### Batch 1：自然语言召回（P0）

1. **查询规范化与上限**
   - 修改：`src/shared_brain/client.py`、`integrations/deepseek-harness/src/index.ts`（自动召回入口）、必要时 `src/shared_brain/api.py`。
   - 只取当前最后一条用户消息；限制发送给服务端的查询长度不超过 1000 字符，并记录截断元数据。
   - 中文、英文、中英混合、标点、代码标识符分别建立测试样例。

2. **服务端 OR/AND/BM25 多路搜索**
   - 修改：`src/shared_brain/db.py`、`src/shared_brain/api.py`。
   - 不再把完整用户句子拼成精确 FTS phrase；对安全 token 做 OR/AND 查询并保留 BM25；无结果时按短语、拆词、LIKE 逐级降级。
   - 返回 `score`、`matched_terms`、`match_strategy`、`reason`，客户端渲染可解释召回原因。
   - 保持 project/trust/deleted 过滤在每条召回路径一致。

3. **质量测试与预算**
   - Test: `tests/test_api.py`、新增 `tests/test_recall_quality.py`、DSH/Hermes prefetch tests。
   - 验收自然问句：记忆“项目使用 PostgreSQL 16”对提问“这个项目现在用的是什么数据库？”在 Top 5；统计 Recall@5、无结果率、平均数量。
   - 增加注入字符/token 上限，超限按相关度截断并在 metadata 标明。

### Batch 2：会话项目隔离与可访问性（P0）

1. **schema migration 基础**
   - 新增：`src/shared_brain/migrations.py` 或等价 migration 模块；修改：`src/shared_brain/db.py`。
   - 新增 `schema_version`，为旧数据库提供顺序迁移；初始化前检查现状、备份提示/恢复说明。
   - sessions 增加 `project_key`、`device_id`（或 `client_instance_id`），唯一键改为项目+agent+设备+session。

2. **API/客户端全链路传递隔离字段**
   - 修改：`src/shared_brain/models.py`、`src/shared_brain/api.py`、`src/shared_brain/client.py`、DSH `src/client.ts`、Hermes session hooks。
   - 所有 sessions 查询、agents 统计、upsert、mark synced 必须带 project/device 过滤；不得依赖调用方自由省略项目。
   - 旧会话迁移采用显式默认项目并记录迁移策略，不能静默跨项目可见。

3. **本地可读会话过滤**
   - 修改：两端命令选择器与会话读取入口。
   - 服务器候选仅作目录；客户端在展示/上传前用本机 session/export API 再验证可读性，其他机器/Agent 会话提前排除并解释原因。
   - 将“本机会话上传”与“共享记忆管理”在命令流程和文案上分开。

**Batch 2 验收：** 相同 session_id 在不同项目/设备互不可见；agent 统计不串项目；选择器不展示本地无法读取的会话；旧库升级测试通过。

### Batch 3：revision 同步与离线复合操作（P0）

1. **同步状态模型**
   - sessions 增加：`content_revision`、`synced_revision`、`content_hash`、`last_content_hash`、`synced_memory_id`、`last_sync_error`（命名统一后以实际 schema 为准）。
   - 新内容上报时 revision 单调递增；上传成功只把对应 revision 标记为 synced；上传后再变化显示“同步后有更新”。
   - API 返回稳定状态：`never_synced|synced|changed|failed`。

2. **摘要写入+同步标记原子语义**
   - 设计服务端幂等复合 endpoint 或同一事务命令：记忆写入成功并绑定 `synced_memory_id/revision` 才完成同步。
   - 重复重放返回原业务结果，不生成重复摘要；409 不阻塞后续独立任务。
   - Test: `tests/test_sessions.py`、`tests/test_client_queue.py`、集成测试。

3. **离线队列完整业务意图**
   - 修改：`src/shared_brain/queue.py`、`integrations/deepseek-harness/src/queue.ts`（若存在）、两端 client。
   - 队列记录 operation type、session identity、revision/hash、摘要/记忆关联，而不是只有 HTTP method/path/body。
   - 加 `max_attempts`、指数退避、`next_retry_at`、可重试/不可重试分类；提供 list/retry/skip/delete 管理接口。
   - 并发、崩溃恢复、部分成功、重复重放测试；队列列表默认隐藏完整 payload。

### Batch 4：doctor、鉴权与 API 契约（P1）

1. **真实鉴权检查**
   - 新增 `/v1/whoami` 或 `/v1/capabilities`，返回服务版本、schema、project 权限信息，不泄露 token。
   - 修改 `src/shared_brain/cli.py`：doctor 分项输出网络、auth、database、project、FTS、queue，支持人类可读与 `--json`，稳定退出码。
   - Test: `tests/test_cli.py`（当前缺少/覆盖不足），错误 token、服务不可达、schema 不匹配分别验证。

2. **凭证安全演进**
   - 从单共享 token 迁移到 client credential/token scope；服务端根据凭证确定 source_agent，禁止请求覆盖。
   - 支持只读/写入/管理/删除权限、吊销/轮换/宽限期、鉴权审计；日志和自检报告脱敏。

3. **API schema 与分页**
   - 使用 Pydantic 明确定义响应和错误结构；列表统一 cursor/`next_cursor`/`has_more`；常用过滤建索引。
   - 对 FTS rebuild、数据库大小、应用操作增加管理接口/命令。

### Batch 5：迁移、备份、可观测性与工程质量（P1/P2）

- `schema_version` 顺序 migration、在线一致性 SQLite backup、WAL/checkpoint/VACUUM、恢复演练与 JSONL 导出。
- applied_ops/tombstone/历史版本/失败队列保留策略和预览审计。
- `/health/live` 与 `/health/ready`；结构化日志、request ID、metrics；严格分流用户消息和诊断日志。
- CI：pytest+coverage、TS check/test/build、Ruff、Pyright/mypy、依赖/镜像扫描、pre-commit、wheel/npm smoke test。
- LICENSE、CHANGELOG、支持版本矩阵、升级指南、README 拆分；更新 `ASSESSMENT.md`，解释/删除 `IDEA.md`。
- 以核心路径为先，将 Python 覆盖率从当前约 57% 提升到 80%+，优先 CLI、client、Hermes、集成测试。

## 每个批次的执行纪律

1. 先读取最新文件和 `git diff`，不覆盖用户改动。
2. 每个行为先写针对性失败测试，再做最小实现，再运行局部测试。
3. 局部绿后运行 canonical 全套：
   - `env -u PYTHONPATH .venv/bin/python -m pytest -q`
   - `cd integrations/deepseek-harness && npm run check && npm run build && npm test`
4. 运行真实 FastAPI/TestClient 集成测试；若涉及平台接线，再运行 fake DSH/Hermes 命令脚本。
5. 每批结束检查 `git diff --check`、`git status --short`，并记录未提交用户改动未被改变。
6. 不在未验证的情况下声称完成；不把计划中的验收当成已完成验收。

## 风险与待确认决策

- Hermes/DSH 官方消息 API 能否做到“展示但不进入模型上下文”需要先查当前宿主版本 API；若不存在，需明确采用默认进入历史或仅 UI notice 的产品取舍。
- 会话本地读取 API 不是 Shared Brain 服务器职责，必须分别确认 DSH 与 Hermes 的官方 export/session API；内部 DB 兼容层只能标为实验性。
- 旧 sessions 没有 project/device 信息，迁移默认值会影响可见性；应提供 dry-run/备份并让用户确认默认项目，而不是猜测。
- 摘要生成是客户端 LLM 行为；服务端零 LLM 不代表上传流程零 LLM，隐私确认必须在客户端完成。
- 第一批实现前需要用户批准上述拆批顺序，尤其是输出“可回看但不进模型上下文”开关的具体宿主能力。
