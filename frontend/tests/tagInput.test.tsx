import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TagInput } from '../src/components/tagManager/TagInput'

// ---------------------------------------------------------------------------
// TagInput unit tests: stale-request cancellation, Alt/Option+Enter commit,
// and the combobox aria wiring (stable ids + visible active suggestion).
// The 220ms debounce is driven with fake timers; every tag-db lookup resolves
// through a manually-released deferred so responses can land out of order.
// ---------------------------------------------------------------------------

interface PendingLookup {
  query: string
  resolve: (body: unknown) => void
}

const dbEntries = {
  haku: [{ name: 'hakurei_reimu', category: 'character', post_count: 90_000, alias_of: null }],
  '1g': [{ name: '1girl', category: 'general', post_count: 4_000_000, alias_of: null }],
}

function setupFetch(): PendingLookup[] {
  const pending: PendingLookup[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://localhost')
    const query = url.searchParams.get('query') ?? ''
    return new Promise((resolve) => {
      pending.push({
        query,
        resolve: (body) => resolve(new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })),
      })
    })
  })
  return pending
}

function typeText(text: string) {
  const input = screen.getByRole('combobox')
  fireEvent.change(input, { target: { value: text } })
  // Flush the debounce window.
  act(() => { vi.advanceTimersByTime(220) })
}

