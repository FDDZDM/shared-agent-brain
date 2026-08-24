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
        if (!s.synced_at || s.sync_status === 'changed' || (s.synced_revision ?? 0) < (s.content_revision ?? 0)) {
          byAgent[s.agent_id].unsynced += 1
        }
      }
      return respond(200, {
        items: Object.entries(byAgent).map(([agent_id, c]) => ({ agent_id, total_count: c.total, unsynced_count: c.unsynced })),
      })
    }
    if (path === '/v1/sessions' && init.method === 'POST') {
      let session = sessions.find(s =>
        s.project_key === body.project_key &&
        s.agent_id === body.agent_id &&
        s.device_id === body.device_id &&
        s.session_id === body.session_id)
      if (!session) {
        session = {
          ...body,
          created_at: '2026-08-21T16:00:00Z',
          synced_at: null,
        }
        sessions.push(session)
      } else {
        session.title = body.title
        session.updated_at = body.updated_at
        if (body.content_hash) session.content_hash = body.content_hash
      }
      return respond(200, session)
    }
    if (path === '/v1/sessions') {
      const agent = parsed.searchParams.get('agent')
      const synced = parsed.searchParams.get('synced')
      let items = sessions
      if (agent) items = items.filter(s => s.agent_id === agent)
      if (synced !== null) {
        const wantSynced = synced === 'true'
        items = items.filter(s => {
          const pending = !s.synced_at || s.sync_status === 'changed' || (s.synced_revision ?? 0) < (s.content_revision ?? 0)
          return wantSynced ? !pending : pending
        })
      }
      return respond(200, { items: items.slice().reverse() })
    }
    const syncedMatch = path.match(/^\/v1\/sessions\/([^/]+)\/([^/]+)\/synced$/)
    if (syncedMatch && init.method === 'POST') {
      const s = sessions.find(x => x.agent_id === syncedMatch[1] && x.session_id === syncedMatch[2])
      if (s) s.synced_at = '2026-08-21T18:00:00Z'
      return respond(200, s ?? { agent_id: syncedMatch[1], session_id: syncedMatch[2], synced_at: 'now' })
    }
    const syncMatch = path.match(/^\/v1\/sessions\/([^/]+)\/([^/]+)\/sync$/)
    if (syncMatch && init.method === 'POST') {
      // 复合原子同步：写入记忆并标记会话。
      const s = sessions.find(x => x.agent_id === syncMatch[1] && x.session_id === syncMatch[2])
      let memory = s?.synced_memory_id
        ? memories.find(item => item.id === s.synced_memory_id)
        : undefined
      if (memory) {
        memory.current_version += 1
        memory.title = body.memory.title
        memory.content_text = body.memory.content_text
      } else {
        memory = {
          id: `m${++nextId}`, current_version: 1, title: body.memory.title,
          content_text: body.memory.content_text, source_agent: body.memory.source_agent,
          source_session_id: body.memory.source_session_id,
        }
        memories.push(memory)
      }
      if (s) {
        s.synced_at = '2026-08-21T18:00:00Z'
        s.synced_memory_id = memory.id
        s.sync_status = 'synced'
        s.synced_revision = s.content_revision ?? 1
      }
      return respond(201, {
        memory,
        session: s ?? { agent_id: syncMatch[1], session_id: syncMatch[2], synced_at: 'now', sync_status: 'synced' },
        sync_status: 'synced',
      })
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
    { project_key: 'alpha', agent_id: 'Mac-DSH', device_id: '', session_id: 'sess-1', title: '会话A', created_at: '2026-08-21T16:00:00Z', updated_at: '2026-08-21T17:00:00Z', synced_at: null },
    { project_key: 'alpha', agent_id: 'Mac-DSH', device_id: '', session_id: 'sess-2', title: '会话B', created_at: '2026-08-21T16:30:00Z', updated_at: '2026-08-21T17:30:00Z', synced_at: '2026-08-21T17:31:00Z' },
  )
  return { sessions, memories, fetchImpl }
}

