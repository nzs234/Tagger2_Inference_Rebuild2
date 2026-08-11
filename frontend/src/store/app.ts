import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type {
  AppPage,
  ImageResult,
  QueueItem,
  QueueState,
  Fl2vaSingleImageRole,
  VideoPromptLanguage,
  VideoPromptMode,
  VideoPromptPackage,
  VideoPromptRevision,
} from '../types'

interface PreferencesState {
  page: AppPage
  compact: boolean
  sidebarOpen: boolean
  setPage: (page: AppPage) => void
  setCompact: (compact: boolean) => void
  setSidebarOpen: (open: boolean) => void
}

export const usePreferences = create<PreferencesState>()(
  persist(
    (set) => ({
      page: 'workbench',
      compact: false,
      sidebarOpen: false,
      setPage: (page) => set({ page, sidebarOpen: false }),
      setCompact: (compact) => set({ compact }),
      setSidebarOpen: (sidebarOpen) => set({ sidebarOpen }),
    }),
    { name: 'tagger2-ui', partialize: (state) => ({ page: state.page, compact: state.compact }) },
  ),
)

interface QueueStateStore {
  items: QueueItem[]
  selectedId?: string
  activeJobId?: string
  addFiles: (files: File[]) => void
  remove: (id: string) => void
  clear: () => void
  select: (id: string) => void
  setActiveJob: (jobId?: string) => void
  update: (id: string, patch: Partial<Pick<QueueItem, 'state' | 'progress' | 'error' | 'result'>>) => void
  updateByName: (name: string, state: QueueState, result?: ImageResult, error?: string) => void
  setAllState: (state: QueueState) => void
}

function createQueueId(file: File): string {
  return `${file.name}:${file.size}:${file.lastModified}:${crypto.randomUUID()}`
}

export const useQueueStore = create<QueueStateStore>((set, get) => ({
  items: [],
  addFiles: (files) => {
    const existing = new Set(get().items.map((item) => `${item.file.name}:${item.file.size}:${item.file.lastModified}`))
    const additions = files
      .filter((file) => file.type.startsWith('image/'))
      .filter((file) => !existing.has(`${file.name}:${file.size}:${file.lastModified}`))
      .map((file) => ({
        id: createQueueId(file),
        file,
        previewUrl: URL.createObjectURL(file),
        state: 'ready' as const,
        progress: 0,
      }))
    if (!additions.length) return
    set((state) => ({
      items: [...state.items, ...additions],
      selectedId: state.selectedId ?? additions[0]?.id,
    }))
  },
  remove: (id) => {
    const item = get().items.find((candidate) => candidate.id === id)
    if (item) URL.revokeObjectURL(item.previewUrl)
    set((state) => {
      const items = state.items.filter((candidate) => candidate.id !== id)
      return { items, selectedId: state.selectedId === id ? items[0]?.id : state.selectedId }
    })
  },
  clear: () => {
    get().items.forEach((item) => URL.revokeObjectURL(item.previewUrl))
    set({ items: [], selectedId: undefined, activeJobId: undefined })
  },
  select: (selectedId) => set({ selectedId }),
  setActiveJob: (activeJobId) => set({ activeJobId }),
  update: (id, patch) => set((state) => ({
    items: state.items.map((item) => (item.id === id ? { ...item, ...patch } : item)),
  })),
  updateByName: (name, queueState, result, error) => set((state) => ({
    items: state.items.map((item) => item.file.name === name
      ? { ...item, state: queueState, progress: queueState === 'done' ? 100 : item.progress, result, error }
      : item),
  })),
  setAllState: (queueState) => set((state) => ({
    items: state.items.map((item) => ({ ...item, state: queueState, progress: queueState === 'ready' ? 0 : item.progress })),
  })),
}))

interface VideoPromptImage {
  file: File
  previewUrl: string
}

interface VideoPromptState {
  images: VideoPromptImage[]
  providerId: string
  providerModel: string
  promptMode: VideoPromptMode
  fl2vaSingleImageRole: Fl2vaSingleImageRole
  instruction: string
  displayLanguage: VideoPromptLanguage
  revisions: VideoPromptRevision[]
  currentRevisionId?: string
  viewedRevisionId?: string
  isGenerating: boolean
  error?: string
  requestToken: number
  addImages: (files: File[]) => void
  removeImage: (index: number) => void
  limitImages: (maxImages: number) => void
  setProvider: (providerId: string, providerModel?: string) => void
  setPromptMode: (mode: VideoPromptMode) => void
  setFl2vaSingleImageRole: (role: Fl2vaSingleImageRole) => void
  setInstruction: (instruction: string) => void
  setDisplayLanguage: (language: VideoPromptLanguage) => void
  beginGeneration: () => number
  completeGeneration: (requestToken: number, mode: VideoPromptMode, instruction: string, promptPackage: VideoPromptPackage) => void
  failGeneration: (requestToken: number, error: string) => void
  viewRevision: (revisionId: string) => void
  restoreRevision: (revisionId: string) => void
  clearRevisions: () => void
  clearTask: () => void
  clearError: () => void
}

