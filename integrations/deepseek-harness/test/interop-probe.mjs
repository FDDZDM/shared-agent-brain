import { readFileSync } from 'node:fs'

import { BrainHttpError, SharedBrainClient } from '../lib/client.js'
import { JsonOperationQueue } from '../lib/queue.js'

const input = JSON.parse(readFileSync(0, 'utf8'))
const client = new SharedBrainClient({
  serverUrl: input.serverUrl,
  token: input.token,
  agentId: 'dsh',
  projectKey: 'alpha',
  queue: new JsonOperationQueue(input.queuePath),
  timeoutMs: 10_000,
})

try {
  let result
  if (input.action === 'remember') {
    result = await client.remember({ title: input.title, content: input.content, sessionId: input.sessionId })
  } else if (input.action === 'search') {
    result = await client.search(input.query)
  } else if (input.action === 'update') {
    result = await client.update({
      memoryId: input.memoryId,
      expectedVersion: input.expectedVersion,
      content: input.content,
    })
  } else if (input.action === 'sync') {
    result = await client.syncSession({
      agentId: 'dsh',
      sessionId: input.sessionId,
      title: input.title,
      content: input.content,
      projectKey: 'alpha',
      deviceId: 'dsh-device',
      contentHash: input.contentHash,
    })
  } else {
    throw new Error(`unknown action: ${input.action}`)
  }
  process.stdout.write(JSON.stringify({ ok: true, result }))
} catch (error) {
  process.stdout.write(JSON.stringify({
    ok: false,
    status: error instanceof BrainHttpError ? error.status : null,
    error: String(error),
  }))
}
