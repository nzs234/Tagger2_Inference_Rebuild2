import { Field } from '../ui'
import {
  formatTagForDisplay,
  type ImageFilterState,
  type TagManagerSidecarFilter,
  type TagManagerSort,
} from '../../lib/tagManager'
import { usePreferences } from '../../store/app'

/** Filters accept either separator style; the backend matches both. */
function parseTagList(value: string): string[] {
  return value.split(',').map((tag) => tag.trim()).filter(Boolean)
}

export function FilterBar({ filter, sort, disabled, onChange, onSortChange }: {
  filter: ImageFilterState
  sort: TagManagerSort
  disabled?: boolean
  onChange: (next: ImageFilterState) => void
  onSortChange: (sort: TagManagerSort) => void
}) {
  const tagStyle = usePreferences((state) => state.tagStyle)
  const render = (tags: string[]) => tags.map((tag) => formatTagForDisplay(tag, tagStyle)).join(', ')

  return <div className="tm-filter-grid">
    <Field label="包含标签" hint="逗号分隔，下划线或空格均可">
      <input
        value={render(filter.includeTags)}
        aria-label="包含标签"
        disabled={disabled}
        spellCheck={false}
        placeholder="solo, long_hair"
        onChange={(event) => onChange({ ...filter, includeTags: parseTagList(event.target.value) })}
      />
    </Field>
    <Field label="匹配模式">
      <select
        value={filter.includeMode}
        aria-label="匹配模式"
        disabled={disabled}
        onChange={(event) => onChange({ ...filter, includeMode: event.target.value as ImageFilterState['includeMode'] })}
      >
        <option value="all">包含全部标签</option>
        <option value="any">包含任意标签</option>
      </select>
    </Field>
    <Field label="排除标签" hint="逗号分隔">
      <input
        value={render(filter.excludeTags)}
        aria-label="排除标签"
        disabled={disabled}
        spellCheck={false}
        placeholder="comic"
        onChange={(event) => onChange({ ...filter, excludeTags: parseTagList(event.target.value) })}
      />
    </Field>
    <Field label="Sidecar 类型">
      <select
        value={filter.kind}
        aria-label="Sidecar 类型"
        disabled={disabled}
        onChange={(event) => onChange({ ...filter, kind: event.target.value as ImageFilterState['kind'] })}
      >
        <option value="any">任意</option>
        <option value="none">无 sidecar</option>
        <option value="tag_txt">tag_txt</option>
        <option value="tags_json">tags_json</option>
        <option value="standard_json">standard_json</option>
        <option value="raw_e621_json">raw_e621_json</option>
      </select>
    </Field>
    <Field label="Sidecar 状态">
      <select
        value={filter.sidecar}
        aria-label="Sidecar 状态"
        disabled={disabled}
        onChange={(event) => onChange({ ...filter, sidecar: event.target.value as TagManagerSidecarFilter })}
      >
        <option value="any">任意</option>
        <option value="present">已有 sidecar</option>
        <option value="missing">缺失 sidecar</option>
      </select>
    </Field>
    <Field label="排序">
      <select value={sort} aria-label="排序" disabled={disabled} onChange={(event) => onSortChange(event.target.value as TagManagerSort)}>
        <option value="name">文件名</option>
        <option value="mtime">修改时间</option>
        <option value="tags">标签数</option>
      </select>
    </Field>
  </div>
}
