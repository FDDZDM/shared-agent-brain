import assert from 'node:assert/strict'
import test from 'node:test'

import { SharedBrainClient } from '../lib/client.js'
import { JsonOperationQueue } from '../lib/queue.js'
import { renderTestReport, runSelftest, SELFTEST_PROJECT } from '../lib/selftest.js'

/** In-memory fake server keyed by URL, mirroring the real REST surface. */
function fakeServer({ healthStatus = 200 } = {}) {
  const memories = []
  let nextId = 0
  const fetchImpl = async (url, init = {}) => {
    const parsed = new URL(url)
    const path = parsed.pathname
    const json = async () => JSON.parse(init.body ?? '{}')
    const respond = (status, data) =>
      new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } })

    if (path === '/health') return respond(healthStatus, { status: 'ok' })
    if (path === '/v1/memories/search') {
      const q = parsed.searchParams.get('q') ?? ''
      const pk = parsed.searchParams.get('project_key')
      if (!init.headers?.authorization) return respond(401, { error: 'unauthorized' })
      // Mirror the real trigram OR behavior for T11: records from the same run
      // share the selftest marker, so deleting one does not imply zero results.
      const marker = q.startsWith('自测检索验证 selftest-') ? q.split(' ').at(-1) : null
      const items = memories.filter(m => m._project_key === pk
        && (m.content_text.includes(q) || (marker && m.content_text.includes(marker))))
      return respond(200, { items })
    }
    if (path === '/v1/memories' && init.method === 'POST') {
      if (!init.headers?.authorization) return respond(401, { error: 'unauthorized' })
      const payload = await json()
      const existing = memories.find(m => m.op_key === init.headers['idempotency-key'])
      if (existing) return respond(201, existing)
      const record = {
        id: `m${++nextId}`,
        current_version: 1,
        title: payload.title,
        content_text: payload.content_text,
        _project_key: payload.project_key,
        op_key: init.headers['idempotency-key'],
      }
      memories.push(record)
      return respond(201, record)
    }
    const versionMatch = path.match(/^\/v1\/memories\/([^/]+)\/versions$/)
    if (versionMatch && init.method === 'POST') {
      const record = memories.find(m => m.id === versionMatch[1])
      if (!record) return respond(404, { error: 'not found' })
      const payload = await json()
      if (payload.expected_version !== record.current_version) {
        return respond(409, { error: 'version conflict' })
      }
      if (payload.content_text !== undefined) record.content_text = payload.content_text
      record.current_version += 1
      return respond(200, record)
    }
    const deleteMatch = path.match(/^\/v1\/memories\/([^/]+)$/)
    if (deleteMatch && init.method === 'DELETE') {
      const index = memories.findIndex(m => m.id === deleteMatch[1])
      if (index === -1) return respond(404, { error: 'not found' })
      const record = memories[index]
      const payload = await json()
      if (payload.expected_version !== record.current_version) {
        return respond(409, { error: 'version conflict' })
      }
      memories.splice(index, 1)
      return respond(200, record)
    }
    return respond(404, { error: 'not found' })
  }
  return { memories, fetchImpl }
}

function makeClient(fetchImpl) {
  return new SharedBrainClient({
    serverUrl: 'http://brain.test',
    token: 'tok',
    agentId: 'deepseek-harness',
    projectKey: 'alpha',
    queue: new JsonOperationQueue('/tmp/queue-selftest-test.json'),
    fetch: fetchImpl,
  })
}

test('full selftest passes all 12 items and leaves no test data', async () => {
  const server = fakeServer()
  const report = await runSelftest(makeClient(server.fetchImpl))
  assert.equal(report.passed, true)
  assert.deepEqual(report.results.map(r => r.id), ['T1', 'T2', 'T3', 'T4', 'T5', 'T6', 'T7', 'T8', 'T9', 'T10', 'T11', 'T12'])
  assert.ok(report.results.every(r => r.status === 'pass'))
  assert.match(report.results.find(r => r.id === 'T11').detail, /另有 1 条同批次命中/)
  assert.ok(server.memories.every(m => m._project_key !== SELFTEST_PROJECT))
})

test('quick selftest runs the first five items plus cleanup', async () => {
  const server = fakeServer()
  const report = await runSelftest(makeClient(server.fetchImpl), { quick: true })
  assert.equal(report.mode, 'quick')
  // quick = 连通/读写 5 项 + 清理（quick 也会创建测试数据，必须一并清理）。
  assert.deepEqual(report.results.map(r => r.id), ['T1', 'T2', 'T3', 'T4', 'T5', 'T12'])
  assert.equal(report.passed, true)
})

test('server down fails T1 and skips the rest', async () => {
  const server = fakeServer({ healthStatus: 500 })
  const report = await runSelftest(makeClient(server.fetchImpl))
  assert.equal(report.passed, false)
  assert.equal(report.results[0].status, 'fail')
  assert.ok(report.results.slice(1).every(r => r.status === 'skip'))
})

test('render includes verdict and counts', async () => {
  const server = fakeServer()
  const report = await runSelftest(makeClient(server.fetchImpl))
  const text = renderTestReport(report)
  assert.match(text, /🧠 Shared Brain 自检报告/)
  assert.match(text, /12 通过 \/ 0 失败 \/ 0 跳过/)
  assert.match(text, /✅ PASS/)
  assert.match(text, /模式: full/)
})
