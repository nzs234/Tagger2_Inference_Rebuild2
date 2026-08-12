import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import type { WorkflowJobEvent } from '../types'

/**
 * Durable workflow event cursor state.
 *
 * The workflow API exposes a finite, replayable JSON page rather than a
 * long-lived stream.  Polling that endpoint with `after_event_id` gives the
 * UI the same reconnect semantics as SSE while still working through the
 * authenticated fetch wrapper and ordinary reverse proxies.
 */
export type WorkflowEventStreamState = 'idle' | 'polling' | 'error'

interface UseWorkflowEventsOptions {
  enabled?: boolean
  pollIntervalMs?: number
  onEvent?: (event: WorkflowJobEvent) => void
}

const MAX_EVENTS = 200
const MAX_PAGES_PER_POLL = 20

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
  onEventRef.current = options.onEvent

  useEffect(() => {
    const generation = generationRef.current + 1
    generationRef.current = generation
    setEvents([])
    setCursor(0)
    setError(null)

    if (!jobId || options.enabled === false) {
      setState('idle')
      return
    }

    const controller = new AbortController()
    let timer: number | undefined
    let currentCursor = 0
    const interval = Math.max(1000, options.pollIntervalMs ?? 3000)

    const schedule = () => {
      if (!controller.signal.aborted && generationRef.current === generation) {
        timer = window.setTimeout(() => void poll(), interval)
      }
    }

    const poll = async () => {
      if (controller.signal.aborted || generationRef.current !== generation) return
      setState('polling')
      try {
        let pageCount = 0
        let hasMore = true
        const received: WorkflowJobEvent[] = []
        while (hasMore && pageCount < MAX_PAGES_PER_POLL) {
          const page = await api.workflowJobEvents(jobId, currentCursor, 100, controller.signal)
          if (controller.signal.aborted || generationRef.current !== generation) return
          pageCount += 1
          for (const event of page.events) {
            // The server cursor is authoritative.  Ignore duplicate rows if
            // a proxy retries a page or a reconnect races with this poll.
            const eventId = Number(event.event_id ?? event.seq ?? 0)
            if (eventId > currentCursor) {
              currentCursor = eventId
              received.push(event)
              onEventRef.current?.(event)
            }
          }
          const next = Number(page.next_after_event_id)
          if (Number.isFinite(next) && next > currentCursor) currentCursor = next
          hasMore = page.has_more === true
        }
        if (generationRef.current !== generation || controller.signal.aborted) return
        if (received.length > 0) {
          setEvents((previous) => [...previous, ...received].slice(-MAX_EVENTS))
          setCursor(currentCursor)
        } else {
          setCursor((previous) => Math.max(previous, currentCursor))
        }
        setError(null)
        setState('polling')
      } catch (reason) {
        if (controller.signal.aborted || generationRef.current !== generation) return
        setError(reason instanceof Error ? reason.message : '工作流事件暂时不可用')
        setState('error')
      } finally {
        schedule()
      }
    }

    void poll()
    return () => {
      controller.abort()
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [jobId, options.enabled, options.pollIntervalMs])

  return { events, cursor, state, error }
}

