import { randomUUID } from 'node:crypto'
import { BrainHttpError, SharedBrainClient } from './client.js'

/**
 * Shared Brain self-test suite.
 *
 * Mirrored by src/shared_brain/selftest.py on the Hermes side. The suite runs
 * against the live server through the real client (except T1/T2/T9, which use
 * a bare fetch to exercise unauthenticated and idempotency paths). Test data
 * lives in the isolated `__selftest__` project and is cleaned up afterwards.
 */

export const SELFTEST_PROJECT = '__selftest__'

export type TestStatus = 'pass' | 'fail' | 'skip'

export interface TestResult {
  id: string
  name: string
  status: TestStatus
  detail: string
  durationMs: number
}

export interface TestReport {
  serverUrl: string
  projectKey: string
  agentId: string
  mode: 'full' | 'quick'
  startedAt: string
  durationMs: number
  results: TestResult[]
  passed: boolean
  text: string
}

function ok(detail: string): { pass: boolean; detail: string } {
  return { pass: true, detail }
}

function bad(detail: string): { pass: boolean; detail: string } {
  return { pass: false, detail }
}

async function runTest(
  id: string,
  name: string,
  fn: () => Promise<{ pass: boolean; detail: string }>,
): Promise<TestResult> {
  const started = performance.now()
  try {
    const outcome = await fn()
    return {
      id,
      name,
      status: outcome.pass ? 'pass' : 'fail',
      detail: outcome.detail,
      durationMs: Math.round(performance.now() - started),
    }
  } catch (error) {
    const detail = error instanceof BrainHttpError
      ? `HTTP ${error.status}: ${error.body.slice(0, 160)}`
      : String(error).slice(0, 200)
    return { id, name, status: 'fail', detail, durationMs: Math.round(performance.now() - started) }
  }
}

function skipped(id: string, name: string, reason: string): TestResult {
  return { id, name, status: 'skip', detail: reason, durationMs: 0 }
}

export function renderTestReport(report: TestReport): string {
  const lines: string[] = [
    '🧠 Shared Brain 自检报告',
    '━━━━━━━━━━━━━━━━━━━━━━━━',
    `环境: ${report.serverUrl} · project: ${report.projectKey} · agent: ${report.agentId}`,
    `时间: ${report.startedAt} · 模式: ${report.mode} · 耗时: ${(report.durationMs / 1000).toFixed(1)}s`,
    '',
  ]
  for (const r of report.results) {
    const icon = r.status === 'pass' ? '✅' : r.status === 'fail' ? '❌' : '⏭'
    const suffix = r.status === 'skip' ? '' : ` (${r.durationMs}ms)`
    lines.push(`${icon} ${r.id} ${r.name} | ${r.detail}${suffix}`)
  }
  const counts = report.results.reduce(
    (acc, r) => {
      acc[r.status] += 1
      return acc
    },
    { pass: 0, fail: 0, skip: 0 },
  )
  lines.push('')
  lines.push(`结果: ${counts.pass} 通过 / ${counts.fail} 失败 / ${counts.skip} 跳过 → ${report.passed ? '✅ PASS' : '❌ FAIL'}`)
  return lines.join('\n')
}

