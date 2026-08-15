import { Clapperboard, Copy, Eye, FileText, Image as ImageIcon, LoaderCircle, RotateCcw, Send, Trash2, X } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { FileDropzone } from '../components/FileDropzone'
import { Button, ConfirmDialog, EmptyState, Field, IconButton, Notice, Panel } from '../components/ui'
import { api, ApiError } from '../lib/api'
import { usePreferences, useVideoPromptStore } from '../store/app'
import type { BilingualText, Fl2vaPromptPackage, Fl2vaSingleImageRole, H3BasePromptMode, Ref2vaPromptPackage, VideoPromptLanguage, VideoPromptMode, VideoPromptRevision } from '../types'

type VideoNotice = { tone: 'success' | 'warning' | 'danger' | 'info'; text: string }
type PendingConfirmation = { title: string; detail: string; confirmLabel: string; action: () => void }

export function VideoPrompts() {
  const setPage = usePreferences((state) => state.setPage)
  const images = useVideoPromptStore((state) => state.images)
  const providerId = useVideoPromptStore((state) => state.providerId)
  const providerModel = useVideoPromptStore((state) => state.providerModel)
  const promptMode = useVideoPromptStore((state) => state.promptMode)
  const fl2vaSingleImageRole = useVideoPromptStore((state) => state.fl2vaSingleImageRole)
  const instruction = useVideoPromptStore((state) => state.instruction)
  const displayLanguage = useVideoPromptStore((state) => state.displayLanguage)
  const revisions = useVideoPromptStore((state) => state.revisions)
  const currentRevisionId = useVideoPromptStore((state) => state.currentRevisionId)
  const viewedRevisionId = useVideoPromptStore((state) => state.viewedRevisionId)
  const isGenerating = useVideoPromptStore((state) => state.isGenerating)
  const error = useVideoPromptStore((state) => state.error)
  const addImages = useVideoPromptStore((state) => state.addImages)
  const removeImage = useVideoPromptStore((state) => state.removeImage)
  const limitImages = useVideoPromptStore((state) => state.limitImages)
  const setProvider = useVideoPromptStore((state) => state.setProvider)
  const setPromptMode = useVideoPromptStore((state) => state.setPromptMode)
  const setFl2vaSingleImageRole = useVideoPromptStore((state) => state.setFl2vaSingleImageRole)
  const setInstruction = useVideoPromptStore((state) => state.setInstruction)
  const setDisplayLanguage = useVideoPromptStore((state) => state.setDisplayLanguage)
  const beginGeneration = useVideoPromptStore((state) => state.beginGeneration)
  const completeGeneration = useVideoPromptStore((state) => state.completeGeneration)
  const failGeneration = useVideoPromptStore((state) => state.failGeneration)
  const viewRevision = useVideoPromptStore((state) => state.viewRevision)
  const restoreRevision = useVideoPromptStore((state) => state.restoreRevision)
  const clearRevisions = useVideoPromptStore((state) => state.clearRevisions)
  const clearTask = useVideoPromptStore((state) => state.clearTask)
  const clearError = useVideoPromptStore((state) => state.clearError)
  const [notice, setNotice] = useState<VideoNotice>()
  const [confirmation, setConfirmation] = useState<PendingConfirmation>()
  const controllerRef = useRef<AbortController | undefined>(undefined)

  const providers = useQuery({ queryKey: ['providers'], queryFn: api.providers, staleTime: 60_000 })
  const providerItems = useMemo(() => providers.data?.items ?? [], [providers.data?.items])
  const currentRevision = useMemo(
    () => revisions.find((revision) => revision.id === currentRevisionId),
    [currentRevisionId, revisions],
  )
  const viewedRevision = useMemo(
    () => revisions.find((revision) => revision.id === viewedRevisionId) ?? revisions.at(-1),
    [revisions, viewedRevisionId],
  )
  const imageLimit = promptMode === 'ref2va' ? 9 : 2
  const availableImageSlots = Math.max(0, imageLimit - images.length)
  const baseMode: H3BasePromptMode = promptMode === 'fl2va'
    ? images.length === 0
      ? 't2va'
      : images.length === 1
        ? fl2vaSingleImageRole === 'first' ? 'i2va' : 'l2va'
        : 'fl2va'
    : 't2va'

  useEffect(() => {
    const selected = providerItems.find((provider) => provider.id === providerId)
    if (selected || !providerItems[0]) return
    setProvider(providerItems[0].id, providerItems[0].primary_model)
  }, [providerId, providerItems, setProvider])

  const requestConfirmation = (confirmation: PendingConfirmation) => setConfirmation(confirmation)

  const selectImages = (files: File[]) => {
    const additions = files.slice(0, availableImageSlots)
    if (!additions.length) {
      setNotice({ tone: 'warning', text: `当前预设最多使用 ${imageLimit} 张参考图片。` })
      return
    }
    const apply = () => {
      controllerRef.current?.abort()
      addImages(additions)
      setNotice(files.length > additions.length
        ? { tone: 'warning', text: `仅添加了前 ${additions.length} 张，当前预设最多使用 ${imageLimit} 张。` }
        : undefined)
    }
    if (revisions.length) {
      requestConfirmation({
        title: '调整参考图片？',
        detail: '添加新的参考图片会清空当前提示词历史，已有版本将无法恢复。',
        confirmLabel: '添加并清空历史',
        action: apply,
      })
      return
    }
    apply()
  }

  const removeReferenceImage = (index: number) => {
    const apply = () => {
      controllerRef.current?.abort()
      removeImage(index)
      setNotice(undefined)
    }
    if (revisions.length) {
      requestConfirmation({
        title: '删除参考图片？',
        detail: '删除参考图片会同时清空当前提示词历史，已有版本将无法恢复。',
        confirmLabel: '删除并清空历史',
        action: apply,
      })
      return
    }
    apply()
  }

  const clear = () => {
    if (!images.length && !revisions.length) return
    requestConfirmation({
      title: '清空当前任务？',
      detail: '当前图片、需求对话和全部版本历史都会被永久清空。',
      confirmLabel: '确认清空',
      action: () => {
        controllerRef.current?.abort()
        controllerRef.current = undefined
        clearTask()
        setNotice(undefined)
      },
    })
  }

  const changePromptMode = (nextMode: VideoPromptMode) => {
    if (nextMode === promptMode) return
    const needsTrim = nextMode === 'fl2va' && images.length > 2
    const apply = () => {
      controllerRef.current?.abort()
      if (revisions.length) clearRevisions()
      if (needsTrim) limitImages(2)
      setPromptMode(nextMode)
      setNotice(undefined)
    }
    if (revisions.length || needsTrim) {
      requestConfirmation({
        title: '切换提示词预设？',
        detail: needsTrim
          ? 'FL2VA 最多支持 2 张图片。切换后只保留前两张参考图，并清空版本历史。'
          : '切换预设会保留参考图、Provider 和语言选择，但会清空版本历史。',
        confirmLabel: '确认切换',
        action: apply,
      })
      return
    }
    apply()
  }

  const changeSingleImageRole = (role: Fl2vaSingleImageRole) => {
    if (role === fl2vaSingleImageRole) return
    const apply = () => {
      controllerRef.current?.abort()
      if (revisions.length) clearRevisions()
      setFl2vaSingleImageRole(role)
      setNotice(undefined)
    }
    if (revisions.length) {
      requestConfirmation({
        title: '切换关键帧角色？',
        detail: '切换单图的首帧或末帧角色会清空当前版本历史。',
        confirmLabel: '确认切换',
        action: apply,
      })
      return
    }
    apply()
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if ((!images.length && promptMode === 'ref2va') || !providerId || !instruction.trim() || isGenerating) return
    const requestInstruction = instruction.trim()
    const requestToken = beginGeneration()
    const controller = new AbortController()
    controllerRef.current = controller
    try {
      const packageResult = await api.generateVideoPrompt({
        images: images.map((image) => image.file),
        providerId,
        providerModel,
        promptMode,
        fl2vaSingleImageRole,
        instruction: requestInstruction,
        currentPackage: currentRevision?.package,
      }, controller.signal)
      completeGeneration(requestToken, promptMode, requestInstruction, packageResult)
      setNotice({ tone: 'success', text: currentRevision ? '已基于当前版本生成新版本。' : '已生成第一版视频提示词。' })
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === 'AbortError') return
      const message = reason instanceof ApiError
        ? reason.message
        : reason instanceof Error
          ? reason.message
          : '生成失败，请稍后重试。'
      failGeneration(requestToken, message)
    } finally {
      if (controllerRef.current === controller) controllerRef.current = undefined
    }
  }

  const copy = async (value: string, label: string) => {
    try {
      await navigator.clipboard.writeText(value)
      setNotice({ tone: 'success', text: `已复制${label}。` })
    } catch {
      setNotice({ tone: 'danger', text: '浏览器未授予剪贴板权限。' })
    }
  }

  const canGenerate = Boolean((promptMode === 'fl2va' || images.length) && providerId && instruction.trim() && !isGenerating)

  return <div className="page page-video-prompts">
    <div className="page-heading">
      <div className="page-heading-copy"><p className="eyebrow">{promptMode === 'fl2va' ? `H3 ${baseMode.toUpperCase()} PROMPT DESK` : 'H3 REF2VA PROMPT DESK'}</p><h1>视频提示词</h1><p className="page-subtitle">{promptMode === 'fl2va' ? '支持无图、首帧、末帧和首尾帧的 H3 Base 提示词；图片数量决定官方模式。' : '以最多 9 张图片作为可复用视觉引用，连续迭代生成 H3 Ref2VA 六段式提示词。'}</p></div>
      <Button variant="danger" icon={<Trash2 size={16} />} aria-label="清空任务" title="清空任务" onClick={clear} disabled={!images.length && !revisions.length}>清空任务</Button>
    </div>

    {notice && <Notice tone={notice.tone}>{notice.text}<IconButton label="关闭提示" onClick={() => setNotice(undefined)}><X size={15} /></IconButton></Notice>}
    {error && <Notice tone="danger">{error}<IconButton label="关闭错误提示" onClick={clearError}><X size={15} /></IconButton></Notice>}

    <div className="video-prompts-grid">
      <Panel title={`参考图片 ${images.length}/${imageLimit}`} eyebrow="01 / SOURCE" className="video-source-panel">
        {images.length ? <div className="video-reference-grid">{images.map((image, index) => <article className="video-reference-card" key={`${image.file.name}-${image.file.lastModified}-${index}`}><img src={image.previewUrl} alt={image.file.name} /><div><code>{pictureLabel(promptMode, images.length, fl2vaSingleImageRole, index)}</code><IconButton label={`删除 ${pictureLabel(promptMode, images.length, fl2vaSingleImageRole, index)}`} onClick={() => removeReferenceImage(index)} disabled={isGenerating}><X size={14} /></IconButton></div><strong title={image.file.name}>{image.file.name}</strong><small>{formatBytes(image.file.size)}</small></article>)}</div> : <EmptyState icon={<ImageIcon size={24} />} title={promptMode === 'fl2va' ? '可选 0 到 2 张参考图片' : '上传 1 到 9 张参考图片'} detail={promptMode === 'fl2va' ? '0 张为 T2VA；1 张可作为首帧或末帧；2 张为首尾帧 FL2VA。' : '图片会按上传顺序标为 <Picture 1> 到 <Picture 9>。'} />}
        <FileDropzone onFiles={selectImages} disabled={isGenerating || availableImageSlots === 0} multiple={availableImageSlots > 1} maxFiles={availableImageSlots} label="添加参考图片" detail={`还可添加 ${availableImageSlots} 张图片`} />
        <div className="video-preset-control">
          <div className="video-preset-heading"><span>提示词预设</span><small>{promptMode === 'fl2va' ? `${baseMode.toUpperCase()} · H3 Base` : 'Ref2VA · 可复用视觉引用'}</small></div>
          <div className="mode-switch video-preset-switch" role="group" aria-label="视频提示词模型">
            <button type="button" aria-pressed={promptMode === 'ref2va'} className={promptMode === 'ref2va' ? 'mode-active' : ''} onClick={() => changePromptMode('ref2va')} disabled={isGenerating}>Ref2VA</button>
            <button type="button" aria-pressed={promptMode === 'fl2va'} className={promptMode === 'fl2va' ? 'mode-active' : ''} onClick={() => changePromptMode('fl2va')} disabled={isGenerating}>FL2VA</button>
          </div>
          {promptMode === 'fl2va' && images.length === 1 && <div className="video-single-role"><span>单图角色</span><div className="mode-switch video-preset-switch" role="group" aria-label="单图关键帧角色"><button type="button" aria-pressed={fl2vaSingleImageRole === 'first'} className={fl2vaSingleImageRole === 'first' ? 'mode-active' : ''} onClick={() => changeSingleImageRole('first')} disabled={isGenerating}>首帧 I2VA</button><button type="button" aria-pressed={fl2vaSingleImageRole === 'last'} className={fl2vaSingleImageRole === 'last' ? 'mode-active' : ''} onClick={() => changeSingleImageRole('last')} disabled={isGenerating}>末帧 L2VA</button></div></div>}
        </div>
        <div className="video-provider-fields">
          <Field label="Provider"><select aria-label="Provider" value={providerId} disabled={isGenerating} onChange={(event) => {
            const next = providerItems.find((provider) => provider.id === event.target.value)
            setProvider(event.target.value, next?.primary_model ?? '')
          }}><option value="">选择 Provider</option>{providerItems.map((provider) => <option key={provider.id} value={provider.id}>{provider.name}{provider.configured ? '' : ' · 未配置'}</option>)}</select></Field>
          <Field label="模型"><input aria-label="模型" value={providerModel} disabled={isGenerating} onChange={(event) => setProvider(providerId, event.target.value)} placeholder="主模型 ID" /></Field>
          {!providerItems.length && !providers.isLoading && <Notice tone="warning">尚未配置在线模型。<Button size="sm" variant="secondary" onClick={() => setPage('providers')}>前往在线模型</Button></Notice>}
        </div>
      </Panel>

      <Panel title="需求对话" eyebrow="02 / ITERATE" className="video-chat-panel" actions={isGenerating ? <LoaderCircle className="spin" size={17} /> : undefined}>
        <div className="video-chat-history" aria-live="polite">
          {revisions.length ? revisions.map((revision) => <RevisionMessage
            key={revision.id}
            revision={revision}
            current={revision.id === currentRevisionId}
            viewed={revision.id === viewedRevision?.id}
            onView={() => viewRevision(revision.id)}
            onRestore={() => { restoreRevision(revision.id); setNotice({ tone: 'info', text: `已恢复第 ${revision.version} 版，下一轮将以它为基线。` }) }}
          />) : <EmptyState icon={<Clapperboard size={23} />} title="描述你希望画面如何动起来" detail="首轮会创建完整提示词，后续要求会生成新的版本。" />}
          {isGenerating && <div className="video-generating"><LoaderCircle className="spin" size={16} /><span>在线模型正在生成完整提示词套件…</span></div>}
        </div>
        <form className="video-composer" onSubmit={submit}>
          <Field label={currentRevision ? `微调第 ${currentRevision.version} 版` : '生成要求'}><textarea aria-label="生成要求" value={instruction} disabled={isGenerating} onChange={(event) => setInstruction(event.target.value)} placeholder={currentRevision ? '例如：镜头改为缓慢环绕，人物动作更克制。' : '例如：让人物轻轻转头，镜头缓慢推进，保持原图风格。'} maxLength={8000} /></Field>
          <div className="video-composer-footer"><span>{instruction.length}/8000</span><Button type="submit" icon={isGenerating ? <LoaderCircle className="spin" size={16} /> : <Send size={16} />} disabled={!canGenerate}>{isGenerating ? '生成中…' : currentRevision ? '生成新版本' : '生成提示词'}</Button></div>
        </form>
      </Panel>

      <Panel title={promptMode === 'fl2va' ? `H3 ${baseMode.toUpperCase()} 套件` : 'H3 Ref2VA 套件'} eyebrow="03 / OUTPUT" className="video-output-panel" actions={viewedRevision ? <div className="video-language-switch" role="group" aria-label="输出语言">{([['both', '中英'], ['zh', '中文'], ['en', 'EN']] as const).map(([value, label]) => <button key={value} type="button" aria-pressed={displayLanguage === value} className={displayLanguage === value ? 'video-language-active' : ''} onClick={() => setDisplayLanguage(value)}>{label}</button>)}</div> : undefined}>
        {viewedRevision ? <PromptPackageView
          revision={viewedRevision}
          current={viewedRevision.id === currentRevisionId}
          language={displayLanguage}
          onCopy={copy}
        /> : <EmptyState icon={<FileText size={23} />} title={promptMode === 'fl2va' ? `H3 ${baseMode.toUpperCase()} 提示词会显示在这里` : 'H3 Ref2VA 提示词会显示在这里'} detail={promptMode === 'fl2va' ? '生成后可复制官方基础指南的对齐说明和三段核心字段。' : '生成后可按中文、英文或中英对照查看并复制六段式结构。'} />}
      </Panel>
    </div>
    {confirmation && <ConfirmDialog
      title={confirmation.title}
      detail={confirmation.detail}
      confirmLabel={confirmation.confirmLabel}
      onClose={() => setConfirmation(undefined)}
      onConfirm={() => {
        const action = confirmation.action
        setConfirmation(undefined)
        action()
      }}
    />}
  </div>
}

