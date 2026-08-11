import { ArrowRight, Clipboard, Cpu, FileJson, Gauge, Image as ImageIcon, LoaderCircle, Play, RefreshCw, RotateCcw, Send, Tags, Trash2, UploadCloud, X } from 'lucide-react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ClassifierSelector } from '../components/ClassifierSelector'
import { FileDropzone } from '../components/FileDropzone'
import { JobControls } from '../components/JobControls'
import { JsonTree } from '../components/JsonTree'
import { PromptEditors } from '../components/PromptEditors'
import { TagCloud } from '../components/TagCloud'
import { Button, EmptyState, Field, IconButton, Notice, Panel, ProgressBar, StatusBadge, VirtualList } from '../components/ui'
import { useJobEvents, type StreamState } from '../hooks/useJobEvents'
import { useOnlinePrompts } from '../hooks/useOnlinePrompts'
import { api, ApiError } from '../lib/api'
import { usePreferences, useQueueStore } from '../store/app'
import type { ImageResult, JobEvent, JobMode, JobState, ModelProfile } from '../types'

type ActiveJobs = Partial<Record<JobMode, string>>
type ChannelResults = Record<string, Partial<Record<JobMode, ImageResult>>>

const terminalStates = new Set<JobState>(['succeeded', 'failed', 'cancelled', 'interrupted'])

