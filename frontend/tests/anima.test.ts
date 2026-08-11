import { describe, expect, it } from 'vitest'
import { formatAnimaTxt, mergeTags, uniqueNormalized } from '../src/lib/anima'
import type { AnimaPayload, TagItem } from '../src/types'

const payload: AnimaPayload = {
  quality: ['masterpiece', 'high_quality'],
  count: '1girl',
  character: 'alice',
  series: 'wonderland',
  artist: 'artist:sample',
  appearance: ['blue_eyes', 'long_hair'],
  tags: ['portrait', 'masterpiece'],
  environment: ['garden'],
  nl: 'A sentence that must not enter the tag text.',
}

describe('Anima TXT export', () => {
  it('uses the fixed field order, removes duplicates, and excludes nl', () => {
    const output = formatAnimaTxt(payload)
    expect(output).toBe('masterpiece, high_quality, 1girl, alice, wonderland, artist:sample, blue_eyes, long_hair, portrait, garden')
    expect(output).not.toContain(payload.nl)
  })

  it('normalizes underscore/space variants when deduplicating', () => {
    expect(uniqueNormalized(['blue_hair', ' Blue Hair ', 'BLUE_HAIR', 'green eyes'])).toEqual(['blue_hair', 'green eyes'])
  })
})

describe('local tag fusion', () => {
  it('retains the highest confidence tag while preserving its source', () => {
    const tags: TagItem[] = [
      { text: 'blue_hair', category: 'appearance', score: 0.62, source: 'model-a', model_id: 'a' },
      { text: 'Blue Hair', category: 'appearance', score: 0.91, source: 'model-b', model_id: 'b' },
    ]
    expect(mergeTags(tags)).toEqual([tags[1]])
  })
})
