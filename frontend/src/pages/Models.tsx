import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Box, CheckCircle2, Cpu, Database, Download, Gauge, GitBranch, HardDrive, Layers3, Link, LoaderCircle, MemoryStick, Power, RefreshCw, RotateCcw, Search, Settings2, ShieldAlert, Sparkles, Unplug, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Button, DialogLayer, EmptyState, Field, IconButton, Notice, Panel, StatusBadge } from '../components/ui'
import { api, ApiError } from '../lib/api'
import type { ClassifierProfile, ModelDownload, ModelProfile } from '../types'

export function Models() {
  const queryClient = useQueryClient()
  const modelQuery = useQuery({ queryKey: ['models'], queryFn: api.models, refetchInterval: 30_000 })
  const classifierQuery = useQuery({ queryKey: ['classifiers'], queryFn: api.classifiers, refetchInterval: 30_000, retry: false })
  const [query, setQuery] = useState('')
  const [backend, setBackend] = useState('all')
  const [selected, setSelected] = useState<ModelProfile>()
  const [modelThresholds, setModelThresholds] = useState<Record<string, number>>({ default: 0.35 })
  const [device, setDevice] = useState('cuda')
  const [trusted, setTrusted] = useState(false)
  const [adapterType, setAdapterType] = useState('none')
  const [adapterPath, setAdapterPath] = useState('')
  const [adapterScale, setAdapterScale] = useState(1)
  const [notice, setNotice] = useState<{ tone: 'success' | 'danger' | 'warning' | 'info'; text: string } | null>(null)
  const [downloadOpen, setDownloadOpen] = useState(false)
  const [downloadUrl, setDownloadUrl] = useState('')
  const [downloadRevision, setDownloadRevision] = useState('')
  const [downloadId, setDownloadId] = useState<string>()
  const [handledDownloadId, setHandledDownloadId] = useState<string>()

  const downloadMutation = useMutation({
    mutationFn: () => api.startModelDownload({ url: downloadUrl.trim(), revision: downloadRevision.trim() || undefined }),
    onSuccess: (record) => { setDownloadId(record.id); setHandledDownloadId(undefined) },
    onError: (error) => setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '无法开始模型下载' }),
  })
  const downloadQuery = useQuery({
    queryKey: ['model-download', downloadId],
    queryFn: () => api.modelDownload(downloadId!),
    enabled: Boolean(downloadId),
    refetchInterval: (query) => {
      const record = query.state.data as ModelDownload | undefined
      return record && ['succeeded', 'failed'].includes(record.status) ? false : 1_000
    },
  })
  const downloadRecord = downloadQuery.data ?? downloadMutation.data

  useEffect(() => {
    if (!downloadRecord || handledDownloadId === downloadRecord.id || !['succeeded', 'failed'].includes(downloadRecord.status)) return
    setHandledDownloadId(downloadRecord.id)
    if (downloadRecord.status === 'succeeded') {
      setNotice({ tone: downloadRecord.load_errors.length ? 'warning' : 'success', text: downloadRecord.load_errors.length ? `${downloadRecord.repo_id} 已下载，但自动加载有失败项` : `${downloadRecord.repo_id} 下载、注册并自动加载完成` })
      void queryClient.invalidateQueries({ queryKey: ['models'] })
    } else {
      setNotice({ tone: 'danger', text: downloadRecord.error || '模型下载失败' })
    }
  }, [downloadRecord, handledDownloadId, queryClient])

  const loadMutation = useMutation({
    mutationFn: ({ model, load }: { model: ModelProfile; load: boolean }) => load
      ? api.loadModel(model.id, { device, trusted_pickle: trusted })
      : api.unloadModel(model.id),
    onSuccess: (model, variables) => {
      setNotice({ tone: 'success', text: `${model.name} 已${variables.load ? '加载' : '卸载'}` })
      void queryClient.invalidateQueries({ queryKey: ['models'] })
    },
    onError: (error) => setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '模型操作失败' }),
  })

  const saveMutation = useMutation({
    mutationFn: () => {
      const preset = selected!.preset_thresholds ?? { default: selected!.threshold ?? 0.35 }
      const reset = thresholdMapsEqual(modelThresholds, preset)
      return api.updateModel(selected!.id, { thresholds: reset ? undefined : modelThresholds, reset_thresholds: reset, trusted_pickle: trusted })
    },
    onSuccess: () => { setNotice({ tone: 'success', text: '模型 Profile 已保存' }); setSelected(undefined); void queryClient.invalidateQueries({ queryKey: ['models'] }) },
    onError: (error) => setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '模型设置保存失败' }),
  })

  const applyLoadMutation = useMutation({
    mutationFn: async () => {
      if (!selected) throw new Error('未选择模型')
      if (selected.loaded) await api.unloadModel(selected.id)
      return api.loadModel(selected.id, {
        device,
        trusted_pickle: trusted,
        adapter_type: adapterType,
        adapter_path: adapterType === 'none' ? undefined : adapterPath.trim() || undefined,
        adapter_scale: adapterScale,
      })
    },
    onSuccess: (model) => { setNotice({ tone: 'success', text: `${model.name} 已按当前配置加载` }); setSelected(undefined); void queryClient.invalidateQueries({ queryKey: ['models'] }) },
    onError: (error) => setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '模型加载失败' }),
  })

  const classifierMutation = useMutation({
    mutationFn: ({ classifier, load }: { classifier: ClassifierProfile; load: boolean }) => load
      ? api.loadClassifier(classifier.id)
      : api.unloadClassifier(classifier.id),
    onSuccess: (classifier, variables) => {
      setNotice(classifier.error
        ? { tone: 'warning', text: classifier.error.message }
        : { tone: 'success', text: `美学评分已${variables.load ? '加载' : '卸载'}` })
      void queryClient.invalidateQueries({ queryKey: ['classifiers'] })
    },
    onError: (error) => setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '分类器操作失败' }),
  })

  const models = useMemo(() => (modelQuery.data?.items ?? []).filter((model) => {
    const matchesQuery = !query || `${model.name} ${model.architecture ?? ''}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())
    return matchesQuery && (backend === 'all' || model.backend === backend)
  }), [backend, modelQuery.data?.items, query])
  const loaded = models.filter((model) => model.loaded)
  const memory = loaded.reduce((sum, model) => sum + (model.memory_mb ?? 0), 0)

  const openSettings = (model: ModelProfile) => {
    setSelected(model)
    setModelThresholds({ ...(model.thresholds ?? { default: model.threshold ?? 0.35 }) })
    setDevice(model.device ?? 'cuda')
    setTrusted(model.trusted_pickle ?? false)
    setAdapterType('none')
    setAdapterPath('')
    setAdapterScale(1)
  }

  return <div className="page page-models">
    <div className="page-heading"><div><p className="eyebrow">LOCAL RUNTIME</p><h1>本地模型</h1><p className="page-subtitle">管理本地权重、推理后端、Adapter 和显存驻留状态。</p></div><div className="model-heading-actions"><div className="heading-stats"><span><strong>{loaded.length}</strong> 已加载</span><span className="heading-divider" /><span><strong>{formatMemory(memory)}</strong> 显存</span></div><Button aria-label="下载 Hugging Face 模型" icon={<Download size={16} />} onClick={() => setDownloadOpen(true)}>下载模型</Button></div></div>
    {notice && <Notice tone={notice.tone}>{notice.text}<IconButton label="关闭" onClick={() => setNotice(null)}><X size={14} /></IconButton></Notice>}
    <div className="runtime-strip">
      <div><span className="runtime-icon runtime-cuda"><Cpu size={19} /></span><div><small>计算设备</small><strong>{loaded[0]?.device?.toUpperCase() ?? '未分配'}</strong></div></div>
      <div><span className="runtime-icon runtime-memory"><MemoryStick size={19} /></span><div><small>模型显存</small><strong>{formatMemory(memory)}</strong></div></div>
      <div><span className="runtime-icon runtime-queue"><Layers3 size={19} /></span><div><small>设备策略</small><strong>GPU 串行</strong></div></div>
      <div><span className="runtime-icon runtime-safe"><ShieldAlert size={19} /></span><div><small>权重策略</small><strong>安全加载</strong></div></div>
    </div>
    <Panel title="美学评分模型" eyebrow="LSE14 SCORER" actions={<IconButton label="刷新美学评分" onClick={() => classifierQuery.refetch()}><RefreshCw size={15} /></IconButton>}>
      {classifierQuery.isLoading ? <div className="loading-block classifier-loading"><LoaderCircle className="spin" size={18} />读取分类器状态…</div> : classifierQuery.isError ? <EmptyState icon={<AlertTriangle size={21} />} title="无法读取分类器状态" action={<Button variant="secondary" size="sm" onClick={() => classifierQuery.refetch()}>重试</Button>} /> : <div className="classifier-runtime-grid">
        {(classifierQuery.data?.items ?? []).map((classifier) => <ClassifierRuntime key={classifier.id} classifier={classifier} pending={classifierMutation.isPending && classifierMutation.variables?.classifier.id === classifier.id} onToggle={() => classifierMutation.mutate({ classifier, load: !classifier.loaded })} />)}
      </div>}
    </Panel>
    <Panel title="模型注册表" eyebrow={`${models.length} MODELS`} actions={<div className="table-filters"><label className="search-box"><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="筛选模型" aria-label="筛选模型" /></label><select value={backend} onChange={(event) => setBackend(event.target.value)} aria-label="后端筛选"><option value="all">全部后端</option><option value="onnx">ONNX</option><option value="pytorch">PyTorch</option><option value="safetensors">safetensors</option></select><IconButton label="刷新模型" onClick={() => modelQuery.refetch()}><RefreshCw size={15} /></IconButton></div>}>
      {modelQuery.isLoading ? <div className="loading-block"><LoaderCircle className="spin" />读取模型注册表…</div> : !models.length ? <EmptyState icon={<Box size={23} />} title="没有匹配的本地模型" /> : <div className="model-table">
        <div className="model-table-head"><span>模型</span><span>推理配置</span><span>扩展</span><span>状态</span><span aria-label="操作" /></div>
        {models.map((model) => <div className="model-row" key={model.id}>
          <div className="model-identity"><span className={`model-logo backend-${model.backend}`}><ModelIcon backend={model.backend} /></span><div><strong>{model.name}</strong><small>{model.architecture ?? '架构未标注'} · ID {model.id.slice(0, 8)}</small></div></div>
          <div className="model-config"><span>{model.backend.toUpperCase()}</span><span>{formatInput(model.input_size)}</span><span>{model.threshold_source === 'custom' ? '自定义阈值' : '模型预设'} · {Object.keys(model.thresholds ?? {}).filter((key) => key !== 'default').length} 类</span></div>
          <div className="model-extensions"><span><Layers3 size={13} /> {model.adapters?.length ?? 0} Adapter</span><span><Gauge size={13} /> {model.classifiers?.length ?? 0} 分类器</span></div>
          <div className="model-state"><StatusBadge state={model.loaded ? 'succeeded' : 'interrupted'} /><small>{model.loaded ? `${model.device?.toUpperCase() ?? ''} · ${formatMemory(model.memory_mb ?? 0)}` : '未加载'}</small></div>
          <div className="model-actions"><IconButton label="模型设置" onClick={() => openSettings(model)}><Settings2 size={15} /></IconButton><Button size="sm" variant={model.loaded ? 'danger' : 'secondary'} icon={loadMutation.isPending && loadMutation.variables?.model.id === model.id ? <LoaderCircle size={14} className="spin" /> : model.loaded ? <Unplug size={14} /> : <Power size={14} />} disabled={loadMutation.isPending} onClick={() => loadMutation.mutate({ model, load: !model.loaded })}>{model.loaded ? '卸载' : '加载'}</Button></div>
        </div>)}
      </div>}
    </Panel>

    {downloadOpen && <DialogLayer onClose={() => setDownloadOpen(false)}><aside className="drawer model-download-drawer" role="dialog" aria-modal="true" aria-labelledby="model-download-title">
      <header className="drawer-header"><div><p className="eyebrow">HUGGING FACE</p><h2 id="model-download-title">下载模型</h2></div><IconButton label="关闭" onClick={() => setDownloadOpen(false)}><X size={18} /></IconButton></header>
      <form className="drawer-body model-download-form" onSubmit={(event) => { event.preventDefault(); downloadMutation.mutate() }}>
        <Field label="Hugging Face 仓库地址"><div className="input-with-icon"><Link size={15} /><input aria-label="Hugging Face 仓库地址" type="url" required value={downloadUrl} onChange={(event) => setDownloadUrl(event.target.value)} placeholder="https://huggingface.co/owner/model" spellCheck={false} /></div></Field>
        <Field label="Revision（可选）"><div className="input-with-icon"><GitBranch size={15} /><input aria-label="Revision（可选）" value={downloadRevision} onChange={(event) => setDownloadRevision(event.target.value)} placeholder="main" spellCheck={false} /></div></Field>
        {downloadRecord && <div className={`model-download-status download-${downloadRecord.status}`} aria-live="polite">
          <span className="model-download-status-icon">{downloadRecord.status === 'succeeded' ? <CheckCircle2 size={19} /> : downloadRecord.status === 'failed' ? <AlertTriangle size={19} /> : <LoaderCircle className="spin" size={19} />}</span>
          <div><strong>{downloadRecord.repo_id}</strong><span>{downloadStatusLabel(downloadRecord)}</span>{downloadRecord.error && <small>{downloadRecord.error}</small>}</div>
        </div>}
        <div className="drawer-actions"><Button type="button" variant="secondary" onClick={() => setDownloadOpen(false)}>关闭</Button><Button type="submit" icon={downloadMutation.isPending || downloadRecord?.status === 'running' || downloadRecord?.status === 'queued' ? <LoaderCircle className="spin" size={15} /> : <Download size={15} />} disabled={!downloadUrl.trim() || downloadMutation.isPending || downloadRecord?.status === 'running' || downloadRecord?.status === 'queued'}>{downloadRecord?.status === 'failed' ? '重新下载' : '开始下载'}</Button></div>
      </form>
    </aside></DialogLayer>}

    {selected && <DialogLayer onClose={() => setSelected(undefined)}><aside className="drawer model-drawer" role="dialog" aria-modal="true" aria-labelledby="model-settings-title">
      <header className="drawer-header"><div><p className="eyebrow">MODEL PROFILE</p><h2 id="model-settings-title">{selected.name}</h2></div><IconButton label="关闭" onClick={() => setSelected(undefined)}><X size={18} /></IconButton></header>
      <div className="drawer-body">
        <div className="model-summary"><span className={`model-logo backend-${selected.backend}`}><ModelIcon backend={selected.backend} /></span><div><strong>{selected.architecture ?? '未标注架构'}</strong><span>{selected.backend.toUpperCase()} · {formatInput(selected.input_size)}</span></div><StatusBadge state={selected.loaded ? 'succeeded' : 'interrupted'} /></div>
        <div className="threshold-editor">
          <div className="threshold-editor-heading"><div><span>分类阈值</span><small>{thresholdMapsEqual(modelThresholds, selected.preset_thresholds ?? {}) ? '使用模型预设' : '自定义配置'}</small></div><Button size="sm" variant="quiet" icon={<RotateCcw size={14} />} onClick={() => setModelThresholds({ ...(selected.preset_thresholds ?? { default: selected.threshold ?? 0.35 }) })}>恢复模型预设</Button></div>
          <div className="threshold-list">{thresholdKeys(modelThresholds).map((key) => { const value = modelThresholds[key] ?? 0; return <Field key={key} label={`${thresholdName(key)} ${value.toFixed(2)}`}><input aria-label={`${thresholdName(key)}阈值`} type="range" min="0" max="1" step="0.01" value={value} onChange={(event) => setModelThresholds((current) => ({ ...current, [key]: Number(event.target.value) }))} /></Field> })}</div>
        </div>
        <Field label="加载设备"><select value={device} onChange={(event) => setDevice(event.target.value)}><option value="cuda">CUDA GPU</option><option value="cpu">CPU</option></select></Field>
        {['pytorch', 'pt', 'bin'].includes(selected.backend) && <div className="unsafe-weight-setting"><Notice tone="warning"><AlertTriangle size={15} />传统 PyTorch 权重可能包含可执行 pickle 数据。</Notice><label className="toggle standalone"><input type="checkbox" checked={trusted} onChange={(event) => setTrusted(event.target.checked)} /><span />明确授信此权重</label></div>}
        <div className="form-divider"><span>Adapter</span></div>
        <div className="adapter-config">
          <Field label="Adapter 类型"><select aria-label="Adapter 类型" value={adapterType} onChange={(event) => setAdapterType(event.target.value)}><option value="none">不使用</option>{selected.adapters?.map((adapter) => <option key={adapter.id} value={adapter.type}>{adapter.name}</option>)}</select></Field>
          {!selected.adapters?.length && <div className="adapter-empty"><Layers3 size={16} /><span>当前模型未注册 Adapter</span></div>}
          {adapterType !== 'none' && <><Field label="模型内相对路径"><input aria-label="Adapter 相对路径" value={adapterPath} onChange={(event) => setAdapterPath(event.target.value)} placeholder="模型目录内的相对路径" /></Field><Field label={`Adapter 强度 ${adapterScale.toFixed(2)}`}><input aria-label="Adapter 强度" type="range" min="0" max="4" step="0.05" value={adapterScale} onChange={(event) => setAdapterScale(Number(event.target.value))} /></Field></>}
        </div>
        <div className="drawer-actions"><Button variant="secondary" onClick={() => setSelected(undefined)}>取消</Button><Button variant="secondary" icon={applyLoadMutation.isPending ? <LoaderCircle size={15} className="spin" /> : <Power size={15} />} disabled={applyLoadMutation.isPending || saveMutation.isPending} onClick={() => applyLoadMutation.mutate()}>{selected.loaded ? '重新加载' : '按配置加载'}</Button><Button icon={saveMutation.isPending ? <LoaderCircle size={15} className="spin" /> : <CheckCircle2 size={15} />} disabled={saveMutation.isPending || applyLoadMutation.isPending} onClick={() => saveMutation.mutate()}>保存 Profile</Button></div>
      </div>
    </aside></DialogLayer>}
  </div>
}

function ClassifierRuntime({ classifier, pending, onToggle }: { classifier: ClassifierProfile; pending: boolean; onToggle: () => void }) {
  const state = classifier.error ? '不可用' : classifier.loaded ? '已加载' : classifier.enabled ? '按需加载' : '已禁用'
  return <article className={`classifier-runtime ${classifier.loaded ? 'classifier-runtime-loaded' : ''} ${classifier.error ? 'classifier-runtime-error' : ''}`}>
    <span className="classifier-runtime-icon"><Sparkles size={20} aria-hidden="true" /></span>
    <div className="classifier-runtime-main"><strong>美学评分</strong><small>LSE14 Fusion / 1-5 分</small>{classifier.error && <span title={classifier.error.code}>{classifier.error.message}</span>}</div>
    <span className="classifier-runtime-state"><i aria-hidden="true" />{state}</span>
    <Button size="sm" variant={classifier.loaded ? 'danger' : 'secondary'} icon={pending ? <LoaderCircle className="spin" size={14} /> : classifier.loaded ? <Unplug size={14} /> : <Power size={14} />} disabled={pending || !classifier.enabled} onClick={onToggle}>{classifier.loaded ? '卸载' : '加载'}</Button>
  </article>
}

function ModelIcon({ backend }: { backend: string }) {
  return backend === 'onnx' ? <Database size={18} /> : backend === 'safetensors' ? <HardDrive size={18} /> : <Cpu size={18} />
}
function formatMemory(value: number) { return value >= 1024 ? `${(value / 1024).toFixed(1)} GB` : `${Math.round(value)} MB` }
function formatInput(value?: number | number[]) { return Array.isArray(value) ? value.join(' × ') : value ? `${value} px` : '自适应尺寸' }
function thresholdKeys(values: Record<string, number>) {
  const order = ['default', 'general', 'character', 'species', 'rating', 'other']
  return Object.keys(values).sort((left, right) => {
    const leftIndex = order.indexOf(left)
    const rightIndex = order.indexOf(right)
    return (leftIndex < 0 ? 99 : leftIndex) - (rightIndex < 0 ? 99 : rightIndex) || left.localeCompare(right)
  })
}
function thresholdName(key: string) {
  return { default: '默认', general: '通用', character: '角色', species: '物种', rating: '分级', other: '其他' }[key] ?? key
}
function thresholdMapsEqual(left: Record<string, number>, right: Record<string, number>) {
  const keys = new Set([...Object.keys(left), ...Object.keys(right)])
  return [...keys].every((key) => left[key] === right[key])
}
function downloadStatusLabel(record: ModelDownload) {
  if (record.status === 'succeeded') return `已注册 ${record.model_ids.length} 个模型 · 自动加载 ${record.loaded_model_ids.length} 个`
  if (record.status === 'failed') return '下载未完成'
  if (record.phase === 'loading') return '文件已下载，正在自动加载…'
  if (record.phase === 'registering') return '正在识别模型文件…'
  if (record.status === 'running') return '正在下载仓库文件…'
  return '等待下载…'
}
