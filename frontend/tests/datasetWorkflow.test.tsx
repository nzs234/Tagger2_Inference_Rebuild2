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
            category: 'replace',
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
