import { useEffect, useRef, useState } from 'react'
import { API_BASE, api, getSseHeaders } from '../lib/api'
import type { WorkflowJobEvent, WorkflowJobStatus } from '../types'

/** Durable workflow event cursor with SSE first and JSON polling fallback. */
export type WorkflowEventStreamState =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'polling'
  | 'error'
  | 'closed'

interface UseWorkflowEventsOptions {
  enabled?: boolean
  pollIntervalMs?: number
  onEvent?: (event: WorkflowJobEvent) => void
}

const MAX_EVENTS = 200
const MAX_PAGES_PER_POLL = 20
const TERMINAL: Set<WorkflowJobStatus | string> = new Set([
  'completed',
  'failed',
  'cancelled',
  'interrupted',
  'rollback_required',
])

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason)
      return
    }
    const timer = window.setTimeout(resolve, ms)
    signal.addEventListener(
      'abort',
      () => {
        window.clearTimeout(timer)
        reject(signal.reason)
      },
      { once: true },
    )
  })
}

function eventId(event: WorkflowJobEvent): number {
  const value = Number(event.event_id ?? event.seq ?? 0)
  return Number.isFinite(value) ? value : 0
}

export function useWorkflowEvents(
  jobId: string | undefined,
  options: UseWorkflowEventsOptions = {},
) {
  const [events, setEvents] = useState<WorkflowJobEvent[]>([])
  const [cursor, setCursor] = useState(0)
  const [state, setState] = useState<WorkflowEventStreamState>('idle')
  const [error, setError] = useState<string | null>(null)
  const onEventRef = useRef(options.onEvent)
  const generationRef = useRef(0)
  const cursorRef = useRef(0)
  const lastJobIdRef = useRef<string | undefined>(undefined)
  onEventRef.current = options.onEvent

  useEffect(() => {
    const generation = generationRef.current + 1
    generationRef.current = generation
    if (lastJobIdRef.current !== jobId) {
      lastJobIdRef.current = jobId
      setEvents([])
      setCursor(0)
      cursorRef.current = 0
      setError(null)
    }

    if (!jobId || options.enabled === false) {
      setState('idle')
      return
    }

    const controller = new AbortController()
    let currentCursor = cursorRef.current
    const interval = Math.max(1000, options.pollIntervalMs ?? 3000)

    const active = () => !controller.signal.aborted && generationRef.current === generation

    const deliver = (received: WorkflowJobEvent[]) => {
      if (!active() || received.length === 0) return
      cursorRef.current = currentCursor
      setCursor(currentCursor)
      setEvents((previous) => [...previous, ...received].slice(-MAX_EVENTS))
      for (const event of received) onEventRef.current?.(event)
    }

    const pollOnce = async () => {
      let pageCount = 0
      let hasMore = true
      const received: WorkflowJobEvent[] = []
      while (hasMore && pageCount < MAX_PAGES_PER_POLL) {
        const page = await api.workflowJobEvents(jobId, currentCursor, 100, controller.signal)
        if (!active()) return
        pageCount += 1
        for (const event of page.events) {
          const id = eventId(event)
          if (id > currentCursor) {
            currentCursor = id
            received.push(event)
          }
        }
        const next = Number(page.next_after_event_id)
        if (Number.isFinite(next) && next > currentCursor) currentCursor = next
        hasMore = page.has_more === true
      }
      deliver(received)
      setState('polling')
    }

    const consumeStream = async (): Promise<boolean> => {
      const response = await fetch(
        `${API_BASE}/workflows/jobs/${encodeURIComponent(jobId)}/events/stream?after_event_id=${currentCursor}`,
        { headers: getSseHeaders(currentCursor), signal: controller.signal },
      )
      if (!response.ok || !response.body) throw new Error(`事件流连接失败 (${response.status})`)
      setState('connected')
      setError(null)
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let terminal = false
      try {
        while (active()) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
          let boundary = buffer.indexOf('\n\n')
          while (boundary >= 0) {
            const block = buffer.slice(0, boundary)
            buffer = buffer.slice(boundary + 2)
            const data = block
              .split('\n')
              .filter((line) => line.startsWith('data:'))
              .map((line) => line.slice(5).trim())
              .join('\n')
            if (data) {
              const event = JSON.parse(data) as WorkflowJobEvent
              const id = eventId(event)
              if (id > currentCursor) {
                currentCursor = id
                deliver([event])
              }
              const status = event.to_status ?? event.status
              terminal = typeof status === 'string' && TERMINAL.has(status)
            }
            boundary = buffer.indexOf('\n\n')
          }
        }
      } finally {
        try {
          await reader.cancel()
        } catch {
          // The stream may already be closed or aborted.
        }
        reader.releaseLock()
      }
      return terminal
    }

    const run = async () => {
      let attempts = 0
      while (active()) {
        try {
          setState(attempts ? 'reconnecting' : 'connecting')
          const terminal = await consumeStream()
          if (terminal) {
            setState('closed')
            return
          }
          throw new Error('事件流已断开')
        } catch (reason) {
          if (!active()) return
          attempts += 1
          setError(reason instanceof Error ? reason.message : '事件流暂时不可用')
          try {
            await pollOnce()
          } catch (pollReason) {
            if (!active()) return
            setError(pollReason instanceof Error ? pollReason.message : '工作流事件暂时不可用')
            setState('error')
          }
          try {
            await delay(
              Math.min(interval, 1000 * 2 ** Math.min(attempts, 4), 15000),
              controller.signal,
            )
          } catch {
            return
          }
        }
      }
    }

    void run()
    return () => controller.abort()
  }, [jobId, options.enabled, options.pollIntervalMs])

  return { events, cursor, state, error }
}
