import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { emptyImageFilter, type ImageFilterState, type TagManagerSort } from '../lib/tagManager'

/** Tag Manager view state that survives a reload (same pattern as usePreferences). */
interface TagManagerViewState {
  filter: ImageFilterState
  sort: TagManagerSort
  page: number
  activeId?: string
  statsOpen: boolean
  setFilter: (filter: ImageFilterState) => void
  setSort: (sort: TagManagerSort) => void
  setPage: (page: number) => void
  setActiveId: (activeId?: string) => void
  setStatsOpen: (statsOpen: boolean) => void
}

export const useTagManagerView = create<TagManagerViewState>()(
  persist(
    (set) => ({
      filter: emptyImageFilter,
      sort: 'name',
      page: 0,
      activeId: undefined,
      statsOpen: true,
      setFilter: (filter) => set({ filter }),
      setSort: (sort) => set({ sort }),
      setPage: (page) => set({ page }),
      setActiveId: (activeId) => set({ activeId }),
      setStatsOpen: (statsOpen) => set({ statsOpen }),
    }),
    {
      name: 'tagger2-tm-view',
      // Persisted `sort` needs no migration when the option list grows: the
      // original values name/mtime/tags all remain valid TagManagerSort
      // members, and the added mtime_asc/tag_count_asc simply appear as new
      // choices. An unknown value from a future build would fall through the
      // select's value lookup but still be sent to the backend as-is.
      partialize: (state) => ({
        filter: state.filter,
        sort: state.sort,
        page: state.page,
        activeId: state.activeId,
        statsOpen: state.statsOpen,
      }),
    },
  ),
)
