import { Copy, LoaderCircle, RotateCcw, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Button, ConfirmDialog, DialogLayer, EmptyState, Field, IconButton, Notice } from '../ui'
import { tagCategoryClass } from '../../lib/tagCategories'
import {
  formatTagForDisplay,
  toWriteStyle,
  translationFor,
  translationKey,
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
import { useTagTranslationMemory } from '../../store/tagTranslationMemory'
import { NlTranslatePanel } from './NlTranslatePanel'
import { TagPillEditor } from './TagPillEditor'
import { TranslateMissingButton } from './TranslateMissingButton'

const QUALITY_CHOICES = ['general', 'sensitive', 'questionable', 'explicit']

const COUNT_CHOICES: Array<{ value: StandardJsonFields['count']; label: string }> = [
  { value: '', label: '（留空）' },
  { value: 'solo', label: 'solo · 单人' },
  { value: 'duo', label: 'duo · 双人' },
  { value: 'trio', label: 'trio · 三人' },
  { value: 'group', label: 'group · 多人' },
]

// Formats a sidecar-less image can be created with; only raw_e621_json stays
// out because it is a verbatim external dump, not an editable shape.
const CREATE_KINDS: Array<{ value: 'tag_txt' | 'tags_json' | 'standard_json'; label: string }> = [
  { value: 'tag_txt', label: 'tag_txt（TXT，每行一个标签）' },
  { value: 'tags_json', label: 'tags_json（JSON 条目，可带分类）' },
  { value: 'standard_json', label: 'standard_json（标准九字段 JSON）' },
]

function emptyEditableContent(kind: 'tag_txt' | 'tags_json' | 'standard_json'): TagManagerEditableContent {
  if (kind === 'tags_json') return { kind: 'tags_json', tags: [] }
  if (kind === 'standard_json') {
    return {
      kind: 'standard_json',
      fields: {
        quality: [],
        count: '',
        character: '',
        series: '',
        artist: '',
        appearance: [],
        tags: [],
        environment: [],
        nl: '',
      },
    }
  }
  return { kind: 'tag_txt', tags: [] }
}

type Translations = Record<string, string>

function isReadOnly(content: TagManagerImageContent): boolean {
  return content.kind === 'raw_e621_json'
}

/** Every tag-like text a sidecar kind renders; `nl` and single-value fields are excluded. */
function contentTagTexts(content: TagManagerImageContent): string[] {
  if (content.kind === 'tag_txt') return content.tags
  if (content.kind === 'tags_json') return content.tags.map((entry) => entry.text)
  if (content.kind === 'standard_json') {
    return [
      ...content.fields.quality,
      ...content.fields.appearance,
      ...content.fields.tags,
      ...content.fields.environment,
    ]
  }
  if (content.kind === 'raw_e621_json') return content.tags
  return []
}

function missingTagTexts(content: TagManagerImageContent, translations: Translations): string[] {
  const seen = new Set<string>()
  const missing: string[] = []
  for (const text of contentTagTexts(content)) {
    const key = translationKey(text)
    if (!key || seen.has(key) || translationFor(translations, text)) continue
    seen.add(key)
    missing.push(text)
  }
  return missing
}

/** Rewrite every tag-like value of an outgoing payload in the active style.
 * Unknown keys (sidecar extras) spread along untouched, so metadata the editor
 * never renders still round-trips to the save request. */
function applyWriteStyle(
  content: TagManagerEditableContent,
  style: TagStyle,
): TagManagerEditableContent {
  if (content.kind === 'tag_txt') {
    return { kind: 'tag_txt', tags: content.tags.map((tag) => toWriteStyle(tag, style)) }
  }
  if (content.kind === 'tags_json') {
    return {
      ...content,
      tags: content.tags.map((entry) => ({ ...entry, text: toWriteStyle(entry.text, style) })),
    }
  }
  const fields = content.fields
  return {
    ...content,
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
 * Right-hand drawer for one image's sidecar content.
 *
 * The parent keys this component by image id only (NOT sidecar mtime): saving
 * must not remount the editor, or scroll position, focus and half-typed input
 * are lost mid-review. A `syncToken` bump forces the draft back to the
 * server content (explicit reload after a conflict).
 */
export function EditorDrawer({ detail, profile, saving, conflict, hasPrev, hasNext, onClose, onNavigate, onSave, onReload, syncToken, saveRevision, saveErrorToken }: {
  detail: TagManagerImageDetail
  profile: TagManagerProfile
  saving: boolean
  conflict: boolean
  hasPrev: boolean
  hasNext: boolean
  onClose: () => void
  onNavigate: (delta: -1 | 1) => void
  onSave: (content: TagManagerEditableContent, action?: 'close' | 'next' | 'prev') => void
  onReload: () => void
  syncToken?: string | number
  saveRevision?: number
  saveErrorToken?: number
}) {
  const [draft, setDraft] = useState<TagManagerImageContent>(() => detail.content)
  // Clean baseline for the dirty guard; it is updated only after the parent
  // reports a successful save or an explicit reload.
  const [baseline, setBaseline] = useState<TagManagerImageContent>(() => detail.content)
  const [pendingNav, setPendingNav] = useState<'close' | 'prev' | 'next' | null>(null)
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const copyTimer = useRef<number | null>(null)
  const pendingBaseline = useRef<TagManagerImageContent | null>(null)
  const tagStyle = usePreferences((state) => state.tagStyle)
  const readOnly = isReadOnly(draft)
  // Server-provided translations merged with the session-local on-demand ones.
  const memory = useTagTranslationMemory((state) => state.map)
  const translations = useMemo(
    () => ({ ...(detail.translations ?? {}), ...memory }),
    [detail.translations, memory],
  )
  const missingTags = useMemo(() => missingTagTexts(draft, translations), [draft, translations])
  const dirty = JSON.stringify(draft) !== JSON.stringify(baseline)

  // Explicit reload (conflict recovery): resync draft and baseline from the
  // `detail` prop when syncToken changes. Timing contract: the parent bumps
  // syncToken only AFTER the detail refetch resolves, so this effect always
  // reads fresh server content; normal refetches never bump the token and
  // never clobber the draft.
  useEffect(() => {
    setDraft(detail.content)
    setBaseline(detail.content)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [syncToken])

  useEffect(() => {
    if (saveRevision == null || pendingBaseline.current == null) return
    setBaseline(pendingBaseline.current)
    pendingBaseline.current = null
  }, [saveRevision])

  // A failed save attempt (network error or sidecar conflict) must never let
  // its in-flight draft become the baseline: drop the pending baseline so the
  // draft stays dirty and only a fresh successful save can bless it.
  useEffect(() => {
    if (saveErrorToken == null) return
    pendingBaseline.current = null
  }, [saveErrorToken])

  const editable = draft.kind === 'tag_txt' || draft.kind === 'tags_json' || draft.kind === 'standard_json'
  const requestNav = (target: 'close' | 'prev' | 'next') => {
    if (dirty && editable && !saving) {
      setPendingNav(target)
      return
    }
    if (target === 'close') onClose()
    else onNavigate(target === 'next' ? 1 : -1)
  }
  const save = (action?: 'close' | 'next' | 'prev') => {
    if (!editable || saving) return
    const payload = applyWriteStyle(draft, tagStyle)
    pendingBaseline.current = draft
    onSave(payload, action)
  }
  const discardAndNav = () => {
    const target = pendingNav
    setPendingNav(null)
    if (target === 'close') onClose()
    else if (target) onNavigate(target === 'next' ? 1 : -1)
  }
  const navLabels: Record<'close' | 'prev' | 'next', string> = {
    close: '关闭编辑器',
    prev: '上一张',
    next: '下一张',
  }
  /** Conflict recovery escape hatch: copy the unsaved draft out before the
   * reload discards it.  jsdom (and hardened browsers) may not expose the
   * clipboard, so failures degrade to a brief 复制失败 feedback. */
  const copyDraft = async () => {
    try {
      if (!navigator.clipboard) throw new Error('Clipboard API unavailable')
      await navigator.clipboard.writeText(JSON.stringify(draft, null, 2))
      setCopyState('copied')
    } catch {
      setCopyState('failed')
    }
    if (copyTimer.current != null) window.clearTimeout(copyTimer.current)
    copyTimer.current = window.setTimeout(() => setCopyState('idle'), 1500)
  }

  return <DialogLayer onClose={() => requestNav('close')}>
    <div className="tm-drawer drawer" role="dialog" aria-modal="true" aria-labelledby="tm-drawer-title">
      <header className="drawer-header">
        <div className="tm-drawer-heading">
          <p className="eyebrow">IMAGE EDITOR</p>
          <h2 id="tm-drawer-title">{detail.file_name}</h2>
        </div>
        <IconButton label="关闭" onClick={() => requestNav('close')}><X size={17} /></IconButton>
      </header>
      <div className="drawer-body tm-drawer-body">
        <div className="tm-drawer-meta">
          <span className="tm-badge">{detail.sidecar_kind === 'none' ? '无 sidecar' : detail.sidecar_kind}</span>
          <small className="mono" title={detail.relative_path}>{detail.relative_path}</small>
          {(detail.width != null || detail.height != null) && <small>{detail.width ?? '?'}×{detail.height ?? '?'}</small>}
          {!readOnly && <TranslateMissingButton profile={profile} tags={missingTags} />}
        </div>
        {conflict && <Notice tone="warning">
          <span>sidecar 在编辑期间被外部修改，本次保存已被拒绝以避免覆盖他人改动。可先把当前草稿复制到剪贴板留底（避免重新加载时丢失），再决定是否重新加载。</span>
          <Button size="sm" variant="outline" icon={<Copy size={13} />} onClick={copyDraft}>
            {copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败' : '复制草稿'}
          </Button>
          <Button size="sm" variant="outline" icon={<RotateCcw size={13} />} onClick={onReload}>重新加载</Button>
        </Notice>}
        <EditorBody
          draft={draft}
          profile={profile}
          readOnly={readOnly}
          translations={translations}
          onDraft={setDraft}
        />
      </div>
      <footer className="tm-drawer-footer">
        <span className="muted">
          保存会写入 sidecar 并记入撤销日志{tagStyle === 'space' ? '，标签以空格写入' : ''}{dirty && ' · 有未保存更改'}
        </span>
        <div className="tm-drawer-footer-actions">
          <Button variant="quiet" disabled={!hasPrev || saving} onClick={() => requestNav('prev')}>上一张</Button>
          <Button variant="quiet" disabled={!hasNext || saving} onClick={() => requestNav('next')}>下一张</Button>
          <Button variant="secondary" onClick={() => requestNav('close')}>关闭</Button>
          {hasPrev && <Button
            icon={saving ? <LoaderCircle className="spin" size={15} /> : undefined}
            disabled={readOnly || !editable || saving}
            onClick={() => save('prev')}
          >保存并上一张</Button>}
          <Button
            icon={saving ? <LoaderCircle className="spin" size={15} /> : undefined}
            disabled={readOnly || !editable || saving}
            onClick={() => save()}
          >保存</Button>
          {hasNext && <Button
            icon={saving ? <LoaderCircle className="spin" size={15} /> : undefined}
            disabled={readOnly || !editable || saving}
            onClick={() => save('next')}
          >保存并下一张</Button>}
        </div>
      </footer>
    </div>
    {pendingNav != null && <ConfirmDialog
      title={dirty ? `有未保存的更改，仍要${navLabels[pendingNav]}？` : navLabels[pendingNav]}
      detail={<span>当前草稿尚未保存，{pendingNav === 'close' ? '关闭编辑器' : '切换图片'}会丢弃这些更改。</span>}
      confirmLabel="丢弃更改"
      onConfirm={discardAndNav}
      onClose={() => setPendingNav(null)}
    />}
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
      <p className="tm-editor-hint">点 × 移除标签；回车或点击建议添加新标签。</p>
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
      <p className="tm-editor-hint">条目可携带分类与置信度；新标签的分类来自标签库查询结果。点 × 移除标签。</p>
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
            onDraft({ ...content, tags: [...content.tags, category ? { text: tag, category } : { text: tag }] })
          }
        }}
        onRemove={(index) => onDraft({ ...content, tags: content.tags.filter((_, candidate) => candidate !== index) })}
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
  return <NoneSidecarCreator onCreate={onDraft} />
}

/** Sidecar-less image: pick a format, then edit and save the empty draft
 * (the PATCH turns kind `none` into any editable kind server-side). */
function NoneSidecarCreator({ onCreate }: { onCreate: (content: TagManagerImageContent) => void }) {
  const [kind, setKind] = useState<'tag_txt' | 'tags_json' | 'standard_json'>('tag_txt')
  return <section className="tm-editor-section" aria-label="新建 sidecar">
    <EmptyState title="暂无 sidecar" detail="该图片还没有标签文件。选择一个格式创建空草稿，编辑后保存即可生成 sidecar。" />
    <div className="tm-create-kind" role="radiogroup" aria-label="sidecar 格式">
      {CREATE_KINDS.map((choice) => (
        <label key={choice.value} className="tm-create-kind-option">
          <input
            type="radio"
            name="tm-sidecar-kind"
            value={choice.value}
            checked={kind === choice.value}
            onChange={() => setKind(choice.value)}
          />
          <span>{choice.label}</span>
        </label>
      ))}
    </div>
    <div>
      <Button onClick={() => onCreate(emptyEditableContent(kind))}>创建</Button>
    </div>
  </section>
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
  const setFields = (patch: Partial<StandardJsonFields>) => onDraft({ ...content, fields: { ...fields, ...patch } })
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