export const useVideoPromptStore = create<VideoPromptState>((set, get) => ({
  images: [],
  providerId: '',
  providerModel: '',
  promptMode: 'ref2va',
  fl2vaSingleImageRole: 'first',
  instruction: '',
  displayLanguage: 'both',
  revisions: [],
  isGenerating: false,
  requestToken: 0,
  addImages: (files) => {
    const additions = files
      .filter((file) => file.type.startsWith('image/'))
      .map((file) => ({ file, previewUrl: URL.createObjectURL(file) }))
    if (!additions.length) return
    set((state) => ({
      images: [...state.images, ...additions],
      instruction: '',
      revisions: [],
      currentRevisionId: undefined,
      viewedRevisionId: undefined,
      isGenerating: false,
      error: undefined,
      requestToken: state.requestToken + 1,
    }))
  },
  removeImage: (index) => {
    const image = get().images[index]
    if (!image) return
    URL.revokeObjectURL(image.previewUrl)
    set((state) => ({
      images: state.images.filter((_candidate, candidateIndex) => candidateIndex !== index),
      instruction: '',
      revisions: [],
      currentRevisionId: undefined,
      viewedRevisionId: undefined,
      isGenerating: false,
      error: undefined,
      requestToken: state.requestToken + 1,
    }))
  },
  limitImages: (maxImages) => {
    const safeMax = Math.max(0, maxImages)
    const extras = get().images.slice(safeMax)
    if (!extras.length) return
    extras.forEach((image) => URL.revokeObjectURL(image.previewUrl))
    set((state) => ({
      images: state.images.slice(0, safeMax),
      instruction: '',
      revisions: [],
      currentRevisionId: undefined,
      viewedRevisionId: undefined,
      isGenerating: false,
      error: undefined,
      requestToken: state.requestToken + 1,
    }))
  },
  setProvider: (providerId, providerModel) => set((state) => ({
    providerId,
    providerModel: providerModel ?? state.providerModel,
  })),
  setPromptMode: (promptMode) => set({ promptMode }),
  setFl2vaSingleImageRole: (fl2vaSingleImageRole) => set({ fl2vaSingleImageRole }),
  setInstruction: (instruction) => set({ instruction }),
  setDisplayLanguage: (displayLanguage) => set({ displayLanguage }),
  beginGeneration: () => {
    const requestToken = get().requestToken + 1
    set({ requestToken, isGenerating: true, error: undefined })
    return requestToken
  },
  completeGeneration: (requestToken, mode, instruction, promptPackage) => {
    if (get().requestToken !== requestToken) return
    const state = get()
    const revision: VideoPromptRevision = {
      id: crypto.randomUUID(),
      version: state.revisions.reduce((latest, item) => Math.max(latest, item.version), 0) + 1,
      mode,
      parent_revision_id: state.currentRevisionId,
      instruction,
      package: promptPackage,
      created_at: new Date().toISOString(),
    }
    set((current) => ({
      revisions: [...current.revisions, revision],
      currentRevisionId: revision.id,
      viewedRevisionId: revision.id,
      instruction: '',
      isGenerating: false,
      error: undefined,
    }))
  },
  failGeneration: (requestToken, error) => {
    if (get().requestToken !== requestToken) return
    set({ isGenerating: false, error })
  },
  viewRevision: (revisionId) => {
    if (get().revisions.some((revision) => revision.id === revisionId)) {
      set({ viewedRevisionId: revisionId })
    }
  },
  restoreRevision: (revisionId) => {
    if (get().revisions.some((revision) => revision.id === revisionId)) {
      set({ currentRevisionId: revisionId, viewedRevisionId: revisionId, error: undefined })
    }
  },
  clearRevisions: () => set((state) => ({
    instruction: '',
    revisions: [],
    currentRevisionId: undefined,
    viewedRevisionId: undefined,
    isGenerating: false,
    error: undefined,
    requestToken: state.requestToken + 1,
  })),
  clearTask: () => {
    get().images.forEach((image) => URL.revokeObjectURL(image.previewUrl))
    set((state) => ({
      images: [],
      instruction: '',
      revisions: [],
      currentRevisionId: undefined,
      viewedRevisionId: undefined,
      isGenerating: false,
      error: undefined,
      requestToken: state.requestToken + 1,
    }))
  },
  clearError: () => set({ error: undefined }),
}))
