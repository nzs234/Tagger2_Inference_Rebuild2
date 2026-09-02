import { LoaderCircle, RotateCcw, X } from 'lucide-react'
import { useState } from 'react'
import { Button, DialogLayer, EmptyState, Field, IconButton, Notice } from '../ui'
import { tagCategoryClass } from '../../lib/tagCategories'
import {
  formatTagForDisplay,
  toWriteStyle,
  translationFor,
  type StandardJsonContent,
  type StandardJsonFields,
  type TagManagerEditableContent,
  type TagManagerImageContent,
  type TagManagerImageDetail,
  type TagManagerProfile,
  type TagsJsonContent,
  type TagTxtContent,
  type TagStyle,
} from '../../lib/tagManager'
import { usePreferences } from '../../store/app'
import { NlTranslatePanel } from './NlTranslatePanel'
import { TagPillEditor } from './TagPillEditor'

const QUALITY_CHOICES = ['general', 'sensitive', 'questionable', 'explicit']

const COUNT_CHOICES: Array<{ value: StandardJsonFields['count']; label: string }> = [
  { value: '', label: '（留空）' },
  { value: 'solo', label: 'solo · 单人' },
  { value: 'duo', label: 'duo · 双人' },
  { value: 'trio', label: 'trio · 三人' },
  { value: 'group', label: 'group · 多人' },
]

type Translations = Record<string, string>

function isReadOnly(content: TagManagerImageContent): boolean {
  return content.kind === 'raw_e621_json' || content.kind === 'none'
}

/** Rewrite every tag-like value of an outgoing payload in the active style. */
function applyWriteStyle(
  content: TagManagerEditableContent,
  style: TagStyle,
): TagManagerEditableContent {
  if (content.kind === 'tag_txt') {
    return { kind: 'tag_txt', tags: content.tags.map((tag) => toWriteStyle(tag, style)) }
  }
  if (content.kind === 'tags_json') {
    return {
      kind: 'tags_json',
      tags: content.tags.map((entry) => ({ ...entry, text: toWriteStyle(entry.text, style) })),
    }
  }
  const fields = content.fields
  return {
    kind: 'standard_json',
    fields: {
      ...fields,
      quality: fields.quality.map((tag) => toWriteStyle(tag, style)),
      appearance: fields.appearance.map((tag) => toWriteStyle(tag, style)),
      tags: fields.tags.map((tag) => toWriteStyle(tag, style)),
      environment: fields.environment.map((tag) => toWriteStyle(tag, style)),
    },
  }
}

/**
 * Right-hand drawer for one image's sidecar content. The parent remounts this
 * component (via `key`) when the image or its sidecar mtime changes, which
 * resets the local draft back to the freshly loaded content.
 */
export function EditorDrawer({ detail, profile, saving, conflict, onClose, onSave, onReload }: {
  detail: TagManagerImageDetail
  profile: TagManagerProfile
  saving: boolean
  conflict: boolean
  onClose: () => void
  onSave: (content: TagManagerEditableContent) => void
  onReload: () => void
}) {
  const [draft, setDraft] = useState<TagManagerImageContent>(() => detail.content)
  const tagStyle = usePreferences((state) => state.tagStyle)
  const readOnly = isReadOnly(draft)

  return <DialogLayer onClose={onClose}>
    <div className="tm-drawer drawer" role="dialog" aria-modal="true" aria-labelledby="tm-drawer-title">
      <header className="drawer-header">
        <div className="tm-drawer-heading">
          <p className="eyebrow">IMAGE EDITOR</p>
          <h2 id="tm-drawer-title">{detail.file_name}</h2>
        </div>
        <IconButton label="关闭" onClick={onClose}><X size={17} /></IconButton>
      </header>
      <div className="drawer-body tm-drawer-body">
        <div className="tm-drawer-meta">
          <span className="tm-badge">{detail.sidecar_kind === 'none' ? '无 sidecar' : detail.sidecar_kind}</span>
          <small className="mono" title={detail.relative_path}>{detail.relative_path}</small>
          {(detail.width != null || detail.height != null) && <small>{detail.width ?? '?'}×{detail.height ?? '?'}</small>}
        </div>
        {conflict && <Notice tone="warning">
          <span>sidecar 在编辑期间被外部修改，本次保存已被拒绝以避免覆盖他人改动。</span>
          <Button size="sm" variant="outline" icon={<RotateCcw size={13} />} onClick={onReload}>重新加载</Button>
        </Notice>}
        <EditorBody
          draft={draft}
          profile={profile}
          readOnly={readOnly}
          translations={detail.translations ?? {}}
          onDraft={setDraft}
        />
      </div>
      <footer className="tm-drawer-footer">
        <span className="muted">
          保存会写入 sidecar 并记入撤销日志{tagStyle === 'space' ? '，标签以空格写入' : ''}
        </span>
        <div className="tm-drawer-footer-actions">
          <Button variant="secondary" onClick={onClose}>关闭</Button>
          <Button
            icon={saving ? <LoaderCircle className="spin" size={15} /> : undefined}
            disabled={readOnly || saving}
            onClick={() => {
              if (draft.kind === 'tag_txt' || draft.kind === 'tags_json' || draft.kind === 'standard_json') {
                onSave(applyWriteStyle(draft, tagStyle))
              }
            }}
          >保存</Button>
        </div>
      </footer>
    </div>
  </DialogLayer>
}