export function Workbench() {
  const queryClient = useQueryClient()
  const setPage = usePreferences((state) => state.setPage)
  const { items, selectedId, addFiles, remove, clear, select, update, updateByName, setAllState } = useQueueStore()
  const [localEnabled, setLocalEnabled] = useState(true)
  const [onlineEnabled, setOnlineEnabled] = useState(false)
  const [providerId, setProviderId] = useState('')
  const [providerModel, setProviderModel] = useState('')
  const [thresholdOverrides, setThresholdOverrides] = useState<Record<string, Record<string, number>>>({})
  const [thresholdEditor, setThresholdEditor] = useState<ModelProfile>()
  const [thresholdDraft, setThresholdDraft] = useState<Record<string, number>>({})
  const [useAestheticClassifier, setUseAestheticClassifier] = useState(false)
  const [replaceUnderscores, setReplaceUnderscores] = useState(false)
  const [includeRating, setIncludeRating] = useState(false)
  const [escapeParentheses, setEscapeParentheses] = useState(true)
  const [activeJobs, setActiveJobs] = useState<ActiveJobs>({})
  const [streamRestarts, setStreamRestarts] = useState<Partial<Record<JobMode, number>>>({})
  const [channelResults, setChannelResults] = useState<ChannelResults>({})
  const [completedModes, setCompletedModes] = useState<JobMode[]>([])
  const [startupErrors, setStartupErrors] = useState<string[]>([])
  const [message, setMessage] = useState<{ tone: 'info' | 'warning' | 'danger' | 'success'; text: string } | null>(null)
  const [isStarting, setIsStarting] = useState(false)
  const loadedJobs = useRef(new Set<string>())
  const completionHandled = useRef(false)

  const providers = useQuery({ queryKey: ['providers'], queryFn: api.providers, staleTime: 60_000 })
  const onlinePrompts = useOnlinePrompts()
  const models = useQuery({ queryKey: ['models'], queryFn: api.models, staleTime: 10_000, refetchInterval: 15_000 })
  const classifiers = useQuery({ queryKey: ['classifiers'], queryFn: api.classifiers, staleTime: 30_000, retry: false })
  const providerItems = providers.data?.items ?? []
  const modelItems = models.data?.items ?? []
  const loadedModels = modelItems.filter((model) => model.loaded)
  const localModelIds = loadedModels.map((model) => model.id)
  const activeThresholds = localModelIds.reduce<Record<string, Record<string, number>>>((result, modelId) => {
    const values = thresholdOverrides[modelId]
    if (values) result[modelId] = values
    return result
  }, {})
  const selected = items.find((item) => item.id === selectedId)
  const selectedResults = selected ? channelResults[selected.file.name] : undefined
  const localResult = selectedResults?.local
  const onlineResult = selectedResults?.online
  const hasResult = Boolean(localResult || onlineResult)
  const visibleTags = [
    ...(localResult?.model_results?.flatMap((group) => group.tags) ?? localResult?.tags ?? []),
    ...(onlineResult?.tags ?? []),
  ]

  useEffect(() => {
    if (!providerId && providerItems[0]) {
      setProviderId(providerItems[0].id)
      setProviderModel(providerItems[0].primary_model)
    }
  }, [providerId, providerItems])

  const handleEvent = useCallback((next: JobEvent) => {
    const total = Math.max(next.total, 1)
    if (next.current_item && !terminalStates.has(next.state)) {
      updateByName(next.current_item, 'processing')
    }
    if (next.total) {
      const percent = Math.round((next.processed / total) * 100)
      items.filter((item) => item.state === 'processing').forEach((item) => {
        update(item.id, { progress: Math.max(item.progress, percent) })
      })
    }
  }, [items, update, updateByName])
  const onLocalEvent = useCallback((event: JobEvent) => handleEvent(event), [handleEvent])
  const onOnlineEvent = useCallback((event: JobEvent) => handleEvent(event), [handleEvent])
  const localStream = useJobEvents(activeJobs.local, { onEvent: onLocalEvent, restartKey: streamRestarts.local })
  const onlineStream = useJobEvents(activeJobs.online, { onEvent: onOnlineEvent, restartKey: streamRestarts.online })

  const loadResults = useCallback(async (mode: JobMode, jobId: string) => {
    if (loadedJobs.current.has(jobId)) return
    loadedJobs.current.add(jobId)
    try {
      const payload = await api.results(jobId)
      setChannelResults((current) => {
        const next = { ...current }
        for (const item of payload.items) {
          next[item.file_name] = { ...next[item.file_name], [mode]: item }
        }
        return next
      })
    } catch (error) {
      setMessage({ tone: 'danger', text: error instanceof ApiError ? error.message : `${mode === 'local' ? '本地' : '在线'}结果读取失败` })
    } finally {
      setCompletedModes((current) => current.includes(mode) ? current : [...current, mode])
      void queryClient.invalidateQueries({ queryKey: ['jobs'] })
    }
  }, [queryClient])

  useEffect(() => {
    if (
      activeJobs.local
      && localStream.event?.job_id === activeJobs.local
      && localStream.event.state
      && terminalStates.has(localStream.event.state)
    ) {
      void loadResults('local', activeJobs.local)
    }
  }, [activeJobs.local, loadResults, localStream.event?.job_id, localStream.event?.state])
  useEffect(() => {
    if (
      activeJobs.online
      && onlineStream.event?.job_id === activeJobs.online
      && onlineStream.event.state
      && terminalStates.has(onlineStream.event.state)
    ) {
      void loadResults('online', activeJobs.online)
    }
  }, [activeJobs.online, loadResults, onlineStream.event?.job_id, onlineStream.event?.state])

  const activeModeKey = `${activeJobs.local ? 'local' : ''}:${activeJobs.online ? 'online' : ''}`
  useEffect(() => {
    const modes = (['local', 'online'] as const).filter((mode) => activeJobs[mode])
    if (!modes.length || completionHandled.current || !modes.every((mode) => completedModes.includes(mode))) return
    completionHandled.current = true
    let failed = 0
    for (const item of items) {
      const results = channelResults[item.file.name]
      const itemFailed = startupErrors.length > 0 || modes.some((mode) => !results?.[mode] || results[mode]?.status === 'failed')
      if (itemFailed) failed += 1
      update(item.id, {
        state: itemFailed ? 'error' : 'done',
        progress: 100,
        result: results?.local ?? results?.online,
        error: itemFailed ? '部分推理通道失败' : undefined,
      })
    }
    setMessage(failed || startupErrors.length
      ? { tone: 'warning', text: `任务完成：${items.length - failed} 项成功，${failed} 项存在失败通道${startupErrors.length ? `；${startupErrors.join('；')}` : ''}` }
      : { tone: 'success', text: `任务完成：${items.length} 项成功` })
  }, [activeModeKey, activeJobs, channelResults, completedModes, items, startupErrors, update])

  const start = async () => {
    if (!items.length || isStarting || (!localEnabled && !onlineEnabled)) return
    setIsStarting(true)
    setMessage(null)
    setActiveJobs({})
    setStreamRestarts({})
    setChannelResults({})
    setCompletedModes([])
    setStartupErrors([])
    loadedJobs.current.clear()
    completionHandled.current = false
    setAllState('uploading')
    try {
      const upload = await api.upload(items.map((item) => item.file))
      setAllState('queued')
      const output = {
        json: false,
        txt: false,
        txt_include_tags: false,
        replace_underscores: replaceUnderscores,
        conflict: 'validate-skip' as const,
      }
      const requests: Array<{ mode: JobMode; task: ReturnType<typeof api.createJob> }> = []
      if (localEnabled) {
        requests.push({
          mode: 'local',
          task: api.createJob({
            mode: 'local',
            source: { type: 'upload', upload_id: upload.upload_id },
            model_ids: localModelIds,
            thresholds: Object.keys(activeThresholds).length ? activeThresholds : undefined,
            classifiers: useAestheticClassifier ? ['aesthetic'] : undefined,
            separate_models: true,
            output: { ...output, include_rating: includeRating, escape_parentheses: escapeParentheses },
          }),
        })
      }
      if (onlineEnabled) {
        requests.push({
          mode: 'online',
          task: api.createJob({
            mode: 'online',
            source: { type: 'upload', upload_id: upload.upload_id },
            provider_id: providerId || undefined,
            provider_model: providerModel.trim() || undefined,
            nl_prompt: onlinePrompts.nl_prompt,
            online_response: 'nl',
            output,
          }),
        })
      }
      const settled = await Promise.allSettled(requests.map(({ task }) => task))
      const created: ActiveJobs = {}
      const errors: string[] = []
      settled.forEach((outcome, index) => {
        const mode = requests[index]?.mode
        if (!mode) return
        if (outcome.status === 'fulfilled') created[mode] = outcome.value.id
        else errors.push(`${mode === 'local' ? '本地' : '在线'}任务创建失败`)
      })
      setStartupErrors(errors)
      if (!created.local && !created.online) throw new Error(errors.join('；') || '任务创建失败')
      setActiveJobs(created)
      setAllState('processing')
      const count = Number(Boolean(created.local)) + Number(Boolean(created.online))
      setMessage(errors.length
        ? { tone: 'warning', text: `${count} 个推理通道已启动；${errors.join('；')}` }
        : { tone: 'info', text: `${count} 个推理通道已并行启动` })
    } catch (error) {
      setAllState('error')
      setMessage({ tone: 'danger', text: error instanceof ApiError ? error.message : error instanceof Error ? error.message : '上传或创建任务失败' })
    } finally {
      setIsStarting(false)
    }
  }

  const action = async (actionName: 'pause' | 'resume' | 'cancel' | 'retry-failed') => {
    const states: Partial<Record<JobMode, JobState>> = {
      local: localStream.event?.state,
      online: onlineStream.event?.state,
    }
    const targets = (Object.entries(activeJobs) as Array<[JobMode, string]>).filter(([mode]) => {
      const state = states[mode] ?? 'running'
      if (actionName === 'pause') return state === 'running'
      if (actionName === 'resume') return state === 'paused'
      if (actionName === 'retry-failed') return ['failed', 'cancelled', 'interrupted'].includes(state)
      return ['queued', 'running', 'paused', 'cancelling'].includes(state)
    })
    if (!targets.length) return
    const outcomes = await Promise.allSettled(targets.map(([, id]) => api.jobAction(id, actionName)))
    if (outcomes.every((outcome) => outcome.status === 'rejected')) {
      setMessage({ tone: 'danger', text: '任务操作失败' })
      return
    }
    if (actionName === 'retry-failed') {
      const retriedModes = targets
        .filter((_target, index) => outcomes[index]?.status === 'fulfilled')
        .map(([mode]) => mode)
      retriedModes.forEach((mode) => {
        const jobId = activeJobs[mode]
        if (jobId) loadedJobs.current.delete(jobId)
      })
      setCompletedModes((current) => current.filter((mode) => !retriedModes.includes(mode)))
      setStreamRestarts((current) => {
        const next = { ...current }
        retriedModes.forEach((mode) => { next[mode] = (next[mode] ?? 0) + 1 })
        return next
      })
      completionHandled.current = false
      setAllState('processing')
    }
    setMessage({ tone: 'info', text: actionName === 'cancel' ? '正在取消活动通道' : actionName === 'retry-failed' ? '正在重试失败通道' : '任务状态已更新' })
  }

  const resetWorkbench = () => {
    clear()
    setActiveJobs({})
    setStreamRestarts({})
    setChannelResults({})
    setCompletedModes([])
    setStartupErrors([])
  }
  const activeStates = (['local', 'online'] as const)
    .filter((mode) => activeJobs[mode])
    .map((mode) => mode === 'local' ? localStream.event?.state : onlineStream.event?.state)
  const combinedState = aggregateState(activeStates)
  const activeCount = Object.values(activeJobs).filter(Boolean).length
  const canStart = items.length > 0
    && (localEnabled || onlineEnabled)
    && (!localEnabled || localModelIds.length > 0)
    && (!onlineEnabled || Boolean(providerId))

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

  return <div className="page page-workbench">
    <div className="page-heading">
      <div className="page-heading-copy"><p className="eyebrow">INFERENCE DESK</p><h1>工作台</h1><p className="page-subtitle">本地与在线视觉模型可独立启用，结果按通道逐项显示。</p></div>
      <div className="workbench-heading-actions">
        <div className="heading-stats"><span><strong>{items.length}</strong> 待处理</span><span className="heading-divider" /><span><strong>{items.filter((item) => item.state === 'done').length}</strong> 已完成</span></div>
        <div className="workbench-run-actions">
          <Button className="workbench-run-button" size="lg" icon={isStarting ? <LoaderCircle size={18} className="spin" /> : activeCount ? <RefreshCw size={17} /> : <Play size={18} />} disabled={isStarting || !canStart} onClick={start}>{isStarting ? '准备任务…' : activeCount ? '再次运行队列' : '开始打标'}</Button>
          {activeCount > 0 && <JobControls state={combinedState} onAction={action} />}
        </div>
      </div>
    </div>
    {message && <Notice tone={message.tone}>{message.text}<IconButton label="关闭提示" onClick={() => setMessage(null)}><X size={15} /></IconButton></Notice>}
    <div className="workbench-grid">
      <Panel title="输入队列" eyebrow="01 / QUEUE" className="queue-panel">
        <FileDropzone onFiles={addFiles} disabled={isStarting} />
        <div className="queue-toolbar"><span>{items.length ? `${items.length} 张图片` : '队列为空'}</span>{items.length > 0 && <Button variant="quiet" size="sm" icon={<Trash2 size={14} />} onClick={resetWorkbench}>清空</Button>}</div>
        <VirtualList items={items} height={Math.min(430, Math.max(112, items.length * 72))} rowHeight={72} getKey={(item) => item.id} empty={<EmptyState icon={<UploadCloud size={22} />} title="还没有图片" detail="拖入、选择或粘贴图片后开始" />} renderRow={(item) => <div className={`queue-row ${selectedId === item.id ? 'queue-row-selected' : ''}`} role="button" tabIndex={0} onClick={() => select(item.id)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') select(item.id) }}><img src={item.previewUrl} alt="" className="queue-thumb" /><span className="queue-row-main"><strong title={item.file.name}>{item.file.name}</strong><small>{formatBytes(item.file.size)}</small><ProgressBar value={item.progress} /></span><span className="queue-row-state"><StatusBadge state={item.state} /><IconButton label={`移除 ${item.file.name}`} onClick={(event) => { event.stopPropagation(); remove(item.id) }}><X size={14} /></IconButton></span></div>} />
      </Panel>

      <Panel title="预览" eyebrow="02 / PREVIEW" className="preview-panel">
        {selected ? <div className="preview-stage"><img src={selected.previewUrl} alt={selected.file.name} /><div className="preview-caption"><span>{selected.file.name}</span><span>{formatBytes(selected.file.size)}</span></div></div> : <EmptyState icon={<ImageIcon size={24} />} title="选择一张图片查看预览" />}
        {activeCount > 0 && <div className="stream-stack" aria-live="polite">{activeJobs.local && <StreamLine label="本地" state={localStream.streamState} error={localStream.error} />}{activeJobs.online && <StreamLine label="在线" state={onlineStream.streamState} error={onlineStream.error} />}</div>}
      </Panel>

      <Panel title="标注结果" eyebrow="03 / RESULT" className="result-panel" actions={hasResult && <IconButton label="复制全部标签" onClick={() => navigator.clipboard.writeText(visibleTags.map((tag) => tag.text).join(', '))}><Clipboard size={15} /></IconButton>}>
        {hasResult ? <div className="result-content"><div className="result-file-line"><StatusBadge state={selected?.state === 'error' ? 'failed' : 'done'} /><span>{selected?.file.name}</span></div><div className={`result-channel-list ${localResult && onlineResult ? 'result-channel-list-dual' : ''}`}>{localResult && <LocalResultSection result={localResult} />}{onlineResult && <OnlineResultSection result={onlineResult} />}</div></div> : <EmptyState icon={<FileJson size={23} />} title="结果会显示在这里" detail="本地标签与在线 Anima 结果会独立更新" />}
      </Panel>
    </div>

    <Panel title="运行设置" eyebrow="TASK CONFIG" className="config-panel workbench-config-panel">
      <div className="channel-config-grid">
        <section className={`channel-config ${localEnabled ? 'channel-config-enabled' : ''}`}>
          <header className="channel-config-header"><span className="channel-config-icon"><Cpu size={17} /></span><div><strong>本地模型</strong><small>LOCAL INFERENCE</small></div><label className="toggle"><input aria-label="启用本地模型" type="checkbox" checked={localEnabled} onChange={(event) => setLocalEnabled(event.target.checked)} /><span />启用</label></header>
          <fieldset className="channel-config-fields local-config-fields" disabled={!localEnabled}>
            <div className="loaded-model-field"><span className="field-label">已加载模型</span>{models.isLoading ? <div className="loaded-model-empty"><LoaderCircle className="spin" size={15} />读取模型状态…</div> : loadedModels.length ? <div className="loaded-model-list" aria-label="已加载模型列表">{loadedModels.map((model) => <div className="loaded-model-row" key={model.id}><span className="loaded-model-icon"><Cpu size={14} /></span><div className="loaded-model-main"><strong title={model.name}>{model.name}</strong><small>{model.backend.toUpperCase()} · {thresholdSummary(model, thresholdOverrides[model.id])}</small></div><div className="loaded-model-actions"><Button type="button" size="sm" variant="secondary" icon={<Gauge size={14} />} aria-label={`调节 ${model.name} 阈值`} title={`调节 ${model.name} 阈值`} onClick={() => openThresholdEditor(model)}>阈值</Button>{thresholdOverrides[model.id] && <IconButton type="button" label={`清除 ${model.name} 本次阈值`} onClick={() => setThresholdOverrides((current) => { const next = { ...current }; delete next[model.id]; return next })}><RotateCcw size={14} /></IconButton>}</div></div>)}</div> : <div className="loaded-model-empty"><span>没有已加载模型</span><Button type="button" size="sm" variant="secondary" icon={<ArrowRight size={14} />} onClick={() => setPage('models')}>前往本地模型</Button></div>}</div>
            <Field label="美学模型"><ClassifierSelector profiles={classifiers.data?.items} aesthetic={useAestheticClassifier} onAestheticChange={setUseAestheticClassifier} /></Field>
          </fieldset>
        </section>
        <section className={`channel-config ${onlineEnabled ? 'channel-config-enabled' : ''}`}>
          <header className="channel-config-header"><span className="channel-config-icon channel-config-icon-online"><Send size={17} /></span><div><strong>在线模型</strong><small>VISION PROVIDER</small></div><label className="toggle"><input aria-label="启用在线模型" type="checkbox" checked={onlineEnabled} onChange={(event) => setOnlineEnabled(event.target.checked)} /><span />启用</label></header>
          <fieldset className="channel-config-fields online-config-fields" disabled={!onlineEnabled}>
            <Field label="Provider"><select value={providerId} onChange={(event) => { setProviderId(event.target.value); setProviderModel(providerItems.find((item) => item.id === event.target.value)?.primary_model ?? '') }}><option value="">选择 Provider</option>{providerItems.map((provider) => <option key={provider.id} value={provider.id}>{provider.name}{provider.configured ? '' : ' · 未配置'}</option>)}</select></Field>
            <Field label="模型"><input value={providerModel} onChange={(event) => setProviderModel(event.target.value)} placeholder="主模型 ID" /></Field>
            <PromptEditors prompts={onlinePrompts} onChange={onlinePrompts.setPrompts} onReset={onlinePrompts.reset} disabled={!onlineEnabled} fields={['nl_prompt']} />
          </fieldset>
        </section>
      </div>
      <footer className="workbench-config-footer">
        <fieldset className="field toggle-field"><legend className="field-label">结果格式</legend><div className="toggle-row"><label className="toggle"><input aria-label="下划线替空格" type="checkbox" checked={replaceUnderscores} onChange={(event) => setReplaceUnderscores(event.target.checked)} /><span />下划线替空格</label><label className="toggle"><input aria-label="输出 Rating 标签" type="checkbox" checked={includeRating} onChange={(event) => setIncludeRating(event.target.checked)} /><span />输出 Rating</label><label className="toggle"><input aria-label="括号转义" type="checkbox" checked={escapeParentheses} onChange={(event) => setEscapeParentheses(event.target.checked)} /><span />括号转义</label></div></fieldset>
      </footer>
    </Panel>
    {thresholdEditor && <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) setThresholdEditor(undefined) }}><div className="secret-dialog quick-threshold-dialog" role="dialog" aria-modal="true" aria-labelledby="quick-threshold-title">
      <header><span className="dialog-icon"><Gauge size={20} /></span><div><p className="eyebrow">TASK THRESHOLD</p><h2 id="quick-threshold-title">{thresholdEditor.name}</h2></div><IconButton label="关闭" onClick={() => setThresholdEditor(undefined)}><X size={17} /></IconButton></header>
      <div className="dialog-body"><Notice tone="info">这里只调整当前工作台任务，不会修改模型文件或本地模型页面的默认配置。</Notice><div className="threshold-list">{thresholdKeys(thresholdDraft).map((key) => { const value = thresholdDraft[key] ?? 0; return <Field key={key} label={`${thresholdName(key)} ${value.toFixed(2)}`}><input aria-label={`${thresholdName(key)}阈值`} type="range" min="0" max="1" step="0.01" value={value} onChange={(event) => setThresholdDraft((current) => ({ ...current, [key]: Number(event.target.value) }))} /></Field> })}</div></div>
      <footer><Button type="button" variant="quiet" icon={<RotateCcw size={14} />} onClick={() => setThresholdDraft(effectiveThresholds(thresholdEditor))}>恢复模型当前值</Button><span className="drawer-actions-spacer" /><Button type="button" variant="secondary" onClick={() => setThresholdEditor(undefined)}>取消</Button><Button type="button" icon={<Gauge size={15} />} onClick={applyThresholdDraft}>应用到本次任务</Button></footer>
    </div></div>}
  </div>
}

