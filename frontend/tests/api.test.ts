import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../src/lib/api'
import type { Fl2vaPromptPackage, VideoPromptPackage } from '../src/types'

const packageResult: VideoPromptPackage = {
  change_summary_zh: 'Created an H3 reference-generation package.',
  subject_definitions: [{
    subject_number: 1,
    picture_number: 1,
    zh: 'reference image main subject and visible styling',
    en: 'the reference image main subject and visible styling',
  }],
  summary: {
    zh: '[reference generation] Create one connected reference-based video.',
    en: '[reference generation] Create one connected reference-based video.',
  },
  retention_analysis: [{
    subject_number: 1,
    shot_number: 1,
    visual_retention: 'fully_preserved',
    zh: 'Keep the visible appearance and composition stable.',
    en: 'Keep the visible appearance and composition stable.',
  }],
  detailed_description: {
    overview: {
      zh: 'a single continuous video with a stable final frame',
      en: 'a single continuous video with a stable final frame',
    },
    shots: [
      { shot_number: 1, cut_time_seconds: null, zh: 'Start in the reference composition.', en: 'Start in the reference composition.' },
      { shot_number: 2, cut_time_seconds: 3.5, zh: 'Settle on the final frame.', en: 'Settle on the final frame.' },
    ],
  },
  overall_soundscape: { zh: 'Quiet room tone.', en: 'Quiet room tone.' },
  non_diegetic_music: { zh: 'Subtle ambient score.', en: 'Subtle ambient score.' },
  assumptions_zh: [],
}

const fl2vaPackage: Fl2vaPromptPackage = {
  change_summary_zh: 'Created an FL2VA motion path.',
  base_mode: 'fl2va',
  reference_alignment: {
    zh: '参考图对齐到 00.00 秒起始状态。',
    en: 'How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; no second reference picture was supplied, so the final [Shot N] reaches the user-described ending state.',
  },
  integrated_multimodal_description: {
    zh: '[Shot 1] 从参考图状态开始，动作连续发展。',
    en: '[Shot 1] The subject begins in the supplied opening reference and moves continuously toward the requested ending state.',
  },
  overall_soundscape: { zh: '安静环境声。', en: 'Quiet ambience.' },
  non_diegetic_music: { zh: '无。', en: 'N/A' },
  assumptions_zh: [],
}

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

describe('API client', () => {
  it('unwraps FastAPI HTTPException detail envelopes', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: { code: 'provider_required', message: 'Choose a provider', retryable: false },
    }), { status: 400, headers: { 'content-type': 'application/json', 'x-request-id': 'req-1' } })))

    await expect(api.health()).rejects.toMatchObject({
      code: 'provider_required',
      message: 'Choose a provider',
      requestId: 'req-1',
      retryable: false,
    })
  })

  it('adds the session bearer token without exposing it in a URL', async () => {
    sessionStorage.setItem('tagger2_access_token', 'session-secret')
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: 'ok' }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await api.health()
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).not.toContain('session-secret')
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer session-secret')
  })

  it('downloads image artifacts with the same bearer-token policy', async () => {
    sessionStorage.setItem('tagger2_access_token', 'artifact-secret')
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      new Blob(['image-bytes'], { type: 'image/png' }),
      { status: 200, headers: { 'content-type': 'image/png' } },
    ))
    vi.stubGlobal('fetch', fetchMock)

    const result = await api.imageGenerationArtifact('artifact-1')
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/image-generation/artifacts/artifact-1')
    expect(url).not.toContain('artifact-secret')
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer artifact-secret')
    expect(result.type).toBe('image/png')
  })

  it('submits H3 Ref2VA generation as multipart data without an upload job', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(packageResult), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await api.generateVideoPrompt({
      images: [
        new File(['image-one'], 'frame-1.png', { type: 'image/png' }),
        new File(['image-two'], 'frame-2.png', { type: 'image/png' }),
      ],
      providerId: 'vision',
      providerModel: 'vision-pro',
      instruction: 'Use a slow camera push-in.',
      promptMode: 'ref2va',
      currentPackage: packageResult,
    })

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/video-prompts/generate')
    expect(init.body).toBeInstanceOf(FormData)
    const form = init.body as FormData
    expect(form.get('provider_id')).toBe('vision')
    expect(form.get('provider_model')).toBe('vision-pro')
    expect(form.get('instruction')).toBe('Use a slow camera push-in.')
    expect(form.get('prompt_mode')).toBe('ref2va')
    expect(form.get('current_package_json')).toContain('subject_definitions')
    expect(form.get('current_package_json')).toContain('[reference generation]')
    expect(form.getAll('images')).toHaveLength(2)
    expect(form.getAll('images').map((item) => (item as File).name)).toEqual(['frame-1.png', 'frame-2.png'])
  })

  it('submits the FL2VA preset and its base-guide package as multipart data', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(fl2vaPackage), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await api.generateVideoPrompt({
      images: [
        new File(['first'], 'first.png', { type: 'image/png' }),
        new File(['last'], 'last.png', { type: 'image/png' }),
      ],
      providerId: 'vision',
      providerModel: 'vision-pro',
      instruction: 'Reach a stable final pose.',
      promptMode: 'fl2va',
      currentPackage: fl2vaPackage,
    })

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const form = init.body as FormData
    expect(form.get('prompt_mode')).toBe('fl2va')
    expect(form.get('fl2va_single_image_role')).toBe('first')
    expect(form.get('current_package_json')).toContain('integrated_multimodal_description')
    expect(form.get('current_package_json')).not.toContain('subject_definitions')
  })
})
