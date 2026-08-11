import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, FolderPlus, HardDrive, KeyRound, LoaderCircle, LockKeyhole, Network, Save, ShieldCheck, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useForm } from 'react-hook-form'
import { z } from 'zod'
import { Button, EmptyState, Field, IconButton, Notice, Panel } from '../components/ui'
import { api, ApiError } from '../lib/api'
import type { RootInfo, RuntimeSettings } from '../types'

const settingsSchema = z.object({
  input_root_id: z.string(), output_root_id: z.string(), default_mode: z.enum(['local', 'online']), default_threshold: z.number().min(0).max(1),
  default_json: z.boolean(), default_txt: z.boolean(), bind_host: z.string().min(1), lan_enabled: z.boolean(),
  access_token_configured: z.boolean(), production: z.boolean(), max_upload_mb: z.number().int().min(1).max(2048), max_image_pixels: z.number().int().min(1_000_000).max(200_000_000),
})
type SettingsValues = z.infer<typeof settingsSchema>

const fallback: RuntimeSettings = { input_root_id: '', output_root_id: '', default_mode: 'online', default_threshold: 0.35, default_json: true, default_txt: false, bind_host: '127.0.0.1', lan_enabled: false, access_token_configured: false, production: true, max_upload_mb: 50, max_image_pixels: 40_000_000 }

