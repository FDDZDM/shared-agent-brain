# Shared Brain

> **面向开放 Agent Harness 的自托管共享记忆后端**，首发原生支持 **Hermes** 与 **DeepSeek Harness**。
> 服务器零 LLM、弱机可跑；客户端走官方扩展点（MemoryProvider ABC / Cordis 插件），不靠目录嗅探和格式猜测。

Phase 1 已实现：

- 版本化 `global` / `user` / `project` 三种 scope 的记忆，项目隔离。
- SQLite **FTS5 trigram** 全文检索（中英混排，`content_text` 内联存储 + 触发器同步）。
- **乐观锁**（`expected_version` 不符 → HTTP 409）、**tombstone 删除**、增量变更流。
- 幂等键（`Idempotency-Key`）与**离线客户端队列**（断网/5xx 自动重试；409/其他 4xx 留作诊断，修正状态后显式重试）。
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
/brain（无参）                                 # 进入列表选择：先选 agent 再选会话/记忆
/brain search <query>                        # 搜索共享记忆（无参=浏览最近记忆）
/brain remember [<title> | <content>]        # 无参=选待同步会话；首次创建，后续更新原记忆版本
/brain update [<id> <expected_version> | <new content>]   # 无参=选记忆后输入新内容
/brain forget [<id> <expected_version>]      # 无参=选记忆后确认删除
/brain test [quick]                          # 全链路自检（T1-T12），会话窗口显示报告
/brain setup                                 # 校验配置、重放队列并重新加载插件生命周期
/brain help                                  # 命令说明书
```

与 DSH 端完全同构（单一 `/brain` + 子命令，子命令与 `brain_*` 工具一一对应，命令面与工具面不撞名）。

**交互设计（v2，列表选择优先）**：无参调用不再要求手输，而是**从服务器拉候选列表让用户选择**。
`/brain` 浏览入口和 `update`/`forget` 可以跨 agent 管理共享内容；`remember` 只列当前客户端所属 agent
在本机可读取的**待同步会话**，不会先让用户选到另一个 agent、随后才因读不到 transcript 而失败。
待同步会话包括
（首次未同步，或同步后继续产生了新对话）→
显示当前会话 Agent 生成的标题以及创建时间、最近修改时间 → 保存前允许用户沿用或自定义标题 →
DSH 端由模型自动提炼会话内容入库；`update`/`forget` 按 agent 分组列记忆后选择；
`search` 无参浏览最近记忆。会话目录（agent → 会话）由各客户端在会话结束时自动上报到服务器
（Hermes 用 `on_session_end`；DSH 同时监听 `turn/end` 和标题生成后的 `session/title`，并在打开
`remember` 列表时从本地会话日志修复旧的空标题；列表不再以 session id 充当标题）。平台差异：DSH 端有弹窗选择 +
LLM 提炼；Hermes 端为两步编号文本（`/brain remember <编号> <标题>`），上传经净化的用户/助手对话文本（未提炼，排除 system、tool 与召回上下文）。

**提炼方式**：DSH 插件复用当前 Agent 已选择的 provider/model 发起一次**隔离模型调用**，
输入只包含用户选中的源会话，不携带当前聊天历史，也不触发 Shared Brain 自动召回。源会话中仅保留
真人用户消息及同一真人轮次的最终助手答复；共享记忆引用、插件通知、工具结果、带工具调用的中间推理，
以及没有真人输入的旧 handoff 轮次全部排除。当前会话会置顶并标注“当前会话”；提炼完成后
插件直接执行复合会话同步。无需配置额外 agent 或 `summarizeModel`，服务器仍保持零 LLM。
首次同步创建记忆 v1；原会话后续内容指纹变化时状态转为 `changed`，再次执行 `remember` 会沿
`synced_memory_id` 原子追加 v2、v3……，不会创建重复记忆。长会话采用“开头 + 最新内容”窗口，
避免超过长度预算后新增对话无法触发变更。不同会话即使摘要文字完全相同，也不会共享同一个可变记忆；
删除会话关联的记忆后，该会话会重新回到待同步列表。

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
/brain remember <title> | <content>          # 直接存一条事实；无参则同步新增/已变化会话
/brain update <id> <expected_version> | <new content>   # 乐观锁更新
/brain forget <id> <expected_version>        # tombstone 删除
/brain test [quick]                          # 全链路自检（T1-T12），会话窗口显示报告
/brain setup                                 # 校验配置和队列；重新加载插件生命周期
/brain help                                  # 命令说明书
```

`/brain setup` 会先重放可执行的离线队列。DSH 端随后重启 Cordis 插件生命周期，适合应用已加载版本的
重新配置；刚替换 `lib/` 代码或首次安装包含 `setup` 的版本时，仍需重启 DSH Desktop 才能保证载入新模块。
Hermes 当前没有对应生命周期，因此完成配置校验后会明确提示重启。

