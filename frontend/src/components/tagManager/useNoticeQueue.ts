import { useCallback, useEffect, useRef, useState } from 'react'

export type NoticeTone = 'info' | 'warning' | 'danger' | 'success'

export interface NoticeEntry {
  id: number
  tone: NoticeTone
  text: string
}

/** Tones that clear themselves; warning/danger stay until dismissed by hand. */
const AUTO_DISMISS_TONES: ReadonlySet<NoticeTone> = new Set(['info', 'success'])
const AUTO_DISMISS_MS = 6000

/**
 * Queue of page notices rendered as a vertical stack.
 *
 * Every push appends a new entry (older entries stay visible instead of being
 * overwritten like the previous single-notice state).  Info/success entries
 * schedule their own auto-dismiss timer; dismissing by hand or unmounting the
 * host component cancels the pending timer so no state update fires afterwards.
 */
export function useNoticeQueue() {
  const [notices, setNotices] = useState<NoticeEntry[]>([])
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>())
  const nextId = useRef(0)

  const dismiss = useCallback((id: number) => {
    const timer = timers.current.get(id)
    if (timer != null) {
      clearTimeout(timer)
      timers.current.delete(id)
    }
    setNotices((current) => current.filter((entry) => entry.id !== id))
  }, [])

  const push = useCallback((tone: NoticeTone, text: string) => {
    nextId.current += 1
    const id = nextId.current
    setNotices((current) => [...current, { id, tone, text }])
    if (AUTO_DISMISS_TONES.has(tone)) {
      timers.current.set(id, setTimeout(() => dismiss(id), AUTO_DISMISS_MS))
    }
  }, [dismiss])

  // Unmount: cancel every pending auto-dismiss timer.
  useEffect(() => () => {
    for (const timer of timers.current.values()) clearTimeout(timer)
    timers.current.clear()
  }, [])

  return { notices, push, dismiss }
}
