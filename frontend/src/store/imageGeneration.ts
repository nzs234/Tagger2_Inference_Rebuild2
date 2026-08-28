import { create } from 'zustand'
import { createJSONStorage, persist } from 'zustand/middleware'

export interface ImageGenerationDraft {
  providerId: string
  model: string
  operation: 'generate' | 'edit'
  prompt: string
  n: number
  aspectRatio: string
  imageSize: string
  resolution: string
  size: string
  quality: string
  background: string
  outputFormat: string
  outputCompression: number
  moderation: string
  inputFidelity: string
  responseFormat: 'b64_json' | 'url'
  includeTextModality: boolean
  systemInstruction: string
  temperature: number
  topP: number
  topK: number
  multiImageStrategy: 'parallel' | 'candidate_count'
}

export const initialImageGenerationDraft: ImageGenerationDraft = {
  providerId: '',
  model: '',
  operation: 'generate',
  prompt: '',
  n: 1,
  aspectRatio: '1:1',
  imageSize: '1K',
  resolution: '1k',
  size: 'auto',
  quality: 'auto',
  background: 'auto',
  outputFormat: 'png',
  outputCompression: 80,
  moderation: 'auto',
  inputFidelity: 'low',
  responseFormat: 'b64_json',
  includeTextModality: false,
  systemInstruction: '',
  temperature: 0.7,
  topP: 0.95,
  topK: 40,
  multiImageStrategy: 'parallel',
}

interface ImageGenerationStore {
  draft: ImageGenerationDraft
  activeJobId?: string
  updateDraft: (patch: Partial<ImageGenerationDraft>) => void
  setActiveJobId: (activeJobId?: string) => void
  resetDraft: () => void
}

export const useImageGenerationStore = create<ImageGenerationStore>()(
  persist(
    (set) => ({
      draft: initialImageGenerationDraft,
      activeJobId: undefined,
      updateDraft: (patch) => set((state) => ({ draft: { ...state.draft, ...patch } })),
      setActiveJobId: (activeJobId) => set({ activeJobId }),
      resetDraft: () => set({ draft: initialImageGenerationDraft }),
    }),
    {
      name: 'tagger2-image-generation-draft',
      version: 2,
      storage: createJSONStorage(() => localStorage),
      partialize: ({ draft, activeJobId }) => ({ draft, activeJobId }),
    },
  ),
)