只有 `/brain help` 显示说明书；`/brain`（无参）进入 agent → 会话/记忆的列表选择。DSH 的说明书、成功通知、错误回执和用法提示都会作为 plugin notice 写入会话；注入成功后命令返回空 success，避免终端重复输出，同时 `agent/pre-step` 会拒绝由该 notice 单独唤起的模型步骤，因此 Agent 不会再对 “forget failed” 或 “Saved v1” 作二次解释和追问。只有会话注入接口不可用时才降级到终端。Hermes 缺少对应的 pre-step 拦截能力，所以全部命令结果直接返回，禁止使用会唤醒模型的 `inject_message(role=user)`。平台有原生选项选择器时优先使用，不要求用户读取终端输出后手输编号。

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
GET    /v1/memories?project_key=...&source_agent=...          # 最近记忆列表（选择器数据源）
GET    /v1/memories/changes?cursor=...&project_key=...
GET    /v1/memories/{id}
GET    /v1/memories/{id}/versions
POST   /v1/memories/{id}/versions          # 乐观锁：body 带 expected_version
DELETE /v1/memories/{id}                   # tombstone：body 带 expected_version

# 会话目录（remember 的 agent → 会话两级选择）
POST   /v1/sessions                        # 客户端上报会话元数据（幂等 upsert，synced 状态保留）
GET    /v1/sessions?agent=...&synced=false # 按 agent 列待同步会话（未同步或 changed）
GET    /v1/sessions/agents                 # distinct agent + 待同步计数
POST   /v1/sessions/{agent}/{session}/sync # 原子写入记忆并标记会话同步
POST   /v1/sessions/{agent}/{session}/synced   # 标记会话已上传
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

Python 套件包含真实跨语言互通测试：临时启动一个 Shared Brain HTTP 服务，
让 Python Hermes provider 与编译后的 TypeScript DSH client 同时连接，验证
Hermes → DSH、DSH → Hermes 双向写读、复合会话同步和并发更新 409。运行该套件
因此需要本机同时安装 Python 依赖、Node.js 与 DSH 插件依赖。

端到端验收（部署/接线后）：
1. 服务器 `/health` 200；无 token 401。
2. 任一端会话执行 **`/brain test`**：12 项自检（连通/鉴权/配置/写入/FTS 中文检索/LIKE 短词/乐观锁 409/版本更新/幂等/项目隔离/tombstone/数据清理）全部通过，测试报告写入会话窗口；`/brain test quick` 为快速版（5 项 + 清理）。
3. Hermes 新会话 `prefetch` 注入（带 `untrusted-reference-data` 安全边界声明）。
4. DSH 会话 `brain_search`/`brain_remember` 可用，step 1 前自动注入引用资料。
5. 两端写同一 `project_key` 的记忆互相可见。
6. 两端并发更新同一版本时恰好一端成功，另一端收到 409，且最终版本两端一致。
7. 在已 remember 的源会话继续对话后，它重新出现在待同步列表；再次 remember 更新同一 memory id 的下一版本。
8. 分别在 DSH 与 Hermes 执行命令，成功/失败回执只出现在会话窗口，不被 Agent 当作新用户请求继续回答。

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
| Hermes 请求莫名 502、DSH 却正常 | Python/Hermes 客户端默认不继承 `HTTP_PROXY`/`HTTPS_PROXY`，与 DSH 的直连行为保持一致；需要代理时请在服务 URL 前配置明确的反向代理，而不是依赖宿主环境变量 |
| 离线队列里 409 一直失败 | 409 是版本/幂等冲突，重复发送旧请求不会自愈；刷新目标版本或会话状态后，用 `amm queue retry <op_key>` 显式重试，或删除已确认无效的队列项 |
| 本地 pytest 撞 Hermes 依赖 | Hermes 注入的 `PYTHONPATH` 遮蔽项目依赖 → `unset PYTHONPATH` + `source .venv/bin/activate` |
| `docker compose up` 被守护误判为常驻进程 | 后台执行（`background=true`） |
| 重启 docker 打断其他容器 | 先查 RestartPolicy（`always` 会自动恢复） |

---

## 八、路线图

- **Phase 1（已实现）**：Shared Memory 同步闭环（写→召回→更新/废弃→溯源）。
- **Phase 1b**：Handoff 结构化任务交接（`on_session_end` 生成摘要 → 新会话注入恢复）。
- **Phase 2**：Skill 共享（标准 SKILL.md bundle；DeepSeek 远程 `SkillProvider` / Hermes 同步 `~/.hermes/skills/`）。记忆=不可信数据、Skill=可执行指令，两者保持安全边界。
