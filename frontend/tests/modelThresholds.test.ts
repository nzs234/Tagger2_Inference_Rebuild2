import { describe, expect, it } from 'vitest'
import { effectiveThresholds, thresholdKeys, thresholdMapsEqual, thresholdName, thresholdSummary } from '../src/lib/modelThresholds'
import type { ModelProfile } from '../src/types'

const model = {
  id: 'model-a',
  name: 'Model A',
  backend: 'onnx',
  loaded: true,
  threshold: 0.35,
  thresholds: { default: 0.35, general: 0.4, character: 0.8 },
  threshold_source: 'model',
} as ModelProfile

describe('model threshold helpers', () => {
  it('merges the default threshold without losing category values', () => {
    expect(effectiveThresholds(model)).toEqual({ default: 0.35, general: 0.4, character: 0.8 })
  })

  it('uses the stable category order and appends unknown keys', () => {
    expect(thresholdKeys({ zeta: 0.2, character: 0.8, default: 0.3, general: 0.4 })).toEqual([
      'default', 'general', 'character', 'zeta',
    ])
    expect(thresholdName('character')).toBe('角色')
    expect(thresholdName('custom')).toBe('custom')
  })

  it('compares the full threshold map and summarizes overrides', () => {
    expect(thresholdMapsEqual({ default: 0.3 }, { default: 0.3 })).toBe(true)
    expect(thresholdMapsEqual({ default: 0.3 }, { default: 0.4 })).toBe(false)
    expect(thresholdMapsEqual({ default: 0.3 }, { default: 0.3, general: 0.3 })).toBe(false)
    expect(thresholdSummary(model)).toBe('模型预设 · 通用 0.40')
    expect(thresholdSummary(model, { general: 0.45 })).toBe('本次自定义 · 1 类')
  })
})
