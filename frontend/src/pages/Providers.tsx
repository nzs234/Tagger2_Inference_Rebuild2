import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, Braces, Check, CircleX, Cloud, KeyRound, LoaderCircle, Pencil, Plus, RefreshCw, Save, Sparkles, TestTube2, Trash2, X } from 'lucide-react'
import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { z } from 'zod'
import { PromptEditors } from '../components/PromptEditors'
import { Button, ConfirmDialog, DialogLayer, EmptyState, Field, IconButton, Notice, Panel, StatusBadge } from '../components/ui'
import { useOnlinePrompts } from '../hooks/useOnlinePrompts'
import { api, ApiError } from '../lib/api'
import type { ProviderKind, ProviderProfile, ProviderProtocol } from '../types'

const providerSchema = z.object({
  name: z.string().trim().min(1, '请输入名称').max(80),
  kind: z.enum(['custom', 'openai', 'gemini', 'claude']),
  protocol: z.enum(['openai', 'gemini', 'claude']),
  base_url: z.string().url('请输入完整的 http/https 地址').refine((value) => /^https?:\/\//i.test(value), '仅支持 http/https'),
  primary_model: z.string().trim().min(1, '请输入主模型'),
  fallback_model: z.string().trim().optional(),
  temperature: z.number().min(0).max(2),
  top_p: z.number().min(0).max(1),
  top_k: z.number().int().min(0).max(1000).optional(),
  max_tokens: z.number().int().min(64).max(65536),
  timeout_seconds: z.number().int().min(5).max(600),
  retries: z.number().int().min(0).max(12),
  api_keys: z.string().max(20000, '密钥内容过长').optional(),
  enabled: z.boolean(),
})

type ProviderValues = z.infer<typeof providerSchema>
type ConfigurableProviderKind = ProviderValues['kind']

const presets: Record<ConfigurableProviderKind, Pick<ProviderValues, 'base_url' | 'primary_model'>> = {
  custom: { base_url: '', primary_model: '' },
  gemini: { base_url: 'https://generativelanguage.googleapis.com/v1beta', primary_model: 'gemini-2.5-flash' },
  openai: { base_url: 'https://api.openai.com/v1', primary_model: 'gpt-4.1-mini' },
  claude: { base_url: 'https://api.anthropic.com', primary_model: 'claude-sonnet-4-5' },
}

const defaults: ProviderValues = {
  name: '', kind: 'custom', protocol: 'openai', ...presets.custom, fallback_model: '', temperature: 0.2, top_p: 0.9, top_k: 40,
  max_tokens: 4096, timeout_seconds: 90, retries: 3, api_keys: '', enabled: true,
}

export function Providers() {
  const queryClient = useQueryClient()
  const onlinePrompts = useOnlinePrompts()
  const providerQuery = useQuery({ queryKey: ['providers'], queryFn: api.providers })
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<string>()
  const [secretProvider, setSecretProvider] = useState<ProviderProfile>()
  const [deleteTarget, setDeleteTarget] = useState<ProviderProfile>()
  const [keys, setKeys] = useState('')
  const [notice, setNotice] = useState<{ tone: 'success' | 'danger' | 'info'; text: string } | null>(null)
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; message: string; latency?: number }>>({})
  const [discovered, setDiscovered] = useState<Record<string, string[]>>({})
  const [discoveryErrors, setDiscoveryErrors] = useState<Record<string, string>>({})
  const [formDiscovered, setFormDiscovered] = useState<string[] | undefined>()
  const [formDiscoveryError, setFormDiscoveryError] = useState('')
  const form = useForm<ProviderValues>({ resolver: zodResolver(providerSchema), defaultValues: defaults })
  const selectedKind = form.watch('kind')

  const saveMutation = useMutation({
    mutationFn: async (values: ProviderValues) => {
      const { api_keys: rawKeys, ...profileValues } = values
      const profile = editingId
        ? await api.updateProvider(editingId, { ...profileValues, fallback_model: values.fallback_model || null, top_k: values.top_k ?? null })
        : await api.createProvider({ ...profileValues, fallback_model: values.fallback_model || null, top_k: values.top_k ?? null })
      const apiKeys = parseApiKeys(rawKeys)
      if (!apiKeys.length) return { profile, secretError: null, secretUpdated: false }
      try {
        await api.setProviderSecret(profile.id, apiKeys)
        return { profile, secretError: null, secretUpdated: true }
      } catch (error) {
        return { profile, secretError: error, secretUpdated: false }
      }
    },
    onSuccess: ({ profile, secretError, secretUpdated }) => {
      const action = editingId ? 'Provider 已更新' : 'Provider 已创建'
      if (secretError) {
        setNotice({ tone: 'danger', text: `${action}，但 API Key 保存失败：${secretError instanceof ApiError ? secretError.message : '凭据存储不可用'}。配置编辑器已保留，可直接重试密钥保存。` })
        setEditingId(profile.id)
        void queryClient.invalidateQueries({ queryKey: ['providers'] })
        return
      }
      setNotice({ tone: 'success', text: secretUpdated ? `${action}，API Key 已安全保存` : action })
      setShowForm(false); setEditingId(undefined); form.reset(defaults)
      void queryClient.invalidateQueries({ queryKey: ['providers'] })
    },
    onError: (error) => setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '保存 Provider 失败' }),
  })

  const secretMutation = useMutation({
    mutationFn: () => api.setProviderSecret(secretProvider!.id, keys.split(/\r?\n|,/).map((key) => key.trim()).filter(Boolean)),
    onSuccess: () => {
      setNotice({ tone: 'success', text: '密钥已安全保存' }); setKeys(''); setSecretProvider(undefined)
      void queryClient.invalidateQueries({ queryKey: ['providers'] })
    },
    onError: (error) => setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '密钥保存失败' }),
  })

  const deleteMutation = useMutation({
    mutationFn: (provider: ProviderProfile) => api.deleteProvider(provider.id).then(() => provider),
    onSuccess: (provider) => {
      setDeleteTarget(undefined)
      setNotice({ tone: 'success', text: `${provider.name} 已删除` })
      setTestResults((current) => { const next = { ...current }; delete next[provider.id]; return next })
      setDiscovered((current) => { const next = { ...current }; delete next[provider.id]; return next })
      if (editingId === provider.id) { setShowForm(false); setEditingId(undefined) }
      void queryClient.invalidateQueries({ queryKey: ['providers'] })
    },
    onError: (error) => setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '删除 Provider 失败' }),
  })

  const testMutation = useMutation({
    mutationFn: (id: string) => api.testProvider(id).then((result) => ({ id, result })),
    onSuccess: ({ id, result }) => setTestResults((current) => ({ ...current, [id]: { ok: result.ok, message: result.message, latency: result.latency_ms } })),
    onError: (error, id) => setTestResults((current) => ({ ...current, [id]: { ok: false, message: error instanceof ApiError ? error.message : '连接测试失败' } })),
  })

  const discoverMutation = useMutation({
    mutationFn: (id: string) => api.providerModels(id).then((result) => ({ id, result })),
    onMutate: (id) => setDiscoveryErrors((current) => ({ ...current, [id]: '' })),
    onSuccess: ({ id, result }) => {
      setDiscovered((current) => ({ ...current, [id]: result.items.map((item) => item.id) }))
      setDiscoveryErrors((current) => ({ ...current, [id]: '' }))
    },
    onError: (error, id) => setDiscoveryErrors((current) => ({
      ...current,
      [id]: error instanceof ApiError ? error.message : '模型发现失败',
    })),
  })

  const formDiscoveryMutation = useMutation({
    mutationFn: (values: ProviderValues) => api.discoverProviderModels({
      kind: values.kind,
      protocol: values.protocol,
      base_url: values.base_url.trim(),
      api_keys: parseApiKeys(values.api_keys),
      timeout_seconds: Number.isFinite(values.timeout_seconds) ? values.timeout_seconds : defaults.timeout_seconds,
    }),
    onMutate: () => setFormDiscoveryError(''),
    onSuccess: (result) => {
      setFormDiscovered(result.items.map((item) => item.id))
      setFormDiscoveryError('')
    },
    onError: (error) => setFormDiscoveryError(error instanceof ApiError ? error.message : '模型发现失败'),
  })

  const startCreate = () => {
    setEditingId(undefined)
    setFormDiscovered(undefined)
    setFormDiscoveryError('')
    form.reset(defaults)
    setShowForm(true)
  }
  const startEdit = (provider: ProviderProfile) => {
    setEditingId(provider.id)
    setFormDiscovered(undefined)
    setFormDiscoveryError('')
    const kind: ConfigurableProviderKind = provider.kind === 'lmstudio' || provider.kind === 'antigravity' ? 'custom' : provider.kind
    const protocol: ProviderProtocol = provider.protocol ?? (provider.kind === 'gemini' || provider.kind === 'antigravity' ? 'gemini' : provider.kind === 'claude' ? 'claude' : 'openai')
    form.reset({
      name: provider.name, kind, protocol, base_url: provider.base_url, primary_model: provider.primary_model,
      fallback_model: provider.fallback_model ?? '', temperature: provider.temperature, top_p: provider.top_p,
      top_k: provider.top_k ?? undefined, max_tokens: provider.max_tokens, timeout_seconds: provider.timeout_seconds,
      retries: provider.retries, api_keys: '', enabled: provider.enabled !== false,
    })
    setShowForm(true)
  }

  const applyPreset = (kind: ConfigurableProviderKind) => {
    form.setValue('kind', kind)
    form.setValue('protocol', kind === 'custom' ? form.getValues('protocol') : kind)
    form.setValue('base_url', presets[kind].base_url, { shouldValidate: false, shouldDirty: true })
    form.setValue('primary_model', presets[kind].primary_model, { shouldDirty: true })
    if (!form.getValues('name')) form.setValue('name', providerKindName(kind))
  }
  const requestDelete = (provider: ProviderProfile) => {
    // Avoid stacking focus traps when deletion starts inside the edit drawer.
    setShowForm(false)
    setDeleteTarget(provider)
  }
  const formBaseUrl = form.watch('base_url')
  const formApiKeys = form.watch('api_keys')
  const hasFormApiKeys = parseApiKeys(formApiKeys).length > 0
  const usesTemporaryDiscovery = !editingId || hasFormApiKeys
  const editingModels = usesTemporaryDiscovery ? formDiscovered : discovered[editingId]
  const editingDiscoveryError = usesTemporaryDiscovery ? formDiscoveryError : discoveryErrors[editingId]
  const editingDiscoveryPending = formDiscoveryMutation.isPending || Boolean(
    editingId && discoverMutation.isPending && discoverMutation.variables === editingId,
  )
  const discoverFromForm = () => {
    const values = form.getValues()
    // A key entered in the editor must be usable before the profile is saved.
    // With no new key, an existing profile can use its credential-store entry.
    if (usesTemporaryDiscovery) formDiscoveryMutation.mutate(values)
    else discoverMutation.mutate(editingId)
  }

  return <div className="page page-providers">
    <div className="page-heading"><div><p className="eyebrow">VISION GATEWAYS</p><h1>在线模型</h1><p className="page-subtitle">管理模型网关、主备模型和受保护的 API 密钥池。</p></div><Button aria-label="添加 Provider" icon={<Plus size={16} />} onClick={startCreate}>添加 Provider</Button></div>
    {notice && <Notice tone={notice.tone}>{notice.text}<IconButton label="关闭" onClick={() => setNotice(null)}><X size={14} /></IconButton></Notice>}
    <Panel title="Provider 列表" eyebrow={`${providerQuery.data?.items.length ?? 0} CONFIGURED`} actions={<IconButton label="刷新" onClick={() => providerQuery.refetch()}><RefreshCw size={16} /></IconButton>}>
      {providerQuery.isLoading ? <div className="loading-block"><LoaderCircle className="spin" />加载配置…</div> : providerQuery.isError ? <EmptyState icon={<CircleX size={22} />} title="无法读取 Provider" action={<Button variant="secondary" size="sm" onClick={() => providerQuery.refetch()}>重试</Button>} /> : !(providerQuery.data?.items.length) ? <EmptyState icon={<Cloud size={23} />} title="还没有在线模型" action={<Button size="sm" icon={<Plus size={14} />} onClick={startCreate}>添加 Provider</Button>} /> : <div className="provider-table">
        <div className="provider-table-head"><span>Provider</span><span>主模型</span><span>连接参数</span><span>状态</span><span aria-label="操作" /></div>
        {providerQuery.data.items.map((provider) => {
          const test = testResults[provider.id]
          const models = discovered[provider.id]
          return <div className="provider-group" key={provider.id}>
            <div className="provider-row">
              <div className="provider-identity"><span className={`provider-logo provider-${provider.kind}`}><ProviderIcon kind={provider.kind} /></span><div><strong>{provider.name}</strong><small>{providerKindName(provider.kind)} · {maskedUrl(provider.base_url)}</small></div></div>
              <div className="provider-model"><strong>{provider.primary_model}</strong>{provider.fallback_model && <small>备用：{provider.fallback_model}</small>}</div>
              <div className="provider-limits"><span>{provider.timeout_seconds}s 超时</span><span>{provider.retries} 次重试</span></div>
              <div className="provider-state"><StatusBadge state={provider.configured && provider.enabled !== false ? 'succeeded' : 'interrupted'} /><small>{provider.configured ? `密钥 ${provider.key_hint ?? '已配置'}` : '未配置密钥'}</small></div>
              <div className="provider-actions"><IconButton label="编辑配置" onClick={() => startEdit(provider)}><Pencil size={15} /></IconButton><IconButton label="设置密钥" onClick={() => setSecretProvider(provider)}><KeyRound size={15} /></IconButton><IconButton label="测试连接" disabled={testMutation.isPending && testMutation.variables === provider.id} onClick={() => testMutation.mutate(provider.id)}>{testMutation.isPending && testMutation.variables === provider.id ? <LoaderCircle className="spin" size={15} /> : <TestTube2 size={15} />}</IconButton><IconButton label="获取可用模型" disabled={discoverMutation.isPending && discoverMutation.variables === provider.id} onClick={() => discoverMutation.mutate(provider.id)}>{discoverMutation.isPending && discoverMutation.variables === provider.id ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}</IconButton><IconButton label="删除 Provider" variant="danger" disabled={deleteMutation.isPending} onClick={() => requestDelete(provider)}>{deleteMutation.isPending && deleteMutation.variables?.id === provider.id ? <LoaderCircle className="spin" size={15} /> : <Trash2 size={15} />}</IconButton></div>
            </div>
            {(test || models) && <div className={`provider-detail ${test?.ok === false ? 'provider-detail-error' : ''}`}>
              {test && <span>{test.ok ? <Check size={14} /> : <CircleX size={14} />}{test.message}{test.latency != null && ` · ${test.latency} ms`}</span>}
              {models && <div className="model-chip-list">{models.length ? models.map((model) => <button key={model} onClick={() => { startEdit(provider); form.setValue('primary_model', model) }}>{model}</button>) : <span>未发现模型</span>}</div>}
            </div>}
          </div>
        })}
      </div>}
    </Panel>

    <Panel title="提示词模板" eyebrow="ONLINE OUTPUT PROFILES" className="provider-prompt-panel">
      <PromptEditors prompts={onlinePrompts} onChange={onlinePrompts.setPrompts} onReset={onlinePrompts.reset} />
    </Panel>

    {showForm && <DialogLayer onClose={() => setShowForm(false)}><aside className="drawer" role="dialog" aria-modal="true" aria-labelledby="provider-form-title">
      <header className="drawer-header"><div><p className="eyebrow">PROVIDER PROFILE</p><h2 id="provider-form-title">{editingId ? '编辑 Provider' : '添加 Provider'}</h2></div><IconButton label="关闭" onClick={() => setShowForm(false)}><X size={18} /></IconButton></header>
      <form className="drawer-body" onSubmit={form.handleSubmit((values) => saveMutation.mutate(values))}>
        <div className="provider-preset-grid">{(['custom', 'openai', 'gemini', 'claude'] as ConfigurableProviderKind[]).map((kind) => <button type="button" key={kind} className={selectedKind === kind ? 'preset-active' : ''} onClick={() => applyPreset(kind)}><ProviderIcon kind={kind} /><span>{providerKindName(kind)}</span></button>)}</div>
        <div className="form-grid two-columns">
          <Field label="名称" error={form.formState.errors.name?.message}><input {...form.register('name')} placeholder="例如 Gemini 主账号" /></Field>
          <Field label="连接类型"><select {...form.register('kind')} onChange={(event) => applyPreset(event.target.value as ConfigurableProviderKind)}><option value="custom">自定义 API</option><option value="openai">OpenAI 官方</option><option value="gemini">Gemini 官方</option><option value="claude">Claude 官方</option></select></Field>
          {selectedKind === 'custom' && <div className="field-span-2"><Field label="兼容协议" hint="选择网关实际兼容的请求格式"><select {...form.register('protocol')}><option value="openai">OpenAI / NewAPI 兼容</option><option value="gemini">Gemini generateContent 兼容</option><option value="claude">Claude Messages 兼容</option></select></Field></div>}
          <div className="field-span-2"><Field label="Base URL" error={form.formState.errors.base_url?.message}><input {...form.register('base_url')} spellCheck={false} /></Field></div>
          <div className="provider-primary-control"><Field label="主模型" error={form.formState.errors.primary_model?.message}><input {...form.register('primary_model')} /></Field><Button type="button" size="sm" variant="secondary" icon={editingDiscoveryPending ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />} disabled={!formBaseUrl?.trim() || editingDiscoveryPending} title={formBaseUrl?.trim() ? '从 Provider 获取可用模型' : '请先填写 Base URL'} onClick={discoverFromForm}>获取可用模型</Button></div>
          <Field label="备用模型"><input {...form.register('fallback_model')} placeholder="可选" /></Field>
          <div className="field-span-2"><Field label="API Key / 密钥池" hint={editingId ? '留空则保留当前密钥；填写后将替换。多个密钥可用换行或逗号分隔。' : '可填写一个或多个密钥，多个密钥使用换行或逗号分隔。'} error={form.formState.errors.api_keys?.message}><textarea {...form.register('api_keys')} rows={3} spellCheck={false} autoComplete="off" placeholder={editingId && providerQuery.data?.items.find((item) => item.id === editingId)?.configured ? '已配置密钥，留空不修改' : '输入 API Key'} /></Field></div>
          {(editingDiscoveryPending || editingDiscoveryError || editingModels) && <div className="field-span-2 provider-discovery" aria-live="polite">
            {editingDiscoveryPending ? <span className="provider-discovery-status"><LoaderCircle className="spin" size={14} />正在获取可用模型</span> : editingDiscoveryError ? <span className="field-error">{editingDiscoveryError}</span> : editingModels?.length ? <Field label={`可用模型 (${editingModels.length})`}><select aria-label="可用模型" value="" onChange={(event) => { if (event.target.value) form.setValue('primary_model', event.target.value, { shouldDirty: true, shouldValidate: true }) }}><option value="">选择并填入主模型</option>{editingModels.map((model) => <option value={model} key={model}>{model}</option>)}</select></Field> : <span className="provider-discovery-status">未发现可用模型</span>}
          </div>}
        </div>
        <div className="form-divider"><span>生成参数</span></div>
        <div className="form-grid three-columns">
          <Field label="Temperature"><input type="number" step="0.05" {...form.register('temperature', { valueAsNumber: true })} /></Field>
          <Field label="Top P"><input type="number" step="0.05" {...form.register('top_p', { valueAsNumber: true })} /></Field>
          <Field label="Top K"><input type="number" {...form.register('top_k', { setValueAs: (value) => value === '' ? undefined : Number(value) })} /></Field>
          <Field label="最大 Token"><input type="number" {...form.register('max_tokens', { valueAsNumber: true })} /></Field>
          <Field label="超时（秒）"><input type="number" {...form.register('timeout_seconds', { valueAsNumber: true })} /></Field>
          <Field label="重试次数"><input type="number" {...form.register('retries', { valueAsNumber: true })} /></Field>
          <Field label="启用"><label className="toggle standalone"><input type="checkbox" {...form.register('enabled')} /><span />接收新任务</label></Field>
        </div>
        <div className="drawer-actions">{editingId && <Button type="button" variant="danger" icon={<Trash2 size={15} />} disabled={deleteMutation.isPending} onClick={() => { const provider = providerQuery.data?.items.find((item) => item.id === editingId); if (provider) requestDelete(provider) }}>删除</Button>}<span className="drawer-actions-spacer" /><Button type="button" variant="secondary" onClick={() => setShowForm(false)}>取消</Button><Button type="submit" icon={saveMutation.isPending ? <LoaderCircle size={15} className="spin" /> : <Save size={15} />} disabled={saveMutation.isPending}>{editingId ? '保存修改' : '创建 Provider'}</Button></div>
      </form>
    </aside></DialogLayer>}

    {deleteTarget && <ConfirmDialog
      title={`删除 ${deleteTarget.name}？`}
      detail={<>此操作会删除 Provider 配置，并从凭据存储中移除关联密钥。删除后无法撤销。</>}
      confirmLabel="删除 Provider"
      busy={deleteMutation.isPending}
      onClose={() => setDeleteTarget(undefined)}
      onConfirm={() => deleteMutation.mutate(deleteTarget)}
    />}

    {secretProvider && <DialogLayer onClose={() => setSecretProvider(undefined)}><div className="secret-dialog" role="dialog" aria-modal="true" aria-labelledby="secret-title">
      <header><span className="dialog-icon"><KeyRound size={20} /></span><div><p className="eyebrow">CREDENTIAL VAULT</p><h2 id="secret-title">{secretProvider.name} 密钥池</h2></div><IconButton label="关闭" onClick={() => setSecretProvider(undefined)}><X size={17} /></IconButton></header>
      <div className="dialog-body"><Notice tone="info">密钥提交后不会再次显示。每行填写一个密钥。</Notice><Field label="API Keys"><textarea value={keys} onChange={(event) => setKeys(event.target.value)} rows={6} spellCheck={false} autoComplete="off" placeholder="••••••••••••••••" /></Field></div>
      <footer><Button variant="secondary" onClick={() => setSecretProvider(undefined)}>取消</Button><Button icon={secretMutation.isPending ? <LoaderCircle size={15} className="spin" /> : <KeyRound size={15} />} disabled={!keys.trim() || secretMutation.isPending} onClick={() => secretMutation.mutate()}>保存密钥</Button></footer>
    </div></DialogLayer>}
  </div>
}

function providerKindName(kind: ProviderKind): string {
  return { custom: '自定义 API', gemini: 'Gemini 官方', openai: 'OpenAI 官方', claude: 'Claude 官方', lmstudio: 'LM Studio（兼容）', antigravity: 'Antigravity（兼容）' }[kind]
}
function maskedUrl(url: string): string {
  try { const parsed = new URL(url); return `${parsed.protocol}//${parsed.host}` } catch { return '地址不可用' }
}
function parseApiKeys(value?: string): string[] {
  return Array.from(new Set((value ?? '').split(/\r?\n|,/).map((key) => key.trim()).filter(Boolean)))
}
function ProviderIcon({ kind }: { kind: ProviderKind }) {
  return kind === 'custom' || kind === 'lmstudio' || kind === 'antigravity' ? <Braces size={17} /> : kind === 'gemini' ? <Sparkles size={17} /> : kind === 'openai' ? <Bot size={17} /> : <Cloud size={17} />
}
