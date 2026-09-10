import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useNoticeQueue } from '../src/components/tagManager/useNoticeQueue'

describe('useNoticeQueue', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('stacks notices instead of overwriting them', () => {
    const { result } = renderHook(() => useNoticeQueue())
    act(() => {
      result.current.push('info', '第一条')
      result.current.push('success', '第二条')
    })
    expect(result.current.notices.map((entry) => entry.text)).toEqual(['第一条', '第二条'])
    expect(result.current.notices.map((entry) => entry.id)).toEqual([1, 2])
  })

  it('auto-dismisses info and success notices after six seconds', () => {
    const { result } = renderHook(() => useNoticeQueue())
    act(() => {
      result.current.push('info', '自动消失')
      result.current.push('warning', '警告保留')
      result.current.push('danger', '错误保留')
    })
    act(() => {
      vi.advanceTimersByTime(5_999)
    })
    expect(result.current.notices).toHaveLength(3)
    act(() => {
      vi.advanceTimersByTime(1)
    })
    expect(result.current.notices.map((entry) => entry.text)).toEqual(['警告保留', '错误保留'])
  })

  it('keeps warning and danger notices beyond the auto-dismiss window', () => {
    const { result } = renderHook(() => useNoticeQueue())
    act(() => {
      result.current.push('warning', '警告保留')
      result.current.push('danger', '错误保留')
    })
    act(() => {
      vi.advanceTimersByTime(60_000)
    })
    expect(result.current.notices.map((entry) => entry.text)).toEqual(['警告保留', '错误保留'])
  })

  it('manual dismissal cancels the pending auto-dismiss timer', () => {
    const { result } = renderHook(() => useNoticeQueue())
    act(() => {
      result.current.push('success', '手动关闭')
    })
    const id = result.current.notices[0]?.id
    act(() => {
      if (id != null) result.current.dismiss(id)
    })
    expect(result.current.notices).toHaveLength(0)
    expect(vi.getTimerCount()).toBe(0)
    act(() => {
      vi.advanceTimersByTime(10_000)
    })
    expect(result.current.notices).toHaveLength(0)
  })

  it('clears all pending timers on unmount', () => {
    const { result, unmount } = renderHook(() => useNoticeQueue())
    act(() => {
      result.current.push('success', '一')
      result.current.push('info', '二')
    })
    expect(vi.getTimerCount()).toBe(2)
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})
