import { join } from 'node:path'
import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import type { PreStepDecision } from '@deepseek-ai/dsh-agent'
import { createUserMessage, BlockAssembler } from '@deepseek-ai/dsh-llm'
import type { UserMessage } from '@deepseek-ai/dsh-session'
// @ts-ignore -- dsh-session-query 的 package.json types 指向不存在的文件（上游缺陷）
import { extractSessionEventText } from '@deepseek-ai/dsh-session-query'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { renderUntrustedMemories, SharedBrainClient, type MemoryRecord, type SessionRecord, type AgentSummary } from './client.js'
import { JsonOperationQueue } from './queue.js'
import { runSelftest } from './selftest.js'

export const name = 'shared-brain'
export const inject = ['commands', 'tools']

export interface Config {
  serverUrl: string
  tokenEnv?: string
  token?: string
  agentId?: string
  projectKey: string
  recallLimit?: number
  requestTimeoutMs?: number
  queuePath?: string
  summarizeModel?: string
}

export const Config: z<Config> = z.object({
  serverUrl: z.string().required(),
  tokenEnv: z.string().default('BRAIN_TOKEN'),
  token: z.string(),
  agentId: z.string().default('deepseek-harness'),
  projectKey: z.string().required(),
  recallLimit: z.number().step(1).min(1).max(20).default(5),
  requestTimeoutMs: z.number().step(1).min(250).max(60_000).default(5_000),
  queuePath: z.string(),
  summarizeModel: z.string(),
})

interface SharedMemorySource {
  readonly kind: 'shared-memory'
  readonly form: 'reference'
}

declare module '@deepseek-ai/dsh-llm' {
  interface MessageSourceMap {
    'shared-memory': SharedMemorySource
  }
}

const TEXT_OUTPUT = {
  schema: { type: 'string' as const },
  render: (_args: unknown, value: string) => [{ type: 'text' as const, text: value }],
}

function userText(messages: UserMessage[]): string {
  return messages
    .filter(message => message.source.kind === 'user')
    .flatMap(message => message.content)
    .map(block => block.type === 'text' ? block.text : '')
    .filter(Boolean)
    .join('\n')
    .trim()
}