function EditorBody({ draft, profile, readOnly, translations, onDraft }: {
  draft: TagManagerImageContent
  profile: TagManagerProfile
  readOnly: boolean
  translations: Translations
  onDraft: (next: TagManagerImageContent) => void
}) {
  if (draft.kind === 'tag_txt') {
    const content = draft as TagTxtContent
    return <section className="tm-editor-section" aria-label="tag_txt 编辑器">
      <p className="tm-editor-hint">点击标签即可移除；回车或点击建议添加新标签。</p>
      <TagPillEditor
        entries={content.tags.map((text) => ({ text, translation: translationFor(translations, text) }))}
        profile={profile}
        addLabel="添加标签"
        disabled={readOnly}
        onAdd={(tag) => {
          if (!content.tags.includes(tag)) onDraft({ kind: 'tag_txt', tags: [...content.tags, tag] })
        }}
        onRemove={(index) => onDraft({ kind: 'tag_txt', tags: content.tags.filter((_, candidate) => candidate !== index) })}
      />
    </section>
  }
  if (draft.kind === 'tags_json') {
    const content = draft as TagsJsonContent
    return <section className="tm-editor-section" aria-label="tags_json 编辑器">
      <p className="tm-editor-hint">条目可携带分类与置信度；新标签的分类来自标签库查询结果。</p>
      <TagPillEditor
        entries={content.tags.map((entry) => ({
          text: entry.text,
          category: entry.category,
          score: entry.score,
          translation: translationFor(translations, entry.text),
        }))}
        profile={profile}
        addLabel="添加标签"
        disabled={readOnly}
        onAdd={(tag, category) => {
          if (!content.tags.some((entry) => entry.text === tag)) {
            onDraft({ kind: 'tags_json', tags: [...content.tags, category ? { text: tag, category } : { text: tag }] })
          }
        }}
        onRemove={(index) => onDraft({ kind: 'tags_json', tags: content.tags.filter((_, candidate) => candidate !== index) })}
      />
    </section>
  }
  if (draft.kind === 'standard_json') {
    return <StandardJsonEditor
      content={draft as StandardJsonContent}
      profile={profile}
      readOnly={readOnly}
      translations={translations}
      onDraft={onDraft}
    />
  }
  if (draft.kind === 'raw_e621_json') {
    return <RawE621View tags={draft.tags} translations={translations} />
  }
  return <EmptyState title="暂无 sidecar" detail="该图片还没有标签文件。可以先使用批量操作为多张图片添加标签，保存后会生成 sidecar。" />
}

function RawE621View({ tags, translations }: { tags: string[]; translations: Translations }) {
  const bilingual = usePreferences((state) => state.bilingualTags)
  const tagStyle = usePreferences((state) => state.tagStyle)
  return <section className="tm-editor-section" aria-label="raw_e621_json 只读视图">
    <Notice tone="info">该图片的 sidecar 是 e621 原始 JSON，只能查看，不能在编辑器中修改。</Notice>
    <div className="tm-pill-row">
      {tags.map((tag) => {
        const translation = bilingual ? translationFor(translations, tag) : null
        const display = formatTagForDisplay(tag, tagStyle)
        return <span key={tag} className={`tm-pill ${tagCategoryClass(null)}`} title={translation ? `${display} · ${translation}` : display}>
          <span>{display}</span>
          {translation && <span className="tm-pill-zh">{translation}</span>}
        </span>
      })}
    </div>
  </section>
}