export function Settings() {
  const queryClient = useQueryClient()
  const settingsQuery = useQuery({ queryKey: ['settings'], queryFn: api.settings, retry: false })
  const rootsQuery = useQuery({ queryKey: ['roots'], queryFn: api.roots, staleTime: 60_000, retry: false })
  const [token, setToken] = useState('')
  const [rootName, setRootName] = useState('')
  const [rootPath, setRootPath] = useState('')
  const [rootKind, setRootKind] = useState<'input' | 'output' | 'model'>('input')
  const [notice, setNotice] = useState<{ tone: 'success' | 'danger' | 'warning' | 'info'; text: string } | null>(null)
  const form = useForm<SettingsValues>({ resolver: zodResolver(settingsSchema), defaultValues: fallback })
  const settings = useMemo(() => normalizeSettings(settingsQuery.data ?? fallback), [settingsQuery.data])
  useEffect(() => { form.reset(settings) }, [form, settings])

  const saveMutation = useMutation({
    mutationFn: (values: SettingsValues) => api.saveSettings(values),
    onSuccess: () => { setNotice({ tone: 'success', text: '运行设置已保存' }); void queryClient.invalidateQueries({ queryKey: ['settings'] }) },
    onError: (error) => setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '设置保存失败' }),
  })
  const rootMutation = useMutation({
    mutationFn: () => api.addRoot({ name: rootName.trim(), path: rootPath.trim(), kind: rootKind }),
    onSuccess: () => { setNotice({ tone: 'success', text: '允许目录已添加' }); setRootName(''); setRootPath(''); void queryClient.invalidateQueries({ queryKey: ['roots'] }) },
    onError: (error) => setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : '目录添加失败' }),
  })

  const storeToken = () => {
    if (!token.trim()) return
    sessionStorage.setItem('tagger2_access_token', token.trim())
    setToken('')
    setNotice({ tone: 'success', text: '本次会话已启用访问令牌' })
  }
  const roots = rootsQuery.data?.items ?? []

  return <div className="page page-settings">
    <div className="page-heading"><div><p className="eyebrow">RUNTIME CONTROL</p><h1>设置</h1><p className="page-subtitle">路径、网络边界和默认任务参数。</p></div><span className="settings-lock"><LockKeyhole size={15} />仅本机配置</span></div>
    {notice && <Notice tone={notice.tone}>{notice.text}<IconButton label="关闭" onClick={() => setNotice(null)}><X size={14} /></IconButton></Notice>}
    <form onSubmit={form.handleSubmit((values) => saveMutation.mutate(values))}>
      <Panel title="默认任务" eyebrow="01 / DEFAULTS">
        <div className="form-grid four-columns">
          <Field label="默认模式"><select {...form.register('default_mode')}><option value="online">在线模型</option><option value="local">本地模型</option></select></Field>
          <Field label={`统一阈值备用值 ${(form.watch('default_threshold') ?? 0).toFixed(2)}`}><input type="range" min="0" max="1" step="0.01" {...form.register('default_threshold', { valueAsNumber: true })} /></Field>
          <Field label="输入根目录"><select {...form.register('input_root_id')}><option value="">未设置</option>{roots.filter((root) => root.kind === 'input').map((root) => <option key={root.id} value={root.id}>{root.name}</option>)}</select></Field>
          <Field label="输出根目录"><select {...form.register('output_root_id')}><option value="">源目录</option>{roots.filter((root) => root.kind === 'output').map((root) => <option key={root.id} value={root.id}>{root.name}</option>)}</select></Field>
          <Field label="输出格式"><div className="toggle-row"><label className="toggle"><input type="checkbox" {...form.register('default_json')} /><span />JSON</label><label className="toggle"><input type="checkbox" {...form.register('default_txt')} /><span />TXT</label></div></Field>
        </div>
      </Panel>
      <Panel title="允许目录" eyebrow="02 / PATH ALLOWLIST" actions={<span className="panel-count">{roots.length} 个目录</span>}>
        <div className="root-list">{roots.length ? roots.map((root) => <RootRow root={root} key={root.id} />) : <EmptyState icon={<HardDrive size={22} />} title="还没有允许目录" detail="服务端不会读取未注册路径" />}</div>
        <div className="add-root-row"><Field label="名称"><input value={rootName} onChange={(event) => setRootName(event.target.value)} placeholder="训练集图片" /></Field><Field label="服务器路径"><input value={rootPath} onChange={(event) => setRootPath(event.target.value)} placeholder="D:\\datasets\\images" /></Field><Field label="用途"><select value={rootKind} onChange={(event) => setRootKind(event.target.value as typeof rootKind)}><option value="input">输入</option><option value="output">输出</option><option value="model">模型</option></select></Field><Button type="button" variant="secondary" icon={rootMutation.isPending ? <LoaderCircle size={15} className="spin" /> : <FolderPlus size={15} />} disabled={!rootName.trim() || !rootPath.trim() || rootMutation.isPending} onClick={() => rootMutation.mutate()}>添加目录</Button></div>
      </Panel>
      <Panel title="网络与安全" eyebrow="03 / BOUNDARY">
        <div className="security-layout">
          <div className="security-options"><Field label="监听地址"><select {...form.register('bind_host')}><option value="127.0.0.1">127.0.0.1（仅本机）</option><option value="0.0.0.0">0.0.0.0（局域网）</option></select></Field><Field label="上传上限（MB）"><input type="number" {...form.register('max_upload_mb', { valueAsNumber: true })} /></Field><Field label="解码像素上限"><input type="number" {...form.register('max_image_pixels', { valueAsNumber: true })} /></Field></div>
          <div className="security-toggles"><label className="toggle large-toggle"><input type="checkbox" {...form.register('lan_enabled')} /><span /><div><strong>允许局域网访问</strong><small>启用时必须配置访问令牌</small></div></label><label className="toggle large-toggle"><input type="checkbox" {...form.register('production')} /><span /><div><strong>生产模式</strong><small>关闭 API 调试文档和详细错误</small></div></label><div className="token-status"><ShieldCheck size={17} /><span>{settings.access_token_configured ? '服务端令牌已配置' : '服务端未配置令牌'}</span></div></div>
        </div>
        <div className="session-token"><Field label="本次会话访问令牌" hint="只保存在当前浏览器会话，不会写入配置文件"><input type="password" value={token} onChange={(event) => setToken(event.target.value)} autoComplete="off" placeholder="输入令牌后点击启用" /></Field><Button type="button" variant="secondary" icon={<KeyRound size={15} />} disabled={!token.trim()} onClick={storeToken}>启用令牌</Button></div>
      </Panel>
      <div className="settings-actions"><span><Network size={15} />保存后新任务使用默认配置</span><Button type="submit" icon={saveMutation.isPending ? <LoaderCircle size={16} className="spin" /> : <Save size={16} />} disabled={saveMutation.isPending}>保存设置</Button></div>
    </form>
  </div>
}

function RootRow({ root }: { root: RootInfo }) {
  return <div className="root-row"><span className={`root-icon root-${root.kind}`}><HardDrive size={16} /></span><div><strong>{root.name}</strong><small>{root.path_hint ?? '服务器路径已隐藏'}</small></div><span className="root-kind">{root.kind === 'input' ? '输入' : root.kind === 'output' ? '输出' : '模型'}{root.writable === false && ' · 只读'}</span><Check size={15} className="root-check" /></div>
}

function normalizeSettings(value: RuntimeSettings): RuntimeSettings {
  return { ...fallback, ...value, input_root_id: value.input_root_id ?? '', output_root_id: value.output_root_id ?? '' }
}
