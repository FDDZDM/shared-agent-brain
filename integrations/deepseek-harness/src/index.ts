import { join } from 'node:path'
import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import type { PreStepDecision } from '@deepseek-ai/dsh-agent'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type { UserMessage } from '@deepseek-ai/dsh-session'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { renderUntrustedMemories, SharedBrainClient, type MemoryRecord } from './client.js'
import { JsonOperationQueue } from './queue.js'

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

  commands.register({
    name: 'brain_search',
    description: 'search Shared Brain for untrusted reference facts',
    input: { hint: '<query>' },
    handler: async (invocation) => {
      const query = invocation.rawInput.trim()
      if (!query) return { kind: 'error', text: 'Usage: /brain_search <query>' }
      try {
        const items = await client.search(query, recallLimit)
        const text = items.length === 0 ? 'No shared memories matched.' : renderResults(items)
        steerResult(invocation, 'brain_search', text)
        return { kind: 'success', text }
      } catch (error) {
        const text = `Shared Brain search failed: ${String(error)}`
        steerResult(invocation, 'brain_search', text)
        return { kind: 'error', text }
      }
    },
  })

  commands.register({
    name: 'brain_remember',
    description: 'save one short durable fact to Shared Brain',
    input: { hint: '<title> | <content>' },
    handler: async (invocation) => {
      const raw = invocation.rawInput.trim()
      const sep = raw.indexOf('|')
      const title = (sep === -1 ? raw.slice(0, 80) : raw.slice(0, sep).trim()).trim()
      const content = sep === -1 ? raw : raw.slice(sep + 1).trim()
      if (!title || !content) return { kind: 'error', text: 'Usage: /brain_remember <title> | <content>' }
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
    },
  })

  commands.register({
    name: 'brain_update',
    description: 'create a new version of a Shared Brain memory (optimistic lock)',
    input: { hint: '<memory_id> <expected_version> | <new content>' },
    handler: async (invocation) => {
      const raw = invocation.rawInput.trim()
      const sep = raw.indexOf('|')
      const head = (sep === -1 ? raw : raw.slice(0, sep)).trim().split(/\s+/)
      const id = head[0] ?? ''
      const version = Number(head[1])
      const content = sep === -1 ? '' : raw.slice(sep + 1).trim()
      if (!id || !Number.isInteger(version) || version < 1 || !content) {
        return { kind: 'error', text: 'Usage: /brain_update <memory_id> <expected_version> | <new content>' }
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
    },
  })

  commands.register({
    name: 'brain_forget',
    description: 'tombstone a Shared Brain memory (optimistic lock)',
    input: { hint: '<memory_id> <expected_version>' },
    handler: async (invocation) => {
      const [id, version] = invocation.rawInput.trim().split(/\s+/)
      const v = Number(version)
      if (!id || !Number.isInteger(v) || v < 1) {
        return { kind: 'error', text: 'Usage: /brain_forget <memory_id> <expected_version>' }
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

  ctx.on('session/event', (_session, event) => {
    if (event.type === 'turn/end') {
      void client.flushQueue().catch(error => {
        ctx.logger.warn(`shared-brain queue flush failed: ${String(error)}`)
      })
    }
  })
}

export { renderUntrustedMemories, SharedBrainClient } from './client.js'
export { JsonOperationQueue } from './queue.js'
