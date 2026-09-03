import { AlertTriangle, LoaderCircle, RefreshCw } from 'lucide-react'
import { Component, lazy, Suspense, type ErrorInfo, type ReactNode } from 'react'
import { AppShell } from './components/AppShell'
import { Button } from './components/ui'
import { usePreferences } from './store/app'

const Workbench = lazy(() => import('./pages/Workbench').then((module) => ({ default: module.Workbench })))
const ImageGeneration = lazy(() => import('./pages/ImageGeneration').then((module) => ({ default: module.ImageGeneration })))
const VideoPrompts = lazy(() => import('./pages/VideoPrompts').then((module) => ({ default: module.VideoPrompts })))
const BatchJobs = lazy(() => import('./pages/BatchJobs').then((module) => ({ default: module.BatchJobs })))
const DatasetWorkflow = lazy(() => import('./pages/DatasetWorkflow').then((module) => ({ default: module.DatasetWorkflow })))
const TagManager = lazy(() => import('./pages/TagManager').then((module) => ({ default: module.TagManager })))
const TagWiki = lazy(() => import('./pages/TagWiki').then((module) => ({ default: module.TagWiki })))
const Providers = lazy(() => import('./pages/Providers').then((module) => ({ default: module.Providers })))
const Models = lazy(() => import('./pages/Models').then((module) => ({ default: module.Models })))
const Settings = lazy(() => import('./pages/Settings').then((module) => ({ default: module.Settings })))

export default function App() {
  const page = usePreferences((state) => state.page)
  return <ErrorBoundary><AppShell><Suspense fallback={<div className="page-loading"><LoaderCircle className="spin" size={23} /><span>正在打开工作区…</span></div>}>
    {page === 'workbench' && <Workbench />}
    {page === 'image-generation' && <ImageGeneration />}
    {page === 'video-prompts' && <VideoPrompts />}
    {page === 'batch' && <BatchJobs />}
    {page === 'dataset-workflow' && <DatasetWorkflow />}
    {page === 'tag-manager' && <TagManager />}
    {page === 'tag-wiki' && <TagWiki />}
    {page === 'providers' && <Providers />}
    {page === 'models' && <Models />}
    {page === 'settings' && <Settings />}
  </Suspense></AppShell></ErrorBoundary>
}

class ErrorBoundary extends Component<{ children: ReactNode }, { error?: Error }> {
  state: { error?: Error } = {}
  static getDerivedStateFromError(error: Error) { return { error } }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('UI error', error, info.componentStack) }
  render() {
    if (!this.state.error) return this.props.children
    return <div className="fatal-error"><AlertTriangle size={30} /><h1>界面发生错误</h1><p>{this.state.error.message}</p><Button icon={<RefreshCw size={15} />} onClick={() => window.location.reload()}>重新载入</Button></div>
  }
}
