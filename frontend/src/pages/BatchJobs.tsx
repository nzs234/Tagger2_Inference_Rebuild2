import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Clock3, Cpu, FileSearch, FolderSearch, Gauge, History, LoaderCircle, Play, RotateCcw, Search, Timer, X, XCircle } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { JobControls } from '../components/JobControls'
import { ClassifierSelector } from '../components/ClassifierSelector'
import { PromptEditors } from '../components/PromptEditors'
import { Button, EmptyState, Field, IconButton, Notice, Panel, ProgressBar, StatusBadge, VirtualList } from '../components/ui'
import { useJobEvents } from '../hooks/useJobEvents'
import { useOnlinePrompts } from '../hooks/useOnlinePrompts'
import { api, ApiError } from '../lib/api'
import type { JobMode, JobSummary, ModelProfile, ScanItem } from '../types'

type BatchMode = JobMode | 'hybrid'

export function BatchJobs() {
  const queryClient = useQueryClient()
  const roots = useQuery({ queryKey: ['roots'], queryFn: api.roots, staleTime: 60_000, retry: false })
  const providers = useQuery({ queryKey: ['providers'], queryFn: api.providers, staleTime: 60_000, retry: false })
  const models = useQuery({ queryKey: ['models'], queryFn: api.models, staleTime: 10_000, refetchInterval: 15_000, retry: false })
  const classifiers = useQuery({ queryKey: ['classifiers'], queryFn: api.classifiers, staleTime: 30_000, retry: false })
  const jobs = useQuery({ queryKey: ['jobs'], queryFn: () => api.jobs(100), refetchInterval: 10_000, retry: false })
  const onlinePrompts = useOnlinePrompts()
  const [inputPath, setInputPath] = useState('')
  const [sourceRootId, setSourceRootId] = useState('')
  const [scannedInputPath, setScannedInputPath] = useState('')
  const [recursive, setRecursive] = useState(true)
  const [pattern, setPattern] = useState('*.png, *.jpg, *.jpeg, *.webp')
  const [mode, setMode] = useState<BatchMode>('online')
  const [providerId, setProviderId] = useState('')
  const [onlineConcurrency, setOnlineConcurrency] = useState(3)
  const [hybridResponse, setHybridResponse] = useState<'nl' | 'json'>('nl')
  const [thresholdOverrides, setThresholdOverrides] = useState<Record<string, Record<string, number>>>({})
  const [thresholdEditor, setThresholdEditor] = useState<ModelProfile>()
  const [thresholdDraft, setThresholdDraft] = useState<Record<string, number>>({})
  const [useAestheticClassifier, setUseAestheticClassifier] = useState(false)
  const [outputRootId, setOutputRootId] = useState('')
  const [outputPath, setOutputPath] = useState('')
  const [localTxtOutput, setLocalTxtOutput] = useState(true)
  const [onlineJsonOutput, setOnlineJsonOutput] = useState(true)
  const [onlineTxtOutput, setOnlineTxtOutput] = useState(false)
  const [onlineTxtIncludeTags, setOnlineTxtIncludeTags] = useState(false)
  const [replaceUnderscores, setReplaceUnderscores] = useState(false)
  const [includeRating, setIncludeRating] = useState(false)
  const [escapeParentheses, setEscapeParentheses] = useState(true)
  const [conflict, setConflict] = useState<'validate-skip' | 'overwrite' | 'rename'>('validate-skip')
  const [scanItems, setScanItems] = useState<ScanItem[]>([])
  const [scanTotal, setScanTotal] = useState(0)
  const [selectedJob, setSelectedJob] = useState<string>()
  const [notice, setNotice] = useState<{ tone: 'info' | 'danger' | 'success' | 'warning'; text: string } | null>(null)

  const rootItems = roots.data?.items ?? []
  const outputRoots = rootItems.filter((root) => root.kind === 'output')
  const loadedModels = (models.data?.items ?? []).filter((model) => model.loaded)
  const modelIds = loadedModels.map((model) => model.id)
  const activeThresholds = modelIds.reduce<Record<string, Record<string, number>>>((result, modelId) => {
    const values = thresholdOverrides[modelId]
    if (values) result[modelId] = values
    return result
  }, {})
  const isHybrid = mode === 'hybrid'
  const usesOnline = mode === 'online' || isHybrid
  const usesLocal = mode === 'local' || isHybrid
  useEffect(() => {
    if (!providerId && providers.data?.items[0]) setProviderId(providers.data.items[0].id)
  }, [providerId, providers.data?.items])

  const scanMutation = useMutation({
    mutationFn: async (path: string) => {
      const normalized = path.replace(/[\\/]+$/, '')
      const name = normalized.split(/[\\/]/).filter(Boolean).at(-1) || '批量输入目录'
      const root = await api.addRoot({ name, kind: 'input', path })
      const scan = await api.scan({
        root_id: root.id,
        relative_path: '',
        recursive,
        patterns: pattern.split(',').map((value) => value.trim()).filter(Boolean),
        page_size: 5000,
      })
      return { path, root, scan }
    },
    onSuccess: ({ path, root, scan }) => {
      setSourceRootId(root.id)
      setScannedInputPath(path)
      setScanItems(scan.items)
      setScanTotal(scan.total)
      setNotice({ tone: 'success', text: `扫描完成：找到 ${scan.total} 张图片` })
      void queryClient.invalidateQueries({ queryKey: ['roots'] })
    },
    onError: (error) => {
      setSourceRootId('')
      setScannedInputPath('')
      setScanItems([])
      setScanTotal(0)
      setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '目录扫描失败' })
    },
  })

  const createMutation = useMutation({
    mutationFn: () => api.createJob({
      mode: isHybrid ? 'local' : mode,
      hybrid: isHybrid,
      source: { type: 'scan', root_id: sourceRootId, relative_path: '', recursive, patterns: pattern.split(',').map((value) => value.trim()).filter(Boolean) },
      provider_id: usesOnline ? providerId : undefined,
      model_ids: usesLocal ? modelIds : undefined,
      thresholds: usesLocal && Object.keys(activeThresholds).length ? activeThresholds : undefined,
      classifiers: usesLocal && useAestheticClassifier ? ['aesthetic'] : undefined,
      tag_prompt: usesOnline ? onlinePrompts.tag_prompt : undefined,
      nl_prompt: usesOnline ? onlinePrompts.nl_prompt : undefined,
      json_prompt: usesOnline ? onlinePrompts.json_prompt : undefined,
      online_response: isHybrid ? hybridResponse : undefined,
      online_concurrency: usesOnline ? onlineConcurrency : undefined,
      output: {
        root_id: outputRootId || undefined,
        relative_path: outputPath,
        json: isHybrid ? hybridResponse === 'json' : mode === 'online' && onlineJsonOutput,
        txt: isHybrid ? true : mode === 'local' ? localTxtOutput : onlineTxtOutput,
        txt_include_tags: mode === 'online' && onlineTxtIncludeTags,
        replace_underscores: replaceUnderscores,
        ...(usesLocal ? { include_rating: includeRating, escape_parentheses: escapeParentheses } : {}),
        conflict,
      },
    }),
    onSuccess: (job) => { setSelectedJob(job.id); setNotice({ tone: 'success', text: `批量任务 ${job.id.slice(0, 8)} 已创建` }); void queryClient.invalidateQueries({ queryKey: ['jobs'] }) },
    onError: (error) => setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '任务创建失败' }),
  })

  const stream = useJobEvents(selectedJob, { onEvent: (event) => {
    queryClient.setQueryData(['jobs'], (current: { items: JobSummary[]; total: number } | undefined) => current ? {
      ...current,
      items: current.items.map((job) => job.id === event.job_id ? { ...job, ...event } : job),
    } : current)
    if (['succeeded', 'failed', 'cancelled', 'interrupted'].includes(event.state)) void queryClient.invalidateQueries({ queryKey: ['jobs'] })
  } })

  const actionMutation = useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'pause' | 'resume' | 'cancel' | 'retry-failed' }) => api.jobAction(id, action),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['jobs'] }),
    onError: (error) => setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '任务操作失败' }),
  })

  const jobItems = jobs.data?.items ?? []
  const activeJob = useMemo(() => {
    const fromList = jobItems.find((job) => job.id === selectedJob)
    return selectedJob && stream.event?.job_id === selectedJob && fromList ? { ...fromList, ...stream.event } : fromList
  }, [jobItems, selectedJob, stream.event])
  const progress = activeJob ? (activeJob.total ? activeJob.processed / activeJob.total * 100 : 0) : 0
  const openThresholdEditor = (model: ModelProfile) => {
    setThresholdEditor(model)
    setThresholdDraft({ ...(thresholdOverrides[model.id] ?? effectiveThresholds(model)) })
  }
  const applyThresholdDraft = () => {
    if (!thresholdEditor) return
    const baseline = effectiveThresholds(thresholdEditor)
    setThresholdOverrides((current) => {
      const next = { ...current }
      if (thresholdMapsEqual(thresholdDraft, baseline)) delete next[thresholdEditor.id]
      else next[thresholdEditor.id] = { ...thresholdDraft }
      return next
    })
    setThresholdEditor(undefined)
  }

  return <div className="page page-batch">
    <div className="page-heading"><div><p className="eyebrow">BATCH PIPELINE</p><h1>批量任务</h1><p className="page-subtitle">扫描本机文件夹，创建可暂停、恢复和重试的持久任务。</p></div><div className="heading-stats"><span><strong>{jobItems.filter((job) => job.state === 'running').length}</strong> 运行中</span><span className="heading-divider" /><span><strong>{jobItems.length}</strong> 历史任务</span></div></div>
    {notice && <Notice tone={notice.tone}>{notice.text}</Notice>}
    <div className="batch-layout">
      <div className="batch-builder">
        <Panel title="目录扫描" eyebrow="01 / SOURCE">
          <div className="form-grid two-columns">
            <Field label="输入文件夹路径"><input value={inputPath} onChange={(event) => { setInputPath(event.target.value); setSourceRootId(''); setScannedInputPath(''); setScanItems([]); setScanTotal(0) }} placeholder="例如 E:\6_santabear1" spellCheck={false} /></Field>
            <Field label="文件过滤"><input value={pattern} onChange={(event) => setPattern(event.target.value)} /></Field>
            <Field label="扫描范围"><label className="toggle standalone"><input type="checkbox" checked={recursive} onChange={(event) => setRecursive(event.target.checked)} /><span />包含子目录</label></Field>
          </div>
          <div className="form-actions"><Button variant="secondary" icon={scanMutation.isPending ? <LoaderCircle className="spin" size={16} /> : <FolderSearch size={16} />} disabled={!inputPath.trim() || scanMutation.isPending} onClick={() => scanMutation.mutate(inputPath.trim())}>扫描目录</Button>{scanTotal > 0 && <span className="scan-count"><CheckCircle2 size={14} /> {scanTotal} 张</span>}</div>
          {scanItems.length > 0 ? <VirtualList items={scanItems} height={250} rowHeight={44} getKey={(item, index) => item.id ?? `${item.relative_path}:${index}`} renderRow={(item) => <div className="scan-row"><FileSearch size={15} /><span title={item.relative_path}>{item.relative_path}</span>{item.size != null && <small>{formatBytes(item.size)}</small>}</div>} /> : <EmptyState icon={<Search size={22} />} title="等待扫描" detail="填写文件夹路径后查看图片清单" />}
        </Panel>

        <Panel title="任务配置" eyebrow="02 / CONFIG">
          <div className="mode-switch batch-mode" role="tablist" aria-label="推理模式">
            <button className={mode === 'online' ? 'mode-active' : ''} onClick={() => setMode('online')} role="tab" aria-selected={mode === 'online'}>在线模型</button>
            <button className={mode === 'local' ? 'mode-active' : ''} onClick={() => setMode('local')} role="tab" aria-selected={mode === 'local'}>本地模型</button>
            <button className={isHybrid ? 'mode-active' : ''} onClick={() => setMode('hybrid')} role="tab" aria-selected={isHybrid}>本地 + 在线</button>
          </div>
          <div className="form-grid two-columns">
            {usesLocal && <Field label="已加载本地模型"><div className="loaded-model-field">{models.isLoading ? <div className="loaded-model-empty"><LoaderCircle className="spin" size={15} />读取模型状态…</div> : loadedModels.length ? <div className="loaded-model-list" aria-label="批量已加载模型列表">{loadedModels.map((model) => <div className="loaded-model-row" key={model.id}><span className="loaded-model-icon"><Cpu size={14} /></span><div className="loaded-model-main"><strong title={model.name}>{model.name}</strong><small>{model.backend.toUpperCase()} · {thresholdSummary(model, thresholdOverrides[model.id])}</small></div><div className="loaded-model-actions"><Button type="button" size="sm" variant="secondary" icon={<Gauge size={14} />} aria-label={`调节 ${model.name} 阈值`} title={`调节 ${model.name} 阈值`} onClick={() => openThresholdEditor(model)}>阈值</Button>{thresholdOverrides[model.id] && <IconButton type="button" label={`清除 ${model.name} 本次阈值`} onClick={() => setThresholdOverrides((current) => { const next = { ...current }; delete next[model.id]; return next })}><RotateCcw size={14} /></IconButton>}</div></div>)}</div> : <div className="loaded-model-empty"><span>没有已加载模型</span></div>}</div></Field>}
            {usesLocal && <Field label="美学模型"><ClassifierSelector profiles={classifiers.data?.items} aesthetic={useAestheticClassifier} onAestheticChange={setUseAestheticClassifier} /></Field>}
            {usesOnline && <Field label="Provider"><select value={providerId} onChange={(event) => setProviderId(event.target.value)}><option value="">选择 Provider</option>{(providers.data?.items ?? []).map((provider) => <option value={provider.id} key={provider.id}>{provider.name}</option>)}</select></Field>}
            {usesOnline && <Field label="并发" hint="同时处理的在线请求数，范围 1-128"><input type="number" min={1} max={128} value={onlineConcurrency} onChange={(event) => setOnlineConcurrency(Math.max(1, Math.min(128, Number(event.target.value) || 1)))} /></Field>}
            {isHybrid && <Field label="在线输出"><select value={hybridResponse} onChange={(event) => setHybridResponse(event.target.value as 'nl' | 'json')}><option value="nl">NL + TAG TXT</option><option value="json">Anima JSON + TAG TXT</option></select></Field>}
            {usesOnline && <PromptEditors prompts={onlinePrompts} onChange={onlinePrompts.setPrompts} onReset={onlinePrompts.reset} fields={isHybrid ? (hybridResponse === 'json' ? ['json_prompt'] : ['nl_prompt']) : undefined} />}
            <Field label="输出目录"><select value={outputRootId} onChange={(event) => setOutputRootId(event.target.value)}><option value="">原始图片文件夹</option>{outputRoots.map((root) => <option key={root.id} value={root.id}>{root.name}</option>)}</select></Field>
            <Field label="输出相对路径"><input value={outputPath} onChange={(event) => setOutputPath(event.target.value)} placeholder="留空时保持原目录结构" /></Field>
            <Field label="已有文件"><select value={conflict} onChange={(event) => setConflict(event.target.value as typeof conflict)}><option value="validate-skip">校验通过后跳过</option><option value="overwrite">覆盖</option><option value="rename">自动重命名</option></select></Field>
            <fieldset className="field toggle-field"><legend className="field-label">产物格式</legend><div className="toggle-row">
              {isHybrid ? <>
                <label className="toggle"><input type="checkbox" checked readOnly disabled /><span />TAG TXT</label>
                {hybridResponse === 'json' && <label className="toggle"><input type="checkbox" checked readOnly disabled /><span />Anima JSON</label>}
                <label className="toggle"><input aria-label="输出 Rating 标签" type="checkbox" checked={includeRating} onChange={(event) => setIncludeRating(event.target.checked)} /><span />输出 Rating</label>
                <label className="toggle"><input aria-label="括号转义" type="checkbox" checked={escapeParentheses} onChange={(event) => setEscapeParentheses(event.target.checked)} /><span />括号转义</label>
              </> : mode === 'online' ? <>
                <label className="toggle"><input type="checkbox" checked={onlineJsonOutput} onChange={(event) => setOnlineJsonOutput(event.target.checked)} /><span />Anima JSON</label>
                <label className="toggle"><input type="checkbox" checked={onlineTxtOutput} onChange={(event) => setOnlineTxtOutput(event.target.checked)} /><span />TXT（NL）</label>
                {onlineTxtOutput && <label className="toggle"><input type="checkbox" checked={onlineTxtIncludeTags} onChange={(event) => setOnlineTxtIncludeTags(event.target.checked)} /><span />TXT 包含 TAG</label>}
              </> : <>
                <label className="toggle"><input type="checkbox" checked={localTxtOutput} onChange={(event) => setLocalTxtOutput(event.target.checked)} /><span />TXT</label>
                <label className="toggle"><input aria-label="输出 Rating 标签" type="checkbox" checked={includeRating} onChange={(event) => setIncludeRating(event.target.checked)} /><span />输出 Rating</label>
                <label className="toggle"><input aria-label="括号转义" type="checkbox" checked={escapeParentheses} onChange={(event) => setEscapeParentheses(event.target.checked)} /><span />括号转义</label>
              </>}
              <label className="toggle"><input aria-label="下划线替空格" type="checkbox" checked={replaceUnderscores} onChange={(event) => setReplaceUnderscores(event.target.checked)} /><span />下划线替空格</label>
            </div></fieldset>
          </div>
          <div className="form-actions form-actions-end"><Button icon={createMutation.isPending ? <LoaderCircle className="spin" size={16} /> : <Play size={15} />} disabled={!sourceRootId || scannedInputPath !== inputPath.trim() || !scanTotal || createMutation.isPending || (usesOnline && !providerId) || (usesLocal && !modelIds.length)} onClick={() => createMutation.mutate()}>创建批量任务</Button></div>
        </Panel>
      </div>

      <div className="batch-monitor">
        <Panel title="任务监控" eyebrow="03 / MONITOR">
          {activeJob ? <div className="active-job">
            <div className="active-job-heading"><div><span className="mono">{activeJob.id.slice(0, 12)}</span><small>{formatDate(activeJob.created_at)}</small></div><StatusBadge state={activeJob.state} /></div>
            <ProgressBar value={progress} label={`任务进度 ${Math.round(progress)}%`} />
            <div className="metrics-grid"><Metric icon={<CheckCircle2 />} label="成功" value={activeJob.succeeded} /><Metric icon={<AlertTriangle />} label="跳过" value={activeJob.skipped} /><Metric icon={<XCircle />} label="失败" value={activeJob.failed} /><Metric icon={<Timer />} label="速度" value={activeJob.rate ? `${activeJob.rate.toFixed(1)}/s` : '—'} /></div>
            {activeJob.current_item && <div className="current-item"><LoaderCircle size={14} className={activeJob.state === 'running' ? 'spin' : ''} /><span title={activeJob.current_item}>{activeJob.current_item}</span></div>}
            <JobControls state={activeJob.state} onAction={(action) => actionMutation.mutate({ id: activeJob.id, action })} />
            <div className="stream-line" aria-live="polite"><span className={`stream-dot stream-${stream.streamState}`} />{stream.streamState === 'connected' ? '实时连接' : stream.streamState === 'reconnecting' ? '正在恢复连接' : '事件流已停止'}</div>
          </div> : <EmptyState icon={<Clock3 size={23} />} title="选择一个任务查看进度" />}
        </Panel>
        <Panel title="任务历史" eyebrow="HISTORY" actions={<Button size="sm" variant="quiet" icon={<RotateCcw size={14} />} onClick={() => jobs.refetch()}>刷新</Button>}>
          <VirtualList items={jobItems} height={420} rowHeight={76} getKey={(job) => job.id} empty={<EmptyState icon={<History size={22} />} title="暂无任务记录" />} renderRow={(job) => <button className={`history-row ${selectedJob === job.id ? 'history-row-selected' : ''}`} onClick={() => setSelectedJob(job.id)}>
            <span className="history-icon"><JobIcon state={job.state} /></span><span className="history-main"><strong>{job.hybrid ? '本地 + 在线批量打标' : job.mode === 'online' ? '在线批量打标' : '本地批量打标'}</strong><small>{formatDate(job.created_at)} · {job.processed}/{job.total}</small></span><StatusBadge state={job.state} />
          </button>} />
        </Panel>
      </div>
    </div>
    {thresholdEditor && <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) setThresholdEditor(undefined) }}><div className="secret-dialog quick-threshold-dialog" role="dialog" aria-modal="true" aria-labelledby="batch-threshold-title">
      <header><span className="dialog-icon"><Gauge size={20} /></span><div><p className="eyebrow">TASK THRESHOLD</p><h2 id="batch-threshold-title">{thresholdEditor.name}</h2></div><IconButton label="关闭" onClick={() => setThresholdEditor(undefined)}><X size={17} /></IconButton></header>
      <div className="dialog-body"><Notice tone="info">这里只调整当前批量任务，不会修改模型文件或本地模型页面的默认配置。</Notice><div className="threshold-list">{thresholdKeys(thresholdDraft).map((key) => { const value = thresholdDraft[key] ?? 0; return <Field key={key} label={`${thresholdName(key)} ${value.toFixed(2)}`}><input aria-label={`${thresholdName(key)}阈值`} type="range" min="0" max="1" step="0.01" value={value} onChange={(event) => setThresholdDraft((current) => ({ ...current, [key]: Number(event.target.value) }))} /></Field> })}</div></div>
      <footer><Button type="button" variant="quiet" icon={<RotateCcw size={14} />} onClick={() => setThresholdDraft(effectiveThresholds(thresholdEditor))}>恢复模型当前值</Button><span className="drawer-actions-spacer" /><Button type="button" variant="secondary" onClick={() => setThresholdEditor(undefined)}>取消</Button><Button type="button" icon={<Gauge size={15} />} onClick={applyThresholdDraft}>应用到本次任务</Button></footer>
    </div></div>}
  </div>
}

