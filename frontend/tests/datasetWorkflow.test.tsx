import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DatasetWorkflow } from '../src/pages/DatasetWorkflow'
import { copyFor, workflowCopy } from '../src/lib/workflowCopy'
import { usePreferences } from '../src/store/app'

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const result = render(
    <QueryClientProvider client={client}>
      <DatasetWorkflow />
    </QueryClientProvider>,
  )
  return { ...result, queryClient: client }
}

describe('workflow copy', () => {
  it('defines the same keys in both languages', () => {
    const zh = Object.keys(workflowCopy.zh).sort()
    const en = Object.keys(workflowCopy.en).sort()
    expect(en).toEqual(zh)
  })

  it('never leaves a blank string in either language', () => {
    for (const language of ['zh', 'en'] as const) {
      for (const [key, value] of Object.entries(copyFor(language))) {
        if (typeof value === 'string') expect(value.trim().length, `${language}.${key}`).toBeGreaterThan(0)
      }
    }
  })

  it('keeps the two languages distinct for user-facing labels', () => {
    expect(copyFor('zh').navLabel).not.toEqual(copyFor('en').navLabel)
    expect(copyFor('en').title).toBe('Dataset Workflow')
  })
})

describe('DatasetWorkflow page', () => {
  let modelItems: Array<Record<string, unknown>> = []

  beforeEach(() => {
    modelItems = []
    usePreferences.setState({ workflowLanguage: 'zh' })
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input)
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      if (url.includes('/workflows/capabilities')) {
        return json({ profiles: ['e621', 'danbooru'], work_modes: ['in_place', 'full_copy'], resources: [] })
      }
      if (url.includes('/workflows/resources')) {
        return json([
          {
            resource_id: 'replace-e621-local-v1',
            // The on-disk catalog uses the canonical replacement_index name.
            // The page must still expose it as a selectable replacement.
            category: 'replacement_index',
            fingerprint: 'a'.repeat(64),
            created_at: '2026-08-11T00:00:00Z',
          },
          {
            resource_id: 'classify-e621-20260812-v1',
            category: 'classify',
            fingerprint: 'b'.repeat(64),
          },
          {
            resource_id: 'tokenizer-qwen3-0-6b-tokenizer-v1',
            category: 'tokenizer',
            fingerprint: 'c'.repeat(64),
          },
        ])
      }
      if (url.includes('/workflows/jobs')) return json([])
      if (url.includes('/models')) return json({ items: modelItems })
      if (url.includes('/roots')) {
        return json({ items: [{ id: 'in', name: 'Input', kind: 'input', writable: false }] })
      }
      return json({})
    })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    usePreferences.setState({ workflowLanguage: 'zh' })
  })

  it('renders Chinese by default and lists registered resources', async () => {
    const { container } = renderPage()
    expect(screen.getByRole('heading', { level: 1, name: '\u6570\u636e\u96c6\u5de5\u4f5c\u6d41' })).toBeInTheDocument()
    // The id appears in both the catalog table and the replace-resource
    // select, so scope the assertion to the table.
    await waitFor(() => {
      const table = container.querySelector('.workflow-table')
      expect(table?.textContent ?? '').toContain('replace-e621-local-v1')
    })
    expect(screen.getByRole('option', { name: 'replace-e621-local-v1' })).toBeInTheDocument()
  })

  it('switches to English and persists the choice', async () => {
    renderPage()
    fireEvent.click(screen.getByRole('button', { name: 'English' }))

    expect(screen.getByRole('heading', { level: 1, name: 'Dataset Workflow' })).toBeInTheDocument()
    expect(usePreferences.getState().workflowLanguage).toBe('en')
    // The compatibility notice must be translated too, not left in Chinese.
    expect(screen.getByText(/rule-only stages/)).toBeInTheDocument()
  })

  it('states that missing resources fail closed instead of falling back', () => {
    renderPage()
    expect(screen.getByText(/不会静默回退/)).toBeInTheDocument()
  })

  it('disables preflight when capability discovery fails', async () => {
    const fetchMock = vi.mocked(globalThis.fetch)
    const fallback = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).includes('/workflows/capabilities')) {
        return new Response(JSON.stringify({ code: 'capabilities_unavailable' }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return fallback(input, init)
    })

    renderPage()

    expect(await screen.findByRole('alert')).toHaveTextContent('任务预检已停用')
    expect(screen.getByRole('button', { name: copyFor('zh').preflight })).toBeDisabled()
    expect(screen.getByLabelText(copyFor('zh').profile)).toBeDisabled()
    expect(screen.getByLabelText(copyFor('zh').workMode)).toBeDisabled()
  })

  it('keeps the create button disabled until preflight passes', async () => {
    renderPage()
    const create = screen.getByRole('button', { name: '创建任务' })
    expect(create).toBeDisabled()
  })

  it('exposes registered classification and tokenizer resources in the job config', async () => {
    renderPage()
    const zh = copyFor('zh')
    await waitFor(() => {
      expect(screen.getByLabelText(zh.enableClassify)).toBeInTheDocument()
      expect(screen.getByLabelText(zh.enableTokenBudget)).toBeInTheDocument()
    })
    fireEvent.change(screen.getByLabelText(zh.enableClassify), { target: { value: 'yes' } })
    fireEvent.change(screen.getByLabelText(zh.enableTokenBudget), { target: { value: 'yes' } })
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'classify-e621-20260812-v1' })).toBeInTheDocument()
      expect(screen.getByRole('option', { name: 'tokenizer-qwen3-0-6b-tokenizer-v1' })).toBeInTheDocument()
    })
    expect((screen.getByLabelText(zh.classifyResource) as HTMLSelectElement).value).toBe(
      'classify-e621-20260812-v1',
    )
    expect((screen.getByLabelText(zh.tokenizerResource) as HTMLSelectElement).value).toBe(
      'tokenizer-qwen3-0-6b-tokenizer-v1',
    )
  })

  it('opens one structured help popover and closes it with Escape', async () => {
    renderPage()
    const helpButtons = await screen.findAllByRole('button', { name: copyFor('zh').helpLabels.button })
    expect(helpButtons.length).toBeGreaterThan(3)
    fireEvent.click(helpButtons[0]!)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText(copyFor('zh').helpLabels.purpose)).toBeInTheDocument()
    fireEvent.click(helpButtons[1]!)
    expect(screen.getAllByRole('dialog')).toHaveLength(1)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('preserves an explicitly selected first model across query refreshes', async () => {
    modelItems = [
      { id: 'model-a', name: 'Model A', backend: 'onnx', loaded: true, threshold_source: 'model' },
      { id: 'model-b', name: 'Model B', backend: 'onnx', loaded: true, threshold_source: 'model' },
    ]
    const { queryClient } = renderPage()
    const captionModel = await screen.findByRole('combobox', { name: copyFor('zh').captionModel }) as HTMLSelectElement

    fireEvent.change(captionModel, { target: { value: 'model-a' } })
    expect(captionModel.value).toBe('model-a')

    modelItems = [
      { ...modelItems[0], memory_mb: 2048 },
      modelItems[1]!,
    ]
    await act(async () => {
      queryClient.setQueryData(['models'], { items: modelItems })
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
    expect(captionModel.value).toBe('model-a')
  })

  it('uses the e621 defaults and accepts complete manual dataset paths', async () => {
    renderPage()
    const zh = copyFor('zh')
    expect((screen.getByLabelText(zh.enableClassify) as HTMLSelectElement).value).toBe('yes')
    expect((screen.getByLabelText(zh.enableReplace) as HTMLSelectElement).value).toBe('yes')
    const source = screen.getByLabelText(zh.sourcePath) as HTMLInputElement
    const output = screen.getByLabelText(zh.outputPath) as HTMLInputElement
    fireEvent.change(source, { target: { value: 'E:\\datasets\\train' } })
    fireEvent.change(output, { target: { value: 'E:\\datasets\\train_processed' } })
    expect(source.value).toBe('E:\\datasets\\train')
    expect(output.value).toBe('E:\\datasets\\train_processed')
    expect(screen.queryByRole('combobox', { name: zh.sourcePath })).not.toBeInTheDocument()
  })
})

describe('DatasetWorkflow count review and job controls', () => {
  const decision = {
    sample_id: 7,
    count_value: 'duo',
    status: 'pending',
    updated_at: '2026-08-11T00:00:00Z',
    version: 3,
    proposed_count: 'duo',
    base_value: 'solo',
    selected_source: 'rules',
    original_normalized: 'solo',
    wiki_value: null,
    matched_tags: ['duo', 'two_characters'],
    conflict: true,
    issue_codes: [],
    warnings: [],
    applied_lower_bounds: [],
    blocking_code: null,
    relative_image_path: 'set-a/img-0007.png',
    nl_observation: {},
  }

  let posts: { url: string; body: unknown }[] = []
  let pending = 1
  let countItems = [decision]

  beforeEach(() => {
    posts = []
    pending = 1
    countItems = [decision]
    usePreferences.setState({ workflowLanguage: 'zh' })
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        const json = (body: unknown, status = 200) =>
          new Response(JSON.stringify(body), {
            status,
            headers: { 'Content-Type': 'application/json' },
          })
        if (init?.method === 'POST') {
          posts.push({ url, body: init.body ? JSON.parse(String(init.body)) : null })
          if (url.includes('/path-bindings/preview')) {
            return json({
              status: 'ready',
              source_bound: true,
              output_bound: true,
              output_create_required: false,
              warnings: [],
              errors: [],
            })
          }
          if (url.includes('/path-bindings')) {
            return json({
              status: 'ready',
              source: { root_id: 'in', relative_path: '' },
              output: { root_id: 'out', relative_path: '' },
              output_created: false,
            })
          }
          if (url.includes('/count-review/resolve')) {
            pending = 0
            return json({ sample_id: 7, count_value: 'duo', version: 4 })
          }
          if (url.includes('/count-review/confirm')) {
            return json({ job_id: 'job-1', confirmed: true, pending: 0 })
          }
          if (url.includes('/repair')) {
            return json({
              job_id: 'job-1',
              reclaimed_samples: 2,
              parked_samples: 1,
              committed_files: 5,
              journal_state: 'validated',
              resumable_samples: 3,
            })
          }
          return json({ job_id: 'job-1', status: 'paused' })
        }
        if (url.includes('/events?')) {
          return json({
            job_id: 'job-1',
            events: [
              {
                event_id: 12,
                job_id: 'job-1',
                event_type: 'stage_started',
                from_status: 'queued',
                to_status: 'running',
                payload: {},
                created_at: '2026-08-11T00:00:01Z',
              },
            ],
            next_after_event_id: 12,
            has_more: false,
          })
        }
        if (url.includes('/count-review')) {
          const parsed = new URL(url, 'http://localhost')
          const offset = Number(parsed.searchParams.get('offset') ?? 0)
          const limit = Number(parsed.searchParams.get('limit') ?? 50)
          return json({
            items: countItems.slice(offset, offset + limit).map((item) => ({
              ...item,
              status: pending ? item.status : 'confirmed',
            })),
            pending,
          })
        }
        if (url.includes('/token-review')) return json({ items: [], unresolved: 0 })
        if (url.includes('/issues')) return json([])
        if (url.includes('/workflows/capabilities')) {
          return json({ profiles: ['e621'], work_modes: ['in_place', 'full_copy'], resources: [] })
        }
        if (url.includes('/workflows/resources')) return json([])
        if (url.includes('/workflows/jobs')) {
          return json([
            {
              job_id: 'job-1',
              status: 'running',
              profile: 'e621',
              processed_samples: 4,
              total_samples: 9,
              current_module_id: 'classify',
              created_at: '2026-08-11T00:00:00Z',
            },
            {
              job_id: 'job-2',
              status: 'paused',
              profile: 'e621',
              processed_samples: 0,
              total_samples: 3,
              current_module_id: null,
              created_at: '2026-08-11T01:00:00Z',
            },
            {
              job_id: 'job-3',
              status: 'pending',
              profile: 'e621',
              work_mode: 'full_copy',
              processed_samples: 0,
              total_samples: 3,
              current_module_id: null,
              created_at: '2026-08-11T02:00:00Z',
            },
            {
              job_id: 'job-4',
              status: 'completed',
              profile: 'e621',
              work_mode: 'in_place',
              pinned: true,
              processed_samples: 3,
              total_samples: 3,
              current_module_id: null,
              created_at: '2026-08-11T03:00:00Z',
            },
            {
              job_id: 'job-5',
              status: 'completed',
              profile: 'e621',
              work_mode: 'full_copy',
              pinned: false,
              processed_samples: 2,
              total_samples: 2,
              current_module_id: null,
              created_at: '2026-08-11T04:00:00Z',
            },
          ])
        }
        if (url.includes('/roots')) {
          return json({ items: [{ id: 'in', name: 'Input', kind: 'input', writable: false }] })
        }
        return json({})
      },
    )
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    usePreferences.setState({ workflowLanguage: 'zh' })
  })

  async function selectJob() {
    renderPage()
    const cell = await screen.findByText(/job-1/)
    fireEvent.click(cell)
    return cell
  }

  it('blocks confirmation while any count is still pending', async () => {
    await selectJob()
    const confirm = await screen.findByRole('button', { name: /确认完成复核/ })
    expect(confirm).toBeDisabled()
    expect(screen.getByText('set-a/img-0007.png')).toBeInTheDocument()
    // Conflicting sources must be surfaced, not silently resolved.
    expect(screen.getByText('来源冲突')).toBeInTheDocument()
  })

  it('sends the stored version so a stale edit is rejected server-side', async () => {
    await selectJob()
    const apply = await screen.findByRole('button', { name: /采用 duo/ })
    fireEvent.click(apply)

    await waitFor(() => {
      const resolve = posts.find((post) => post.url.includes('/count-review/resolve'))
      expect(resolve?.body).toEqual({
        sample_id: 7,
        expected_version: 3,
        count: 'duo',
        source: 'manual',
      })
    })
  })

  it('enables confirmation once nothing is pending', async () => {
    await selectJob()
    fireEvent.click(await screen.findByRole('button', { name: /采用 duo/ }))

    await waitFor(async () => {
      expect(await screen.findByRole('button', { name: /确认完成复核/ })).toBeEnabled()
    })
  })

  it('offers only the four count values the API accepts', async () => {
    await selectJob()
    await screen.findByRole('button', { name: /采用 duo/ })
    for (const value of ['solo', 'duo', 'trio', 'group']) {
      expect(screen.getByRole('button', { name: new RegExp(`采用 ${value}`) })).toBeInTheDocument()
    }
    expect(screen.queryByRole('button', { name: /采用 zero/ })).not.toBeInTheDocument()
  })

  it('paginates beyond the first 50 count-review rows', async () => {
    countItems = Array.from({ length: 51 }, (_, index) => ({
      ...decision,
      sample_id: index + 1,
      relative_image_path: `set-a/img-${String(index + 1).padStart(4, '0')}.png`,
    }))
    pending = 51

    await selectJob()
    expect(await screen.findByText('set-a/img-0001.png')).toBeInTheDocument()
    const next = await screen.findByRole('button', { name: '下一页' })
    expect(next).toBeEnabled()
    fireEvent.click(next)

    expect(await screen.findByText('set-a/img-0051.png')).toBeInTheDocument()
    expect(screen.getByText('第 2 页')).toBeInTheDocument()
    expect(
      vi.mocked(globalThis.fetch).mock.calls.some(([input]) => String(input).includes('offset=50')),
    ).toBe(true)
  })

  it('pauses a running job and reports repair results', async () => {
    await selectJob()
    fireEvent.click(await screen.findByRole('button', { name: /暂停/ }))
    await waitFor(() => {
      expect(posts.some((post) => post.url.endsWith('/pause'))).toBe(true)
    })

    fireEvent.click(screen.getByRole('button', { name: /修复中断/ }))
    await waitFor(() => {
      expect(screen.getByLabelText('修复结果')).toBeInTheDocument()
    })
    expect(screen.getByText('validated')).toBeInTheDocument()
  })

  it('drops a repair report when switching to another job', async () => {
    await selectJob()
    fireEvent.click(await screen.findByRole('button', { name: /修复中断/ }))
    await waitFor(() => {
      expect(screen.getByLabelText('修复结果')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByText(/job-2/))

    // The report describes job-1, so it must not linger under job-2.
    await waitFor(() => {
      expect(screen.queryByLabelText('修复结果')).not.toBeInTheDocument()
    })
  })

  it('shows resume instead of pause for a paused job', async () => {
    renderPage()
    fireEvent.click(await screen.findByText(/job-2/))

    expect(await screen.findByRole('button', { name: /继续/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /暂停/ })).not.toBeInTheDocument()
  })

  it('keeps creation and execution separate with an explicit start action', async () => {
    renderPage()
    fireEvent.click(await screen.findByText(/job-3/))

    expect(await screen.findByRole('button', { name: copyFor('zh').startJob })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: copyFor('zh').startJob }))

    await waitFor(() => {
      expect(posts.some((post) => post.url.endsWith('/start'))).toBe(true)
    })
  })

  it('replays durable events with a cursor for the selected job', async () => {
    await selectJob()
    expect(await screen.findByRole('heading', { name: '事件' })).toBeInTheDocument()
    expect(await screen.findByText('stage_started')).toBeInTheDocument()
    expect(screen.getByText(/游标 12/)).toBeInTheDocument()
  })

  it('prevents discarding a pinned terminal job', async () => {
    renderPage()
    fireEvent.click(await screen.findByText(/job-4/))

    const discard = await screen.findByRole('button', { name: /丢弃工作区/ })
    expect(discard).toBeDisabled()
    expect(screen.getByText(/取消固定后才能丢弃工作区/)).toBeInTheDocument()
  })

  it('does not poll durable events for a terminal job', async () => {
    renderPage()
    fireEvent.click(await screen.findByText(/job-5/))
    await screen.findByRole('button', { name: /丢弃工作区/ })

    expect(
      vi.mocked(globalThis.fetch).mock.calls.some(([input]) => String(input).includes('/job-5/events')),
    ).toBe(false)
  })

  it('clears the selected job after discarding its workspace', async () => {
    renderPage()
    fireEvent.click(await screen.findByText(/job-5/))
    fireEvent.click(await screen.findByRole('button', { name: /丢弃工作区/ }))

    await waitFor(() => {
      expect(posts.some((post) => post.url.endsWith('/job-5/discard'))).toBe(true)
      expect(screen.queryByRole('button', { name: /丢弃工作区/ })).not.toBeInTheDocument()
    })
  })
})

