import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download, ImagePlus, LoaderCircle, Pause, RefreshCw, RotateCcw, Send, Trash2, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { FileDropzone } from '../components/FileDropzone'
import { Button, ConfirmDialog, EmptyState, Field, IconButton, Notice, Panel, ProgressBar, StatusBadge } from '../components/ui'
import { api, ApiError } from '../lib/api'
import { useImageGenerationStore } from '../store/imageGeneration'
import type { ImageGenerationArtifact, ImageGenerationCapability, ImageGenerationJob } from '../types'

type ReferenceImage = { file: File; url: string }

const TERMINAL = new Set(['cancelled', 'succeeded', 'partial', 'failed'])

export function ImageGeneration() {
  const queryClient = useQueryClient()
  const providers = useQuery({ queryKey: ['providers'], queryFn: api.providers, staleTime: 60_000 })
  const imageProviders = useMemo(
    () => (providers.data?.items ?? []).filter((provider) => provider.enabled !== false && provider.image_enabled !== false && provider.kind !== 'claude'),
    [providers.data?.items],
  )
  const draft = useImageGenerationStore((state) => state.draft)
  const updateDraft = useImageGenerationStore((state) => state.updateDraft)
  const activeJobId = useImageGenerationStore((state) => state.activeJobId)
  const setActiveJobId = useImageGenerationStore((state) => state.setActiveJobId)
  const {
    providerId, model, operation, prompt, n, aspectRatio, imageSize, resolution, size, quality,
    background, outputFormat, outputCompression, moderation, inputFidelity,
    responseFormat, includeTextModality, systemInstruction, temperature, topP,
    topK, multiImageStrategy,
  } = draft
  const [references, setReferences] = useState<ReferenceImage[]>([])
  const [notice, setNotice] = useState<{ tone: 'info' | 'success' | 'warning' | 'danger'; text: string }>()
  const [historyQuery, setHistoryQuery] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<ImageGenerationJob>()
  const referencesRef = useRef<ReferenceImage[]>([])

  useEffect(() => {
    const selected = imageProviders.find((provider) => provider.id === providerId)
    if (selected) return
    const first = imageProviders[0]
    if (first) {
      updateDraft({ providerId: first.id, model: first.primary_model })
    }
  }, [imageProviders, providerId, updateDraft])

  useEffect(() => {
    referencesRef.current = references
  }, [references])
  useEffect(() => () => referencesRef.current.forEach((reference) => URL.revokeObjectURL(reference.url)), [])

  const capabilityQuery = useQuery({
    queryKey: ['image-capabilities', providerId, model],
    queryFn: () => api.imageCapabilities(providerId, model),
    enabled: Boolean(providerId && model.trim()),
    staleTime: 60_000,
  })
  const modelQuery = useQuery({
    queryKey: ['provider-models', providerId],
    queryFn: () => api.providerModels(providerId),
    enabled: Boolean(providerId),
    staleTime: 60_000,
    retry: false,
  })
  const capability = capabilityQuery.data && 'families' in capabilityQuery.data
    ? capabilityQuery.data.families[0]
    : capabilityQuery.data as ImageGenerationCapability | undefined
  const activeJobQuery = useQuery({
    queryKey: ['image-generation-job', activeJobId],
    queryFn: () => api.imageGenerationJob(activeJobId!),
    enabled: Boolean(activeJobId),
    refetchInterval: (query) => {
      const state = (query.state.data as ImageGenerationJob | undefined)?.state
      return state && TERMINAL.has(state) ? false : 1500
    },
  })
  const history = useQuery({
    queryKey: ['image-generation-history', historyQuery],
    queryFn: () => api.imageGenerationJobs({ limit: 80, q: historyQuery }),
    refetchInterval: 5000,
  })
  const activeJob = activeJobQuery.data

  useEffect(() => {
    if (!capability) return
    const current = useImageGenerationStore.getState().draft
    updateDraft({
      n: Math.min(current.n, capability.max_outputs),
      operation: capability.operations.includes(current.operation) ? current.operation : 'generate',
      aspectRatio: capability.defaults.aspect_ratio ? String(capability.defaults.aspect_ratio) : current.aspectRatio,
      imageSize: capability.defaults.image_size ? String(capability.defaults.image_size) : current.imageSize,
      resolution: capability.defaults.resolution ? String(capability.defaults.resolution) : current.resolution,
      size: capability.defaults.size ? String(capability.defaults.size) : current.size,
      quality: capability.defaults.quality ? String(capability.defaults.quality) : current.quality,
      multiImageStrategy: hasParameter(capability, 'multi_image_strategy') ? current.multiImageStrategy : 'parallel',
    })
  }, [capability, updateDraft])

  useEffect(() => {
    if (outputFormat === 'jpeg' && background === 'transparent') updateDraft({ background: 'opaque' })
  }, [background, outputFormat, updateDraft])

  useEffect(() => {
    if (activeJobQuery.error instanceof ApiError && activeJobQuery.error.status === 404) {
      setActiveJobId(undefined)
      setNotice({ tone: 'warning', text: '上次查看的图像任务已不存在。' })
    }
  }, [activeJobQuery.error, setActiveJobId])

  const createMutation = useMutation({
    mutationFn: () => api.createImageGenerationJob(buildConfig(capability, {
      providerId, model, operation, prompt, n, aspectRatio, imageSize, resolution, size, quality, background,
      outputFormat, outputCompression, moderation, inputFidelity, responseFormat, includeTextModality,
      systemInstruction, temperature, topP, topK, multiImageStrategy,
    }), references.map((reference) => reference.file)),
    onSuccess: (job) => {
      setActiveJobId(job.id)
      setNotice({ tone: 'success', text: '图像任务已排队。' })
      void queryClient.invalidateQueries({ queryKey: ['image-generation-history'] })
    },
    onError: (error) => setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '创建图像任务失败' }),
  })
  const cancelMutation = useMutation({
    mutationFn: (id: string) => api.cancelImageGenerationJob(id),
    onSuccess: (job) => { setActiveJobId(job.id); void queryClient.invalidateQueries({ queryKey: ['image-generation-history'] }) },
    onError: (error) => setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '取消任务失败' }),
  })
  const retryMutation = useMutation({
    mutationFn: (id: string) => api.retryImageGenerationJob(id),
    onSuccess: (job) => { setActiveJobId(job.id); void queryClient.invalidateQueries({ queryKey: ['image-generation-history'] }) },
    onError: (error) => setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '重试任务失败' }),
  })
  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteImageGenerationJob(id),
    onSuccess: () => {
      setDeleteTarget(undefined)
      setActiveJobId(undefined)
      void queryClient.invalidateQueries({ queryKey: ['image-generation-history'] })
    },
    onError: (error) => setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '删除历史失败' }),
  })

  const addReferences = (files: File[]) => {
    const max = capability?.max_references ?? 4
    const allowed = files.slice(0, Math.max(0, max - references.length))
    if (allowed.length < files.length) setNotice({ tone: 'warning', text: `当前模型最多使用 ${max} 张参考图。` })
    setReferences((current) => [...current, ...allowed.map((file) => ({ file, url: URL.createObjectURL(file) }))])
    if (allowed.length && capability?.family !== 'google_gemini' && capability?.operations.includes('edit')) {
      updateDraft({ operation: 'edit' })
    }
  }
  const removeReference = (index: number) => {
    const removed = references[index]
    if (removed) URL.revokeObjectURL(removed.url)
    setReferences((current) => current.filter((_item, itemIndex) => itemIndex !== index))
  }
  const chooseProvider = (nextId: string) => {
    const next = imageProviders.find((provider) => provider.id === nextId)
    updateDraft({ providerId: nextId, model: next?.primary_model ?? '' })
    setReferences((current) => { current.forEach((reference) => URL.revokeObjectURL(reference.url)); return [] })
  }
  const canSubmit = Boolean(
    providerId
    && model.trim()
    && prompt.trim()
    && !createMutation.isPending
    && (operation !== 'edit' || references.length > 0)
    && (!references.length || operation === 'edit' || capability?.family === 'google_gemini'),
  )
  const progress = activeJob ? Math.round((activeJob.completed_count / Math.max(1, activeJob.requested_count)) * 100) : 0

  return <div className="page page-image-generation">
    <div className="page-heading">
      <div className="page-heading-copy"><p className="eyebrow">MULTI-VENDOR IMAGE DESK</p><h1>图像生成</h1><p className="page-subtitle">Grok、Nano Banana 和 GPT Image 的统一任务与历史工作区。</p></div>
      <Button variant="quiet" icon={<RefreshCw size={16} />} aria-label="刷新历史" title="刷新历史" onClick={() => void history.refetch()} />
    </div>
    {notice && <Notice tone={notice.tone}>{notice.text}<IconButton label="关闭提示" onClick={() => setNotice(undefined)}><X size={14} /></IconButton></Notice>}
    {deleteTarget && <ConfirmDialog
      title="删除这条图像历史？"
      detail="任务记录、参考图副本和生成产物都会永久删除，此操作无法撤销。"
      confirmLabel="确认删除"
      busy={deleteMutation.isPending}
      onClose={() => setDeleteTarget(undefined)}
      onConfirm={() => deleteMutation.mutate(deleteTarget.id)}
    />}
    {!imageProviders.length && !providers.isLoading && <Notice tone="warning">尚未启用可用的图像 Provider。请先在“在线模型”中添加 OpenAI、xAI、Gemini 或兼容线路。</Notice>}

    <div className="image-generation-grid">
      <Panel title="生成设置" eyebrow="01 / COMPOSE" className="image-generation-settings">
        <div className="image-form-body">
          <div className="form-grid two-columns">
            <Field label="Provider"><select value={providerId} onChange={(event) => chooseProvider(event.target.value)}><option value="">选择 Provider</option>{imageProviders.map((provider) => <option key={provider.id} value={provider.id}>{provider.name}</option>)}</select></Field>
            <Field label="模型"><input list="image-generation-models" value={model} onChange={(event) => updateDraft({ model: event.target.value })} placeholder="模型 ID" /><datalist id="image-generation-models">{modelQuery.data?.items.map((item) => <option key={item.id} value={item.id}>{item.name ?? item.id}</option>)}</datalist></Field>
          </div>
          {capability && <div className="image-capability-line"><span>{capability.label}</span><code>{capability.known ? capability.family : 'unknown / safe mode'}{capability.api_style ? ` · ${capability.api_style}` : ''}</code><small>最多 {capability.max_outputs} 张 · 参考图 {capability.max_references} 张</small></div>}
          <Field label="操作"><select value={operation} onChange={(event) => updateDraft({ operation: event.target.value as 'generate' | 'edit' })}><option value="generate">文生图</option><option value="edit" disabled={!capability?.operations.includes('edit')}>图像编辑</option></select></Field>
          <Field label="提示词"><textarea value={prompt} onChange={(event) => updateDraft({ prompt: event.target.value })} rows={7} placeholder="描述你要生成的画面" /></Field>
          <div className="form-grid three-columns">
            <Field label="数量"><input type="number" min={1} max={capability?.max_outputs ?? 8} value={n} onChange={(event) => updateDraft({ n: Math.max(1, Math.min(capability?.max_outputs ?? 8, Number(event.target.value) || 1)) })} /></Field>
            {hasParameter(capability, 'aspect_ratio') && <Field label="比例"><select value={aspectRatio} onChange={(event) => updateDraft({ aspectRatio: event.target.value })}>{options(capability, 'aspect_ratio', [aspectRatio]).map((value) => <option key={value}>{value}</option>)}</select></Field>}
            {hasParameter(capability, 'resolution') && <Field label="分辨率"><select value={resolution} onChange={(event) => updateDraft({ resolution: event.target.value })}>{options(capability, 'resolution', ['1k', '2k']).map((value) => <option key={value}>{value.toUpperCase()}</option>)}</select></Field>}
            {hasParameter(capability, 'image_size') && <Field label="图像尺寸"><select value={imageSize} onChange={(event) => updateDraft({ imageSize: event.target.value })}>{options(capability, 'image_size', ['1K']).map((value) => <option key={value}>{value}</option>)}</select></Field>}
            {hasParameter(capability, 'size') && <Field label="画布尺寸">{supportsCustomSize(capability)
              ? <input list="image-size-presets" value={size} onChange={(event) => updateDraft({ size: event.target.value })} placeholder="预设或自定义 宽x高" />
              : <select value={size} onChange={(event) => updateDraft({ size: event.target.value })}>{options(capability, 'size', ['auto']).map((value) => <option key={value}>{value}</option>)}</select>}
              <datalist id="image-size-presets">{options(capability, 'size', ['auto']).filter((value) => value !== 'custom').map((value) => <option key={value} value={value}>{value}</option>)}</datalist>
            </Field>}
          </div>
          <details className="image-advanced-block">
            <summary className="image-advanced-heading"><strong>高级参数</strong><small>{capability?.known ? '按模型能力显示' : '兼容模式仅发送基础字段'}</small></summary>
            <div className="form-grid two-columns">
              {hasParameter(capability, 'quality') && <Field label="质量"><select value={quality} onChange={(event) => updateDraft({ quality: event.target.value })}>{options(capability, 'quality', ['auto']).map((value) => <option key={value}>{value}</option>)}</select></Field>}
              {hasParameter(capability, 'background') && <Field label="背景"><select value={background} onChange={(event) => updateDraft({ background: event.target.value })}>{options(capability, 'background', ['auto']).map((value) => <option key={value}>{value}</option>)}</select></Field>}
              {hasParameter(capability, 'output_format') && <Field label="输出格式"><select value={outputFormat} onChange={(event) => updateDraft({ outputFormat: event.target.value })}>{options(capability, 'output_format', ['png']).map((value) => <option key={value}>{value}</option>)}</select></Field>}
              {hasParameter(capability, 'output_compression') && ['jpeg', 'webp'].includes(outputFormat) && <Field label="压缩"><input type="number" min={0} max={100} value={outputCompression} onChange={(event) => updateDraft({ outputCompression: Math.max(0, Math.min(100, Number(event.target.value) || 0)) })} /></Field>}
              {hasParameter(capability, 'moderation') && <Field label="审核级别"><select value={moderation} onChange={(event) => updateDraft({ moderation: event.target.value })}>{options(capability, 'moderation', ['auto']).map((value) => <option key={value}>{value}</option>)}</select></Field>}
              {operation === 'edit' && hasParameter(capability, 'input_fidelity') && <Field label="输入保真度"><select value={inputFidelity} onChange={(event) => updateDraft({ inputFidelity: event.target.value })}>{options(capability, 'input_fidelity', ['low']).map((value) => <option key={value}>{value}</option>)}</select></Field>}
              {hasParameter(capability, 'response_format') && <Field label="响应格式"><select value={responseFormat} onChange={(event) => updateDraft({ responseFormat: event.target.value as 'b64_json' | 'url' })}>{options(capability, 'response_format', ['b64_json']).map((value) => <option key={value}>{value}</option>)}</select></Field>}
              {hasParameter(capability, 'temperature') && <Field label="Temperature"><input type="number" min={0} max={2} step={0.05} value={temperature} onChange={(event) => updateDraft({ temperature: Number(event.target.value) || 0 })} /></Field>}
              {hasParameter(capability, 'top_p') && <Field label="Top P"><input type="number" min={0} max={1} step={0.05} value={topP} onChange={(event) => updateDraft({ topP: Number(event.target.value) || 0 })} /></Field>}
              {hasParameter(capability, 'top_k') && <Field label="Top K"><input type="number" min={0} max={1000} value={topK} onChange={(event) => updateDraft({ topK: Number(event.target.value) || 0 })} /></Field>}
              {hasParameter(capability, 'multi_image_strategy') && <Field label="多图策略"><select value={multiImageStrategy} onChange={(event) => updateDraft({ multiImageStrategy: event.target.value as 'parallel' | 'candidate_count' })}><option value="parallel">并行请求</option><option value="candidate_count">Candidate count</option></select></Field>}
              {hasParameter(capability, 'include_text_modality') && <Field label="同时返回文本"><label className="toggle standalone"><input type="checkbox" checked={includeTextModality} onChange={(event) => updateDraft({ includeTextModality: event.target.checked })} /><span />TEXT + IMAGE</label></Field>}
            </div>
            {hasParameter(capability, 'system_instruction') && <Field label="System instruction"><textarea value={systemInstruction} onChange={(event) => updateDraft({ systemInstruction: event.target.value })} rows={3} /></Field>}
          </details>
          <Button size="lg" icon={createMutation.isPending ? <LoaderCircle className="spin" size={17} /> : <Send size={17} />} disabled={!canSubmit} onClick={() => createMutation.mutate()}>提交生成任务</Button>
        </div>
      </Panel>

      <Panel title={`参考图 ${references.length}/${capability?.max_references ?? 4}`} eyebrow="02 / REFERENCES" className="image-generation-source">
        {references.length ? <div className="image-reference-grid">{references.map((reference, index) => <article className="image-reference-card" key={`${reference.file.name}-${reference.file.lastModified}-${index}`}><img src={reference.url} alt={reference.file.name} /><div><strong>{index + 1}</strong><IconButton label={`删除参考图 ${index + 1}`} onClick={() => removeReference(index)}><X size={14} /></IconButton></div><small title={reference.file.name}>{reference.file.name}</small></article>)}</div> : <EmptyState icon={<ImagePlus size={22} />} title="尚未添加参考图" detail="图像编辑需要至少一张参考图。" />}
        <FileDropzone onFiles={addReferences} disabled={references.length >= (capability?.max_references ?? 4)} maxFiles={Math.max(0, (capability?.max_references ?? 4) - references.length)} multiple label="添加参考图" detail="拖入图片或选择文件" />
      </Panel>

      <Panel title="任务结果" eyebrow="03 / RESULTS" className="image-generation-results" actions={activeJob ? <StatusBadge state={activeJob.state} /> : undefined}>
        {activeJob && <div className="image-job-progress"><div><strong>{activeJob.model}</strong><small>{activeJob.completed_count}/{activeJob.requested_count} 张 · {activeJob.phase}</small></div><ProgressBar value={progress} />{!TERMINAL.has(activeJob.state) && <Button size="sm" variant="danger" icon={<Pause size={14} />} onClick={() => cancelMutation.mutate(activeJob.id)} disabled={cancelMutation.isPending}>取消</Button>}{['failed', 'partial', 'cancelled'].includes(activeJob.state) && <Button size="sm" variant="secondary" icon={<RotateCcw size={14} />} onClick={() => retryMutation.mutate(activeJob.id)} disabled={retryMutation.isPending}>重试</Button>}</div>}
        {activeJob?.artifacts.length ? <div className="image-result-grid">{activeJob.artifacts.map((artifact) => <article className="image-result-card" key={artifact.id}><AuthenticatedArtifact artifact={artifact} alt={`${activeJob.model} ${artifact.ordinal + 1}`} /><footer><span>{artifact.width && artifact.height ? `${artifact.width} × ${artifact.height}` : artifact.mime_type}</span><IconButton label="下载图像" onClick={() => void downloadArtifact(artifact, activeJob.model).catch((error) => setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '下载图像失败' }))}><Download size={15} /></IconButton></footer></article>)}</div> : activeJob && TERMINAL.has(activeJob.state) ? <EmptyState icon={<ImagePlus size={24} />} title={activeJob.state === 'cancelled' ? '任务已取消' : '任务未生成可用图像'} detail={activeJob.error_code ?? undefined} /> : activeJob ? <div className="loading-block"><LoaderCircle className="spin" size={20} />等待上游返回图像…</div> : <EmptyState icon={<ImagePlus size={24} />} title="提交任务后在这里查看结果" />}
      </Panel>

      <Panel title="历史任务" eyebrow="04 / HISTORY" className="image-generation-history" actions={<input className="image-history-search" value={historyQuery} onChange={(event) => setHistoryQuery(event.target.value)} placeholder="搜索模型或提示词" aria-label="搜索历史" />}>
        {history.isLoading ? <div className="loading-block"><LoaderCircle className="spin" />读取历史…</div> : history.data?.items.length ? <div className="image-history-list">{history.data.items.map((job) => <button type="button" className={`image-history-row ${job.id === activeJobId ? 'is-active' : ''}`} key={job.id} onClick={() => setActiveJobId(job.id)}><span className="image-history-thumb">{job.artifacts[0] ? <AuthenticatedArtifact artifact={job.artifacts[0]} alt="" /> : <ImagePlus size={15} />}</span><span><strong>{job.model}</strong><small>{job.config.prompt ? String(job.config.prompt).slice(0, 80) : '无提示词'} · {job.completed_count}/{job.requested_count}</small></span><StatusBadge state={job.state} /></button>)}</div> : <EmptyState icon={<RefreshCw size={22} />} title="暂无历史任务" />}
        {activeJob && TERMINAL.has(activeJob.state) && <div className="image-history-actions"><Button size="sm" variant="danger" icon={<Trash2 size={14} />} onClick={() => setDeleteTarget(activeJob)} disabled={deleteMutation.isPending}>删除当前历史</Button></div>}
      </Panel>
    </div>
  </div>
}

