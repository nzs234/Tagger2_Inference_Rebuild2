import { useQuery } from '@tanstack/react-query'
import { Copy, LoaderCircle, Languages } from 'lucide-react'
import { useState } from 'react'
import { api, ApiError } from '../../lib/api'
import { tagManagerApi } from '../../lib/tagManager'
import { usePreferences } from '../../store/app'
import { Button, Field, Notice } from '../ui'

const MAX_NL_LENGTH = 8000

/**
 * Translates the nine-field `nl` paragraph with one of the configured online
 * providers. The result is never written into the draft automatically: the user
 * applies it explicitly so a translation cannot silently replace their text.
 */
export function NlTranslatePanel({ text, disabled, onApply }: {
  text: string
  disabled?: boolean
  onApply: (translated: string) => void
}) {
  const providerId = usePreferences((state) => state.tagManagerTranslateProviderId)
  const model = usePreferences((state) => state.tagManagerTranslateModel)
  const setTranslateProvider = usePreferences((state) => state.setTagManagerTranslate)
  const [target, setTarget] = useState<'zh' | 'en'>('zh')
  const [result, setResult] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [copied, setCopied] = useState(false)

  const providers = useQuery({
    queryKey: ['providers'],
    queryFn: api.providers,
    staleTime: 60_000,
    retry: false,
  })
  // Only a provider that holds a credential can serve a translation; an
  // unkeyed default profile would fail upstream instead of here.
  const providerItems = (providers.data?.items ?? []).filter(
    (item) => item.enabled !== false && item.configured,
  )

  const translate = async () => {
    const source = text.trim()
    if (!source) {
      setError('自然语言描述为空，没有可翻译的内容。')
      return
    }
    if (source.length > MAX_NL_LENGTH) {
      setError(`自然语言描述超过 ${MAX_NL_LENGTH} 字，请先精简后再翻译。`)
      return
    }
    setPending(true)
    setError(null)
    setCopied(false)
    try {
      const response = await tagManagerApi.nlTranslate({
        text: source,
        target,
        provider_id: providerId || undefined,
        model: model || undefined,
      })
      setResult(response.text)
      // Remember what actually served the request so the next image reuses it.
      setTranslateProvider(response.provider_id, response.model)
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'nl_translate_unavailable') {
        setError('没有可用的在线模型：请先在「Provider 配置」页添加并启用一个在线模型，然后回到本页重试。')
      } else {
        setError(caught instanceof ApiError ? caught.message : '翻译失败，请稍后重试。')
      }
      setResult('')
    } finally {
      setPending(false)
    }
  }

  return <div className="tm-nl-translate">
    <div className="tm-nl-translate-controls">
      <Field label="翻译方向">
        <select
          aria-label="翻译方向"
          value={target}
          disabled={disabled || pending}
          onChange={(event) => setTarget(event.target.value === 'en' ? 'en' : 'zh')}
        >
          <option value="zh">译为中文</option>
          <option value="en">译为英文</option>
        </select>
      </Field>
      <Field label="在线模型">
        <select
          aria-label="翻译使用的在线模型"
          value={providerId}
          disabled={disabled || pending}
          onChange={(event) => setTranslateProvider(event.target.value)}
        >
          <option value="">（使用第一个可用模型）</option>
          {providerItems.map((provider) => (
            <option key={provider.id} value={provider.id}>{provider.name || provider.id}</option>
          ))}
        </select>
      </Field>
      <Field label="模型 ID" hint="留空则使用该 provider 的主模型">
        <input
          aria-label="翻译模型 ID"
          value={model}
          disabled={disabled || pending}
          spellCheck={false}
          placeholder="可留空"
          onChange={(event) => setTranslateProvider(providerId, event.target.value)}
        />
      </Field>
      <Button
        size="sm"
        icon={pending ? <LoaderCircle className="spin" size={14} /> : <Languages size={14} />}
        disabled={disabled || pending}
        onClick={() => void translate()}
      >{pending ? '翻译中…' : '翻译'}</Button>
    </div>
    {error && <Notice tone="warning">{error}</Notice>}
    {result && <div className="tm-nl-translate-result">
      <p className="tm-nl-translate-text">{result}</p>
      <div className="tm-nl-translate-actions">
        <Button
          size="sm"
          variant="quiet"
          icon={<Copy size={13} />}
          onClick={() => {
            void navigator.clipboard?.writeText(result)
            setCopied(true)
          }}
        >{copied ? '已复制' : '复制'}</Button>
        <Button size="sm" variant="outline" disabled={disabled} onClick={() => onApply(result)}>替换 NL</Button>
      </div>
    </div>}
  </div>
}
