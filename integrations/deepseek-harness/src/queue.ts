import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import type { PendingOperation, QueueAdapter } from './client.js'

export class JsonOperationQueue implements QueueAdapter {
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

  list(): PendingOperation[] {
    return this.read()
  }

  remove(opKey: string): void {
    this.write(this.read().filter(item => item.opKey !== opKey))
  }

  fail(opKey: string, error: string): void {
    this.write(this.read().map(item => item.opKey === opKey
      ? { ...item, attempts: item.attempts + 1, lastError: error.slice(0, 2000) }
      : item))
  }
}
