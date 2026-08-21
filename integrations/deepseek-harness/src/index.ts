import { join } from 'node:path'
import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import type { PreStepDecision } from '@deepseek-ai/dsh-agent'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type { UserMessage } from '@deepseek-ai/dsh-session'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { renderUntrustedMemories, SharedBrainClient } from './client.js'
import { JsonOperationQueue } from './queue.js'

export const name = 'shared-brain'
export const inject = ['tools']

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
