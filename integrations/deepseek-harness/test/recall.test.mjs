import assert from 'node:assert/strict'
import test from 'node:test'

import { userText } from '../lib/index.js'

test('auto recall uses only the last user message, not the full history', () => {
  const messages = [
    { source: { kind: 'user' }, content: [{ type: 'text', text: '早上的旧问题' }] },
    { source: { kind: 'shared-memory', form: 'reference' }, content: [{ type: 'text', text: '召回资料，不应混入' }] },
    { source: { kind: 'user' }, content: [{ type: 'text', text: '现在问的是这个' }] },
  ]
  assert.equal(userText(messages), '现在问的是这个')
})

test('auto recall query is capped at 1000 characters', () => {
  const long = 'x'.repeat(2000)
  const messages = [{ source: { kind: 'user' }, content: [{ type: 'text', text: long }] }]
  assert.equal(userText(messages).length, 1000)
})

test('auto recall ignores non-user messages entirely', () => {
  const messages = [
    { source: { kind: 'assistant' }, content: [{ type: 'text', text: '助手输出' }] },
    { source: { kind: 'plugin', plugin: 'shared-brain', form: 'notice' }, content: [{ type: 'text', text: '命令 notice' }] },
    { source: { kind: 'tool' }, content: [{ type: 'text', text: '工具结果' }] },
  ]
  assert.equal(userText(messages), '')
})

test('auto recall does not reuse an older user message for a later plugin notice', () => {
  const messages = [
    { source: { kind: 'user' }, content: [{ type: 'text', text: '旧问题不应再次召回' }] },
    { source: { kind: 'plugin', plugin: 'shared-brain', form: 'notice' }, content: [{ type: 'text', text: '隔离提炼完成' }] },
  ]
  assert.equal(userText(messages), '')
})
