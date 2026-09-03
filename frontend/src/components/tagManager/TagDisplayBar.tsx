import { useState } from 'react'
import { BookOpen, Languages } from 'lucide-react'
import { tagManagerApi, type TagManagerProfile } from '../../lib/tagManager'
import { useQuery } from '@tanstack/react-query'
import { usePreferences } from '../../store/app'
import { WikiDrawer } from '../tagWiki/WikiDrawer'
import { TranslateMissingButton } from './TranslateMissingButton'

/**
 * Display preferences for booru tags. The separator switch is not cosmetic:
 * the active style is also the spelling written back to the sidecar on save,
 * which matches BooruDatasetTagManager's behaviour.
 */
export function TagDisplayBar({ profile, missingTags }: {
  profile: TagManagerProfile
  missingTags?: string[]
}) {
  const [wikiTag, setWikiTag] = useState<string | null>(null)
  const [wikiInput, setWikiInput] = useState('')
  const bilingual = usePreferences((state) => state.bilingualTags)
  const setBilingual = usePreferences((state) => state.setBilingualTags)
  const tagStyle = usePreferences((state) => state.tagStyle)
  const setTagStyle = usePreferences((state) => state.setTagStyle)

  const openWiki = () => {
    const tag = wikiInput.trim()
    if (!tag) return
    setWikiTag(tag)
    setWikiInput('')
  }

  const info = useQuery({
    queryKey: ['tag-manager', 'tag-db-info'],
    queryFn: tagManagerApi.tagDbInfo,
    staleTime: 300_000,
    retry: false,
  })
  const dictionary = info.data?.translations?.[profile]
  const dictionaryMissing = dictionary != null && dictionary.entries === 0

  return <div className="tm-display-bar">
    <div className="tm-toolbar-controls">
      <label className="toggle standalone">
        <input
          type="checkbox"
          aria-label="双语显示"
          checked={bilingual}
          onChange={(event) => setBilingual(event.target.checked)}
        />
        <span />双语显示
      </label>
      <div className="tm-scope-switch" role="group" aria-label="标签分隔符风格">
        <button
          type="button"
          className={tagStyle === 'underscore' ? 'mode-active' : ''}
          aria-pressed={tagStyle === 'underscore'}
          onClick={() => setTagStyle('underscore')}
        >下划线</button>
        <button
          type="button"
          className={tagStyle === 'space' ? 'mode-active' : ''}
          aria-pressed={tagStyle === 'space'}
          onClick={() => setTagStyle('space')}
        >空格</button>
      </div>
      <span className="tm-toolbar-hint">
        {tagStyle === 'space'
          ? '空格模式下保存时标签以空格写入 sidecar'
          : '下划线模式下保存时标签以下划线写入 sidecar'}
      </span>
      {missingTags != null && <TranslateMissingButton profile={profile} tags={missingTags} />}
      <form
        className="tm-wiki-lookup"
        onSubmit={(event) => {
          event.preventDefault()
          openWiki()
        }}
      >
        <input
          type="text"
          aria-label="在 Tag Wiki 中查询标签"
          placeholder="查询标签 Wiki，如 solo"
          value={wikiInput}
          onChange={(event) => setWikiInput(event.target.value)}
        />
        <button type="submit" disabled={!wikiInput.trim()} title={`在本页内置 Wiki 中查询 ${wikiInput.trim() || '标签'}`}>
          <BookOpen size={13} aria-hidden="true" />
          <span>查 Wiki</span>
        </button>
      </form>
    </div>
    {dictionaryMissing && <p className="tm-toolbar-hint tm-toolbar-warning">
      <Languages size={13} aria-hidden="true" />
      未找到 {profile} 的离线中文词库，标签暂时只显示英文。运行 scripts/build_tag_translations.py 生成词库后即可双语显示。
    </p>}
    {dictionary != null && dictionary.entries > 0 && <p className="tm-toolbar-hint">
      {profile} 离线词库：{dictionary.entries.toLocaleString('zh-CN')} 条
      {dictionary.updated ? ` · 生成于 ${dictionary.updated.slice(0, 10)}` : ''}
    </p>}
    {wikiTag && <WikiDrawer tag={wikiTag} onClose={() => setWikiTag(null)} />}
  </div>
}