describe('TagInput', () => {
  let pending: PendingLookup[]
  const onAdd = vi.fn()

  beforeEach(() => {
    vi.useFakeTimers()
    pending = setupFetch()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    vi.clearAllMocks()
  })

  it('ignores a stale lookup response that lands after a newer query', async () => {
    render(<TagInput profile="e621" label="添加标签" onAdd={onAdd} />)
    typeText('haku')
    expect(pending).toHaveLength(1)
    expect(pending[0]!.query).toBe('haku')

    // A newer query supersedes the in-flight one.
    typeText('1g')
    expect(pending).toHaveLength(2)
    expect(pending[1]!.query).toBe('1g')

    act(() => { pending[1]!.resolve({ profile: 'e621', items: dbEntries['1g'] }) })
    await act(async () => {})
    expect(screen.getByRole('option', { name: /1girl/ })).toBeInTheDocument()

    // The stale response for 'haku' arrives last and must not win.
    act(() => { pending[0]!.resolve({ profile: 'e621', items: dbEntries.haku }) })
    await act(async () => {})
    expect(screen.queryByRole('option', { name: /hakurei_reimu/ })).not.toBeInTheDocument()
    expect(screen.getByRole('option', { name: /1girl/ })).toBeInTheDocument()
  })

  it('cancels an in-flight lookup when the input is cleared', async () => {
    render(<TagInput profile="e621" label="添加标签" onAdd={onAdd} />)
    typeText('haku')
    expect(pending).toHaveLength(1)

    // Clearing resets the input; the response may not reopen the dropdown.
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: '' } })
    act(() => { vi.advanceTimersByTime(220) })
    act(() => { pending[0]!.resolve({ profile: 'e621', items: dbEntries.haku }) })
    await act(async () => {})

    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(screen.getByRole('combobox')).toHaveAttribute('aria-expanded', 'false')
  })

  it('commits the highlighted suggestion with Alt/Option+Enter after ArrowDown', async () => {
    render(<TagInput profile="e621" label="添加标签" onAdd={onAdd} />)
    typeText('haku')
    act(() => { pending[0]!.resolve({ profile: 'e621', items: dbEntries.haku }) })
    await act(async () => {})

    // The suggestion is visible but not pre-selected; the arrow key picks it.
    const option = screen.getByRole('option', { name: /hakurei_reimu/ })
    expect(option).toHaveAttribute('aria-selected', 'false')
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'ArrowDown' })
    expect(option).toHaveAttribute('aria-selected', 'true')

    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter', altKey: true })
    expect(onAdd).toHaveBeenCalledWith('hakurei_reimu', 'character')
  })

  it('commits the typed text on Enter even while suggestions are open', async () => {
    // 'haku' is a real dataset tag: the open dropdown for hakurei_reimu must
    // not hijack the Enter into the suggestion (the P2 regression).
    render(<TagInput profile="e621" label="添加标签" onAdd={onAdd} />)
    typeText('haku')
    act(() => { pending[0]!.resolve({ profile: 'e621', items: dbEntries.haku }) })
    await act(async () => {})
    expect(screen.getByRole('option', { name: /hakurei_reimu/ })).toBeInTheDocument()

    const input = screen.getByRole('combobox')
    expect(input).not.toHaveAttribute('aria-activedescendant')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onAdd).toHaveBeenCalledOnce()
    expect(onAdd).toHaveBeenCalledWith('haku', undefined)
  })

  it('resets the highlight when a newer lookup lands, so Enter commits the edited text', async () => {
    render(<TagInput profile="e621" label="添加标签" onAdd={onAdd} />)
    typeText('haku')
    act(() => { pending[0]!.resolve({ profile: 'e621', items: dbEntries.haku }) })
    await act(async () => {})
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'ArrowDown' })
    expect(screen.getByRole('option', { name: /hakurei_reimu/ })).toHaveAttribute('aria-selected', 'true')

    // Editing the text supersedes the pick: the fresh lookup clears the
    // highlight, so Enter commits what is typed now, not the old suggestion.
    typeText('totally_new_tag')
    act(() => { pending[1]!.resolve({ profile: 'e621', items: [] }) })
    await act(async () => {})
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    expect(onAdd).toHaveBeenCalledWith('totally_new_tag', undefined)
  })

  it('commits the raw text with Alt/Option+Enter when no suggestion matches', () => {
    render(<TagInput profile="e621" label="添加标签" onAdd={onAdd} />)
    typeText('totally_new_tag')
    expect(pending).toHaveLength(1)
    act(() => { pending[0]!.resolve({ profile: 'e621', items: [] }) })

    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter', altKey: true })
    expect(onAdd).toHaveBeenCalledWith('totally_new_tag', undefined)
  })

  it('ignores Enter on an empty input', () => {
    render(<TagInput profile="e621" label="添加标签" onAdd={onAdd} />)
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    expect(onAdd).not.toHaveBeenCalled()
  })

  it('wires stable combobox ids and a visible active state', async () => {
    const entries = [...dbEntries.haku, { name: 'haku_eyes', category: 'general', post_count: 12_000, alias_of: null }]
    render(<TagInput profile="e621" label="添加标签" onAdd={onAdd} />)
    typeText('haku')
    act(() => { pending[0]!.resolve({ profile: 'e621', items: entries }) })
    await act(async () => {})

    const input = screen.getByRole('combobox')
    const list = screen.getByRole('listbox', { name: '添加标签建议' })
    // The aria-controls target must exist with exactly the referenced id.
    expect(input).toHaveAttribute('aria-controls', list.id)
    expect(list).toHaveAttribute('id', list.id)

    const options = within(list).getAllByRole('option')
    expect(options).toHaveLength(2)
    // Nothing is highlighted until the user starts arrowing: Enter must keep
    // committing typed text while the dropdown is merely open.
    expect(options[0]!).toHaveAttribute('aria-selected', 'false')
    expect(options[0]!).not.toHaveClass('tm-suggest-active')
    expect(input).not.toHaveAttribute('aria-activedescendant')

    // Arrow-down starts the selection at the first suggestion (id + class).
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(options[0]!).toHaveAttribute('aria-selected', 'true')
    expect(options[0]!.className).toContain('tm-suggest-active')
    expect(options[1]!).toHaveAttribute('aria-selected', 'false')
    expect(input.getAttribute('aria-activedescendant')).toBe(options[0]!.id)

    // A second ArrowDown moves the active descendant along.
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(options[0]!).not.toHaveClass('tm-suggest-active')
    expect(options[1]!).toHaveClass('tm-suggest-active')
    expect(input.getAttribute('aria-activedescendant')).toBe(options[1]!.id)
  })
})
