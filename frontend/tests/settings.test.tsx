import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Settings } from '../src/pages/Settings'

function renderSettings() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <Settings />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('Settings page', () => {
  it('fails closed when runtime settings cannot be loaded', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/settings')) {
        return new Response(JSON.stringify({ code: 'settings_unavailable', message: 'offline' }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })

    renderSettings()

    expect(await screen.findByText(/避免覆盖现有配置/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试设置' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '保存设置' })).toBeDisabled()
  })

  it('fails closed when the path allowlist cannot be loaded', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/roots')) {
        return new Response(JSON.stringify({ code: 'roots_unavailable', message: 'offline' }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response(JSON.stringify({
        input_root_id: '', output_root_id: '', default_mode: 'online', default_threshold: 0.35,
        default_json: true, default_txt: false, bind_host: '127.0.0.1', lan_enabled: false,
        access_token_configured: false, production: true, max_upload_mb: 32, max_image_pixels: 80_000_000,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    })

    renderSettings()

    expect(await screen.findByText(/避免清空路径选择/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试目录' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '保存设置' })).toBeDisabled()
  })
})
