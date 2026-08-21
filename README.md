# Shared Brain

> **面向开放 Agent Harness 的自托管共享记忆后端**，首发原生支持 **Hermes** 与 **DeepSeek Harness**。
> 服务器零 LLM、弱机可跑；客户端走官方扩展点（MemoryProvider ABC / Cordis 插件），不靠目录嗅探和格式猜测。

Phase 1 已实现：

- 版本化 `global` / `user` / `project` 三种 scope 的记忆，项目隔离。
- SQLite **FTS5 trigram** 全文检索（中英混排，`content_text` 内联存储 + 触发器同步）。
- **乐观锁**（`expected_version` 不符 → HTTP 409）、**tombstone 删除**、增量变更流。
- 幂等键（`Idempotency-Key`）与**离线客户端队列**（断网排队、恢复幂等重试）。
- 召回注入自带**不可信数据边界**（`untrusted-reference-data` 标记，记忆只作引用、永不提升为系统指令）。
- Hermes `MemoryProvider` 插件 + DeepSeek Harness Cordis 插件（各含 4 个 `brain_*` 工具）。

Handoff（结构化交接）与 Skill 共享为 Phase 1b / Phase 2；raw 会话备份明确不做。
详细架构与决策日志见 [`共享大脑设计方案.md`](共享大脑设计方案.md)。

---

## 总体架构

```
┌───────────────────────────────────────────────┐
│  Shared Brain 服务器 (零 LLM, FastAPI+SQLite)  │
│  REST: /v1/memories ... + FTS5(trigram)       │
└──────────▲──────────────────────────▲─────────┘
           │ REST + Bearer token       │ REST + Bearer token
┌──────────┴──────────┐   ┌────────────┴──────────┐
│ Hermes              │   │ DeepSeek Harness      │
│ amm-memory-provider │   │ @shared-agent-brain/  │
│ (MemoryProvider ABC)│   │ dsh-plugin (Cordis)   │
└─────────────────────┘   └───────────────────────┘
```

---

## 一、部署服务器

### 1.1 本机开发

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
BRAIN_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  .venv/bin/amm-server --host 127.0.0.1
```

### 1.2 服务器部署（Docker Compose，含中国大陆镜像源）

前置：服务器有 docker + compose v2。

```bash
# 1) 同步代码（macOS 无 rsync 时用 tar 管道）
tar --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='.pytest_cache' --exclude='.coverage' --exclude='.DS_Store' \
    --exclude='integrations/deepseek-harness/node_modules' \
    -czf - . | ssh root@SERVER 'rm -rf /root/shared-brain && mkdir -p /root/shared-brain && tar -xzf - -C /root/shared-brain'

# 2) Docker Hub 被墙 → 配镜像源（改完重启 docker，先确认其他容器 RestartPolicy）
#    /etc/docker/daemon.json 的 registry-mirrors 加：
#    ["https://docker.1ms.run", "https://docker.m.daocloud.io", "https://dockerproxy.net"]
#    systemctl restart docker

# 3) 生成 token 写 .env（勿进 git）
ssh root@SERVER 'umask 077; printf "BRAIN_TOKEN=%s\n" "$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")" > /root/shared-brain/.env'

# 4) 远程访问时放开端口绑定（默认只绑 127.0.0.1）
#    .env 加：BRAIN_BIND_IP=0.0.0.0

# 5) 构建启动（中国大陆必须传清华 pip 源，否则 pip 卡死）
ssh root@SERVER 'cd /root/shared-brain && export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple && docker compose up -d --build'
```

服务监听 `8787`；数据在 docker 命名卷 `brain-data`（`/data/shared-brain.db`），重建容器不丢。
对外使用前建议挂 HTTPS 反向代理。

### 1.3 验证服务器

```bash
BASE=http://SERVER:8787; TOKEN=$(ssh root@SERVER 'grep BRAIN_TOKEN /root/shared-brain/.env | cut -d= -f2')
curl -s $BASE/health                                   # {"status":"ok",...}
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/v1/memories/search?q=test"   # 401（无 token）
curl -s -X POST $BASE/v1/memories -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -H "Idempotency-Key: smoke-1" \
  -d '{"scope":"project","kind":"fact","project_key":"demo","title":"部署验证","content_text":"服务器部署成功 中文检索测试","source_agent":"smoke","trust_level":1}'   # 201
curl -s -G $BASE/v1/memories/search --data-urlencode "q=中文检索" \
  --data-urlencode "project_key=demo" -H "Authorization: Bearer $TOKEN"        # 命中
