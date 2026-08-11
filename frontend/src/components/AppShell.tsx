import { useQuery } from '@tanstack/react-query'
import { Activity, Boxes, ChevronLeft, ChevronRight, Clapperboard, Cpu, Database, Menu, Network, PanelLeftClose, Settings, Tags, X } from 'lucide-react'
import { useEffect } from 'react'
import { api } from '../lib/api'
import { usePreferences } from '../store/app'
import type { AppPage } from '../types'
import { IconButton } from './ui'

const navItems: Array<{ id: AppPage; label: string; icon: typeof Tags; hint: string }> = [
  { id: 'workbench', label: '工作台', icon: Tags, hint: '单图与快速队列' },
  { id: 'video-prompts', label: '视频提示词', icon: Clapperboard, hint: '图生视频提示词工作区' },
  { id: 'batch', label: '批量任务', icon: Database, hint: '目录扫描与历史' },
  { id: 'providers', label: '在线模型', icon: Network, hint: 'Provider 与密钥' },
  { id: 'models', label: '本地模型', icon: Cpu, hint: '模型、Adapter、阈值' },
  { id: 'settings', label: '设置', icon: Settings, hint: '路径与运行策略' },
]

export function AppShell({ children }: { children: React.ReactNode }) {
  const { page, setPage, sidebarOpen, setSidebarOpen, compact, setCompact } = usePreferences()
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, retry: false, refetchInterval: 30_000 })

  useEffect(() => {
    document.title = `${navItems.find((item) => item.id === page)?.label ?? '工作台'} · Tagger2`
  }, [page])

  return <div className={`app-shell ${compact ? 'app-compact' : ''}`}>
    <div className={`sidebar-backdrop ${sidebarOpen ? 'is-visible' : ''}`} onClick={() => setSidebarOpen(false)} aria-hidden="true" />
    <aside className={`sidebar ${sidebarOpen ? 'sidebar-open' : ''}`} aria-label="主导航">
      <div className="brand-row">
        <div className="brand-mark"><Boxes size={19} aria-hidden="true" /></div>
        <div className="brand-name"><strong>Tagger2</strong><span>INFERENCE</span></div>
        <IconButton label="关闭导航" onClick={() => setSidebarOpen(false)} className="mobile-only"><X size={17} /></IconButton>
      </div>
      <div className="sidebar-section-label">工作区</div>
      <nav className="main-nav">
        {navItems.map(({ id, label, icon: Icon, hint }) => <button key={id} className={`nav-item ${page === id ? 'nav-item-active' : ''}`} onClick={() => setPage(id)} title={hint}>
          <Icon size={18} aria-hidden="true" /><span>{label}</span><ChevronRight className="nav-chevron" size={14} aria-hidden="true" />
        </button>)}
      </nav>
      <div className="sidebar-footer">
        <div className={`connection-status ${health.isSuccess ? 'connection-online' : health.isPending ? '' : 'connection-offline'}`}>
          <span className="connection-dot" aria-hidden="true" />
          <span>{health.isSuccess ? '服务在线' : health.isPending ? '连接中' : '服务离线'}</span>
          {health.data?.version && <small>v{health.data.version}</small>}
        </div>
        <button className="collapse-button" onClick={() => setCompact(!compact)} aria-label={compact ? '展开侧栏' : '收起侧栏'} title={compact ? '展开侧栏' : '收起侧栏'}>
          {compact ? <ChevronRight size={16} /> : <><PanelLeftClose size={16} /><span>收起侧栏</span><ChevronLeft size={14} /></>}
        </button>
      </div>
    </aside>
    <main className="main-area">
      <header className="topbar">
        <IconButton label="打开导航" onClick={() => setSidebarOpen(true)} className="mobile-only"><Menu size={19} /></IconButton>
        <div className="breadcrumb"><span>Tagger2</span><ChevronRight size={14} /><strong>{navItems.find((item) => item.id === page)?.label}</strong></div>
        <div className="topbar-actions"><div className="live-indicator"><Activity size={14} aria-hidden="true" /><span>本地工作区</span></div></div>
      </header>
      <div className="page-content">{children}</div>
    </main>
  </div>
}
