import { describe, expect, it } from 'vitest'
import { formatBatchResultNotice } from '../src/components/tagManager/useTagManagerSessions'

// The success notice surfaces the backend's skip counters; zero-valued parts
// are omitted so the common all-written case stays a single clause.

describe('formatBatchResultNotice', () => {
  it('reports only the affected count when nothing was skipped', () => {
    expect(formatBatchResultNotice({ affected: 5 })).toBe('已修改 5 张')
    expect(formatBatchResultNotice({ affected: 5, skipped_read_only: 0, no_change: 0 })).toBe('已修改 5 张')
  })

  it('appends the read-only skip count when present', () => {
    expect(formatBatchResultNotice({ affected: 2, skipped_read_only: 3 })).toBe('已修改 2 张，跳过 3 张（只读）')
  })

  it('appends the no-change count when present', () => {
    expect(formatBatchResultNotice({ affected: 2, no_change: 7 })).toBe('已修改 2 张，7 张无变化')
  })

  it('lists both skip kinds in order, dropping nothing non-zero', () => {
    expect(formatBatchResultNotice({ affected: 1, skipped_read_only: 2, no_change: 3 }))
      .toBe('已修改 1 张，跳过 2 张（只读），3 张无变化')
  })
})