function StandardJsonEditor({ content, profile, readOnly, translations, onDraft }: {
  content: StandardJsonContent
  profile: TagManagerProfile
  readOnly: boolean
  translations: Translations
  onDraft: (next: TagManagerImageContent) => void
}) {
  const fields = content.fields
  const setFields = (patch: Partial<StandardJsonFields>) => onDraft({ kind: 'standard_json', fields: { ...fields, ...patch } })
  const toggleQuality = (choice: string) => {
    setFields({
      quality: fields.quality.includes(choice)
        ? fields.quality.filter((item) => item !== choice)
        : [...fields.quality, choice],
    })
  }
  const listEditor = (key: 'appearance' | 'tags' | 'environment', label: string) => <TagPillEditor
    entries={fields[key].map((text) => ({ text, translation: translationFor(translations, text) }))}
    profile={profile}
    addLabel={`添加${label}标签`}
    disabled={readOnly}
    onAdd={(tag) => { if (!fields[key].includes(tag)) setFields({ [key]: [...fields[key], tag] } as Partial<StandardJsonFields>) }}
    onRemove={(index) => setFields({ [key]: fields[key].filter((_, candidate) => candidate !== index) } as Partial<StandardJsonFields>)}
  />
  return <section className="tm-editor-section" aria-label="standard_json 编辑器">
    <div className="tm-editor-block">
      <p className="tm-editor-hint">质量</p>
      <div className="tm-quality-row" role="group" aria-label="质量快捷标签">
        {QUALITY_CHOICES.map((choice) => (
          <button
            key={choice}
            type="button"
            className={`tm-chip ${fields.quality.includes(choice) ? 'tm-chip-active' : ''}`}
            aria-pressed={fields.quality.includes(choice)}
            disabled={readOnly}
            onClick={() => toggleQuality(choice)}
          >{choice}</button>
        ))}
      </div>
      <TagPillEditor
        entries={fields.quality.map((text) => ({ text, translation: translationFor(translations, text) }))}
        profile={profile}
        addLabel="添加质量标签"
        disabled={readOnly}
        onAdd={(tag) => { if (!fields.quality.includes(tag)) setFields({ quality: [...fields.quality, tag] }) }}
        onRemove={(index) => setFields({ quality: fields.quality.filter((_, candidate) => candidate !== index) })}
      />
    </div>
    <div className="tm-editor-block">
      <Field label="数量">
        <select aria-label="数量" value={fields.count} disabled={readOnly} onChange={(event) => setFields({ count: event.target.value as StandardJsonFields['count'] })}>
          {COUNT_CHOICES.map((choice) => <option key={choice.value} value={choice.value}>{choice.label}</option>)}
        </select>
      </Field>
      <div className="tm-text-grid">
        <Field label="角色"><input aria-label="角色" value={fields.character} disabled={readOnly} onChange={(event) => setFields({ character: event.target.value })} /></Field>
        <Field label="作品"><input aria-label="作品" value={fields.series} disabled={readOnly} onChange={(event) => setFields({ series: event.target.value })} /></Field>
        <Field label="作者"><input aria-label="作者" value={fields.artist} disabled={readOnly} onChange={(event) => setFields({ artist: event.target.value })} /></Field>
      </div>
    </div>
    <div className="tm-editor-block"><p className="tm-editor-hint">外观</p>{listEditor('appearance', '外观')}</div>
    <div className="tm-editor-block"><p className="tm-editor-hint">标签</p>{listEditor('tags', '标签')}</div>
    <div className="tm-editor-block"><p className="tm-editor-hint">环境</p>{listEditor('environment', '环境')}</div>
    <div className="tm-editor-block">
      <Field label="自然语言描述">
        <textarea aria-label="自然语言描述" value={fields.nl} disabled={readOnly} onChange={(event) => setFields({ nl: event.target.value })} />
      </Field>
      <NlTranslatePanel
        text={fields.nl}
        disabled={readOnly}
        onApply={(translated) => setFields({ nl: translated })}
      />
    </div>
  </section>
}
