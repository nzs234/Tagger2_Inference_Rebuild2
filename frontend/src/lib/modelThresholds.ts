import type { ModelProfile } from '../types'

const THRESHOLD_ORDER = ['default', 'general', 'character', 'species', 'rating', 'other']

export function effectiveThresholds(model: Pick<ModelProfile, 'threshold' | 'thresholds'>): Record<string, number> {
  return { default: model.threshold ?? 0.35, ...(model.thresholds ?? {}) }
}

export function thresholdKeys(values: Record<string, number>): string[] {
  return Object.keys(values).sort((left, right) => {
    const leftIndex = THRESHOLD_ORDER.indexOf(left)
    const rightIndex = THRESHOLD_ORDER.indexOf(right)
    return (leftIndex < 0 ? 99 : leftIndex) - (rightIndex < 0 ? 99 : rightIndex) || left.localeCompare(right)
  })
}

export function thresholdName(key: string): string {
  return { default: '默认', general: '通用', character: '角色', species: '物种', rating: '分级', other: '其他' }[key] ?? key
}

export function thresholdMapsEqual(left: Record<string, number>, right: Record<string, number>): boolean {
  const keys = new Set([...Object.keys(left), ...Object.keys(right)])
  return [...keys].every((key) => left[key] === right[key])
}

export function thresholdSummary(model: ModelProfile, override?: Record<string, number>): string {
  if (override) return `本次自定义 · ${Object.keys(override).length} 类`
  return `${model.threshold_source === 'custom' ? '模型自定义' : '模型预设'} · 通用 ${(model.thresholds?.general ?? model.threshold ?? 0.35).toFixed(2)}`
}
