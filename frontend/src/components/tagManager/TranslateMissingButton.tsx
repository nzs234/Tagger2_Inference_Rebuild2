import { useMutation } from '@tanstack/react-query'
import { Languages, LoaderCircle } from 'lucide-react'
import { useState } from 'react'
import { ApiError } from '../../lib/api'
import { tagManagerApi } from '../../lib/tagManager'
import { usePreferences } from '../../store/app'
import { useTagTranslationMemory } from '../../store/tagTranslationMemory'
import { Button } from '../ui'

const MAX_TAGS_PER_REQUEST = 200

/**
 * Translates the given tags with the configured online model and remembers
 * the results (backend-side in the user dictionary, client-side in the
 * translation memory store) so they never need translating again. Renders
 * nothing when every visible tag already resolves.
 */
export function TranslateMissingButton({ profile, tags }: {
  profile: 'e621' | 'danbooru'
  tags: string[]
}) {
  const providerId = usePreferences((state) => state.tagManagerTranslateProviderId)
  const model = usePreferences((state) => state.tagManagerTranslateModel)
  const ingest = useTagTranslationMemory((state) => state.ingest)
  const [saved, setSaved] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: (batch: string[]) =>
      tagManagerApi.translateTags({
        profile,
        tags: batch,
        provider_id: providerId || undefined,
        model: model || undefined,
      }),
    onSuccess: (result) => {
      ingest(result.translations)
      setSaved(result.translated_now)
      setError(null)
    },
    onError: (caught) => {
      setSaved(null)
      if (caught instanceof ApiError && caught.code === 'tag_translate_unavailable') {
        setError('没有可用的在线模型：请先在「Provider 配置」页添加并启用一个在线模型。')
      } else {
        setError(caught instanceof ApiError ? caught.message : '在线翻译失败，请稍后重试。')
      }
    },
  })

  if (tags.length === 0 && saved == null && error == null) return null
  const pending = mutation.isPending
  return <span className="tm-translate">
    {tags.length > 0 && <Button
      size="sm"
      variant="outline"
      disabled={pending}
      icon={pending ? <LoaderCircle className="spin" size={13} /> : <Languages size={13} />}
      onClick={() => mutation.mutate(tags.slice(0, MAX_TAGS_PER_REQUEST))}
    >{pending ? '翻译中…' : `在线翻译缺失标签（${tags.length}）`}</Button>}
    {saved != null && saved > 0 && <small className="tm-translate-hint">已翻译 {saved} 条并保存到本地词库</small>}
    {error && <small className="tm-translate-hint tm-translate-error">{error}</small>}
  </span>
}
