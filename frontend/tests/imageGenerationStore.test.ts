import { beforeEach, describe, expect, it } from 'vitest'
import {
  initialImageGenerationDraft,
  useImageGenerationStore,
} from '../src/store/imageGeneration'

beforeEach(() => {
  localStorage.clear()
  useImageGenerationStore.setState({
    draft: { ...initialImageGenerationDraft },
    activeJobId: undefined,
  })
})

describe('image generation draft store', () => {
  it('persists serializable controls and the active job without file objects', () => {
    useImageGenerationStore.getState().updateDraft({
      providerId: 'xai-images',
      model: 'grok-2-image-1212',
      prompt: 'A clean product photograph',
      n: 3,
      responseFormat: 'url',
    })
    useImageGenerationStore.getState().setActiveJobId('job-123')

    const raw = localStorage.getItem('tagger2-image-generation-draft')
    expect(raw).not.toBeNull()
    const persisted = JSON.parse(raw!) as {
      state: { draft: Record<string, unknown>; activeJobId?: string }
    }
    expect(persisted.state.draft).toMatchObject({
      providerId: 'xai-images',
      model: 'grok-2-image-1212',
      prompt: 'A clean product photograph',
      n: 3,
      responseFormat: 'url',
    })
    expect(persisted.state.activeJobId).toBe('job-123')
    expect(raw).not.toContain('references')
    expect(raw).not.toContain('previewUrl')
  })
})
