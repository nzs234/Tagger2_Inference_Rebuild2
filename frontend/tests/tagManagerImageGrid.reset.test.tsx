import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ImageGrid } from '../src/components/tagManager/ImageGrid'
import type { TagManagerImageSummary } from '../src/lib/tagManager'

// The grid keeps its scroll offset while the same result set re-renders, but a
// new resetKey (session/page/filter/sort) must jump the viewport back to the
// top so a new page never opens halfway down.

function item(id: number): TagManagerImageSummary {
  return {
    id,
    relative_path: `${id}.png`,
    file_name: `${id}.png`,
    image_format: 'png',
    sidecar_kind: 'tag_txt',
    mtime: 1_000 + id,
    width: 64,
    height: 64,
    tag_count: 1,
    tags: [],
  }
}

const images = [item(1), item(2), item(3)]

function renderGrid(resetKey: string) {
  const view = render(<ImageGrid
    images={images}
    loadThumbnail={vi.fn().mockResolvedValue(new Blob())}
    selectedIds={new Set<number>()}
    resetKey={resetKey}
    onToggleSelect={vi.fn()}
    onOpen={vi.fn()}
  />)
  const container = screen.getByRole('grid') as HTMLDivElement
  return { view, container }
}

describe('ImageGrid scroll reset', () => {
  it('scrolls to the top when resetKey changes', () => {
    const { view, container } = renderGrid('ds-1:0:{}:name')
    container.scrollTop = 480
    expect(container.scrollTop).toBe(480)

    view.rerender(<ImageGrid
      images={images}
      loadThumbnail={vi.fn().mockResolvedValue(new Blob())}
      selectedIds={new Set<number>()}
      resetKey="ds-1:1:{}:name"
      onToggleSelect={vi.fn()}
      onOpen={vi.fn()}
    />)
    expect(container.scrollTop).toBe(0)
  })

  it('keeps the scroll offset when the key is unchanged', () => {
    const { view, container } = renderGrid('ds-1:0:{}:name')
    container.scrollTop = 300

    view.rerender(<ImageGrid
      images={images}
      loadThumbnail={vi.fn().mockResolvedValue(new Blob())}
      selectedIds={new Set<number>([1])}
      resetKey="ds-1:0:{}:name"
      onToggleSelect={vi.fn()}
      onOpen={vi.fn()}
    />)
    expect(container.scrollTop).toBe(300)
  })
})