export async function runSelftest(
  client: SharedBrainClient,
  options: { quick?: boolean } = {},
): Promise<TestReport> {
  const mode = options.quick ? 'quick' : 'full'
  const startedAt = new Date().toISOString()
  const started = performance.now()
  const { serverUrl, token, projectKey, agentId } = client.options
  const base = serverUrl.replace(/\/$/, '')
  const fetchImpl = client.options.fetch ?? globalThis.fetch
  const results: TestResult[] = []
  let aborted = false

  const t = async (id: string, name: string, fn: () => Promise<{ pass: boolean; detail: string }>): Promise<void> => {
    if (aborted) {
      results.push(skipped(id, name, '前置失败，跳过'))
      return
    }
    const result = await runTest(id, name, fn)
    results.push(result)
    if (result.status === 'fail' && (id === 'T1' || id === 'T3')) aborted = true
  }

  // 测试数据：独立项目 + 唯一 marker；T12 负责全部清理。
  const marker = `selftest-${Date.now().toString(36)}-${randomUUID().slice(0, 8)}`
  const created: { id: string; version: number }[] = []
  const seeded = { title: `自测检索验证 ${marker}`, content: `自测检索验证 ${marker} Shared Brain selftest` }
  const noData = (): { pass: boolean; detail: string } => bad('无测试数据（T4 失败）')
  const firstCreated = (): { id: string; version: number } | null => created[0] ?? null

  await t('T1', '服务器可达', async () => {
    const response = await fetchImpl(`${base}/health`, { signal: AbortSignal.timeout(5_000) })
    return response.status === 200 ? ok(`HTTP ${response.status}`) : bad(`期望 200, 实得 ${response.status}`)
  })

  await t('T2', '鉴权拦截', async () => {
    const response = await fetchImpl(`${base}/v1/memories/search?q=selftest`, {
      signal: AbortSignal.timeout(5_000),
    })
    return response.status === 401 ? ok('401') : bad(`期望 401, 实得 ${response.status}`)
  })

  await t('T3', '配置完整性', async () => {
    const missing: string[] = []
    if (!serverUrl) missing.push('server_url')
    if (!token) missing.push('token')
    if (!projectKey) missing.push('project_key')
    return missing.length === 0 ? ok('已配置') : bad(`缺少: ${missing.join(', ')}`)
  })

  await t('T4', '写入', async () => {
    const record = await client.remember({
      title: seeded.title,
      content: seeded.content,
      scope: 'project',
      kind: 'fact',
      projectKey: SELFTEST_PROJECT,
    })
    if ('queued' in record) return bad('写入了离线队列（服务器不可达？）')
    created.push({ id: record.id, version: record.current_version })
    return ok(`id=${record.id.slice(0, 8)}…, v${record.current_version}`)
  })

  await t('T5', '中文检索(FTS)', async () => {
    const record = firstCreated()
    if (!record) return noData()
    const items = await client.search('自测检索', 5, SELFTEST_PROJECT)
    return items.some(item => item.content_text.includes(marker))
      ? ok(`${items.length} 条命中`)
      : bad(`期望命中含 ${marker}, 实得 ${items.length} 条`)
  })

  if (mode === 'full') {
    await t('T6', '短词检索(LIKE)', async () => {
      const record = firstCreated()
      if (!record) return noData()
      const items = await client.search('自测', 5, SELFTEST_PROJECT)
      return items.some(item => item.content_text.includes(marker))
        ? ok(`${items.length} 条命中`)
        : bad(`期望命中含 ${marker}, 实得 ${items.length} 条`)
    })

    await t('T7', '乐观锁', async () => {
      const record = firstCreated()
      if (!record) return noData()
      try {
        await client.update({ memoryId: record.id, expectedVersion: 99_999, content: 'stale write' })
        return bad('期望 409, 实得成功')
      } catch (error) {
        if (error instanceof BrainHttpError && error.status === 409) return ok('409')
        return bad(`期望 409, 实得 ${error instanceof BrainHttpError ? `HTTP ${error.status}` : String(error)}`)
      }
    })

    await t('T8', '版本更新', async () => {
      const record = firstCreated()
      if (!record) return noData()
      const previous = record.version
      const updated = await client.update({
        memoryId: record.id,
        expectedVersion: previous,
        content: `${seeded.content} v2`,
      })
      if ('queued' in updated) return bad('入队离线（未同步）')
      record.version = updated.current_version
      return updated.current_version === previous + 1
        ? ok(`v${previous}→v${updated.current_version}`)
        : bad(`期望 v${previous + 1}, 实得 v${updated.current_version}`)
    })

    await t('T9', '幂等', async () => {
      const opKey = `selftest-idem-${marker}`
      const payload = {
        scope: 'project',
        kind: 'fact',
        project_key: SELFTEST_PROJECT,
        title: `幂等验证 ${marker}`,
        content_text: `幂等验证 ${marker}`,
        source_agent: agentId,
        trust_level: 0,
      }
      const post = (): Promise<Record<string, unknown>> =>
        fetchImpl(`${base}/v1/memories`, {
          method: 'POST',
          signal: AbortSignal.timeout(5_000),
          headers: {
            authorization: `Bearer ${token}`,
            'content-type': 'application/json',
            'idempotency-key': opKey,
          },
          body: JSON.stringify(payload),
        }).then(response => response.json() as Promise<Record<string, unknown>>)
      const first = await post()
      const second = await post()
      if (first.id !== second.id) {
        return bad(`同 op_key 两次返回不同 id: ${String(first.id)} vs ${String(second.id)}`)
      }
      if (typeof first.id === 'string' && !created.some(item => item.id === first.id)) {
        created.push({ id: first.id, version: Number(first.current_version ?? 1) })
      }
      return ok(`同 id=${String(first.id).slice(0, 8)}…`)
    })

    await t('T10', '项目隔离', async () => {
      const record = firstCreated()
      if (!record) return noData()
      const items = await client.search(marker, 5)
      return items.length === 0 ? ok('0 命中') : bad(`期望 0 命中, 实得 ${items.length}`)
    })

    await t('T11', 'tombstone 删除', async () => {
      const target = firstCreated()
      if (!target) return noData()
      const record = await client.forget(target.id, target.version)
      if ('queued' in record) return bad('入队离线（未同步）')
      // 只匹配 T4 创建的那条（幂等测试记录不含 `自测检索验证` 前缀）。
      const items = await client.search(`自测检索验证 ${marker}`, 5, SELFTEST_PROJECT)
      return items.length === 0 ? ok('已删除') : bad(`删除后仍命中 ${items.length} 条`)
    })
  }

  await t('T12', '数据清理', async () => {
    // 1) 本轮创建的记录（含幂等测试记录）。
    for (const item of created) {
      try {
        await client.forget(item.id, item.version)
      } catch {
        // best-effort: 已删/版本冲突的记录无需处理
      }
    }
    // 2) 历史残留：此前运行中断或 quick 模式遗留的 `自测检索验证` 记录。
    const leftovers = await client.search('自测检索验证', 20, SELFTEST_PROJECT)
    for (const item of leftovers) {
      try {
        await client.forget(item.id, item.current_version)
      } catch {
        // best-effort
      }
    }
    const items = await client.search('selftest-', 20, SELFTEST_PROJECT)
    return items.length === 0 ? ok('已清理') : bad(`残留 ${items.length} 条`)
  })

  const report: TestReport = {
    serverUrl,
    projectKey,
    agentId,
    mode,
    startedAt,
    durationMs: Math.round(performance.now() - started),
    results,
    passed: results.every(result => result.status !== 'fail'),
    text: '',
  }
  report.text = renderTestReport(report)
  return report
}
