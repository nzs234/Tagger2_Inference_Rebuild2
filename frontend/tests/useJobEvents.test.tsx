import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useJobEvents } from '../src/hooks/useJobEvents'
import type { JobEvent } from '../src/types'

type FakeReader = {
  read: ReturnType<typeof vi.fn>
  cancel: ReturnType<typeof vi.fn>
  releaseLock: ReturnType<typeof vi.fn>
}

const encoder = new TextEncoder()

function jobEvent(jobId: string, seq: number, state: JobEvent['state']): JobEvent {
  return {
    seq,
    job_id: jobId,
    state,
    phase: state,
    processed: state === 'running' ? 0 : 1,
    total: 1,
    succeeded: state === 'succeeded' ? 1 : 0,
    skipped: 0,
    failed: state === 'failed' ? 1 : 0,
  }
}

function streamResponse(events: JobEvent[], closes = false) {
  const reads: Array<ReadableStreamReadResult<Uint8Array>> = events.map((event) => ({
    done: false,
    value: encoder.encode(`id: ${event.seq}\nevent: job\ndata: ${JSON.stringify(event)}\n\n`),
  }))
  if (closes) reads.push({ done: true, value: undefined })
  const reader: FakeReader = {
    read: vi.fn(),
    cancel: vi.fn().mockResolvedValue(undefined),
    releaseLock: vi.fn(),
  }
  for (const result of reads) reader.read.mockResolvedValueOnce(result)
  if (!closes) reader.read.mockImplementation(() => new Promise(() => undefined))
  const response = {
    ok: true,
    status: 200,
    body: { getReader: () => reader },
  } as unknown as Response
  return { response, reader }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('useJobEvents', () => {
  it('cancels and releases the reader after a terminal event', async () => {
    const terminal = jobEvent('job-1', 1, 'succeeded')
    const { response, reader } = streamResponse([terminal])
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response))
    const delivered = vi.fn()
    const { result } = renderHook(() => useJobEvents('job-1', { onEvent: delivered }))

    await waitFor(() => expect(result.current.streamState).toBe('closed'))

    expect(result.current.event).toEqual(terminal)
    expect(delivered).toHaveBeenCalledWith(terminal)
    expect(reader.cancel).toHaveBeenCalledOnce()
    expect(reader.releaseLock).toHaveBeenCalledOnce()
  })

  it('reconnects after a server close with Last-Event-ID and cleans up the delay listener', async () => {
    const running = jobEvent('job-1', 1, 'running')
    const terminal = jobEvent('job-1', 2, 'succeeded')
    const first = streamResponse([running], true)
    const second = streamResponse([terminal])
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(first.response)
      .mockResolvedValueOnce(second.response)
    vi.stubGlobal('fetch', fetchMock)
    const removeEventListener = vi.spyOn(AbortSignal.prototype, 'removeEventListener')
    const { result } = renderHook(() => useJobEvents('job-1'))

    await waitFor(() => expect(result.current.streamState).toBe('closed'), { timeout: 4_000 })

    expect(fetchMock).toHaveBeenCalledTimes(2)
    const reconnectHeaders = new Headers(fetchMock.mock.calls[1]?.[1]?.headers)
    expect(reconnectHeaders.get('Last-Event-ID')).toBe('1')
    expect(first.reader.cancel).toHaveBeenCalledOnce()
    expect(first.reader.releaseLock).toHaveBeenCalledOnce()
    expect(second.reader.cancel).toHaveBeenCalledOnce()
    expect(second.reader.releaseLock).toHaveBeenCalledOnce()
    expect(removeEventListener).toHaveBeenCalledWith('abort', expect.any(Function))
  })

  it('aborts an in-flight connection when unmounted', async () => {
    let signal: AbortSignal | undefined
    const fetchMock = vi.fn().mockImplementation((_url: string, init: RequestInit) => {
      signal = init.signal as AbortSignal
      return new Promise<Response>((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(signal?.reason), { once: true })
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { unmount } = renderHook(() => useJobEvents('job-1'))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce())

    act(() => unmount())

    expect(signal?.aborted).toBe(true)
  })

  it('does not send the previous job sequence when the job changes', async () => {
    const first = streamResponse([jobEvent('job-1', 7, 'succeeded')])
    const second = streamResponse([jobEvent('job-2', 1, 'succeeded')])
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(first.response)
      .mockResolvedValueOnce(second.response)
    vi.stubGlobal('fetch', fetchMock)
    const { result, rerender } = renderHook(
      ({ jobId }) => useJobEvents(jobId),
      { initialProps: { jobId: 'job-1' } },
    )
    await waitFor(() => expect(result.current.event?.job_id).toBe('job-1'))

    rerender({ jobId: 'job-2' })
    await waitFor(() => expect(result.current.event?.job_id).toBe('job-2'))

    const nextJobHeaders = new Headers(fetchMock.mock.calls[1]?.[1]?.headers)
    expect(nextJobHeaders.get('Last-Event-ID')).toBeNull()
  })
})
