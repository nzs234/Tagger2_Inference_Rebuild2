import { useState, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { Field } from '../ui'
import {
  formatTagForDisplay,
  translationFor,
  type ImageFilterState,
  type TagManagerProfile,
  type TagManagerSidecarFilter,
  type TagManagerSort,
} from '../../lib/tagManager'
import { tagCategoryClass } from '../../lib/tagCategories'
import { usePreferences } from '../../store/app'
import { useTagTranslationMemory } from '../../store/tagTranslationMemory'
import { TagInput } from './TagInput'

/**
 * Field shell for one filter tag list.  Built from the same classes as the
 * shared `Field` but without its labelled group wrapper, so the autocomplete
 * input stays the single element named 包含标签/排除标签.
 */
function TagFilterField({ label, hint, children }: { label: string; hint: string; children: ReactNode }) {
  return <div className="field">
    <div className="field-label-row"><span className="field-label">{label}</span></div>
    {children}
    <span className="field-hint">{hint}</span>
  </div>
}

/**
 * One selected filter tag as a removable category-coloured pill.  The Chinese
 * name shows only when the session-local translation memory already knows the
 * tag; no lookups are fired for it.
 */
function FilterChip({ tag, category, removeLabel, onRemove }: {
  tag: string
  category?: string
  removeLabel: string
  onRemove: (tag: string) => void
}) {
  const bilingual = usePreferences((state) => state.bilingualTags)
  const tagStyle = usePreferences((state) => state.tagStyle)
  const memory = useTagTranslationMemory((state) => state.map)
  const translation = bilingual ? translationFor(memory, tag) : null

  return <span className={`tm-pill ${tagCategoryClass(category)}`}>
    <span>{formatTagForDisplay(tag, tagStyle)}</span>
    {translation && <span className="tm-pill-zh">{translation}</span>}
    <button
      type="button"
      className="tm-pill-remove"
      aria-label={removeLabel}
      title={removeLabel}
      onClick={() => onRemove(tag)}
    >
      <X size={11} aria-hidden="true" />
    </button>
  </span>
}

/**
 * Image filter bar.  Include/exclude tags are picked through the shared
 * autocomplete input and kept as chips — one tag per chip, so a tag that
 * itself contains a comma never needs escaping.  The raw tags go straight
 * into the filter state and are escaped per tag when the images query is
 * serialised.
 */
export function FilterBar({ filter, sort, profile, disabled, onChange, onSortChange }: {
  filter: ImageFilterState
  sort: TagManagerSort
  profile: TagManagerProfile
  disabled?: boolean
  onChange: (next: ImageFilterState) => void
  onSortChange: (sort: TagManagerSort) => void
}) {
  const tagStyle = usePreferences((state) => state.tagStyle)
  // Categories seen through the autocomplete, so chips keep their colour;
  // tags added elsewhere (e.g. from the stats panel) fall back to the general
  // palette instead of firing extra tag-db lookups.
  const [categories, setCategories] = useState<Record<string, string>>({})

  const addIncludeTag = (tag: string, category?: string) => {
    if (category && !categories[tag]) setCategories({ ...categories, [tag]: category })
    if (filter.includeTags.includes(tag)) return
    onChange({ ...filter, includeTags: [...filter.includeTags, tag] })
  }
  const addExcludeTag = (tag: string, category?: string) => {
    if (category && !categories[tag]) setCategories({ ...categories, [tag]: category })
    if (filter.excludeTags.includes(tag)) return
    onChange({ ...filter, excludeTags: [...filter.excludeTags, tag] })
  }
  const removeIncludeTag = (tag: string) => {
    if (!filter.includeTags.includes(tag)) return
    onChange({ ...filter, includeTags: filter.includeTags.filter((entry) => entry !== tag) })
  }
  const removeExcludeTag = (tag: string) => {
    if (!filter.excludeTags.includes(tag)) return
    onChange({ ...filter, excludeTags: filter.excludeTags.filter((entry) => entry !== tag) })
  }

  return <div className="tm-filter-grid">
    <TagFilterField label="包含标签" hint="自动补全；回车或点建议添加，点 × 移除">
      <div className="tm-filter-tags">
        {filter.includeTags.length > 0 && <div className="tm-pill-row">
          {filter.includeTags.map((tag) => (
            <FilterChip
              key={tag}
              tag={tag}
              category={categories[tag]}
              removeLabel={`移除筛选 ${formatTagForDisplay(tag, tagStyle)}`}
              onRemove={removeIncludeTag}
            />
          ))}
        </div>}
        <TagInput profile={profile} label="包含标签" disabled={disabled} onAdd={addIncludeTag} />
      </div>
    </TagFilterField>
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
    <TagFilterField label="排除标签" hint="自动补全；回车或点建议添加，点 × 移除">
      <div className="tm-filter-tags">
        {filter.excludeTags.length > 0 && <div className="tm-pill-row">
          {filter.excludeTags.map((tag) => (
            <FilterChip
              key={tag}
              tag={tag}
              category={categories[tag]}
              removeLabel={`移除排除 ${formatTagForDisplay(tag, tagStyle)}`}
              onRemove={removeExcludeTag}
            />
          ))}
        </div>}
        <TagInput profile={profile} label="排除标签" disabled={disabled} onAdd={addExcludeTag} />
      </div>
    </TagFilterField>
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
