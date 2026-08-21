import assert from 'node:assert/strict'
import test from 'node:test'

import { apply } from '../lib/index.js'

/** In-memory fake server for the session-directory + memories surface. */
function fakeServer() {
  const sessions = []
  const memories = []
  let nextId = 0
  const fetchImpl = async (url, init = {}) => {
    const parsed = new URL(url)
    const path = parsed.pathname
    const respond = (status, data) =>
      new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } })
    const body = init.body ? JSON.parse(init.body) : {}

    if (path === '/v1/sessions/agents') {
      const byAgent = {}
      for (const s of sessions) {
        byAgent[s.agent_id] ??= { total: 0, unsynced: 0 }
        byAgent[s.agent_id].total += 1
        if (!s.synced_at) byAgent[s.agent_id].unsynced += 1
      }
      return respond(200, {
        items: Object.entries(byAgent).map(([agent_id, c]) => ({ agent_id, total_count: c.total, unsynced_count: c.unsynced })),
      })
    }
    if (path === '/v1/sessions') {
      const agent = parsed.searchParams.get('agent')
      const synced = parsed.searchParams.get('synced')
      let items = sessions
      if (agent) items = items.filter(s => s.agent_id === agent)
      if (synced !== null) {
        const wantSynced = synced === 'true'
        items = items.filter(s => (s.synced_at !== null) === wantSynced)
      }
      return respond(200, { items: items.slice().reverse() })
    }
    const syncedMatch = path.match(/^\/v1\/sessions\/([^/]+)\/([^/]+)\/synced$/)
    if (syncedMatch && init.method === 'POST') {
      const s = sessions.find(x => x.agent_id === syncedMatch[1] && x.session_id === syncedMatch[2])
      if (s) s.synced_at = '2026-08-21T18:00:00Z'
      return respond(200, s ?? { agent_id: syncedMatch[1], session_id: syncedMatch[2], synced_at: 'now' })
    }
    if (path === '/v1/memories' && init.method === 'POST') {
      const record = {
        id: `m${++nextId}`, current_version: 1, title: body.title,
        content_text: body.content_text, source_agent: body.source_agent,
        source_session_id: body.source_session_id,
      }
      memories.push(record)
      return respond(201, record)
    }
    const updateMatch = path.match(/^\/v1\/memories\/([^/]+)\/versions$/)
    if (updateMatch && init.method === 'POST') {
      const memory = memories.find(m => m.id === updateMatch[1])
      if (!memory) return respond(404, { error: 'not found' })
      memory.content_text = body.content_text
      memory.current_version += 1
      return respond(200, memory)
    }
    if (path === '/v1/memories') {
      const sourceAgent = parsed.searchParams.get('source_agent')
      const items = sourceAgent ? memories.filter(m => m.source_agent === sourceAgent) : memories
      return respond(200, { items: items.slice().reverse() })
    }
    return respond(404, { error: 'not found' })
  }
  sessions.push(
    { agent_id: 'Mac-DSH', session_id: 'sess-1', title: '会话A', updated_at: '2026-08-21T17:00:00Z', synced_at: null },
    { agent_id: 'Mac-DSH', session_id: 'sess-2', title: '会话B', updated_at: '2026-08-21T17:30:00Z', synced_at: '2026-08-21T17:31:00Z' },
  )
  return { sessions, memories, fetchImpl }
}

function makeCtx({ server, answers, custom }) {
  let askIndex = 0
  const steered = []
  const tools = new Map()
  const commands = new Map()
  const hooks = new Map()
  const fakeAgent = { session: { id: 'verify-session' }, steer: m => steered.push(m) }
  return {
    ctx: {
      tools: { register: t => tools.set(t.name, t) },
      commands: { register: d => commands.set(d.name, d) },
      on: (evt, fn) => hooks.set(evt, fn),
      logger: { warn: () => {} },
      get: name => {
        if (name === 'userQuestions') {
          return {
            ask: async req => {
              const idx = askIndex++
              const question = req.questions[0]
              return {
                answers: [{
                  id: question.id,
                  selected: answers[idx] ? [answers[idx]] : [],
                  custom: custom?.[idx],
                }],
              }
            },
          }
        }
        if (name === 'sessionQuery') {
          return {
            readTitle: async () => '会话A',
            readSession: async () => ({
              events: [
                { type: 'user/message', data: { content: [{ type: 'text', text: '你好，帮我看看这个项目' }] } },
                { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: '好的，我来分析' }] } } },
              ],
            }),
          }
        }
        if (name === 'llm') {
          return {
            stream: async function* () {
              yield { type: 'text-delta', index: 0, text: '这是提炼后的记忆内容。' }
              yield { type: 'finish', reason: 'completed', replayState: {} }
            },
          }
        }
        return undefined
      },
    },
    fakeAgent,
    steered,
    run: raw => commands.get('brain').handler({ rawInput: raw, agent: fakeAgent }),
  }
}

test('remember without args walks agent -> session -> title and uploads summary', async t => {
  const server = fakeServer()
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  const { ctx, run } = makeCtx({
    server,
    // 单个 agent 自动选中（不弹窗），因此 ask 序列从会话选择开始
    answers: ['会话A'],
    // ask 索引 0 = 选会话（无 custom）；1 = 标题输入
    custom: [undefined, '我的自定义标题'],
  })
  apply(ctx, {
    serverUrl: 'http://brain.test',
    token: 'tok',
    agentId: 'Mac-DSH',
    projectKey: 'alpha',
    summarizeModel: 'deepseek-chat',
  })

  const result = await run('remember')
  assert.equal(result.kind, 'success')
  assert.match(result.text, /我的自定义标题/)
  assert.match(result.text, /已标记/)

  const uploaded = server.memories[0]
  assert.ok(uploaded, 'memory should be uploaded')
  assert.equal(uploaded.title, '我的自定义标题')
  assert.equal(uploaded.content_text, '这是提炼后的记忆内容。')
  assert.equal(uploaded.source_session_id, 'sess-1')
  // 会话已标记为 synced
  const synced = server.sessions.find(s => s.session_id === 'sess-1')
  assert.ok(synced.synced_at !== null)
})

test('remember without args skips when nothing unsynced', async t => {
  const server = fakeServer()
  server.sessions[0].synced_at = '2026-08-21T16:00:00Z'  // 全部已同步
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  const { ctx, run } = makeCtx({ server, answers: [], custom: [] })
  apply(ctx, {
    serverUrl: 'http://brain.test', token: 'tok', agentId: 'Mac-DSH', projectKey: 'alpha',
  })
  const result = await run('remember')
  assert.equal(result.kind, 'success')
  assert.match(result.text, /没有未上传的会话/)
})

test('update without args picks memory then takes new content', async t => {
  const server = fakeServer()
  server.memories.push({
    id: 'm9', current_version: 1, title: '旧记忆', content_text: '旧内容',
    source_agent: 'Mac-DSH', source_session_id: null, updated_at: '2026-08-21T10:00:00Z',
  })
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  const { ctx, run } = makeCtx({
    server,
    // 单个 agent 自动选中；ask 序列：0=选记忆（无 custom）→ 1=新内容输入
    answers: ['旧记忆'],
    custom: [undefined, '全新内容'],
  })
  apply(ctx, {
    serverUrl: 'http://brain.test', token: 'tok', agentId: 'Mac-DSH', projectKey: 'alpha',
  })
  const result = await run('update')
  assert.equal(result.kind, 'success')
  assert.match(result.text, /Updated to v2/)
})