function makeCtx({
  server,
  answers,
  custom,
  cancelAskAt = -1,
  summary = '仅基于所选会话的摘要。',
  titleSnapshot = '会话A',
  sessionEvents,
  agentSessionId = 'verify-session',
}) {
  let askIndex = 0
  const asked = []
  const steered = []
  const llmCalls = []
  const tools = new Map()
  const commands = new Map()
  const hooks = new Map()
  let restartCount = 0
  const fakeAgent = {
    session: { id: agentSessionId },
    options: { provider: 'test-provider', model: 'test-model', maxTokens: 1200 },
    steer: m => steered.push(m),
  }
  return {
    ctx: {
      fiber: { restart: async () => { restartCount += 1 } },
      tools: { register: t => tools.set(t.name, t) },
      commands: { register: d => commands.set(d.name, d) },
      on: (evt, fn) => hooks.set(evt, fn),
      logger: { warn: () => {} },
      get: name => {
        if (name === 'userQuestions') {
          return {
            ask: async req => {
              asked.push(req)
              const idx = askIndex++
              // 模拟用户取消选择弹窗：DSH 宿主抛 UserQuestionError(ASK_CANCELLED)。
              if (idx === cancelAskAt) {
                throw { name: 'UserQuestionError', code: 'ASK_CANCELLED', message: 'the user cancelled ask_user_question' }
              }
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
            readTitle: async () => titleSnapshot,
            readSession: async () => ({
              events: sessionEvents ?? [
                { type: 'turn/start', data: { turn: 1 } },
                { type: 'user/message', data: { source: { kind: 'user' }, content: [{ type: 'text', text: '你好，帮我看看这个项目' }] } },
                { type: 'user/message', data: { source: { kind: 'shared-memory', form: 'reference' }, content: [{ type: 'text', text: '越界的共享记忆内容' }] } },
                { type: 'user/message', data: { source: { kind: 'plugin', plugin: 'shared-brain', form: 'notice' }, content: [{ type: 'text', text: '越界的插件通知' }] } },
                { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: '好的，我来分析' }] } } },
                { type: 'tool/result', data: { content: [{ type: 'text', text: '越界的工具结果' }] } },
                { type: 'turn/end', data: { turn: 1 } },
              ],
            }),
          }
        }
        if (name === 'llm') {
          return {
            stream: async function* (options) {
              llmCalls.push(options)
              yield { type: 'text-delta', index: 0, text: summary }
              yield { type: 'finish', reason: { kind: 'stop' } }
            },
          }
        }
        return undefined
      },
    },
    fakeAgent,
    steered,
    llmCalls,
    asked,
    hooks,
    restartCount: () => restartCount,
    run: raw => commands.get('brain').handler({ rawInput: raw, agent: fakeAgent }),
    callTool: (name, args) => tools.get(name).execute(args, { agent: fakeAgent }),
  }
}

