# Shared Brain 双 Agent 桌面验收（2026-08-25）

## 结论

DSH Desktop 2.0.1 与 Hermes 0.20.0 已完成真实输入、跨 Agent 互通、异常路径和服务端数据对照验收。服务器重部署后两端全量自检均为 **12/12 PASS**；T11 tombstone 的目标记忆删除后不可见，T12 无自检数据残留。

## 验收范围与结果

| 场景 | DSH | Hermes | 结果 |
|---|---:|---:|---|
| `/brain help` 在当前交互面显示，且不触发 Agent 二次解释 | ✅ | ✅ | 通过 |
| 新会话第一条 `/brain` 命令立即显示结果 | ✅ | 不适用（终端面） | 通过 |
| `/brain test quick` | 6/6 | — | 通过 |
| `/brain test` | 12/12 | 12/12 | 通过 |
| DSH 写入、Hermes 搜索读取 | ✅ | ✅ | 通过 |
| Hermes 写入、DSH 搜索读取 | ✅ | ✅ | 通过 |
| DSH v1→v2 更新、Hermes 读取 v2 | ✅ | ✅ | 通过 |
| Hermes 删除 DSH 记忆、DSH 确认不再命中 | ✅ | ✅ | 通过 |
| DSH 删除 Hermes 记忆 | ✅ | ✅ | 通过 |
| 无结果搜索明确显示 `No shared memories matched.` | ✅ | — | 通过 |
| 404 删除错误显示在命令结果行，不变成模型输入 | ✅ | — | 通过 |
| `remember` 列表显示 Agent 生成标题、创建时间、最近修改时间 | ✅ | — | 通过 |
| 已同步会话继续对话后重新进入待同步列表，并更新原记忆至 v2 | ✅ | 服务端状态对照 | 通过 |

互通标记 `QA-D2H-20260825-1701`、`QA-H2D-20260825-1702` 和会话重同步记忆均已在验收后 tombstone 清理；重同步记忆按 ID 再读返回 404。

## 本轮发现并修复的问题

1. **DSH 新会话命令输出丢失或落到错误通道**：移除 `agent.steer()` 与 plugin-only pre-step 分支。实际结果现在只走原生 `command/done`；对命令-only 草稿追加零宽挂载标记，保证第一条命令可见，同时不把结果送进模型上下文。
2. **DSH Desktop 加载了旧插件副本**：Desktop 实际优先加载 `~/.dsh/profiles/desktop/node_modules/`。安装说明改为同步 desktop 与 hoisted 两个位置，避免“源码已修、桌面仍旧行为”。
3. **已 remember 会话继续对话后不再出现**：远端部署仍使用旧的 `synced_at IS NULL` 过滤。服务器已部署当前实现，待同步条件覆盖未同步、`synced_revision < content_revision` 和上次同步失败三种状态；原会话现在更新同一 memory id 的 v2/v3，不会重复创建。

服务器更新保留 Docker 数据卷；部署前代码目录备份位于 `/root/shared-brain-backup-20260825`。

## 截图证据

- [DSH 全量自检](dsh-full-selftest.png)
- [DSH 全量自检底部（T11、T12 与最终结论）](dsh-full-selftest-bottom.png)
- [Hermes 全量自检](hermes-full-selftest.png)
- [Hermes 读取 DSH 写入](hermes-reads-dsh.png)
- [DSH 读取 Hermes 写入](dsh-reads-hermes.png)

截图仅保留验收所需交互面；测试用记忆已清理。

## 自动化验证

```text
Python: 93 passed
DSH plugin: 31 passed
TypeScript check/build: passed
git diff --check: passed
```