function LocalResultSection({ result }: { result: ImageResult }) {
  const groups = result.model_results?.length ? result.model_results : [{ model_id: 'local', model_name: '本地模型', tags: result.tags }]
  return <section className="result-channel"><header className="result-channel-heading"><Cpu size={15} /><strong>本地模型</strong><small>{groups.length} 个结果</small></header><div className="model-result-list">{groups.map((group) => <section className="result-section model-result-section" key={group.model_id}><div className="model-result-heading"><div className="section-label"><Tags size={14} /><span>{group.model_name}</span><small>{group.tags.length}</small></div><IconButton label={`复制 ${group.model_name} 标签`} onClick={() => navigator.clipboard.writeText(group.tags.map((tag) => tag.text).join(', '))}><Clipboard size={14} /></IconButton></div><TagCloud tags={group.tags} /></section>)}</div>{result.warnings.length > 0 && <Notice tone="warning">{result.warnings.join('；')}</Notice>}</section>
}

function OnlineResultSection({ result }: { result: ImageResult }) {
  const model = result.model_id ?? result.tags[0]?.model_id
  return <section className="result-channel"><header className="result-channel-heading"><Send size={15} /><strong>在线模型</strong>{model && <small title={model}>{model}</small>}</header>{result.anima && <div className="result-section"><div className="section-label"><FileJson size={14} /> Anima JSON</div><JsonTree value={result.anima} /></div>}{result.tags.length > 0 && <div className="result-section"><div className="section-label"><Tags size={14} /> 标签预览</div><TagCloud tags={result.tags} /></div>}{result.caption && <div className="caption-block"><span>自然语言描述</span><p>{result.caption}</p></div>}{result.warnings.length > 0 && <Notice tone="warning">{result.warnings.join('；')}</Notice>}</section>
}

