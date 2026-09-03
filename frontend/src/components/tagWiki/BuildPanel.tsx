import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  Database,
  Download,
  Languages,
  LoaderCircle,
  RefreshCw,
} from 'lucide-react'
import {
  describeWikiError,
  tagWikiApi,
  type TagWikiStatus,
  type TranslateRequest,
  type TranslateStatus,
} from '../../lib/tagWiki'
import { Button, Notice, ProgressBar } from '../ui'

const PHASE_LABELS: Record<string, string> = {
  idle: '空闲',
  download: '正在下载 E621 数据包…',
  parse: '正在解析 Wiki 词条…',
  model: '正在准备 Embedding 向量模型…',
  embed: '正在生成向量索引…',
  done: '构建完成',
}

export const clampInt = (value: number, min: number, max: number) => {
  const base = Number.isFinite(value) ? value : min
  return Math.min(max, Math.max(min, Math.round(base)))
}

export function BuildPanel() {
  const [collapsed, setCollapsed] = useState(false)
  const [downloadDump, setDownloadDump] = useState(true)
  const [reindex, setReindex] = useState(true)
  const [forceReembed, setForceReembed] = useState(false)
  const [scope, setScope] = useState<TranslateRequest['scope']>('model_vocab')
  const [minPostCount, setMinPostCount] = useState<number>(1000)
  const [maxPages, setMaxPages] = useState<number>(2000)
  const [localError, setLocalError] = useState<string | null>(null)

  const queryClient = useQueryClient()

  const statusQuery = useQuery<TagWikiStatus>({
    queryKey: ['tag-wiki', 'status'],
    queryFn: tagWikiApi.status,
    refetchInterval: (query) => {
      const data = query.state.data
      const isBuilding = data?.build?.state === 'running'
      const isTranslating = data?.translate?.state === 'running'
      return isBuilding || isTranslating ? 2000 : 30_000
    },
    retry: 1,
  })

  const status = statusQuery.data
  const isTranslating = status?.translate?.state === 'running'

  // Adopt the server-configured default threshold (config [tag_wiki]) once it
  // is known; the effect only re-runs when that configured value changes.
  const configuredMinPostCount = status?.index?.min_post_count
  useEffect(() => {
    if (typeof configuredMinPostCount === 'number' && configuredMinPostCount >= 0) {
      setMinPostCount(configuredMinPostCount)
    }
  }, [configuredMinPostCount])

  // Dedicated fine-grained progress poll for the translate job; the aggregate
  // /status response above already covers the build pipeline.
  const translateProgressQuery = useQuery<TranslateStatus>({
    queryKey: ['tag-wiki', 'translate-progress'],
    queryFn: tagWikiApi.translateProgress,
    enabled: Boolean(isTranslating),
    // Stop as soon as this endpoint itself stops reporting a running job
    // instead of hard-polling for up to a status-refresh interval.
    refetchInterval: (query) => (query.state.data?.state === 'running' ? 2000 : false),
    retry: 1,
  })

  // The moment the fine-grained poll sees the job finish, refresh the
  // aggregate status so isTranslating flips and the poll stays disabled.
  const progressState = translateProgressQuery.data?.state
  useEffect(() => {
    if (progressState && progressState !== 'running') {
      void queryClient.invalidateQueries({ queryKey: ['tag-wiki', 'status'] })
    }
  }, [progressState, queryClient])

  const buildMutation = useMutation({
    mutationFn: () => tagWikiApi.build({ download_dump: downloadDump, reindex, force_reembed: forceReembed }),
    onSuccess: (data) => {
      queryClient.setQueryData(['tag-wiki', 'status'], data)
      setLocalError(null)
    },
    onError: (err) => {
      setLocalError(describeWikiError(err, '启动 Wiki 构建失败'))
    },
  })

  const translateMutation = useMutation({
    mutationFn: () =>
      tagWikiApi.translate({
        scope,
        min_post_count: scope === 'popular' ? minPostCount : undefined,
        max_pages: maxPages,
      }),
    onSuccess: (data) => {
      queryClient.setQueryData<TagWikiStatus>(['tag-wiki', 'status'], (old) => {
        if (!old) return old
        return { ...old, translate: data }
      })
      // Drop the cached progress of any previous run so the live poll takes over.
      queryClient.removeQueries({ queryKey: ['tag-wiki', 'translate-progress'] })
      setLocalError(null)
    },
    onError: (err) => {
      setLocalError(describeWikiError(err, '启动中文翻译失败'))
    },
  })

  const build = status?.build
  const translate = status?.translate
  const db = status?.database
  const idx = status?.index

  const isBuilding = build?.state === 'running'
  const buildError = build?.error || (build?.state === 'error' ? build?.message : null)
  const translateError = translate?.error || (translate?.state === 'error' ? translate?.message : null)

  // Prefer the fine-grained progress endpoint while it reports a running job.
  const progressData = translateProgressQuery.data
  const liveTranslate = progressData && progressData.state === 'running' ? progressData : translate

  const translatePercent =
    liveTranslate && liveTranslate.total > 0
      ? Math.min(100, Math.round((liveTranslate.done / liveTranslate.total) * 100))
      : 0

  return (
    <div className="tw-build-panel">
      <div className="tw-build-panel-header">
        <div className="tw-build-title-row">
          <Database size={16} aria-hidden="true" />
          <h2 className="tw-build-title">Wiki 数据库与翻译构建</h2>
          {isBuilding && (
            <span className="tw-running-tag">
              <LoaderCircle size={12} className="spin" />
              构建中
            </span>
          )}
          {isTranslating && (
            <span className="tw-running-tag">
              <LoaderCircle size={12} className="spin" />
              翻译中
            </span>
          )}
        </div>
        <button
          type="button"
          className="tw-collapse-btn"
          onClick={() => setCollapsed((v) => !v)}
          aria-expanded={!collapsed}
          title={collapsed ? '展开构建面板' : '收起构建面板'}
        >
          {collapsed ? <ChevronDown size={16} /> : <ChevronUp size={16} />}
        </button>
      </div>

      {!collapsed && (
        <div className="tw-build-panel-body">
          {/* Status Chips */}
          <div className="tw-status-chips">
            <div className="tw-chip-item">
              <span className="tw-chip-label">Wiki 页数</span>
              <strong>{db?.pages ? db.pages.toLocaleString('zh-CN') : 0}</strong>
            </div>
            <div className="tw-chip-item">
              <span className="tw-chip-label">章节数</span>
              <strong>{db?.chunks ? db.chunks.toLocaleString('zh-CN') : 0}</strong>
            </div>
            <div className="tw-chip-item">
              <span className="tw-chip-label">已向量化</span>
              <strong>
                {db?.embedded_chunks ? db.embedded_chunks.toLocaleString('zh-CN') : 0}
              </strong>
            </div>
            <div className="tw-chip-item">
              <span className="tw-chip-label">已翻译摘要</span>
              <strong>
                {db?.translated_pages ? db.translated_pages.toLocaleString('zh-CN') : 0}
              </strong>
            </div>
            <div className="tw-chip-item">
              <span className="tw-chip-label">Dump 日期</span>
              <strong>{db?.dump_date ?? '未同步'}</strong>
            </div>
            <div className="tw-chip-item">
              <span className="tw-chip-label">检索状态</span>
              <strong className={idx?.search_ready ? 'tw-text-success' : 'tw-text-warning'}>
                {idx?.search_ready ? '就绪' : '未就绪'}
              </strong>
            </div>
          </div>

          {/* Active progress notifications */}
          {isBuilding && (
            <div className="tw-active-progress">
              <div className="tw-progress-desc">
                <LoaderCircle size={14} className="spin" />
                <span>{PHASE_LABELS[build?.phase ?? 'idle'] || build?.message || '正在构建…'}</span>
              </div>
            </div>
          )}

          {isTranslating && (
            <div className="tw-active-progress">
              <div className="tw-progress-desc">
                <LoaderCircle size={14} className="spin" />
                <span>
                  {liveTranslate?.message || '正在批量翻译摘要…'}{' '}
                  <small>
                    ({liveTranslate?.done ?? 0} / {liveTranslate?.total ?? 0}
                    {liveTranslate?.failed ? `，失败 ${liveTranslate.failed}` : ''})
                  </small>
                </span>
              </div>
              <ProgressBar value={translatePercent} label="翻译进度" />
            </div>
          )}

          {/* Error messages */}
          {(buildError || translateError || localError) && (
            <Notice tone="danger">
              <AlertTriangle size={15} />
              <span>{localError || buildError || translateError}</span>
            </Notice>
          )}

          {/* Build options */}
          <div className="tw-build-flags">
            <label>
              <input
                type="checkbox"
                checked={downloadDump}
                disabled={isBuilding || isTranslating || buildMutation.isPending}
                onChange={(e) => setDownloadDump(e.target.checked)}
              />
              下载最新 Dump
            </label>
            <label>
              <input
                type="checkbox"
                checked={reindex}
                disabled={isBuilding || isTranslating || buildMutation.isPending}
                onChange={(e) => setReindex(e.target.checked)}
              />
              重建索引
            </label>
            <label>
              <input
                type="checkbox"
                checked={forceReembed}
                disabled={isBuilding || isTranslating || buildMutation.isPending}
                onChange={(e) => setForceReembed(e.target.checked)}
              />
              强制重新向量化
            </label>
          </div>

          {/* Controls */}
          <div className="tw-build-actions-row">
            <div className="tw-build-action-group">
              <Button
                variant="outline"
                size="sm"
                icon={buildMutation.isPending || isBuilding ? <LoaderCircle className="spin" size={14} /> : <Download size={14} />}
                disabled={isBuilding || isTranslating || buildMutation.isPending}
                onClick={() => buildMutation.mutate()}
              >
                {isBuilding ? '正在构建 Wiki…' : '下载/更新 Wiki 数据'}
              </Button>
            </div>

            <div className="tw-translate-controls">
              <label className="tw-inline-label">
                <span>翻译范围</span>
                <select
                  value={scope}
                  disabled={isBuilding || isTranslating || translateMutation.isPending}
                  onChange={(e) => setScope(e.target.value as TranslateRequest['scope'])}
                >
                  <option value="model_vocab">模型词表标签</option>
                  <option value="popular">高频标签</option>
                  <option value="all">全部已知标签</option>
                </select>
              </label>

              {scope === 'popular' && (
                <label className="tw-inline-label">
                  <span>最小帖子数</span>
                  <input
                    type="number"
                    min={0}
                    step={100}
                    value={minPostCount}
                    disabled={isBuilding || isTranslating || translateMutation.isPending}
                    onChange={(e) => setMinPostCount(clampInt(Number(e.target.value), 0, 1_000_000))}
                    style={{ width: '90px' }}
                  />
                </label>
              )}

              <label className="tw-inline-label">
                <span>单次页数上限</span>
                <input
                  type="number"
                  className="tw-max-pages-input"
                  min={1}
                  max={50000}
                  step={100}
                  value={maxPages}
                  disabled={isBuilding || isTranslating || translateMutation.isPending}
                  onChange={(e) => setMaxPages(clampInt(Number(e.target.value), 1, 50_000))}
                />
              </label>

              <Button
                variant="outline"
                size="sm"
                icon={translateMutation.isPending || isTranslating ? <LoaderCircle className="spin" size={14} /> : <Languages size={14} />}
                disabled={isBuilding || isTranslating || translateMutation.isPending}
                onClick={() => translateMutation.mutate()}
              >
                {isTranslating ? '正在翻译…' : '翻译中文摘要'}
              </Button>
            </div>

            <button
              type="button"
              className="icon-button icon-button-quiet"
              title="刷新状态"
              aria-label="刷新状态"
              onClick={() => statusQuery.refetch()}
              disabled={statusQuery.isFetching}
            >
              <RefreshCw size={14} className={statusQuery.isFetching ? 'spin' : ''} />
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
