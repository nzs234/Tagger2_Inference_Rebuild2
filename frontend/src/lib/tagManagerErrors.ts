import { ApiError } from './api'

/**
 * Chinese copy for the backend error codes documented in docs/tag_manager.md
 * (错误码 section).  Backend messages are mostly English technical copy, so the
 * page maps known codes to user-facing Chinese text; codes missing here fall
 * back to the backend message, and non-ApiError values to the caller fallback.
 */
const KNOWN_CODES: Record<string, string> = {
  sidecar_conflict: 'sidecar 已被外部修改，请重新加载',
  sidecar_kind_mismatch: '编辑内容与 sidecar 格式不符',
  sidecar_read_only: 'raw e621 只读',
  batch_too_large: '超过单批 2000 张上限',
  session_busy: '会话正在执行其它操作，请稍后重试',
  root_not_writable: '根目录未开启可写',
  tag_db_unavailable: '标签库未就绪',
  nl_translate_unavailable: '没有可用的在线模型',
  tag_translate_unavailable: '没有可用的在线模型',
  nl_translate_failed: '在线翻译失败，可重试',
  tag_translate_failed: '在线翻译失败，可重试',
  thumbnail_failed: '缩略图生成失败',
  path_not_allowed: '路径未授权',
  dataset_not_found: '会话不存在',
  image_not_found: '图片不存在',
  undo_empty: '没有可撤销的操作',
  redo_empty: '没有可重做的操作',
}

/** Human-facing message for a Tag Manager request failure. */
export function describeTagManagerError(error: unknown, fallback: string): string {
  if (error instanceof ApiError) return KNOWN_CODES[error.code] ?? error.message
  return fallback
}

/**
 * Codes that describe an expected, non-destructive state rather than a real
 * failure: pressing undo/redo when the history is empty is a no-op the user
 * caused, so it renders as a warning instead of the danger tone.
 */
const WARNING_CODES: ReadonlySet<string> = new Set(['undo_empty', 'redo_empty'])

/** Notice tone for a Tag Manager failure; warning for benign empty-history codes. */
export function tagManagerErrorTone(error: unknown): 'warning' | 'danger' {
  if (error instanceof ApiError && WARNING_CODES.has(error.code)) return 'warning'
  return 'danger'
}
