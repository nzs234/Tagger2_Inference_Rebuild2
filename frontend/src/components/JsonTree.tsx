import type { AnimaPayload } from '../types'

export function JsonTree({ value }: { value: AnimaPayload }) {
  const fields: Array<{ key: keyof AnimaPayload; label: string }> = [
    { key: 'quality', label: 'quality' }, { key: 'count', label: 'count' }, { key: 'character', label: 'character' },
    { key: 'series', label: 'series' }, { key: 'artist', label: 'artist' }, { key: 'appearance', label: 'appearance' },
    { key: 'tags', label: 'tags' }, { key: 'environment', label: 'environment' }, { key: 'nl', label: 'nl' },
  ]
  return <dl className="json-tree">
    {fields.map(({ key, label }) => {
      const item = value[key]
      return <div className="json-row" key={key}><dt>"{label}"</dt><dd>{Array.isArray(item) ? `[${item.map((v) => `"${v}"`).join(', ')}]` : item ? `"${item}"` : '""'}</dd></div>
    })}
  </dl>
}
