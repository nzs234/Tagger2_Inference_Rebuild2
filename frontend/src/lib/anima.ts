import type { AnimaPayload, TagItem } from '../types'

const TXT_FIELDS: Array<keyof Pick<AnimaPayload, 'quality' | 'count' | 'character' | 'series' | 'artist' | 'appearance' | 'tags' | 'environment'>> = [
  'quality',
  'count',
  'character',
  'series',
  'artist',
  'appearance',
  'tags',
  'environment',
]

export function formatAnimaTxt(payload: AnimaPayload): string {
  const parts: string[] = []
  for (const field of TXT_FIELDS) {
    const value = payload[field]
    if (Array.isArray(value)) {
      parts.push(...value.filter(Boolean))
    } else if (value.trim()) {
      parts.push(value.trim())
    }
  }
  return uniqueNormalized(parts).join(', ')
}

export function uniqueNormalized(values: string[]): string[] {
  const seen = new Set<string>()
  const result: string[] = []
  for (const value of values) {
    const clean = value.trim().replace(/\s+/g, ' ')
    const key = clean.toLocaleLowerCase().replace(/_/g, ' ')
    if (clean && !seen.has(key)) {
      seen.add(key)
      result.push(clean)
    }
  }
  return result
}

export function mergeTags(tags: TagItem[]): TagItem[] {
  const byTag = new Map<string, TagItem>()
  for (const tag of tags) {
    const key = tag.text.trim().toLocaleLowerCase().replace(/_/g, ' ')
    const current = byTag.get(key)
    if (!current || (tag.score ?? -1) > (current.score ?? -1)) {
      byTag.set(key, tag)
    }
  }
  return [...byTag.values()].sort((a, b) => (b.score ?? -1) - (a.score ?? -1))
}

export function downloadText(name: string, text: string, type = 'text/plain;charset=utf-8'): void {
  const blob = new Blob([text], { type })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  anchor.click()
  URL.revokeObjectURL(url)
}