test('remember without args summarizes only the selected session in an isolated model call', async t => {
  const server = fakeServer()
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  const { ctx, run, steered, llmCalls } = makeCtx({
    server,
    // 单个 agent 自动选中（不弹窗），因此 ask 序列从会话选择开始
    answers: ['1. 会话A'],
    // ask 索引 0 = 选会话（无 custom）；1 = 标题输入
    custom: [undefined, '我的自定义标题'],
  })
  apply(ctx, {
    serverUrl: 'http://brain.test',
    token: 'tok',
    agentId: 'Mac-DSH',
    projectKey: 'alpha',
  })

  const result = await run('remember')
  assert.equal(result.kind, 'success')
  assert.equal(result.text, '')
  assert.match(steered.at(-1).content[0].text, /已仅基于所选会话提炼并保存 v1/)
  assert.equal(llmCalls.length, 1)
  assert.equal(llmCalls[0].provider, 'test-provider')
  assert.equal(llmCalls[0].model, 'test-model')
  assert.equal(llmCalls[0].messages.length, 1)
  assert.match(llmCalls[0].system, /不得推断任何其他聊天历史或共享记忆/)
  const isolatedInput = JSON.parse(llmCalls[0].messages[0].content[0].text)
  assert.equal(isolatedInput.source_session_id, 'sess-1')
  assert.match(isolatedInput.conversation, /你好，帮我看看这个项目/)
  assert.doesNotMatch(isolatedInput.conversation, /verify-session/)
  assert.doesNotMatch(isolatedInput.conversation, /越界的共享记忆内容/)
  assert.doesNotMatch(isolatedInput.conversation, /越界的插件通知/)
  assert.doesNotMatch(isolatedInput.conversation, /越界的工具结果/)

  const uploaded = server.memories[0]
  assert.ok(uploaded, 'memory should be uploaded')
  assert.equal(uploaded.title, '我的自定义标题')
  assert.equal(uploaded.content_text, '仅基于所选会话的摘要。')
  assert.equal(uploaded.source_session_id, 'sess-1')
  // 会话已标记为 synced
  const synced = server.sessions.find(s => s.session_id === 'sess-1')
  assert.ok(synced.synced_at !== null)

})

test('remember lists Agent-generated titles with created and modified times, never session tokens', async t => {
  const server = fakeServer()
  server.sessions[0].title = null
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  const { ctx, run, asked } = makeCtx({
    server,
    answers: ['1. Agent 生成的项目审查标题（当前会话）', 'Agent 生成的项目审查标题'],
    custom: [],
    agentSessionId: 'sess-1',
    titleSnapshot: {
      title: 'Agent 生成的项目审查标题',
      updatedAt: Date.parse('2026-08-21T17:05:00Z'),
    },
  })
  apply(ctx, {
    serverUrl: 'http://brain.test', token: 'tok', agentId: 'Mac-DSH', projectKey: 'alpha',
  })

  const result = await run('remember')
  assert.equal(result.kind, 'success')
  const selector = asked[0].questions[0]
  assert.equal(selector.options[0].label, '1. Agent 生成的项目审查标题（当前会话）')
  assert.doesNotMatch(selector.options[0].label, /sess-1/)
  assert.match(selector.options[0].description, /创建/)
  assert.match(selector.options[0].description, /最近修改/)
  assert.equal(server.sessions[0].title, 'Agent 生成的项目审查标题')
})

test('remember excludes recalled memory, tool-use reasoning, and plugin-only handoff turns', async t => {
  const server = fakeServer()
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  const { ctx, run, llmCalls } = makeCtx({
    server,
    answers: ['1. 会话A', '会话A'],
    custom: [],
    sessionEvents: [
      { type: 'turn/start', data: { turn: 1 } },
      { type: 'user/message', data: { source: { kind: 'shared-memory' }, content: [{ type: 'text', text: '错误召回：项目使用 PostgreSQL' }] } },
      { type: 'user/message', data: { source: { kind: 'user' }, content: [{ type: 'text', text: '当前会话事实：项目使用 MongoDB' }] } },
      { type: 'assistant/message', data: { message: { content: [
        { type: 'text', text: '工具调用前推理提到了 PostgreSQL' },
        { type: 'tool-call', name: 'brain_search' },
      ] } } },
      { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: '已确认当前会话使用 MongoDB。' }] } } },
      { type: 'turn/end', data: { turn: 1 } },
      { type: 'turn/start', data: { turn: 2 } },
      { type: 'user/message', data: { source: { kind: 'plugin', plugin: 'shared-brain' }, content: [{ type: 'text', text: '旧版 Shared Brain 会话提炼任务与 handoffId' }] } },
      { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: '错误保存了其他会话的记忆' }] } } },
      { type: 'turn/end', data: { turn: 2 } },
    ],
  })
  apply(ctx, {
    serverUrl: 'http://brain.test', token: 'tok', agentId: 'Mac-DSH', projectKey: 'alpha',
  })

  const result = await run('remember')
  assert.equal(result.kind, 'success')
  const isolatedInput = JSON.parse(llmCalls[0].messages[0].content[0].text)
  assert.match(isolatedInput.conversation, /当前会话事实：项目使用 MongoDB/)
  assert.match(isolatedInput.conversation, /已确认当前会话使用 MongoDB/)
  assert.doesNotMatch(isolatedInput.conversation, /PostgreSQL/)
  assert.doesNotMatch(isolatedInput.conversation, /handoffId/)
  assert.doesNotMatch(isolatedInput.conversation, /错误保存了其他会话/)
})

