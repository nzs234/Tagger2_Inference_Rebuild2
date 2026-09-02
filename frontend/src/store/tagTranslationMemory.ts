import { create } from 'zustand'
import { translationKey } from '../lib/tagManager'

/**
 * Session-local cache of on-demand tag translations. The backend persists
 * every on-demand translation into its user dictionary, so this store only
 * needs to make freshly translated tags visible in already-rendered views
 * until the next server fetch carries them.
 */
interface TagTranslationMemoryState {
  map: Record<string, string>
  ingest: (translations: Record<string, string>) => void
  reset: () => void
}

export const useTagTranslationMemory = create<TagTranslationMemoryState>((set) => ({
  map: {},
  ingest: (translations) =>
    set((state) => {
      const next = { ...state.map }
      let changed = false
      for (const [tag, zh] of Object.entries(translations)) {
        const key = translationKey(tag)
        if (key && zh && next[key] !== zh) {
          next[key] = zh
          changed = true
        }
      }
      return changed ? { map: next } : state
    }),
  reset: () => set({ map: {} }),
}))
