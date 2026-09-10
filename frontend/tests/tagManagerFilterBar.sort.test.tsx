import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { FilterBar } from '../src/components/tagManager/FilterBar'
import { emptyImageFilter, type TagManagerSort } from '../src/lib/tagManager'

// The sort dropdown grew from three to five entries when the backend added the
// `_asc` variants.  Both legacy descending values and the new ascending ones
// must be rendered and forwarded unchanged through onSortChange.

const OPTIONS: Array<[string, string]> = [
  ['name', '名称'],
  ['mtime', '修改时间（新→旧）'],
  ['mtime_asc', '修改时间（旧→新）'],
  ['tags', '标签数（多→少）'],
  ['tag_count_asc', '标签数（少→多）'],
]

function renderBar(sort: TagManagerSort = 'name') {
  const onSortChange = vi.fn()
  render(<FilterBar
    filter={emptyImageFilter}
    sort={sort}
    profile="e621"
    onChange={vi.fn()}
    onSortChange={onSortChange}
  />)
  return { onSortChange }
}

describe('FilterBar sort control', () => {
  it('renders the five labelled sort options in order', () => {
    renderBar()
    const select = screen.getByLabelText('排序') as HTMLSelectElement
    const rendered = Array.from(select.options).map((option) => [option.value, option.textContent] as [string, string])
    expect(rendered).toEqual(OPTIONS)
  })

  it('forwards each sort value, ascending variants included', () => {
    const { onSortChange } = renderBar()
    const select = screen.getByLabelText('排序')
    for (const [value] of OPTIONS) {
      fireEvent.change(select, { target: { value } })
    }
    expect(onSortChange.mock.calls.map(([value]) => value)).toEqual(OPTIONS.map(([value]) => value))
  })

  it('reflects a persisted ascending value as the selected option', () => {
    renderBar('tag_count_asc')
    expect(screen.getByLabelText('排序')).toHaveValue('tag_count_asc')
  })
})