function pictureLabel(mode: VideoPromptMode, imageCount: number, role: Fl2vaSingleImageRole, index: number): string {
  const number = index + 1
  if (mode === 'ref2va') return `<Picture ${number}>`
  if (imageCount === 1) return role === 'first' ? '<Picture 1> · 首帧' : '<Picture 1> · 末帧'
  return number === 1 ? '<Picture 1> · 首帧' : '<Picture 2> · 末帧'
}

function RevisionMessage({ revision, current, viewed, onView, onRestore }: {
  revision: VideoPromptRevision
  current: boolean
  viewed: boolean
  onView: () => void
  onRestore: () => void
}) {
  return <article className={`video-revision ${viewed ? 'video-revision-viewed' : ''}`}>
    <div className="video-user-message"><span>需求</span><p>{revision.instruction}</p></div>
    <div className="video-assistant-message"><div className="video-revision-heading"><span>第 {revision.version} 版{current ? ' · 当前基线' : ''}</span><small>{formatDate(revision.created_at)}</small><span className="video-revision-actions"><IconButton label={`查看第 ${revision.version} 版`} onClick={onView}><Eye size={14} /></IconButton><IconButton label={`恢复第 ${revision.version} 版`} onClick={onRestore}><RotateCcw size={14} /></IconButton></span></div><p>{revision.package.change_summary_zh}</p></div>
  </article>
}