test('remember keeps recent additions when a session exceeds the transcript budget', async t => {
  const server = fakeServer()
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  const { ctx, run, llmCalls } = makeCtx({
    server,
    answers: ['1. 会话A', '会话A'],
    custom: [],
    sessionEvents: [
      { type: 'turn/start', data: { turn: 1 } },
      { type: 'user/message', data: { source: { kind: 'user' }, content: [{ type: 'text', text: `最初决定：${'甲'.repeat(21_000)}` }] } },
      { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: '已记录最初决定。' }] } } },
      { type: 'turn/end', data: { turn: 1 } },
      { type: 'turn/start', data: { turn: 2 } },
      { type: 'user/message', data: { source: { kind: 'user' }, content: [{ type: 'text', text: 'LATEST-CHANGE：后续改用 Redis 7' }] } },
      { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: '已确认最新变更。' }] } } },
      { type: 'turn/end', data: { turn: 2 } },
    ],
  })
  apply(ctx, {
    serverUrl: 'http://brain.test', token: 'tok', agentId: 'Mac-DSH', projectKey: 'alpha',
  })

  await run('remember')
  const isolatedInput = JSON.parse(llmCalls[0].messages[0].content[0].text)
  assert.match(isolatedInput.conversation, /最初决定/)
  assert.match(isolatedInput.conversation, /LATEST-CHANGE：后续改用 Redis 7/)
  assert.match(isolatedInput.conversation, /中间内容已截断/)
})

test('session/title refreshes the directory after the current Agent generates a title', async t => {
  const server = fakeServer()
  server.sessions[0].title = null
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  const { ctx, hooks } = makeCtx({ server, answers: [], custom: [] })
  apply(ctx, {
    serverUrl: 'http://brain.test', token: 'tok', agentId: 'Mac-DSH', projectKey: 'alpha',
  })

  await hooks.get('session/event')(
    { id: 'sess-1' },
    {
      type: 'session/title',
      seq: 2,
      time: Date.parse('2026-08-21T17:06:00Z'),
      data: { title: '当前 Agent 自动生成的标题', messageSeqs: [1], source: { kind: 'fallback' } },
    },
  )

  assert.equal(server.sessions[0].title, '当前 Agent 自动生成的标题')
  assert.equal(server.sessions[0].updated_at, '2026-08-21T17:06:00.000Z')
})

test('remember without args skips when nothing unsynced', async t => {
  const server = fakeServer()
  server.sessions[0].synced_at = '2026-08-21T16:00:00Z'  // 全部已同步
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  const { ctx, run, steered } = makeCtx({ server, answers: [], custom: [] })
  apply(ctx, {
    serverUrl: 'http://brain.test', token: 'tok', agentId: 'Mac-DSH', projectKey: 'alpha',
  })
  const result = await run('remember')
  assert.equal(result.kind, 'success')
  assert.equal(result.text, '')
  assert.match(steered.at(-1).content[0].text, /没有待同步的会话/)
})

