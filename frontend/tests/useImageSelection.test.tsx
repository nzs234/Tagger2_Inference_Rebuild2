import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { useImageSelection } from '../src/components/tagManager/useImageSelection'
import type { TagManagerImageSummary } from '../src/lib/tagManager'

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

/** Minimal harness: one toggle button per image forwarding the click modifiers. */
function Harness({ images }: { images: TagManagerImageSummary[] }) {
  const { selectedIds, selectedIdList, toggle, selectAll, clear } = useImageSelection(images)
  return <div>
    <span data-testid="ids">{selectedIdList.join(',')}</span>
    <span data-testid="count">{selectedIds.size}</span>
    {images.map((image) => (
      <button
        key={image.id}
        type="button"
        onClick={(event) => toggle(image, { shift: event.shiftKey, ctrl: event.ctrlKey })}
      >
        toggle-{image.id}
      </button>
    ))}
    <button type="button" onClick={selectAll}>select-all</button>
    <button type="button" onClick={clear}>clear</button>
  </div>
}

const pageOne = [item(1), item(2), item(3)]
const pageTwo = [item(101), item(102), item(103)]

/** The rendered id list, sorted numerically (the Set's order is insertion order). */
function sortedIds(): number[] {
  return screen.getByTestId('ids').textContent!.split(',').filter(Boolean).map(Number).sort((left, right) => left - right)
}

describe('useImageSelection', () => {
  it('toggles single cards with plain clicks', () => {
    render(<Harness images={pageOne} />)
    fireEvent.click(screen.getByText('toggle-1'))
    expect(screen.getByTestId('ids')).toHaveTextContent('1')
    fireEvent.click(screen.getByText('toggle-1'))
    expect(screen.getByTestId('ids')).toHaveTextContent('')
  })

  it('ctrl-click adds to and removes from the selection without clearing it', () => {
    render(<Harness images={pageOne} />)
    fireEvent.click(screen.getByText('toggle-1'))
    fireEvent.click(screen.getByText('toggle-3'), { ctrlKey: true })
    expect(screen.getByTestId('ids')).toHaveTextContent('1,3')
    fireEvent.click(screen.getByText('toggle-1'), { ctrlKey: true })
    expect(screen.getByTestId('ids')).toHaveTextContent('3')
  })

  it('extends the selection from the anchor with shift-click', () => {
    render(<Harness images={pageOne} />)
    fireEvent.click(screen.getByText('toggle-1'))
    fireEvent.click(screen.getByText('toggle-3'), { shiftKey: true })
    expect(screen.getByTestId('ids')).toHaveTextContent('1,2,3')

    // Every click re-anchors: unselect the middle card, then extend the range
    // backwards from the new anchor (extension adds, it never replaces).
    fireEvent.click(screen.getByText('toggle-2'))
    expect(screen.getByTestId('ids')).toHaveTextContent('1,3')
    fireEvent.click(screen.getByText('toggle-1'), { shiftKey: true })
    expect(sortedIds()).toEqual([1, 2, 3])
  })

  it('treats a shift-click without an anchor as a plain toggle', () => {
    render(<Harness images={pageOne} />)
    fireEvent.click(screen.getByText('toggle-3'), { shiftKey: true })
    expect(screen.getByTestId('ids')).toHaveTextContent('3')
  })

  it('drops the range anchor when the page composition changes underneath it', () => {
    const { rerender } = render(<Harness images={pageOne} />)
    fireEvent.click(screen.getByText('toggle-1'))
    // A page flip replaces the images: the a-page anchor becomes stale.
    rerender(<Harness images={pageTwo} />)
    fireEvent.click(screen.getByText('toggle-103'), { shiftKey: true })
    // Plain toggle of the clicked card plus the still-selected first page id.
    expect(screen.getByTestId('ids')).toHaveTextContent('1,103')
  })

  it('resets the anchor on clear so the next shift-click is a plain toggle', () => {
    render(<Harness images={pageOne} />)
    fireEvent.click(screen.getByText('toggle-1'))
    fireEvent.click(screen.getByText('clear'))
    fireEvent.click(screen.getByText('toggle-3'), { shiftKey: true })
    expect(screen.getByTestId('ids')).toHaveTextContent('3')
  })

  it('selects every image on the current page with selectAll', () => {
    render(<Harness images={pageOne} />)
    fireEvent.click(screen.getByText('select-all'))
    expect(screen.getByTestId('ids')).toHaveTextContent('1,2,3')
  })
})