function StreamLine({ label, state, error }: { label: string; state: StreamState; error: string | null }) {
  const text = state === 'connected' ? '实时进度已连接' : state === 'reconnecting' ? '连接中断，正在重连' : state === 'closed' ? '任务流已结束' : '正在连接任务流'
  return <div className="stream-line"><strong>{label}</strong><span className={`stream-dot stream-${state}`} /><span>{text}</span>{error && <small>{error}</small>}</div>
}

function aggregateState(states: Array<JobState | undefined>): JobState {
  if (!states.length) return 'queued'
  if (states.some((state) => state === 'cancelling')) return 'cancelling'
  if (states.some((state) => !state || state === 'queued' || state === 'running')) return 'running'
  if (states.some((state) => state === 'paused')) return 'paused'
  if (states.some((state) => state === 'failed')) return 'failed'
  if (states.some((state) => state === 'interrupted')) return 'interrupted'
  if (states.some((state) => state === 'cancelled')) return 'cancelled'
  return 'succeeded'
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

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

function thresholdMapsEqual(left: Record<string, number>, right: Record<string, number>) {
  const keys = new Set([...Object.keys(left), ...Object.keys(right)])
  return [...keys].every((key) => left[key] === right[key])
}

function thresholdSummary(model: ModelProfile, override?: Record<string, number>) {
  if (override) return `本次自定义 · ${Object.keys(override).length} 类`
  return `${model.threshold_source === 'custom' ? '模型自定义' : '模型预设'} · 通用 ${(model.thresholds?.general ?? model.threshold ?? 0.35).toFixed(2)}`
}