```

---

## 二、Hermes 接线（amm-memory-provider）

> 机制：Hermes 的 memory provider 靠**目录扫描**发现（bundled `plugins/memory/<name>/` 或 `$HERMES_HOME/plugins/<name>/`），不是 pip entry point。且 **single-select**：只能激活一个外部 provider；内置 `MEMORY.md`/`USER.md` 不受影响。

```bash
# 1) 装包进 Hermes venv
~/.hermes/hermes-agent/venv/bin/pip install -e ~/Projects/shared-agent-brain

# 2) 建 provider 薄包装目录（指向已装包）
#    ~/.hermes/plugins/shared-brain/__init__.py:
#      from shared_brain.hermes_plugin import SharedBrainMemoryProvider, register
#      __all__ = ["SharedBrainMemoryProvider", "register"]
#    ~/.hermes/plugins/shared-brain/plugin.yaml:
#      name: shared-brain / version / description / hooks: [on_session_end]

# 3) 配置 ~/.hermes/shared-brain.json（token 不放这里）
#    {"server_url": "http://SERVER:8787", "agent_id": "hermes-mac", "project_key": "my-project"}

# 4) token 进 ~/.hermes/.env（0600）
#    BRAIN_TOKEN=<BRAIN_TOKEN>

# 5) 激活（用官方命令，勿手改 config.yaml）
hermes config set memory.provider shared-brain
hermes memory status   # Provider: shared-brain / available ✓
```

激活后**从下一个新会话生效**。provider 每轮 `prefetch(query)` 按 query + `project_key` 召回注入；`on_memory_write` 自动镜像内置记忆写入；暴露工具：

- `brain_search(query, limit)` — 搜索共享记忆
- `brain_remember(title, content, scope, kind, trust_level)` — 存一条事实/偏好/决策/坑
- `brain_update(memory_id, expected_version, ...)` — 乐观锁更新
- `brain_forget(memory_id, expected_version)` — tombstone 删除

**斜杠命令**（与 DSH 端同名同义，全端统一词汇）：Hermes 的 memory provider 路径本身不支持注册命令（`kind=exclusive` 路由限制），需在插件 `plugin.yaml` 显式声明 `kind: standalone` 并 `hermes plugins enable shared-brain`，让通用 PluginManager 加载（`register()` 内 hasattr 双守卫兼容两条加载路径）。启用后新会话可用：

```text
/brain search <query>                        # 搜索共享记忆
/brain remember <title> | <content>          # 存一条事实
/brain update <id> <expected_version> | <new content>   # 乐观锁更新
/brain forget <id> <expected_version>        # tombstone 删除
/brain test [quick]                          # 全链路自检（T1-T12），会话窗口显示报告
/brain help                                  # 命令说明书
```

与 DSH 端完全同构（单一 `/brain` + 子命令，子命令与 `brain_*` 工具一一对应，命令面与工具面不撞名）。

---

## 三、DSH Desktop 接线（Cordis 插件）

> 机制：DSH Desktop 插件是 `~/.dsh/profiles/desktop/` 下的 pnpm workspace；**`cordis.yml` 每次启动会被 launcher 重写为 `[]`**，用户插件必须写 `cordis.patch.yml` 的 `insert` 层。GUI 进程无环境变量注入，**token 必须用 `config.token` 字段**（插件优先读它，兜底 `tokenEnv`）。

```bash
# 1) 构建插件（仓库 integrations/deepseek-harness）
cd ~/Projects/shared-agent-brain/integrations/deepseek-harness
npm run check && npm test && npm run build        # 产出 lib/

# 2) 装入 profile 的 hoisted node_modules
DEST=~/.dsh/profiles/node_modules/@shared-agent-brain/dsh-plugin
mkdir -p "$DEST" && cp package.json "$DEST/" && cp -R lib "$DEST/"

# 3) ~/.dsh/profiles/desktop/package.json dependencies 加
#    "@shared-agent-brain/dsh-plugin": "file:../node_modules/@shared-agent-brain/dsh-plugin"

# 4) 写 ~/.dsh/profiles/desktop/cordis.patch.yml（用户补丁层）
#    - insert:
#        - id: shared-brain
#          name: '@shared-agent-brain/dsh-plugin'
#          config:
#            serverUrl: http://SERVER:8787
#            projectKey: my-project
#            agentId: deepseek-harness
#            token: <BRAIN_TOKEN>          # GUI 无 env，必须放这
#            queuePath: /Users/<user>/.dsh/shared-brain-queue.json

