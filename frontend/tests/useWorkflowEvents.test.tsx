import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useWorkflowEvents } from '../src/hooks/useWorkflowEvents'
import { api } from '../src/lib/api'
import type { WorkflowJobEvent } from '../src/types'

function workflowEvent(jobId: string, eventId: number): WorkflowJobEvent {
  return { job_id: jobId, event_id: eventId, event_type: 'progress' }
}

function page(jobId: string, events: WorkflowJobEvent[], hasMore = false) {
  const next = events.at(-1)?.event_id ?? 0
  return { job_id: jobId, events, next_after_event_id: next, has_more: hasMore }
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('useWorkflowEvents', () => {
  it('does not request events while disabled', () => {
    const request = vi.spyOn(api, 'workflowJobEvents')
    const { result } = renderHook(() => useWorkflowEvents('job-1', { enabled: false }))

    expect(request).not.toHaveBeenCalled()
    expect(result.current).toMatchObject({ events: [], cursor: 0, state: 'idle', error: null })
  })

  it('resets event history and cursor when the selected job changes', async () => {
    const request = vi.spyOn(api, 'workflowJobEvents').mockImplementation(async (jobId) => (
      page(jobId, [workflowEvent(jobId, jobId === 'job-1' ? 4 : 1)])
    ))
    const { result, rerender } = renderHook(
      ({ jobId }) => useWorkflowEvents(jobId, { pollIntervalMs: 60_000 }),
      { initialProps: { jobId: 'job-1' } },
    )
    await waitFor(() => expect(result.current.cursor).toBe(4))

    rerender({ jobId: 'job-2' })

    await waitFor(() => expect(result.current.events).toEqual([workflowEvent('job-2', 1)]))
    expect(result.current.cursor).toBe(1)
    expect(request).toHaveBeenCalledWith('job-2', 0, 100, expect.any(AbortSignal))
  })

  it('retains history and resumes from its cursor after polling is disabled', async () => {
    const delivered = vi.fn()
    const request = vi.spyOn(api, 'workflowJobEvents').mockResolvedValue(
      page('job-1', [workflowEvent('job-1', 5)]),
    )
    const { result, rerender } = renderHook(
      ({ enabled }) => useWorkflowEvents('job-1', {
        enabled,
        pollIntervalMs: 60_000,
        onEvent: delivered,
      }),
      { initialProps: { enabled: true } },
    )
    await waitFor(() => expect(result.current.cursor).toBe(5))

    rerender({ enabled: false })
    expect(result.current.state).toBe('idle')
    expect(result.current.events).toEqual([workflowEvent('job-1', 5)])

    rerender({ enabled: true })
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2))

    expect(request).toHaveBeenLastCalledWith('job-1', 5, 100, expect.any(AbortSignal))
    expect(result.current.events).toEqual([workflowEvent('job-1', 5)])
    expect(delivered).toHaveBeenCalledOnce()
  })

  it('ignores a stale page that resolves after switching jobs', async () => {
    let resolveOld: ((value: ReturnType<typeof page>) => void) | undefined
    const oldPage = new Promise<ReturnType<typeof page>>((resolve) => { resolveOld = resolve })
    const delivered = vi.fn()
    vi.spyOn(api, 'workflowJobEvents').mockImplementation((jobId) => (
      jobId === 'old-job'
        ? oldPage
        : Promise.resolve(page('new-job', [workflowEvent('new-job', 1)]))
    ))
    const { result, rerender } = renderHook(
      ({ jobId }) => useWorkflowEvents(jobId, { pollIntervalMs: 60_000, onEvent: delivered }),
      { initialProps: { jobId: 'old-job' } },
    )

    rerender({ jobId: 'new-job' })
    await waitFor(() => expect(result.current.events).toEqual([workflowEvent('new-job', 1)]))
    await act(async () => {
      resolveOld?.(page('old-job', [workflowEvent('old-job', 9)]))
      await oldPage
    })

    expect(result.current.events).toEqual([workflowEvent('new-job', 1)])
    expect(delivered).toHaveBeenCalledOnce()
    expect(delivered).toHaveBeenCalledWith(workflowEvent('new-job', 1))
  })

  it('reads every available page in one poll', async () => {
    const request = vi.spyOn(api, 'workflowJobEvents')
      .mockResolvedValueOnce(page('job-1', [
        workflowEvent('job-1', 1),
        workflowEvent('job-1', 2),
      ], true))
      .mockResolvedValueOnce(page('job-1', [workflowEvent('job-1', 3)]))
    const { result } = renderHook(() => useWorkflowEvents('job-1', { pollIntervalMs: 60_000 }))

    await waitFor(() => expect(result.current.events).toHaveLength(3))

    expect(result.current.cursor).toBe(3)
    expect(request).toHaveBeenNthCalledWith(1, 'job-1', 0, 100, expect.any(AbortSignal))
    expect(request).toHaveBeenNthCalledWith(2, 'job-1', 2, 100, expect.any(AbortSignal))
  })

  it('prefers SSE and advances the durable Last-Event-ID cursor', async () => {
    const encoder = new TextEncoder()
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(
          'id: 7\nevent: workflow\ndata: {"job_id":"job-1","event_id":7,"to_status":"completed"}\n\n',
        ))
        controller.close()
      },
    })
    const fetchMock = vi.fn().mockResolvedValue(new Response(stream, { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const polling = vi.spyOn(api, 'workflowJobEvents')

    const { result } = renderHook(() => useWorkflowEvents('job-1'))

    await waitFor(() => expect(result.current.cursor).toBe(7))
    await waitFor(() => expect(result.current.state).toBe('closed'))
    expect(result.current.events).toEqual([
      expect.objectContaining({ job_id: 'job-1', event_id: 7, to_status: 'completed' }),
    ])
    expect(polling).not.toHaveBeenCalled()
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit
    expect(new Headers(init.headers).get('Last-Event-ID')).toBe('0')
  })
})
