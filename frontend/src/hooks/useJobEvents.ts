import { useCallback, useEffect, useRef, useState } from 'react'
import { API_BASE, getSseHeaders } from '../lib/api'
import type { JobEvent } from '../types'

export type StreamState = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'closed'

interface UseJobEventsOptions {
  enabled?: boolean
  onEvent?: (event: JobEvent) => void
  restartKey?: number
}

const TERMINAL = new Set(['cancelled', 'succeeded', 'failed', 'interrupted'])

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, ms)
    signal.addEventListener('abort', () => {
      window.clearTimeout(timer)
      reject(signal.reason)
    }, { once: true })
  })
}

export function useJobEvents(jobId: string | undefined, options: UseJobEventsOptions = {}) {
  const [streamState, setStreamState] = useState<StreamState>('idle')
  const [event, setEvent] = useState<JobEvent | null>(null)
  const [error, setError] = useState<string | null>(null)
  const lastSeq = useRef<number | undefined>(undefined)
  const lastJobId = useRef<string | undefined>(undefined)
  const onEventRef = useRef(options.onEvent)
  onEventRef.current = options.onEvent

  const consume = useCallback(async (response: Response, signal: AbortSignal) => {
    if (!response.ok || !response.body) throw new Error(`事件流连接失败 (${response.status})`)
    setStreamState('connected')
    setError(null)
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (!signal.aborted) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const lines = block.split('\n')
        const data = lines.filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trim()).join('\n')
        const id = lines.find((line) => line.startsWith('id:'))?.slice(3).trim()
        if (id && Number.isFinite(Number(id))) lastSeq.current = Number(id)
        if (data) {
          const parsed = JSON.parse(data) as JobEvent
          lastSeq.current = parsed.seq ?? lastSeq.current
          setEvent(parsed)
          onEventRef.current?.(parsed)
          if (TERMINAL.has(parsed.state)) return true
        }
        boundary = buffer.indexOf('\n\n')
      }
    }
    return false
  }, [])

  useEffect(() => {
    // A new job has its own event sequence. Never let the terminal event or
    // Last-Event-ID from the previous run trigger/load the new job's results.
    if (lastJobId.current !== jobId) {
      lastJobId.current = jobId
      lastSeq.current = undefined
      setEvent(null)
      setError(null)
    }
    if (!jobId || options.enabled === false) {
      setStreamState('idle')
      return
    }

    const controller = new AbortController()
    setEvent(null)
    setError(null)
    setStreamState('idle')
    let attempts = 0
    const run = async () => {
      while (!controller.signal.aborted) {
        try {
          setStreamState(attempts ? 'reconnecting' : 'connecting')
          const response = await fetch(`${API_BASE}/jobs/${encodeURIComponent(jobId)}/events`, {
            headers: getSseHeaders(lastSeq.current),
            signal: controller.signal,
          })
          const terminal = await consume(response, controller.signal)
          if (terminal) {
            setStreamState('closed')
            return
          }
          attempts += 1
          setError('事件流已断开')
          setStreamState('reconnecting')
        } catch (reason) {
          if (controller.signal.aborted) return
          setError(reason instanceof Error ? reason.message : '事件流已断开')
          setStreamState('reconnecting')
          attempts += 1
        }
        try {
          await delay(Math.min(1000 * 2 ** Math.min(attempts, 4), 15000), controller.signal)
        } catch {
          return
        }
      }
    }
    void run()
    return () => controller.abort()
  }, [consume, jobId, options.enabled, options.restartKey])

  return { streamState, event, error }
}
