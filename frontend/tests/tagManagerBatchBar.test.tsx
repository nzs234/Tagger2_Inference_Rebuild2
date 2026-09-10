import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { BatchBar } from '../src/components/tagManager/BatchBar'
import { emptyImageFilter, type ImageFilterState } from '../src/lib/tagManager'

// Mirrors the backend's MAX_BATCH_IMAGES; the bar must block and explain the
// cap for BOTH scopes (selected and filtered) before any request is sent.

function setupFetch() {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://localhost')
    if (url.pathname.endsWith('/tag-db')) {
      const body = { profile: 'e621', items: [{ name: '1girl', category: 'general', post_count: 4_000_000, alias_of: null }] }
      return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    return new Response(JSON.stringify({}), { status: 200, headers: { 'Content-Type': 'application/json' } })
  })
}

function renderBar(props: { selectedIds?: number[]; filteredTotal?: number; filter?: ImageFilterState }) {
  const onSubmit = vi.fn()
  const view = render(<BatchBar
    profile="e621"
    filter={props.filter ?? emptyImageFilter}
    selectedIds={props.selectedIds ?? []}
    filteredTotal={props.filteredTotal ?? 10}
    submitting={false}
    disabled={false}
    onSubmit={onSubmit}
  />)
  return { onSubmit, view }
}

async function addTag(text: string) {
  const input = screen.getByRole('combobox', { name: '批量标签' })
  fireEvent.change(input, { target: { value: text } })
  const option = await screen.findByRole('option', { name: /1girl/ })
  fireEvent.mouseDown(within(option).getByRole('button'))
  await waitFor(() => expect(screen.getByRole('button', { name: '移除 1girl' })).toBeInTheDocument())
}

describe('BatchBar image cap', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('blocks and explains a selected scope above the 2000 image cap', async () => {
    setupFetch()
    const selectedIds = Array.from({ length: 2001 }, (_, index) => index + 1)
    const { view } = renderBar({ selectedIds })

    const warning = screen.getByRole('alert')
    expect(warning.textContent).toContain('已选中 2001 张图片，超过单批 2000 张的上限')
    expect(screen.getByRole('button', { name: '执行' })).toBeDisabled()

    // Dropping under the cap clears the warning without a reload.
    view.rerender(<BatchBar
      profile="e621"
      filter={emptyImageFilter}
      selectedIds={selectedIds.slice(0, 2)}
      filteredTotal={10}
      submitting={false}
      disabled={false}
      onSubmit={vi.fn()}
    />)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('blocks and explains a filtered scope above the 2000 image cap', async () => {
    setupFetch()
    const { onSubmit } = renderBar({ filteredTotal: 2500 })

    const warning = screen.getByRole('alert')
    expect(warning.textContent).toContain('当前过滤结果有 2500 张，超过单批 2000 张的上限')
    await addTag('1g')
    expect(screen.getByRole('button', { name: '执行' })).toBeDisabled()
    // The cap blocks the submit: nothing reaches the network layer.
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('submits the filtered-scope payload once the scope is under the cap', async () => {
    setupFetch()
    const filter = { ...emptyImageFilter, includeTags: ['1girl'] }
    const { onSubmit } = renderBar({ filteredTotal: 3, filter })

    await addTag('1g')
    const execute = screen.getByRole('button', { name: '执行' })
    await waitFor(() => expect(execute).toBeEnabled())
    fireEvent.click(execute)
    fireEvent.click(screen.getByRole('button', { name: '确认执行' }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(onSubmit.mock.calls[0]![0]).toEqual({
      op: 'add',
      tags: ['1girl'],
      use_regex: false,
      filter: {
        include_tags: ['1girl'],
        exclude_tags: [],
        include_mode: 'all',
        kind: 'any',
        sidecar: 'any',
      },
    })
    // The filtered scope never carries explicit image ids.
    expect(onSubmit.mock.calls[0]![0].image_ids).toBeUndefined()
  })
})