test('remember updates the linked memory when a synced session has changed', async t => {
  const server = fakeServer()
  const linked = {
    id: 'm-linked', current_version: 1, title: '会话A', content_text: '旧摘要',
    source_agent: 'Mac-DSH', source_session_id: 'sess-1',
  }
  server.memories.push(linked)
  Object.assign(server.sessions[0], {
    synced_at: '2026-08-21T17:01:00Z',
    synced_memory_id: linked.id,
    content_revision: 2,
    synced_revision: 1,
    sync_status: 'changed',
  })
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  const { ctx, run, steered } = makeCtx({
    server,
    answers: ['1. 会话A', '会话A'],
    custom: [],
    summary: '包含后续对话的新摘要。',
  })
  apply(ctx, {
    serverUrl: 'http://brain.test', token: 'tok', agentId: 'Mac-DSH', projectKey: 'alpha',
  })

  const result = await run('remember')
  assert.equal(result.kind, 'success')
  assert.match(steered.at(-1).content[0].text, /更新至 v2/)
  assert.equal(server.memories.length, 1)
  assert.equal(server.memories[0].id, 'm-linked')
  assert.equal(server.memories[0].current_version, 2)
  assert.equal(server.memories[0].content_text, '包含后续对话的新摘要。')
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
  const { ctx, run, steered } = makeCtx({
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
  assert.equal(result.text, '')
  assert.match(steered.at(-1).content[0].text, /Updated to v2/)
})

test('help renders in the session without waking the Agent and bare brain is not help', async t => {
  const server = fakeServer()
  server.memories.push({
    id: 'm-help', current_version: 1, title: '可浏览记忆', content_text: '会话可见结果',
    source_agent: 'Mac-DSH', source_session_id: null, updated_at: '2026-08-21T10:00:00Z',
  })
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  const { ctx, run, steered } = makeCtx({ server, answers: ['记忆', '可浏览记忆'], custom: [] })
  apply(ctx, { serverUrl: 'http://brain.test', token: 'tok', agentId: 'Mac-DSH', projectKey: 'alpha' })

  const help = await run('help')
  assert.equal(help.kind, 'success')
  assert.equal(help.text, '')
  assert.match(steered[0].content[0].text, /\/brain search/)

  const bare = await run('')
  assert.equal(bare.kind, 'success')
  assert.equal(bare.text, '')
  assert.doesNotMatch(steered[1].content[0].text, /命令说明书/)
  assert.match(steered[1].content[0].text, /会话可见结果/)
  assert.equal(steered.length, 2)
})

test('cancelling a native selector is a silent no-op', async t => {
  const server = fakeServer()
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  const { ctx, run, steered } = makeCtx({ server, answers: [], custom: [] })
  apply(ctx, { serverUrl: 'http://brain.test', token: 'tok', agentId: 'Mac-DSH', projectKey: 'alpha' })

  const result = await run('')
  assert.equal(result.kind, 'success')
  assert.equal(result.text, '')
  assert.equal(steered.length, 0)
})

test('browse cancellation is a silent success, not a failed error', async t => {
  const server = fakeServer()
  // 需要第二个 agent 才会触发 agent 选择弹窗
  server.sessions.push({
    agent_id: 'agent-2', session_id: 'sess-3', title: '会话C',
    updated_at: '2026-08-21T18:00:00Z', synced_at: null,
  })
  server.memories.push({
    id: 'm-cancel', current_version: 1, title: '取消测试记忆', content_text: 'x',
    source_agent: 'agent-2', source_session_id: null, updated_at: '2026-08-21T10:00:00Z',
  })
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  // 在第一个弹窗（选 agent）就取消
  const { ctx, run, steered } = makeCtx({ server, answers: [], custom: [], cancelAskAt: 0 })
  apply(ctx, { serverUrl: 'http://brain.test', token: 'tok', agentId: 'Mac-DSH', projectKey: 'alpha' })

  const res = await run('')
  assert.equal(res.kind, 'success')
  assert.equal(res.text, '')
  assert.doesNotMatch(res.text, /failed/)
  assert.equal(steered.length, 0)
})

test('browse cancellation later in the flow still cancels, not fails', async t => {
  const server = fakeServer()
  server.sessions.push({
    agent_id: 'agent-2', session_id: 'sess-3', title: '会话C',
    updated_at: '2026-08-21T18:00:00Z', synced_at: null,
  })
  server.memories.push({
    id: 'm-cancel', current_version: 1, title: '取消测试记忆', content_text: 'x',
    source_agent: 'agent-2', source_session_id: null, updated_at: '2026-08-21T10:00:00Z',
  })
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  // 选 agent(0) 正常 → 选「记忆」(1) 正常 → 选具体记忆(2) 时取消
  const { ctx, run } = makeCtx({ server, answers: ['agent-2', '记忆', '取消测试记忆'], custom: [], cancelAskAt: 2 })
  apply(ctx, { serverUrl: 'http://brain.test', token: 'tok', agentId: 'Mac-DSH', projectKey: 'alpha' })

  const res = await run('')
  assert.equal(res.kind, 'success')
  assert.equal(res.text, '')
  assert.doesNotMatch(res.text, /failed/)
})

test('a steered command result is not returned for duplicate command-plane rendering', async t => {
  const server = fakeServer()
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  const { ctx, run, steered, hooks } = makeCtx({ server, answers: [], custom: [] })
  apply(ctx, { serverUrl: 'http://brain.test', token: 'tok', agentId: 'Mac-DSH', projectKey: 'alpha' })

  const result = await run('remember 项目数据库 | PostgreSQL 16')

  assert.equal(result.kind, 'success')
  assert.equal(result.text, '')
  assert.equal(steered.length, 1)
  assert.equal(steered[0].source.kind, 'plugin')
  assert.equal(steered[0].source.plugin, 'shared-brain')
  assert.equal(steered[0].source.form, 'notice')

  const decision = await hooks.get('agent/pre-step')(
    { messages: [steered[0]], step: 1, signal: new AbortController().signal },
    async () => ({ kind: 'enter', messages: [steered[0]] }),
  )
  assert.deepEqual(decision, { kind: 'reject' })
})

test('usage errors render as session notices without terminal duplication', async t => {
  const server = fakeServer()
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  const { ctx, run, steered } = makeCtx({ server, answers: [], custom: [] })
  apply(ctx, { serverUrl: 'http://brain.test', token: 'tok', agentId: 'Mac-DSH', projectKey: 'alpha' })

  const usage = await run('nonexistent-subcommand')
  assert.equal(usage.kind, 'success')
  assert.equal(usage.text, '')
  assert.match(steered[0].content[0].text, /Usage/)
})

test('forget failures render in the session and never become model input', async t => {
  const server = fakeServer()
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  const { ctx, run, steered, hooks } = makeCtx({ server, answers: [], custom: [] })
  apply(ctx, { serverUrl: 'http://brain.test', token: 'tok', agentId: 'Mac-DSH', projectKey: 'alpha' })

  const result = await run('forget missing-memory 1')
  assert.equal(result.kind, 'success')
  assert.equal(result.text, '')
  assert.match(steered[0].content[0].text, /Shared Brain forget failed/)
  const decision = await hooks.get('agent/pre-step')(
    { messages: [steered[0]], step: 1, signal: new AbortController().signal },
    async () => ({ kind: 'enter', messages: [steered[0]] }),
  )
  assert.deepEqual(decision, { kind: 'reject' })
})

test('setup validates the runtime and hot-reloads the plugin after replying', async t => {
  const server = fakeServer()
  const originalFetch = globalThis.fetch
  globalThis.fetch = server.fetchImpl
  t.after(() => { globalThis.fetch = originalFetch })
  const { ctx, run, steered, restartCount } = makeCtx({ server, answers: [], custom: [] })
  apply(ctx, { serverUrl: 'http://brain.test', token: 'tok', agentId: 'Mac-DSH', projectKey: 'alpha' })

  const result = await run('setup')
  assert.equal(result.kind, 'success')
  assert.equal(result.text, '')
  assert.match(steered.at(-1).content[0].text, /热重载/)
  assert.equal(restartCount(), 0, 'reload must not dispose the command before it replies')
  await new Promise(resolve => setTimeout(resolve, 10))
  assert.equal(restartCount(), 1)
})