function PromptPackageView({ revision, current, language, onCopy }: {
  revision: VideoPromptRevision
  current: boolean
  language: VideoPromptLanguage
  onCopy: (value: string, label: string) => void
}) {
  if (revision.mode === 'fl2va') return <Fl2vaPackageView revision={revision} current={current} language={language} onCopy={onCopy} />
  const prompt = revision.package as Ref2vaPromptPackage
  return <div className="video-package">
    <div className="video-package-meta"><span>第 {revision.version} 版{current ? ' · 当前基线' : ' · 历史版本'}</span><div><Button size="sm" variant="secondary" icon={<Copy size={14} />} onClick={() => void onCopy(formatPackage(prompt, 'zh'), '完整中文 H3 套件')}>复制中文 H3</Button><Button size="sm" variant="secondary" icon={<Copy size={14} />} onClick={() => void onCopy(formatPackage(prompt, 'en'), '完整英文 H3 套件')}>复制英文 H3</Button></div></div>
    <H3PromptSection title="subject_definitions" section="subject_definitions" prompt={prompt} onCopy={onCopy}>
      <div className="video-h3-list">{prompt.subject_definitions.map((subject) => <div className="video-h3-entry" key={subject.subject_number}><code>{`<Subject ${subject.subject_number}> from <Picture ${subject.picture_number}>`}</code><BilingualValue value={subject} language={language} /></div>)}</div>
    </H3PromptSection>
    <H3PromptSection title="summary" section="summary" prompt={prompt} onCopy={onCopy}><BilingualValue value={prompt.summary} language={language} /></H3PromptSection>
    <H3PromptSection title="retention_analysis" section="retention_analysis" prompt={prompt} onCopy={onCopy}>
      <div className="video-h3-list">{prompt.retention_analysis.map((retention, index) => <div className="video-h3-entry" key={`${retention.subject_number}-${retention.shot_number}-${index}`}><code>{`<Subject ${retention.subject_number}> (appears in [Shot ${retention.shot_number}]): ${retention.visual_retention}`}</code><BilingualValue value={retention} language={language} /></div>)}</div>
    </H3PromptSection>
    <H3PromptSection title="detailed_description" section="detailed_description" prompt={prompt} onCopy={onCopy}>
      <div className="video-h3-overview"><code>The target video is</code><BilingualValue value={prompt.detailed_description.overview} language={language} /></div>
      <div className="video-h3-list">{prompt.detailed_description.shots.map((shot) => <div className="video-h3-entry" key={shot.shot_number}><code>{formatShotLabel(shot.shot_number, shot.cut_time_seconds)}</code><BilingualValue value={shot} language={language} /></div>)}</div>
    </H3PromptSection>
    <H3PromptSection title="overall_soundscape" section="overall_soundscape" prompt={prompt} onCopy={onCopy}><BilingualValue value={prompt.overall_soundscape} language={language} /></H3PromptSection>
    <H3PromptSection title="non_diegetic_music" section="non_diegetic_music" prompt={prompt} onCopy={onCopy}><BilingualValue value={prompt.non_diegetic_music} language={language} /></H3PromptSection>
    {prompt.assumptions_zh.length > 0 && <section className="video-package-section"><header><strong>假设</strong></header><ul className="video-assumptions">{prompt.assumptions_zh.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul></section>}
  </div>
}

function Fl2vaPackageView({ revision, current, language, onCopy }: {
  revision: VideoPromptRevision
  current: boolean
  language: VideoPromptLanguage
  onCopy: (value: string, label: string) => void
}) {
  const prompt = revision.package as Fl2vaPromptPackage
  return <div className="video-package">
    <div className="video-package-meta"><span>第 {revision.version} 版 · {prompt.base_mode.toUpperCase()}{current ? ' · 当前基线' : ' · 历史版本'}</span><div><Button size="sm" variant="secondary" icon={<Copy size={14} />} onClick={() => void onCopy(formatFl2vaPackage(prompt, 'zh'), `完整中文 ${prompt.base_mode.toUpperCase()} 套件`)}>复制中文 H3</Button><Button size="sm" variant="secondary" icon={<Copy size={14} />} onClick={() => void onCopy(formatFl2vaPackage(prompt, 'en'), `完整英文 ${prompt.base_mode.toUpperCase()} 套件`)}>复制英文 H3</Button></div></div>
    {prompt.reference_alignment && <Fl2vaPromptSection title="reference_alignment" section="reference_alignment" prompt={prompt} onCopy={onCopy}><BilingualValue value={prompt.reference_alignment} language={language} /></Fl2vaPromptSection>}
    <Fl2vaPromptSection title="integrated_multimodal_description" section="integrated_multimodal_description" prompt={prompt} onCopy={onCopy}><BilingualValue value={prompt.integrated_multimodal_description} language={language} /></Fl2vaPromptSection>
    <Fl2vaPromptSection title="overall_soundscape" section="overall_soundscape" prompt={prompt} onCopy={onCopy}><BilingualValue value={prompt.overall_soundscape} language={language} /></Fl2vaPromptSection>
    <Fl2vaPromptSection title="non_diegetic_music" section="non_diegetic_music" prompt={prompt} onCopy={onCopy}><BilingualValue value={prompt.non_diegetic_music} language={language} /></Fl2vaPromptSection>
    {prompt.assumptions_zh.length > 0 && <section className="video-package-section"><header><strong>假设</strong></header><ul className="video-assumptions">{prompt.assumptions_zh.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul></section>}
  </div>
}

type Fl2vaSection = 'reference_alignment' | 'integrated_multimodal_description' | 'overall_soundscape' | 'non_diegetic_music'

function Fl2vaPromptSection({ title, section, prompt, onCopy, children }: {
  title: string
  section: Fl2vaSection
  prompt: Fl2vaPromptPackage
  onCopy: (value: string, label: string) => void
  children: ReactNode
}) {
  return <section className="video-package-section video-h3-section video-fl2va-section"><header><strong>{title}</strong><span><IconButton label={`复制 ${title} 中文`} onClick={() => void onCopy(formatFl2vaSection(prompt, 'zh', section), `${title} 中文`)}><Copy size={14} /></IconButton><IconButton label={`复制 ${title} 英文`} onClick={() => void onCopy(formatFl2vaSection(prompt, 'en', section), `${title} 英文`)}><Copy size={14} /></IconButton></span></header>{children}</section>
}

type H3Section = 'subject_definitions' | 'summary' | 'retention_analysis' | 'detailed_description' | 'overall_soundscape' | 'non_diegetic_music'

function H3PromptSection({ title, section, prompt, onCopy, children }: {
  title: string
  section: H3Section
  prompt: Ref2vaPromptPackage
  onCopy: (value: string, label: string) => void
  children: ReactNode
}) {
  return <section className="video-package-section video-h3-section"><header><strong>{title}</strong><span><IconButton label={`复制 ${title} 中文`} onClick={() => void onCopy(formatH3Section(prompt, 'zh', section), `${title} 中文`)}><Copy size={14} /></IconButton><IconButton label={`复制 ${title} 英文`} onClick={() => void onCopy(formatH3Section(prompt, 'en', section), `${title} 英文`)}><Copy size={14} /></IconButton></span></header>{children}</section>
}

function BilingualValue({ value, language }: { value: BilingualText; language: VideoPromptLanguage }) {
  if (language === 'zh') return <p className="video-copy">{value.zh}</p>
  if (language === 'en') return <p className="video-copy">{value.en}</p>
  return <div className="video-bilingual"><div><small>中文</small><p className="video-copy">{value.zh}</p></div><div><small>EN</small><p className="video-copy">{value.en}</p></div></div>
}

function formatFl2vaPackage(value: Fl2vaPromptPackage, language: 'zh' | 'en'): string {
  const choose = (item: BilingualText) => item[language]
  return [
    ...(value.reference_alignment ? [choose(value.reference_alignment).trim()] : []),
    `integrated_multimodal_description: ${choose(value.integrated_multimodal_description).trim()}`,
    `overall_soundscape: ${choose(value.overall_soundscape).trim()}`,
    `non_diegetic_music: ${choose(value.non_diegetic_music).trim()}`,
  ].join('\n\n')
}

function formatFl2vaSection(value: Fl2vaPromptPackage, language: 'zh' | 'en', section: Fl2vaSection): string {
  const choose = (item: BilingualText) => item[language]
  if (section === 'reference_alignment') return value.reference_alignment ? choose(value.reference_alignment) : ''
  if (section === 'integrated_multimodal_description') return `integrated_multimodal_description:\n${choose(value.integrated_multimodal_description)}`
  if (section === 'overall_soundscape') return `overall_soundscape:\n${choose(value.overall_soundscape)}`
  return `non_diegetic_music:\n${choose(value.non_diegetic_music)}`
}

function formatPackage(value: Ref2vaPromptPackage, language: 'zh' | 'en'): string {
  const order: H3Section[] = ['subject_definitions', 'summary', 'retention_analysis', 'detailed_description', 'overall_soundscape', 'non_diegetic_music']
  return order.map((section) => formatH3Section(value, language, section)).join('\n\n')
}

function formatH3Section(value: Ref2vaPromptPackage, language: 'zh' | 'en', section: H3Section): string {
  const choose = (item: BilingualText) => item[language]
  if (section === 'subject_definitions') {
    return `subject_definitions:\n${value.subject_definitions.map((subject) => language === 'zh'
      ? `<Subject ${subject.subject_number}> 是 ${asPhrase(choose(subject))}，来自 <Picture ${subject.picture_number}>。`
      : `<Subject ${subject.subject_number}> is ${asPhrase(choose(subject))} from <Picture ${subject.picture_number}>.`
    ).join('\n')}`
  }
  if (section === 'summary') return `summary:\n${choose(value.summary)}`
  if (section === 'retention_analysis') {
    return `retention_analysis:\n${value.retention_analysis.map((retention) => `<Subject ${retention.subject_number}> (appears in [Shot ${retention.shot_number}]): ${retention.visual_retention} - ${choose(retention)}`).join('\n')}`
  }
  if (section === 'detailed_description') {
    const target = language === 'zh' ? `目标视频是 ${asSentence(choose(value.detailed_description.overview), language)}` : `The target video is ${asSentence(choose(value.detailed_description.overview), language)}`
    const shots = value.detailed_description.shots.map((shot) => `${formatShotLabel(shot.shot_number, shot.cut_time_seconds)} ${asSentence(choose(shot), language)}`)
    return `detailed_description:\n${[target, ...shots].join('\n')}`
  }
  if (section === 'overall_soundscape') return `overall_soundscape:\n${choose(value.overall_soundscape)}`
  return `non_diegetic_music:\n${choose(value.non_diegetic_music)}`
}

function asPhrase(value: string): string {
  return value.trim().replace(/[.。]+$/u, '')
}

function asSentence(value: string, language: 'zh' | 'en'): string {
  const text = value.trim()
  return /[.。!?！？]$/u.test(text) ? text : `${text}${language === 'zh' ? '。' : '.'}`
}

function formatShotLabel(shotNumber: number, cutTimeSeconds: number | null): string {
  if (shotNumber === 1 || cutTimeSeconds === null) return `[Shot ${shotNumber}]`
  return `[Shot ${shotNumber}] At ${formatH3Timestamp(cutTimeSeconds)},`
}

function formatH3Timestamp(seconds: number): string {
  const totalMilliseconds = Math.round(seconds * 1_000)
  const minutes = Math.floor(totalMilliseconds / 60_000)
  const remainder = totalMilliseconds % 60_000
  const wholeSeconds = Math.floor(remainder / 1_000)
  const milliseconds = remainder % 1_000
  return `${String(minutes).padStart(2, '0')}:${String(wholeSeconds).padStart(2, '0')}.${String(milliseconds).padStart(3, '0')}`
}

function formatDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
