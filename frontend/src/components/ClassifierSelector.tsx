import { Sparkles } from 'lucide-react'
import type { ClassifierProfile } from '../types'

interface ClassifierSelectorProps {
  profiles?: ClassifierProfile[]
  aesthetic: boolean
  onAestheticChange: (value: boolean) => void
}

export function ClassifierSelector({ profiles, aesthetic, onAestheticChange }: ClassifierSelectorProps) {
  const profile = profiles?.find((item) => item.id === 'aesthetic')
  const unavailable = profile?.enabled === false || Boolean(profile?.error)
  const state = profile?.error
    ? '不可用'
    : profile?.loaded
      ? '已加载'
      : profile?.enabled === false
        ? '已禁用'
        : '按需加载'
  return <div className="classifier-selector classifier-selector-single">
    <label className={`classifier-option ${aesthetic ? 'classifier-option-active' : ''} ${unavailable ? 'classifier-option-unavailable' : ''}`} title={profile?.error?.message}>
      <span className="classifier-option-icon"><Sparkles size={16} aria-hidden="true" /></span>
      <span className="classifier-option-copy"><strong>美学评分</strong><small>{state}</small></span>
      <input type="checkbox" aria-label="启用美学评分" checked={aesthetic} disabled={unavailable} onChange={(event) => onAestheticChange(event.target.checked)} />
    </label>
  </div>
}
