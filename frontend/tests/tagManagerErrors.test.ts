import { describe, expect, it } from 'vitest'
import { ApiError } from '../src/lib/api'
import { describeTagManagerError, tagManagerErrorTone } from '../src/lib/tagManagerErrors'

/** Every code documented in docs/tag_manager.md (错误码 section). */
const DOCUMENTED_CODES = [
  'sidecar_conflict',
  'sidecar_kind_mismatch',
  'sidecar_read_only',
  'batch_too_large',
  'session_busy',
  'root_not_writable',
  'tag_db_unavailable',
  'nl_translate_unavailable',
  'tag_translate_unavailable',
  'nl_translate_failed',
  'tag_translate_failed',
  'thumbnail_failed',
  'path_not_allowed',
  'dataset_not_found',
  'image_not_found',
  'undo_empty',
  'redo_empty',
]

describe('describeTagManagerError', () => {
  it('maps known backend error codes to Chinese copy', () => {
    expect(describeTagManagerError(new ApiError('Sidecar was modified', 409, 'sidecar_conflict'), '保存失败'))
      .toBe('sidecar 已被外部修改，请重新加载')
    expect(describeTagManagerError(new ApiError('Too many images', 413, 'batch_too_large'), '批量失败'))
      .toBe('超过单批 2000 张上限')
    expect(describeTagManagerError(new ApiError('Session busy', 409, 'session_busy'), '操作失败'))
      .toBe('会话正在执行其它操作，请稍后重试')
    expect(describeTagManagerError(new ApiError('No model', 409, 'tag_translate_unavailable'), '翻译失败'))
      .toBe('没有可用的在线模型')
  })

  it('covers every documented error code with a non-fallback Chinese mapping', () => {
    for (const code of DOCUMENTED_CODES) {
      expect(describeTagManagerError(new ApiError(code, 400, code), '兜底文案'), code).not.toBe('兜底文案')
    }
  })

  it('falls back to the backend message for unknown ApiError codes', () => {
    expect(describeTagManagerError(new ApiError('原始后端消息', 500, 'something_new'), '操作失败'))
      .toBe('原始后端消息')
  })

  it('falls back to the caller fallback for non-ApiError values', () => {
    expect(describeTagManagerError(new Error('boom'), '操作失败')).toBe('操作失败')
    expect(describeTagManagerError(undefined, '操作失败')).toBe('操作失败')
    expect(describeTagManagerError('raw string', '操作失败')).toBe('操作失败')
  })
})

describe('tagManagerErrorTone', () => {
  // Empty-history undo/redo is an expected no-op, so it must not render with
  // the destructive danger tone used for real failures.
  it('demotes undo_empty/redo_empty to the warning tone', () => {
    expect(tagManagerErrorTone(new ApiError('nothing to undo', 409, 'undo_empty'))).toBe('warning')
    expect(tagManagerErrorTone(new ApiError('nothing to redo', 409, 'redo_empty'))).toBe('warning')
  })

  it('keeps other ApiError codes on the danger tone', () => {
    expect(tagManagerErrorTone(new ApiError('busy', 409, 'session_busy'))).toBe('danger')
    expect(tagManagerErrorTone(new ApiError('unknown', 500, 'mystery'))).toBe('danger')
  })

  it('treats non-ApiError values as danger', () => {
    expect(tagManagerErrorTone(new Error('boom'))).toBe('danger')
    expect(tagManagerErrorTone(undefined)).toBe('danger')
  })
})