function AuthenticatedArtifact({ artifact, alt }: { artifact: ImageGenerationArtifact; alt: string }) {
  const image = useQuery({
    queryKey: ['image-generation-artifact', artifact.id, artifact.sha256],
    queryFn: () => api.imageGenerationArtifact(artifact.id),
    staleTime: Infinity,
  })
  const [url, setUrl] = useState('')
  useEffect(() => {
    if (!image.data) return
    const next = URL.createObjectURL(image.data)
    setUrl(next)
    return () => URL.revokeObjectURL(next)
  }, [image.data])
  if (image.isError) return <span className="image-artifact-loading" title="图像加载失败"><X size={18} /></span>
  if (!url) return <span className="image-artifact-loading"><LoaderCircle className="spin" size={18} /></span>
  return <img src={url} alt={alt} />
}

async function downloadArtifact(artifact: ImageGenerationArtifact, model: string): Promise<void> {
  const blob = await api.imageGenerationArtifact(artifact.id)
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  const extension = artifact.mime_type.includes('jpeg') ? 'jpg' : artifact.mime_type.split('/')[1] || 'png'
  link.href = url
  link.download = `${model.replace(/[^a-zA-Z0-9._-]+/g, '-')}-${artifact.ordinal + 1}.${extension}`
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

function hasParameter(capability: ImageGenerationCapability | undefined, name: string): boolean {
  return Boolean(capability?.parameters.includes(name))
}

function options(capability: ImageGenerationCapability | undefined, name: string, fallback: string[]): string[] {
  const values = capability?.enums[name]
  return values?.length ? values : fallback
}

function supportsCustomSize(capability: ImageGenerationCapability | undefined): boolean {
  return Boolean(capability?.enums.size?.includes('custom'))
}

function buildConfig(capability: ImageGenerationCapability | undefined, values: {
  providerId: string; model: string; operation: 'generate' | 'edit'; prompt: string; n: number; aspectRatio: string; imageSize: string; resolution: string; size: string; quality: string; background: string; outputFormat: string; outputCompression: number; moderation: string; inputFidelity: string; responseFormat: string; includeTextModality: boolean; systemInstruction: string; temperature: number; topP: number; topK: number; multiImageStrategy: string
}): Record<string, unknown> {
  const config: Record<string, unknown> = { provider_id: values.providerId, model: values.model.trim(), operation: values.operation, prompt: values.prompt.trim(), n: values.n }
  const add = (name: string, value: unknown) => { if (hasParameter(capability, name)) config[name] = value }
  add('aspect_ratio', values.aspectRatio); add('image_size', values.imageSize); add('resolution', values.resolution); add('size', values.size.trim() || undefined); add('quality', values.quality); add('background', values.background)
  add('output_format', values.outputFormat); if (['jpeg', 'webp'].includes(values.outputFormat)) add('output_compression', values.outputCompression); add('moderation', values.moderation); if (values.operation === 'edit') add('input_fidelity', values.inputFidelity); add('response_format', values.responseFormat)
  add('include_text_modality', values.includeTextModality); add('system_instruction', values.systemInstruction.trim() || undefined); add('temperature', values.temperature); add('top_p', values.topP); add('top_k', values.topK)
  add('multi_image_strategy', values.multiImageStrategy)
  return config
}