describe('DatasetWorkflow token budget review', () => {
  const item = {
    sample_id: 4,
    nl_text: 'a b c d e',
    token_count: 5,
    token_limit: 3,
    status: 'overflow',
    proposal_text: null,
    proposal_token_count: null,
    over_by: 2,
    updated_at: '2026-08-11T00:00:00Z',
  }

  let posts: { url: string; body: unknown }[] = []
  let tokenizerAvailable = true

  beforeEach(() => {
    posts = []
    tokenizerAvailable = true
    usePreferences.setState({ workflowLanguage: 'zh' })
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        const json = (body: unknown, status = 200) =>
          new Response(JSON.stringify(body), {
            status,
            headers: { 'Content-Type': 'application/json' },
          })
        if (init?.method === 'POST') {
          posts.push({ url, body: init.body ? JSON.parse(String(init.body)) : null })
          if (url.includes('/token-review/review')) {
            if (!tokenizerAvailable) {
              return json(
                { code: 'token_review_unavailable', message: 'no tokenizer' },
                503,
              )
            }
            return json({ ...item, status: 'edited', proposal_text: 'a b', proposal_token_count: 2 })
          }
          return json({})
        }
        if (url.includes('/token-review')) return json({ items: [item], unresolved: 1 })
        if (url.includes('/count-review')) return json({ items: [], pending: 0 })
        if (url.includes('/issues')) return json([])
        if (url.includes('/workflows/capabilities')) {
          return json({ profiles: ['e621'], work_modes: ['in_place', 'full_copy'], resources: [] })
        }
        if (url.includes('/workflows/resources')) return json([])
        if (url.includes('/workflows/jobs')) {
          return json([
            {
              job_id: 'job-1',
              status: 'running',
              profile: 'e621',
              processed_samples: 4,
              total_samples: 9,
              current_module_id: 'token_budget',
              created_at: '2026-08-11T00:00:00Z',
            },
          ])
        }
        if (url.includes('/roots')) {
          return json({ items: [{ id: 'in', name: 'Input', kind: 'input', writable: false }] })
        }
        return json({})
      },
    )
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    usePreferences.setState({ workflowLanguage: 'zh' })
  })

  async function selectJob() {
    renderPage()
    fireEvent.click(await screen.findByText(/job-1/))
  }

  it('shows the overflow margin and blocks confirmation', async () => {
    await selectJob()
    expect(await screen.findByText('超出')).toBeInTheDocument()
    const confirm = screen.getByRole('button', { name: /确认完成 Token 复核/ })
    expect(confirm).toBeDisabled()
    // The reviewer must know a proposal is not the export value.
    expect(screen.getByText(/不会写入最终 JSON/)).toBeInTheDocument()
  })

  it('cannot apply until a proposal exists', async () => {
    await selectJob()
    expect(await screen.findByRole('button', { name: /应用候选/ })).toBeDisabled()
  })

  it('sends the current status so a stale review is rejected server-side', async () => {
    await selectJob()
    fireEvent.click(await screen.findByRole('button', { name: /手动改写/ }))

    await waitFor(() => {
      const review = posts.find((post) => post.url.includes('/token-review/review'))
      expect(review?.body).toEqual({
        sample_id: 4,
        action: 'edit',
        expected_status: 'overflow',
        text: 'a b c d e',
      })
    })
  })

  it('reports a missing tokenizer instead of failing silently', async () => {
    tokenizerAvailable = false
    await selectJob()
    fireEvent.click(await screen.findByRole('button', { name: /重新计数/ }))

    expect(await screen.findByText(/未注册 Tokenizer 资源/)).toBeInTheDocument()
  })
})

