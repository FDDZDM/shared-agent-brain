import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import type { PendingOperation, QueueAdapter } from './client.js'

export class JsonOperationQueue implements QueueAdapter {
  private static readonly maxAttempts = 10
  constructor(readonly path: string) {
    mkdirSync(dirname(path), { recursive: true })
  }

  private read(): PendingOperation[] {
    if (!existsSync(this.path)) return []
    try {
      const value = JSON.parse(readFileSync(this.path, 'utf8')) as unknown
      return Array.isArray(value) ? value as PendingOperation[] : []
    } catch (error) {
      throw new Error(`shared-brain queue is unreadable at ${this.path}: ${String(error)}`)
    }
  }

  private write(items: PendingOperation[]): void {
    const temporary = `${this.path}.tmp`
    writeFileSync(temporary, `${JSON.stringify(items, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 })
    renameSync(temporary, this.path)
  }

  enqueue(operation: PendingOperation): void {
    const items = this.read()
    if (!items.some(item => item.opKey === operation.opKey)) {
      items.push(operation)
      this.write(items)
    }
  }

  list(dueOnly = false): PendingOperation[] {
    const now = new Date().toISOString()
    return this.read().filter(item => !dueOnly || (
      item.status !== 'failed' && (!item.nextRetryAt || item.nextRetryAt <= now)
    ))
  }

  remove(opKey: string): void {
    this.write(this.read().filter(item => item.opKey !== opKey))
  }

  fail(opKey: string, error: string, retryable = true): void {
    this.write(this.read().map(item => {
      if (item.opKey !== opKey) return item
      const attempts = item.attempts + 1
      if (!retryable || attempts >= JsonOperationQueue.maxAttempts) {
        return { ...item, attempts, lastError: error.slice(0, 2000), status: 'failed', nextRetryAt: undefined }
      }
      const delayMs = Math.min(2 ** attempts, 3600) * 1000
      return {
        ...item,
        attempts,
        lastError: error.slice(0, 2000),
        status: 'pending',
        nextRetryAt: new Date(Date.now() + delayMs).toISOString(),
      }
    }))
  }
}