# 5) 重启 DSH Desktop
```

插件在**每轮第一步（step 1）前**把召回记忆以 `reference` 引用资料形式注入（在后续指令与用户材料之前），并注册同名 4 个 `brain_*` 工具；断网写入排队到 `.dsh/shared-brain-queue.json`，turn 结束时重放。

**斜杠命令**（单一 `/brain` 命令 + 子命令，用户命令面与 `brain_*` 工具名不撞车；与 Hermes 端完全一致）：

```text
/brain search <query>                        # 搜索共享记忆
/brain remember <title> | <content>          # 存一条事实（scope=project）
/brain update <id> <expected_version> | <new content>   # 乐观锁更新
/brain forget <id> <expected_version>        # tombstone 删除
/brain test [quick]                          # 全链路自检（T1-T12），会话窗口显示报告
/brain help                                  # 命令说明书
```

`/brain`（无参）与 `/brain help` 显示说明书；命令执行结果通过 `agent.steer` 以 **plugin notice** 形式**写入会话**（`plugin: shared-brain`）供回看，usage/未知子命令提示不写入避免噪音。注意：写入会话意味着结果进入会话历史，后续轮次的模型上下文可见（有少量 token 成本）。

---

## 四、使用示例（CLI）

```bash
amm config http://SERVER:8787 <agent_id> --project-key my-project
amm doctor
amm memory remember "Runtime" "This project uses Python 3.12"
amm memory search "Python"
```

> 中文检索：≥3 字符走 FTS5 trigram 相关度排序；**1~2 字短查询自动走 LIKE 子串兜底**（服务端已实现），中文短词（如"验证"）也能搜到。

---

## 五、API 概览

所有 `/v1` 路由要求 `Authorization: Bearer <BRAIN_TOKEN>`；写操作还需唯一 `Idempotency-Key` 头。

```text
POST   /v1/memories
GET    /v1/memories/search?q=...&project_key=...&scope=...&kind=...&min_trust_level=...
GET    /v1/memories/changes?cursor=...&project_key=...
GET    /v1/memories/{id}
GET    /v1/memories/{id}/versions
POST   /v1/memories/{id}/versions          # 乐观锁：body 带 expected_version
DELETE /v1/memories/{id}                   # tombstone：body 带 expected_version
```

OpenAPI 文档：服务运行中访问 `/docs`。

---

## 六、验证

```bash
# Python 后端
.venv/bin/python -m pytest

# DSH 插件
cd integrations/deepseek-harness
npm run check && npm test && npm run build
```

端到端验收（部署/接线后）：
1. 服务器 `/health` 200；无 token 401。
2. 任一端会话执行 **`/brain test`**：12 项自检（连通/鉴权/配置/写入/FTS 中文检索/LIKE 短词/乐观锁 409/版本更新/幂等/项目隔离/tombstone/数据清理）全部通过，测试报告写入会话窗口；`/brain test quick` 为快速版（5 项 + 清理）。
3. Hermes 新会话 `prefetch` 注入（带 `untrusted-reference-data` 安全边界声明）。
4. DSH 会话 `brain_search`/`brain_remember` 可用，step 1 前自动注入引用资料。
5. 两端写同一 `project_key` 的记忆互相可见。

---

## 七、常见坑（FAQ）

| 坑 | 说明 / 解法 |
|---|---|
| Docker Hub / PyPI 被墙 | daemon `registry-mirrors`（1ms.run / daocloud / dockerproxy.net）；构建传 `PIP_INDEX_URL` 清华源 |
| 系统 Python 的 SQLite 无 trigram | SQLite < 3.34 不支持 trigram → **必须用 Docker**（`python:3.12-slim` 自带新版） |
| 中文短词（1~2 字）搜不到 | 服务端已实现 LIKE 兜底（`db.py` search：<3 字符走 `LIKE '%词%'`），"验证""命"均可命中；FTS trigram 仅用于 ≥3 字符查询 |
| Hermes provider 不出现 | 发现机制是**目录扫描**（`$HERMES_HOME/plugins/<name>/`），不是 entry point |
| DSH 插件不生效 | `cordis.yml` 每次启动被重写 → 必须写 `cordis.patch.yml` 的 `insert` 层 |
| DSH 启动报 token 缺失 | GUI 无环境变量 → 用 `config.token` 字段而非 `tokenEnv` |
| 本地 pytest 撞 Hermes 依赖 | Hermes 注入的 `PYTHONPATH` 遮蔽项目依赖 → `unset PYTHONPATH` + `source .venv/bin/activate` |
| `docker compose up` 被守护误判为常驻进程 | 后台执行（`background=true`） |
| 重启 docker 打断其他容器 | 先查 RestartPolicy（`always` 会自动恢复） |

---

## 八、路线图

- **Phase 1（已实现）**：Shared Memory 同步闭环（写→召回→更新/废弃→溯源）。
- **Phase 1b**：Handoff 结构化任务交接（`on_session_end` 生成摘要 → 新会话注入恢复）。
- **Phase 2**：Skill 共享（标准 SKILL.md bundle；DeepSeek 远程 `SkillProvider` / Hermes 同步 `~/.hermes/skills/`）。记忆=不可信数据、Skill=可执行指令，两者保持安全边界。