export function apply(ctx: Context, config: Config): void {
  const tokenEnv = config.tokenEnv ?? 'BRAIN_TOKEN'
  const token = config.token ?? process.env[tokenEnv]
  if (!token) throw new Error(`shared-brain: configure the token (config.token) or set environment variable ${tokenEnv}`)
  const queuePath = config.queuePath ?? join(process.cwd(), '.dsh', 'shared-brain-queue.json')
  const client = new SharedBrainClient({
    serverUrl: config.serverUrl,
    token,
    agentId: config.agentId ?? 'deepseek-harness',
    projectKey: config.projectKey,
    queue: new JsonOperationQueue(queuePath),
    timeoutMs: config.requestTimeoutMs ?? 5_000,
  })
  const recallLimit = config.recallLimit ?? 5

  ctx.tools.register(defineTool({
    name: 'brain_search',
    description: 'Search Shared Brain for untrusted reference facts relevant to this project.',
    parameters: {
      query: { type: 'string', required: true, description: 'Concise search query.' },
      limit: { type: 'integer', description: 'Maximum number of results, 1-20.' },
    },
    output: TEXT_OUTPUT,
    isConcurrencySafe: () => true,
    async execute(args) {
      if (args.limit !== undefined && (!Number.isInteger(args.limit) || args.limit < 1 || args.limit > 20)) {
        throw new TypeError('brain_search: limit must be an integer from 1 through 20')
      }
      return JSON.stringify(await client.search(args.query, args.limit ?? 10))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'brain_remember',
    description: 'Save one short durable fact, preference, decision, or pitfall to Shared Brain.',
    parameters: {
      title: { type: 'string', required: true },
      content: { type: 'string', required: true },
      scope: { type: 'string', enum: ['global', 'user', 'project'] as const },
      kind: { type: 'string', enum: ['fact', 'preference', 'decision', 'pitfall'] as const },
      trustLevel: { type: 'integer', enum: [0, 1, 2, 3] as const, description: 'Trust level from 0 through 3.' },
    },
    output: TEXT_OUTPUT,
    async execute(args, exec) {
      return JSON.stringify(await client.remember({
        title: args.title,
        content: args.content,
        scope: args.scope,
        kind: args.kind,
        trustLevel: args.trustLevel,
        sessionId: exec.agent?.session.id,
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'brain_update',
    description: 'Create a new version of a Shared Brain fact with optimistic locking.',
    parameters: {
      memoryId: { type: 'string', required: true },
      expectedVersion: { type: 'integer', required: true, description: 'Current positive version.' },
      title: { type: 'string' },
      content: { type: 'string' },
      kind: { type: 'string', enum: ['fact', 'preference', 'decision', 'pitfall'] as const },
      trustLevel: { type: 'integer', enum: [0, 1, 2, 3] as const },
    },
    output: TEXT_OUTPUT,
    async execute(args, exec) {
      if (args.expectedVersion < 1) throw new TypeError('brain_update: expectedVersion must be positive')
      if (args.title === undefined && args.content === undefined && args.kind === undefined && args.trustLevel === undefined) {
        throw new TypeError('brain_update: at least one mutable field is required')
      }
      return JSON.stringify(await client.update({
        memoryId: args.memoryId,
        expectedVersion: args.expectedVersion,
        title: args.title,
        content: args.content,
        kind: args.kind,
        trustLevel: args.trustLevel,
        sessionId: exec.agent?.session.id,
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'brain_forget',
    description: 'Tombstone a Shared Brain fact with optimistic locking.',
    parameters: {
      memoryId: { type: 'string', required: true },
      expectedVersion: { type: 'integer', required: true, description: 'Current positive version.' },
    },
    output: TEXT_OUTPUT,
    async execute(args) {
      if (args.expectedVersion < 1) throw new TypeError('brain_forget: expectedVersion must be positive')
      return JSON.stringify(await client.forget(args.memoryId, args.expectedVersion))
    },
  }))

  // --- 斜杠命令：结果直接渲染进 UI，不进模型历史（零 token 消耗） ---
  type CommandResult = { kind: 'success' | 'error'; text: string }
  type CommandInvocation = { rawInput: string; agent?: unknown }
  const commands = (ctx as unknown as {
    commands: {
      register(definition: {
        name: string
        description: string
        input?: { hint: string }
        handler: (invocation: CommandInvocation) => CommandResult | Promise<CommandResult>
      }): void
    }
  }).commands
  const sessionIdOf = (invocation: CommandInvocation): string | undefined => {
    const agent = invocation.agent as { session?: { id?: string } } | undefined
    return agent?.session?.id
  }
  const renderResults = (items: MemoryRecord[]): string =>
    renderUntrustedMemories(items) ?? 'No shared memories matched.'
  // 把命令结果以 plugin notice 消息写入会话，供用户在会话中回看（plan-mode 同款模式）。
  const steerResult = (invocation: CommandInvocation, commandName: string, text: string): void => {
    const agent = invocation.agent as { steer?: (message: UserMessage) => unknown } | undefined
    if (!agent?.steer || !text) return
    try {
      agent.steer(createUserMessage({
        content: [{ type: 'text', text }],
        source: {
          kind: 'plugin',
          plugin: 'shared-brain',
          form: 'notice',
          summary: `Shared Brain: ${commandName}`,
        } as unknown as UserMessage['source'],
      }))
    } catch {
      // best-effort: 失败时命令结果仍会渲染在 UI 命令平面
    }
  }

  // --- UX 选择器：无参调用时从列表选择，避免手输（userQuestions 弹窗） ---
  type UserQuestionsService = {
    ask(request: {
      questions: Array<{
        id: string
        header?: string
        question: string
        detail?: string
        options?: Array<{ label: string; description?: string }>
      }>
      agent?: unknown
      signal?: AbortSignal
    }): Promise<{ answers: Array<{ id: string; selected: string[]; custom?: string }> }>
  }
  type SessionQueryService = {
    readTitle(sessionId: string): Promise<string | undefined>
    readSession(sessionId: string): Promise<unknown>
  }
  type LlmService = {
    stream(options: Record<string, unknown>): AsyncIterable<unknown>
  }
  const userQuestions = () => ctx.get('userQuestions') as UserQuestionsService | undefined
  const sessionQuery = () => ctx.get('sessionQuery') as SessionQueryService | undefined
  const llm = () => ctx.get('llm') as LlmService | undefined

  interface AskOutcome { label?: string; custom?: string }
  const askOne = async (
    invocation: CommandInvocation,
    question: { id: string; header: string; question: string; options: Array<{ label: string; description?: string }> },
    allowCustom = false,
  ): Promise<AskOutcome | undefined> => {
    const uq = userQuestions()
    if (!uq) return undefined
    const answer = await uq.ask({
      questions: [{ id: question.id, header: question.header, question: question.question, options: question.options }],
      agent: invocation.agent,
    })
    const first = answer.answers[0]
    if (!first) return undefined
    return { label: first.selected[0], custom: allowCustom ? first.custom : undefined }
  }

  const pickAgent = async (invocation: CommandInvocation): Promise<AgentSummary | undefined> => {
    const agents = await client.listAgents()
    const candidates = agents.filter(a => a.unsynced_count > 0 || a.total_count > 0)
    if (candidates.length === 0) return undefined
    if (candidates.length === 1) return candidates[0]
    const chosen = await askOne(invocation, {
      id: 'agent',
      header: '选择 agent',
      question: '记忆归属哪个 agent？',
      options: candidates.map(a => ({
        label: a.agent_id,
        description: `${a.total_count} 个会话 · ${a.unsynced_count} 个未上传`,
      })),
    })
    return candidates.find(a => a.agent_id === chosen?.label)
  }

  const pickUnsyncedSession = async (
    invocation: CommandInvocation,
    agent: string,
  ): Promise<SessionRecord | undefined> => {
    const sessions = await client.listSessions({ agent, synced: false })
    if (sessions.length === 0) return undefined
    const chosen = await askOne(invocation, {
      id: 'session',
      header: `选择会话（${agent}）`,
      question: '上传哪个会话？',
      options: sessions.map(s => ({
        label: s.title || s.session_id.slice(0, 12),
        description: `${s.updated_at}${s.title ? ` · ${s.session_id.slice(0, 12)}` : ''}`,
      })),
    })
    return sessions.find(s => (s.title || s.session_id.slice(0, 12)) === chosen?.label)
  }

  const pickMemory = async (
    invocation: CommandInvocation,
    memories: MemoryRecord[],
    header: string,
  ): Promise<MemoryRecord | undefined> => {
    if (memories.length === 0) return undefined
    const chosen = await askOne(invocation, {
      id: 'memory',
      header,
      question: '选择一条记忆：',
      options: memories.map(m => ({
        label: m.title.slice(0, 60),
        description: `${m.source_agent} · v${m.current_version} · ${m.updated_at.slice(0, 16)}`,
      })),
    })
    return memories.find(m => m.title.slice(0, 60) === chosen?.label)
  }

  const extractConversationText = async (sessionId: string): Promise<string> => {
    const sq = sessionQuery()
    if (!sq) return ''
    try {
      const log = await sq.readSession(sessionId) as unknown
      const events = Array.isArray(log)
        ? log
        : ((log as { events?: unknown[] })?.events ?? [])
      const parts: string[] = []
      for (const event of events) {
        try {
          const text = extractSessionEventText(event)
          if (text) parts.push(text)
        } catch {
          // 跳过无法投影的事件
        }
      }
      return parts.join('\n').slice(0, 20_000)
    } catch {
      return ''
    }
  }

  const summarizeWithLlm = async (invocation: CommandInvocation, conversation: string): Promise<string> => {
    const llmService = llm()
    if (!llmService) throw new Error('llm 服务不可用，无法提炼会话')
    const model = config.summarizeModel
    if (!model) throw new Error('未配置提炼模型：在 cordis.patch.yml 的 config 中加 summarizeModel（如 "deepseek-chat"）')
    const system = (
      '你是信息提炼助手。把一段对话提炼为简洁的项目记忆：只保留事实、决策、偏好和踩坑教训，'
      + '删除寒暄与无关内容，用客观陈述句，中文输出，不超过 500 字。直接输出提炼结果，不要任何前缀。'
    )
    const assembler = new BlockAssembler()
    for await (const chunk of llmService.stream({
      model,
      messages: [{ role: 'user', content: `对话内容：\n${conversation}` }],
      system,
      maxTokens: 900,
      purpose: 'shared-brain-summarize',
      sessionId: (invocation.agent as { session?: { id?: string } } | undefined)?.session?.id,
    })) {
      assembler.push(chunk as Parameters<typeof assembler.push>[0])
    }
    const blocks = assembler.blocks()
    const text = blocks.filter(b => b.type === 'text').map(b => (b as { text: string }).text).join(' ').trim()
    if (!text) throw new Error('提炼模型没有产生文本')
    return text.slice(0, 8000)
  }

  const helpText = [
    'Shared Brain 命令说明书（子命令与 brain_* 工具一一对应）:',
    '/brain（无参）                                # 列表选择模式：先选 agent 再选会话/记忆',
    '/brain search <query>                        # 搜索共享记忆（无参=浏览最近记忆）',
    '/brain remember [<title> | <content>]        # 无参=选未上传会话自动提炼上传；带参=直接保存',
    '/brain update [<id> <expected_version> | <new content>]   # 无参=选记忆后输入新内容',
    '/brain forget [<id> <expected_version>]      # 无参=选记忆后确认删除',
    '/brain test [quick]                          # 运行全链路自检并显示报告',
    '/brain help                                  # 显示本说明书',
  ].join('\n')

  commands.register({
    name: 'brain',
    description: 'Shared Brain: search/remember/update/forget memories. Run /brain help for the manual.',
    input: { hint: '<search|remember|update|forget|help> ...' },
    handler: async (invocation) => {
      const [sub, ...rest] = invocation.rawInput.trim().split(/\s+/)
      const args = rest.join(' ').trim()
      const command = (sub ?? '').toLowerCase()
      if (!command || command === 'help') return { kind: 'success', text: helpText }

      const usage = (hint: string) => ({ kind: 'error' as const, text: `Usage: ${hint}` })

      switch (command) {
        case 'search': {
          if (!args) {
            // 无参：列最近记忆供选择浏览
            try {
              const memories = await client.listMemories({ limit: 20 })
              if (memories.length === 0) {
                const text = '暂无记忆。'
                steerResult(invocation, 'brain_search', text)
                return { kind: 'success', text }
              }
              const picked = await pickMemory(invocation, memories, '最近记忆')
              if (!picked) return { kind: 'error', text: '未选择记忆' }
              const text = renderResults(memories.filter(m => m.id === picked.id))
              steerResult(invocation, 'brain_search', text)
              return { kind: 'success', text }
            } catch (error) {
              const text = `Shared Brain search failed: ${String(error)}`
              steerResult(invocation, 'brain_search', text)
              return { kind: 'error', text }
            }
          }
          try {
            const items = await client.search(args, recallLimit)
            const text = items.length === 0 ? 'No shared memories matched.' : renderResults(items)
            steerResult(invocation, 'brain_search', text)
            return { kind: 'success', text }
          } catch (error) {
            const text = `Shared Brain search failed: ${String(error)}`
            steerResult(invocation, 'brain_search', text)
            return { kind: 'error', text }
          }
        }
        case 'remember': {
          if (!args) {
            // 无参：选 agent → 选未上传会话 → 设标题 → 提炼上传
            try {
              const agent = await pickAgent(invocation)
              if (!agent) {
                const text = '没有未上传的会话（会话会在结束时自动上报）。'
                steerResult(invocation, 'brain_remember', text)
                return { kind: 'success', text }
              }
              const session = await pickUnsyncedSession(invocation, agent.agent_id)
              if (!session) {
                const text = `${agent.agent_id} 没有未上传的会话。`
                steerResult(invocation, 'brain_remember', text)
                return { kind: 'success', text }
              }
              // 标题：用户手动设置（可自由输入，或用会话原标题）
              const titleOutcome = await askOne(
                invocation,
                {
                  id: 'title',
                  header: '设置标题',
                  question: '为这条记忆设置标题（可直接输入新标题，或选会话原标题）：',
                  options: [{ label: session.title || session.session_id, description: '使用会话原标题' }],
                },
                true,
              )
              if (!titleOutcome) return { kind: 'error', text: '未设置标题' }
              const title = (titleOutcome.custom || titleOutcome.label || '').trim()
              if (!title) return { kind: 'error', text: '标题不能为空' }
              // 提炼会话内容（上传 agent 完成信息提炼）
              const conversation = await extractConversationText(session.session_id)
              if (!conversation) {
                const text = '无法读取会话内容，已中止。'
                steerResult(invocation, 'brain_remember', text)
                return { kind: 'error', text }
              }
              const summary = await summarizeWithLlm(invocation, conversation)
              const record = await client.remember({
                title,
                content: summary,
                scope: 'project',
                kind: 'fact',
                sessionId: session.session_id,
              })
              if ('queued' in record) {
                const text = '离线排队，稍后自动同步。'
                steerResult(invocation, 'brain_remember', text)
                return { kind: 'success', text }
              }
              await client.markSessionSynced(agent.agent_id, session.session_id)
              const text = `✅ 已上传 "${record.title}"（v${record.current_version}）· ${agent.agent_id} 的会话已标记`
              steerResult(invocation, 'brain_remember', text)
              return { kind: 'success', text }
            } catch (error) {
              const text = `Shared Brain save failed: ${String(error)}`
              steerResult(invocation, 'brain_remember', text)
              return { kind: 'error', text }
            }
          }
          const sep = args.indexOf('|')
          const title = (sep === -1 ? args.slice(0, 80) : args.slice(0, sep).trim()).trim()
          const content = sep === -1 ? args : args.slice(sep + 1).trim()
          if (!title || !content) return usage('/brain remember <title> | <content>')
          try {
            const rec = await client.remember({
              title,
              content,
              scope: 'project',
              kind: 'fact',
              sessionId: sessionIdOf(invocation),
            })
            const text = 'queued' in rec
              ? 'Saved to offline queue (will sync when back online).'
              : `Saved v${rec.current_version}: ${rec.title}`
            steerResult(invocation, 'brain_remember', text)
            return { kind: 'success', text }
          } catch (error) {
            const text = `Shared Brain save failed: ${String(error)}`
            steerResult(invocation, 'brain_remember', text)
            return { kind: 'error', text }
          }
        }
        case 'update': {
          if (!args) {
            // 无参：选 agent → 选记忆 → 输新内容
            try {
              const agent = await pickAgent(invocation)
              if (!agent) {
                const text = '没有可管理的记忆。'
                steerResult(invocation, 'brain_update', text)
                return { kind: 'success', text }
              }
              const memories = await client.listMemories({ sourceAgent: agent.agent_id, limit: 20 })
              const picked = await pickMemory(invocation, memories, `更新记忆（${agent.agent_id}）`)
              if (!picked) return { kind: 'error', text: '未选择记忆' }
              const contentOutcome = await askOne(
                invocation,
                { id: 'content', header: '新内容', question: '输入更新后的内容：', options: [] },
                true,
              )
              const content = (contentOutcome?.custom || '').trim()
              if (!content) return { kind: 'error', text: '新内容不能为空' }
              const record = await client.update({
                memoryId: picked.id,
                expectedVersion: picked.current_version,
                content,
                sessionId: sessionIdOf(invocation),
              })
              const text = 'queued' in record
                ? 'Queued offline (will sync when back online).'
                : `Updated to v${record.current_version}: ${record.title}`
              steerResult(invocation, 'brain_update', text)
              return { kind: 'success', text }
            } catch (error) {
              const text = `Shared Brain update failed: ${String(error)}`
              steerResult(invocation, 'brain_update', text)
              return { kind: 'error', text }
            }
          }
          const sep = args.indexOf('|')
          const head = (sep === -1 ? args : args.slice(0, sep)).trim().split(/\s+/)
          const id = head[0] ?? ''
          const version = Number(head[1])
          const content = sep === -1 ? '' : args.slice(sep + 1).trim()
          if (!id || !Number.isInteger(version) || version < 1 || !content) {
            return usage('/brain update <id> <expected_version> | <new content>')
          }
          try {
            const rec = await client.update({
              memoryId: id,
              expectedVersion: version,
              content,
              sessionId: sessionIdOf(invocation),
            })
            const text = 'queued' in rec
              ? 'Queued offline (will sync when back online).'
              : `Updated to v${rec.current_version}: ${rec.title}`
            steerResult(invocation, 'brain_update', text)
            return { kind: 'success', text }
          } catch (error) {
            const text = `Shared Brain update failed: ${String(error)}`
            steerResult(invocation, 'brain_update', text)
            return { kind: 'error', text }
          }
        }
        case 'forget': {
          if (!args) {
            // 无参：选 agent → 选记忆 → 确认删除
            try {
              const agent = await pickAgent(invocation)
              if (!agent) {
                const text = '没有可管理的记忆。'
                steerResult(invocation, 'brain_forget', text)
                return { kind: 'success', text }
              }
              const memories = await client.listMemories({ sourceAgent: agent.agent_id, limit: 20 })
              const picked = await pickMemory(invocation, memories, `删除记忆（${agent.agent_id}）`)
              if (!picked) return { kind: 'error', text: '未选择记忆' }
              const confirm = await askOne(invocation, {
                id: 'confirm',
                header: '确认删除',
                question: `确定删除 "${picked.title}"（v${picked.current_version}）？`,
                options: [{ label: '删除', description: 'tombstone 该记忆' }, { label: '取消' }],
              })
              if (confirm?.label !== '删除') return { kind: 'success', text: '已取消' }
              const result = await client.forget(picked.id, picked.current_version)
              const text = 'queued' in result
                ? 'Queued offline (will sync when back online).'
                : `Forgotten: ${result.title || result.id}`
              steerResult(invocation, 'brain_forget', text)
              return { kind: 'success', text }
            } catch (error) {
              const text = `Shared Brain forget failed: ${String(error)}`
              steerResult(invocation, 'brain_forget', text)
              return { kind: 'error', text }
            }
          }
          const [id, version] = args.split(/\s+/)
          const v = Number(version)
          if (!id || !Number.isInteger(v) || v < 1) {
            return usage('/brain forget <id> <expected_version>')
          }
          try {
            const res = await client.forget(id, v)
            const text = 'queued' in res
              ? 'Queued offline (will sync when back online).'
              : `Forgotten: ${JSON.stringify(res)}`
            steerResult(invocation, 'brain_forget', text)
            return { kind: 'success', text }
          } catch (error) {
            const text = `Shared Brain forget failed: ${String(error)}`
            steerResult(invocation, 'brain_forget', text)
            return { kind: 'error', text }
          }
        }
        case 'test': {
          const quick = args === 'quick'
          if (args && !quick) return usage('/brain test [quick]')
          try {
            const report = await runSelftest(client, { quick })
            steerResult(invocation, 'brain_test', report.text)
            return { kind: report.passed ? 'success' : 'error', text: report.text }
          } catch (error) {
            const text = `Shared Brain selftest failed: ${String(error)}`
            steerResult(invocation, 'brain_test', text)
            return { kind: 'error', text }
          }
        }
        default:
          return usage(`/brain ${command} ... — unknown subcommand; /brain help for the manual`)
      }
    },
  })

  ctx.on('agent/pre-step', async ({ messages, step, signal }, next): Promise<PreStepDecision> => {
    const downstream = await next()
    if (downstream.kind === 'reject' || step !== 1) return downstream
    const query = userText(messages)
    if (!query) return downstream
    try {
      signal.throwIfAborted()
      const recalled = await client.search(query, recallLimit)
      signal.throwIfAborted()
      const rendered = renderUntrustedMemories(recalled)
      if (!rendered) return downstream
      const reference = createUserMessage({
        content: [{ type: 'text', text: rendered }],
        source: { kind: 'shared-memory', form: 'reference' },
      })
      // Reference data goes before downstream instructions and user material.
      return { kind: 'enter', messages: [reference, ...downstream.messages] }
    } catch (error) {
      ctx.logger.warn(`shared-brain recall failed: ${String(error)}`)
      return downstream
    }
  })

  ctx.on('session/event', (session, event) => {
    if (event.type !== 'turn/end') return
    void client.flushQueue().catch(error => {
      ctx.logger.warn(`shared-brain queue flush failed: ${String(error)}`)
    })
    // 会话目录上报：让 remember 的选择器能列出本机未上传会话。
    const sessionId = (session as { id?: string } | undefined)?.id
    if (!sessionId) return
    void (async () => {
      try {
        const title = await sessionQuery()?.readTitle(sessionId)
        await client.upsertSession({
          agentId: config.agentId ?? 'deepseek-harness',
          sessionId,
          title: title ?? null,
          updatedAt: new Date().toISOString(),
        })
      } catch (error) {
        ctx.logger.warn(`shared-brain session upsert failed: ${String(error)}`)
      }
    })()
  })
}

export { renderUntrustedMemories, SharedBrainClient } from './client.js'
export { JsonOperationQueue } from './queue.js'
