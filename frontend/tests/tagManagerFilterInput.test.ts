import { describe, expect, it } from 'vitest'
import {
  escapeTagForQuery,
  formatTagFilterInput,
  parseTagFilterInput,
} from '../src/lib/tagManager'

describe('escapeTagForQuery', () => {
  it('passes plain booru tags through unchanged', () => {
    expect(escapeTagForQuery('long_hair')).toBe('long_hair')
    expect(escapeTagForQuery('solo')).toBe('solo')
    expect(escapeTagForQuery('Long Hair')).toBe('Long Hair')
  })

  it('escapes literal commas and backslashes only', () => {
    expect(escapeTagForQuery('1girl, smile')).toBe('1girl\\, smile')
    expect(escapeTagForQuery('a\\b')).toBe('a\\\\b')
    expect(escapeTagForQuery('a\\,b')).toBe('a\\\\\\,b')
    expect(escapeTagForQuery('wolf;fox')).toBe('wolf;fox')
  })
})

describe('parseTagFilterInput', () => {
  it('splits legacy comma-separated input and trims each tag', () => {
    expect(parseTagFilterInput('solo, long_hair ,comic')).toEqual(['solo', 'long_hair', 'comic'])
    expect(parseTagFilterInput('')).toEqual([])
    expect(parseTagFilterInput('solo,,comic,')).toEqual(['solo', 'comic'])
  })

  it('keeps a tag containing an escaped comma intact', () => {
    expect(parseTagFilterInput('1girl\\, smile')).toEqual(['1girl, smile'])
    expect(parseTagFilterInput('1girl\\, smile, wolf')).toEqual(['1girl, smile', 'wolf'])
  })

  it('unescapes escaped backslashes and keeps other escapes verbatim', () => {
    expect(parseTagFilterInput('a\\\\b')).toEqual(['a\\b'])
    expect(parseTagFilterInput('a\\;b')).toEqual(['a\\;b'])
    expect(parseTagFilterInput('a\\')).toEqual(['a\\'])
  })

  it('preserves underscores, spaces and letter case', () => {
    expect(parseTagFilterInput('Long_Hair, blue eyes, WOLF')).toEqual([
      'Long_Hair',
      'blue eyes',
      'WOLF',
    ])
  })
})

describe('formatTagFilterInput round trip', () => {
  const tags = ['solo', 'long_hair', '1girl, smile', 'a\\b']

  it('round trips in underscore style', () => {
    const rendered = formatTagFilterInput(tags, 'underscore')
    // The display form collapses whitespace to underscores (pre-existing
    // behaviour); commas and backslashes must still survive exactly.
    expect(parseTagFilterInput(rendered)).toEqual([
      'solo',
      'long_hair',
      '1girl,_smile',
      'a\\b',
    ])
  })

  it('round trips in space style', () => {
    const rendered = formatTagFilterInput(tags, 'space')
    // The display form swaps underscores for spaces, so the round trip returns
    // the space spelling; commas and backslashes must still survive exactly.
    expect(parseTagFilterInput(rendered)).toEqual([
      'solo',
      'long hair',
      '1girl, smile',
      'a\\b',
    ])
  })

  it('renders an empty list as an empty input', () => {
    expect(formatTagFilterInput([], 'underscore')).toBe('')
  })

  it('round trips overlong input without truncation; the backend enforces the 100-char per-tag cap with a stable 422', () => {
    const longTag = 'w'.repeat(300)
    const rendered = formatTagFilterInput([longTag], 'underscore')
    expect(parseTagFilterInput(rendered)).toEqual([longTag])
  })
})