describe('DatasetWorkflow OCR', () => {
  let posted: unknown
  let reportBody: unknown

  beforeEach(() => {
    posted = undefined
    reportBody = {
      job_id: 'job-1',
      available: true,
      report: {
        total_samples: 3,
        exported_samples: 3,
        ocr: { processed: 2, failed: 1, regions: 5 },
      },
    }
    usePreferences.setState({ workflowLanguage: 'zh' })
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        const json = (body: unknown, status = 200) =>
          new Response(JSON.stringify(body), {
            status,
            headers: { 'Content-Type': 'application/json' },
          })
        if (init?.method === 'POST') {
          posted = init.body ? JSON.parse(String(init.body)) : null
          if (url.includes('/path-bindings/preview')) {
            return json({
              status: 'ready',
              source_bound: true,
              output_bound: false,
              output_create_required: false,
              warnings: [],
              errors: [],
            })
          }
          if (url.includes('/path-bindings')) {
            return json({
              status: 'ready',
              source: { root_id: 'in', relative_path: '' },
              output: null,
              output_created: false,
            })
          }
          return json({ valid: true, errors: [], warnings: [] })
        }
        if (url.includes('/report')) return json(reportBody)
        if (url.endsWith('/models')) {
          return json({ items: [{ id: 'local-caption-v1', name: 'Local Caption', backend: 'onnx', loaded: true }] })
        }
        if (url.includes('/token-review')) return json({ items: [], unresolved: 0 })
        if (url.includes('/count-review')) return json({ items: [], pending: 0 })
        if (url.includes('/issues')) return json([])
        if (url.includes('/workflows/capabilities')) {
          return json({ profiles: ['e621'], work_modes: ['in_place', 'full_copy'], resources: [] })
        }
        if (url.includes('/workflows/resources')) {
          return json([
            { resource_id: 'classify-e621-20260812-v1', category: 'classify', fingerprint: 'a'.repeat(64) },
            { resource_id: 'replace-e621-pass-drop-v2', category: 'replace', fingerprint: 'b'.repeat(64) },
            { resource_id: 'ocr-paddleocr-2-9-1-cpu-v1', category: 'ocr', fingerprint: 'c'.repeat(64) },
          ])
        }
        if (url.includes('/workflows/jobs')) {
          return json([
            {
              job_id: 'job-1',
              status: 'completed',
              profile: 'e621',
              processed_samples: 3,
              total_samples: 3,
              current_module_id: 'export',
              created_at: '2026-08-11T00:00:00Z',
            },
          ])
        }
        if (url.includes('/roots')) {
          return json({ items: [{ id: 'in', name: 'Input', kind: 'input', writable: false }] })
        }
        return json({})
      },
    )
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    usePreferences.setState({ workflowLanguage: 'zh' })
  })

  it('is disabled by default and hides the confidence input', () => {
    renderPage()
    const toggle = screen.getByLabelText(copyFor('zh').enableOcr) as HTMLSelectElement
    expect(toggle.value).toBe('no')
    expect(screen.queryByLabelText(copyFor('zh').ocrMinConfidence)).not.toBeInTheDocument()
  })

  it('sends the confidence threshold once OCR is enabled', async () => {
    renderPage()

    const zh = copyFor('zh')
    const workMode = screen.getByLabelText(zh.workMode)
    await waitFor(() => expect(workMode).toBeEnabled())
    fireEvent.change(workMode, { target: { value: 'in_place' } })

    const sourcePath = screen.getByLabelText(zh.sourcePath) as HTMLInputElement
    fireEvent.change(sourcePath, { target: { value: 'C:\\datasets\\sample' } })
    expect(sourcePath.value).toBe('C:\\datasets\\sample')

    fireEvent.change(screen.getByLabelText(zh.enableOcr), {
      target: { value: 'yes' },
    })

    const confidence = screen.getByLabelText(copyFor('zh').ocrMinConfidence)
    expect(confidence).toBeInTheDocument()
    fireEvent.change(confidence, { target: { value: '0.8' } })

    const preflight = screen.getByRole('button', { name: copyFor('zh').preflight })
    await waitFor(() => expect(preflight).toBeEnabled())
    fireEvent.click(preflight)

    await waitFor(() => {
      expect((posted as { ocr?: unknown })?.ocr).toEqual({
        enabled: true,
        min_confidence: 0.8,
        resource_id: 'ocr-paddleocr-2-9-1-cpu-v1',
      })
    })
  })

  it('shows the per-stage OCR counters for a finished job', async () => {
    renderPage()
    fireEvent.click(await screen.findByText(/job-1/))

    expect(await screen.findByText(copyFor('zh').ocrTitle)).toBeInTheDocument()
    // A failed image is reported, and the panel says it is non-blocking.
    expect(screen.getByText(copyFor('zh').ocrRegions)).toBeInTheDocument()
    expect(screen.getByText(copyFor('zh').ocrUnavailableHint)).toBeInTheDocument()
  })

  it('renders no OCR panel when the job never ran OCR', async () => {
    reportBody = {
      job_id: 'job-1',
      available: true,
      report: { total_samples: 3, exported_samples: 3 },
    }
    renderPage()
    fireEvent.click(await screen.findByText(/job-1/))

    await waitFor(() => {
      expect(screen.queryByText(copyFor('zh').ocrTitle)).not.toBeInTheDocument()
    })
  })
})