function Metric({ icon, label, value }: { icon: React.ReactNode; label: string; value: string | number }) {
  return <div className="metric"><span>{icon}</span><div><small>{label}</small><strong>{value}</strong></div></div>
}
function JobIcon({ state }: { state: string }) {
  if (state === 'succeeded') return <CheckCircle2 size={16} />
  if (state === 'failed') return <XCircle size={16} />
  if (state === 'running') return <LoaderCircle className="spin" size={16} />
  return <Clock3 size={16} />
}
function formatDate(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(date)
}
function formatBytes(value: number) { return value > 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(value / 1024))} KB` }

function effectiveThresholds(model: ModelProfile): Record<string, number> {
  return { default: model.threshold ?? 0.35, ...(model.thresholds ?? {}) }
}

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

function thresholdSummary(model: ModelProfile, override?: Record<string, number>) {
  if (override) return `本次自定义 · ${Object.keys(override).length} 类`
  return `${model.threshold_source === 'custom' ? '模型自定义' : '模型预设'} · 通用 ${(model.thresholds?.general ?? model.threshold ?? 0.35).toFixed(2)}`
}

function thresholdMapsEqual(left: Record<string, number>, right: Record<string, number>) {
  const keys = new Set([...Object.keys(left), ...Object.keys(right)])
  return [...keys].every((key) => left[key] === right[key])
}
