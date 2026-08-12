import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DatasetWorkflow } from '../src/pages/DatasetWorkflow'
import { copyFor, workflowCopy } from '../src/lib/workflowCopy'
import { usePreferences } from '../src/store/app'

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <DatasetWorkflow />
    </QueryClientProvider>,
  )
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
        expect(value.trim().length, `${language}.${key}`).toBeGreaterThan(0)
      }
    }
  })

  it('keeps the two languages distinct for user-facing labels', () => {
    expect(copyFor('zh').navLabel).not.toEqual(copyFor('en').navLabel)
    expect(copyFor('en').title).toBe('Dataset Workflow')
  })
})

describe('DatasetWorkflow page', () => {
  beforeEach(() => {
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
        ])
      }
      if (url.includes('/workflows/jobs')) return json([])
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

  it('keeps the create button disabled until preflight passes', async () => {
    renderPage()
    const create = screen.getByRole('button', { name: '创建任务' })
    expect(create).toBeDisabled()
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

  beforeEach(() => {
    posts = []
    pending = 1
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
        if (url.includes('/count-review')) {
          return json({ items: [{ ...decision, status: pending ? 'pending' : 'confirmed' }], pending })
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

    expect(await screen.findByRole('button', { name: /开始任务/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /开始任务/ }))

    await waitFor(() => {
      expect(posts.some((post) => post.url.endsWith('/start'))).toBe(true)
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
          return json({ valid: true, errors: [], warnings: [] })
        }
        if (url.includes('/report')) return json(reportBody)
        if (url.includes('/token-review')) return json({ items: [], unresolved: 0 })
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
    fireEvent.change(screen.getByLabelText(zh.workMode), { target: { value: 'in_place' } })

    // The root list arrives asynchronously; selecting before it lands is a no-op.
    const sourceRoot = screen.getByLabelText(zh.sourceRoot) as HTMLSelectElement
    await waitFor(() => {
      expect(sourceRoot.options.length).toBeGreaterThan(1)
    })
    fireEvent.change(sourceRoot, { target: { value: 'in' } })
    expect(sourceRoot.value).toBe('in')

    fireEvent.change(screen.getByLabelText(zh.enableOcr), {
      target: { value: 'yes' },
    })

    const confidence = screen.getByLabelText(copyFor('zh').ocrMinConfidence)
    expect(confidence).toBeInTheDocument()
    fireEvent.change(confidence, { target: { value: '0.8' } })

    fireEvent.click(screen.getByRole('button', { name: copyFor('zh').preflight }))

    await waitFor(() => {
      expect((posted as { ocr?: unknown })?.ocr).toEqual({
        enabled: true,
        min_confidence: 0.8,
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
