import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { BatchBar } from '../src/components/tagManager/BatchBar'
import { emptyImageFilter, type BatchPreviewResponse, type ImageFilterState } from '../src/lib/tagManager'
import type { NoticeTone } from '../src/components/tagManager/useNoticeQueue'

// Covers the batch bar's guardrails (MAX_BATCH_IMAGES) and the preview-first
// execution flow: 执行 fetches a read-only preview, the dialog states what
// would change, and only 确认执行 submits the exact same payload.

const preview: BatchPreviewResponse = {
  targets: 3,
  affected: 2,
  no_change: 1,
  skipped_read_only: 0,
  will_create: 1,
  formats: { tag_txt: 2, tags_json: 1, standard_json: 0, none: 0 },
  samples: [
    {
      image_id: 1,
      file_name: 'a.png',
      kind: 'tag_txt',
      before_tags: ['solo', 'long_hair'],
      after_tags: ['long_hair', '1girl'],
    },
  ],
}

/** When true the preview route answers 500 so the fallback path can be tested. */
let previewFails = false

function setupFetch() {
  previewFails = false
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://localhost')
    const json = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
    if (url.pathname.endsWith('/batch/preview')) {
      if (previewFails) return json({ code: 'request_failed', message: 'preview failed' }, 500)
      return json(preview)
    }
    if (url.pathname.endsWith('/tag-db')) {
      const body = { profile: 'e621', items: [{ name: '1girl', category: 'general', post_count: 4_000_000, alias_of: null }] }
      return json(body)
    }
    return json({})
  })
}

function renderBar(props: { selectedIds?: number[]; filteredTotal?: number; filter?: ImageFilterState }) {
  const onSubmit = vi.fn()
  const notify = vi.fn<(tone: NoticeTone, text: string) => void>()
  const view = render(<BatchBar
    sessionId="ds-1"
    profile="e621"
    filter={props.filter ?? emptyImageFilter}
    selectedIds={props.selectedIds ?? []}
    filteredTotal={props.filteredTotal ?? 10}
    submitting={false}
    disabled={false}
    notify={notify}
    onSubmit={onSubmit}
  />)
  return { onSubmit, notify, view }
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
      sessionId="ds-1"
      profile="e621"
      filter={emptyImageFilter}
      selectedIds={selectedIds.slice(0, 2)}
      filteredTotal={10}
      submitting={false}
      disabled={false}
      notify={vi.fn()}
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
})

describe('BatchBar preview flow', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders the summary, format distribution, create hint and sample diff', async () => {
    setupFetch()
    renderBar({ filteredTotal: 3 })

    await addTag('1g')
    fireEvent.click(screen.getByRole('button', { name: '执行' }))

    const dialog = await screen.findByRole('alertdialog', { name: '批量操作预览' })
    expect(dialog).toHaveTextContent('将修改 2 张')
    expect(dialog).toHaveTextContent('无变化 1 张')
    expect(dialog).toHaveTextContent('跳过 0 张（只读）')
    expect(dialog).toHaveTextContent('目标总数 3 张')
    expect(dialog).toHaveTextContent('其中 1 张没有 sidecar，将以 tag_txt 格式新建')
    // Only non-zero formats are listed.
    expect(dialog).toHaveTextContent('TXT 2 张')
    expect(dialog).toHaveTextContent('本地 JSON 1 张')
    expect(dialog).not.toHaveTextContent('九字段 JSON')
    // Sample: file name + kind badge + before/after diff classes.
    expect(dialog).toHaveTextContent('a.png')
    expect(dialog).toHaveTextContent('TXT')
    const added = dialog.querySelectorAll('.tm-preview-tag-added')
    expect(Array.from(added).map((node) => node.textContent)).toEqual(['1girl'])
    const removed = dialog.querySelectorAll('.tm-preview-tag-removed')
    expect(Array.from(removed).map((node) => node.textContent)).toEqual(['solo'])
    // Untouched tags stay plain in both lines.
    expect(dialog.querySelectorAll('.tm-preview-tag-added')).toHaveLength(1)
    expect(dialog.querySelectorAll('.tm-preview-tag-removed')).toHaveLength(1)
  })

  it('submits the exact preview payload only after confirmation', async () => {
    setupFetch()
    const filter = { ...emptyImageFilter, includeTags: ['1girl'] }
    const { onSubmit } = renderBar({ filteredTotal: 3, filter })

    await addTag('1g')
    const execute = screen.getByRole('button', { name: '执行' })
    await waitFor(() => expect(execute).toBeEnabled())
    fireEvent.click(execute)

    const dialog = await screen.findByRole('alertdialog', { name: '批量操作预览' })
    // Nothing is written before the confirmation.
    expect(onSubmit).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: '确认执行' }))

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

  it('falls back to the plain confirmation when the preview fails', async () => {
    setupFetch()
    previewFails = true
    const { onSubmit, notify } = renderBar({ selectedIds: [1, 2] })

    await addTag('1g')
    fireEvent.click(screen.getByRole('button', { name: '执行' }))

    await waitFor(() => expect(notify).toHaveBeenCalledWith('warning', '预览加载失败，将直接确认执行'))
    const confirm = await screen.findByRole('alertdialog', { name: '对 2 张图片执行「添加」？' })
    // Cross-page/cross-filter selection persists, so the copy makes the
    // off-screen members of the selection explicit before the write.
    expect(confirm.textContent).toContain('选中的 2 张图片（可能包含当前筛选结果之外的图片）')

    fireEvent.click(within(confirm).getByRole('button', { name: '确认执行' }))
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(onSubmit.mock.calls[0]![0]).toMatchObject({ op: 'add', tags: ['1girl'], image_ids: [1, 2] })
  })

  it('blocks the confirm when the preview changes nothing', async () => {
    setupFetch()
    const { onSubmit } = renderBar({ filteredTotal: 3 })

    await addTag('1g')
    // Replace the default preview with a no-op result.
    preview.affected = 0
    preview.no_change = 3
    preview.formats = { tag_txt: 0, tags_json: 0, standard_json: 0, none: 0 }
    preview.samples = []
    try {
      fireEvent.click(screen.getByRole('button', { name: '执行' }))
      const dialog = await screen.findByRole('alertdialog', { name: '批量操作预览' })
      expect(dialog).toHaveTextContent('没有会产生修改的目标')
      expect(within(dialog).getByRole('button', { name: '确认执行' })).toBeDisabled()
      expect(onSubmit).not.toHaveBeenCalled()
    } finally {
      preview.affected = 2
      preview.no_change = 1
      preview.formats = { tag_txt: 2, tags_json: 1, standard_json: 0, none: 0 }
      preview.samples = [{
        image_id: 1,
        file_name: 'a.png',
        kind: 'tag_txt',
        before_tags: ['solo', 'long_hair'],
        after_tags: ['long_hair', '1girl'],
      }]
    }
  })
})
