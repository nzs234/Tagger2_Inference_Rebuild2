import { useQuery } from '@tanstack/react-query'
import { Activity, Boxes, ChevronLeft, ChevronRight, Clapperboard, Cpu, Database, ImagePlus, Menu, MonitorCog, Moon, Network, PanelLeftClose, Settings, Sun, Tag, Tags, Workflow, X } from 'lucide-react'
import { useEffect, useMemo, useRef } from 'react'
import { api } from '../lib/api'
import { usePreferences } from '../store/app'
import type { AppPage } from '../types'
import { IconButton } from './ui'

type NavItem = { id: AppPage; label: string; icon: typeof Tags; hint: string }

const navGroups: Array<{ label: string; items: NavItem[] }> = [
  {
    label: '创作与处理',
    items: [
      { id: 'workbench', label: '工作台', icon: Tags, hint: '单图与快速队列' },
      { id: 'image-generation', label: '图像生成', icon: ImagePlus, hint: 'Grok、Nano Banana 与 GPT Image' },
      { id: 'video-prompts', label: '视频提示词', icon: Clapperboard, hint: '图生视频提示词工作区' },
      { id: 'batch', label: '批量任务', icon: Database, hint: '目录扫描与历史' },
      { id: 'dataset-workflow', label: '数据集工作流', icon: Workflow, hint: '事务化标注流水线' },
      { id: 'tag-manager', label: '标签管理', icon: Tag, hint: '数据集标签批量编辑' },
    ],
  },
  {
    label: '资源与系统',
    items: [
      { id: 'providers', label: '在线模型', icon: Network, hint: 'Provider 与密钥' },
      { id: 'models', label: '本地模型', icon: Cpu, hint: '模型、Adapter、阈值' },
      { id: 'settings', label: '设置', icon: Settings, hint: '路径与运行策略' },
    ],
  },
]

const navItems = navGroups.flatMap((group) => group.items)

export function AppShell({ children }: { children: React.ReactNode }) {
  const { page, setPage, sidebarOpen, setSidebarOpen, compact, setCompact, themeMode, setThemeMode } = usePreferences()
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, retry: false, refetchInterval: 30_000 })

  const resolvedTheme = useMemo(() => {
    if (themeMode !== 'system') return themeMode
    return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  }, [themeMode])

  useEffect(() => {
    document.title = `${navItems.find((item) => item.id === page)?.label ?? '工作台'} · Tagger2`
  }, [page])

  const previousSidebarFocus = useRef<HTMLElement | null>(null)

  useEffect(() => {
    const root = document.documentElement
    const themeColor = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')
    const applyTheme = (theme: 'light' | 'dark') => {
      root.dataset.theme = theme
      themeColor?.setAttribute('content', theme === 'dark' ? '#0d0f19' : '#f4f6fb')
    }
    applyTheme(resolvedTheme)
    if (themeMode !== 'system' || !window.matchMedia) return
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const update = () => applyTheme(media.matches ? 'dark' : 'light')
    media.addEventListener?.('change', update)
    return () => media.removeEventListener?.('change', update)
  }, [resolvedTheme, themeMode])

  useEffect(() => {
    if (!sidebarOpen) return
    previousSidebarFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const sidebar = document.getElementById('app-sidebar')
    const main = document.querySelector<HTMLElement>('.main-area')
    main?.setAttribute('inert', '')
    sidebar?.querySelector<HTMLElement>('[data-sidebar-close]')?.focus()

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        setSidebarOpen(false)
        return
      }
      if (event.key !== 'Tab' || !sidebar) return
      const focusable = Array.from(sidebar.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'))
        .filter((element) => {
          const style = window.getComputedStyle(element)
          return !element.hidden && style.display !== 'none' && style.visibility !== 'hidden'
        })
      if (!focusable.length) return
      const first = focusable[0]!
      const last = focusable[focusable.length - 1]!
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      main?.removeAttribute('inert')
      previousSidebarFocus.current?.focus()
      previousSidebarFocus.current = null
    }
  }, [setSidebarOpen, sidebarOpen])

  const cycleTheme = () => {
    setThemeMode(themeMode === 'light' ? 'dark' : themeMode === 'dark' ? 'system' : 'light')
  }
  const themeLabel = themeMode === 'light' ? '浅色主题' : themeMode === 'dark' ? '深色主题' : '跟随系统主题'

  return <div className={`app-shell ${compact ? 'app-compact' : ''}`}>
    <div className={`sidebar-backdrop ${sidebarOpen ? 'is-visible' : ''}`} onClick={() => setSidebarOpen(false)} aria-hidden="true" />
    <aside id="app-sidebar" className={`sidebar ${sidebarOpen ? 'sidebar-open' : ''}`} aria-label="主导航">
      <div className="brand-row">
        <div className="brand-mark"><Boxes size={19} aria-hidden="true" /></div>
        <div className="brand-name"><strong>Tagger2</strong><span>INFERENCE</span></div>
        <IconButton label="关闭导航" data-sidebar-close onClick={() => setSidebarOpen(false)} className="mobile-only"><X size={17} /></IconButton>
      </div>
      <div className="sidebar-navigation">
        {navGroups.map((group) => <section className="nav-group" key={group.label}>
          <div className="sidebar-section-label">{group.label}</div>
          <nav className="main-nav" aria-label={group.label}>
            {group.items.map(({ id, label, icon: Icon, hint }) => <button key={id} className={`nav-item ${page === id ? 'nav-item-active' : ''}`} onClick={() => { setPage(id); setSidebarOpen(false) }} title={hint} aria-current={page === id ? 'page' : undefined}>
              <Icon size={18} aria-hidden="true" /><span className="nav-copy"><strong>{label}</strong><small>{hint}</small></span><ChevronRight className="nav-chevron" size={14} aria-hidden="true" />
            </button>)}
          </nav>
        </section>)}
      </div>
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
        <IconButton label="打开导航" data-sidebar-open aria-expanded={sidebarOpen} aria-controls="app-sidebar" onClick={() => setSidebarOpen(true)} className="mobile-only"><Menu size={19} /></IconButton>
        <div className="breadcrumb"><span>Tagger2</span><ChevronRight size={14} /><strong>{navItems.find((item) => item.id === page)?.label}</strong></div>
        <div className="topbar-actions">
          <div className="live-indicator"><Activity size={14} aria-hidden="true" /><span>本地工作区</span></div>
          <button type="button" className="theme-switch" onClick={cycleTheme} aria-label={themeLabel} title={themeLabel}>
            {themeMode === 'light' ? <Sun size={16} aria-hidden="true" /> : themeMode === 'dark' ? <Moon size={16} aria-hidden="true" /> : <MonitorCog size={16} aria-hidden="true" />}
            <span>{themeLabel}</span>
          </button>
        </div>
      </header>
      <div className="page-content">{children}</div>
    </main>
  </div>
}
