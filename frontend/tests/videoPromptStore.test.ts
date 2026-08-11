import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useVideoPromptStore } from '../src/store/app'
import type { VideoPromptPackage } from '../src/types'

const promptPackage: VideoPromptPackage = {
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

beforeEach(() => {
  vi.stubGlobal('URL', {
    createObjectURL: vi.fn(() => 'blob:video-prompt'),
    revokeObjectURL: vi.fn(),
  })
  useVideoPromptStore.setState({
    images: [],
    providerId: '',
    providerModel: '',
    promptMode: 'ref2va',
    fl2vaSingleImageRole: 'first',
    instruction: '',
    displayLanguage: 'both',
    revisions: [],
    currentRevisionId: undefined,
    viewedRevisionId: undefined,
    isGenerating: false,
    error: undefined,
    requestToken: 0,
  })
})

afterEach(() => {
  useVideoPromptStore.getState().clearTask()
  vi.unstubAllGlobals()
})

describe('video prompt task store', () => {
  it('keeps viewing a revision separate from restoring its generation baseline', () => {
    const store = useVideoPromptStore
    const image = new File(['image'], 'reference.png', { type: 'image/png' })
    store.getState().addImages([image])
    store.getState().setProvider('vision', 'vision-model')

    const firstToken = store.getState().beginGeneration()
    store.getState().completeGeneration(firstToken, 'ref2va', 'Create a slow push-in.', promptPackage)
    const first = store.getState().revisions[0]
    expect(first?.parent_revision_id).toBeUndefined()
    expect(first?.mode).toBe('ref2va')

    const secondToken = store.getState().beginGeneration()
    store.getState().completeGeneration(secondToken, 'ref2va', 'Make the camera static.', promptPackage)
    const second = store.getState().revisions[1]
    expect(second?.parent_revision_id).toBe(first?.id)

    store.getState().viewRevision(first!.id)
    expect(store.getState().viewedRevisionId).toBe(first?.id)
    expect(store.getState().currentRevisionId).toBe(second?.id)

    store.getState().restoreRevision(first!.id)
    const thirdToken = store.getState().beginGeneration()
    store.getState().completeGeneration(thirdToken, 'ref2va', 'Add a gentle blink.', promptPackage)
    const third = store.getState().revisions[2]
    expect(third?.parent_revision_id).toBe(first?.id)
    expect(store.getState().currentRevisionId).toBe(third?.id)
  })

  it('drops stale responses after clearing and preserves provider choices', () => {
    const store = useVideoPromptStore
    store.getState().addImages([new File(['image'], 'reference.png', { type: 'image/png' })])
    store.getState().setProvider('vision', 'vision-model')
    store.getState().setDisplayLanguage('en')
    const requestToken = store.getState().beginGeneration()

    store.getState().clearTask()
    store.getState().completeGeneration(requestToken, 'ref2va', 'Stale result', promptPackage)

    expect(store.getState().revisions).toHaveLength(0)
    expect(store.getState().images).toHaveLength(0)
    expect(store.getState().providerId).toBe('vision')
    expect(store.getState().providerModel).toBe('vision-model')
    expect(store.getState().displayLanguage).toBe('en')
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:video-prompt')
  })

  it('switches presets without mixing revision baselines', () => {
    const store = useVideoPromptStore
    store.getState().addImages([new File(['image'], 'reference.png', { type: 'image/png' })])
    store.getState().setProvider('vision', 'vision-model')
    const token = store.getState().beginGeneration()
    store.getState().completeGeneration(token, 'ref2va', 'Create a reference prompt.', promptPackage)

    store.getState().clearRevisions()
    store.getState().setPromptMode('fl2va')

    expect(store.getState().promptMode).toBe('fl2va')
    expect(store.getState().revisions).toHaveLength(0)
    expect(store.getState().images[0]?.file.name).toBe('reference.png')
    expect(store.getState().providerId).toBe('vision')
  })

  it('keeps ordered reference images and revokes previews removed by a mode limit', () => {
    const store = useVideoPromptStore
    store.getState().addImages([
      new File(['one'], 'one.png', { type: 'image/png' }),
      new File(['two'], 'two.png', { type: 'image/png' }),
      new File(['three'], 'three.png', { type: 'image/png' }),
    ])

    expect(store.getState().images.map((image) => image.file.name)).toEqual(['one.png', 'two.png', 'three.png'])
    store.getState().limitImages(2)

    expect(store.getState().images.map((image) => image.file.name)).toEqual(['one.png', 'two.png'])
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:video-prompt')
  })
})
