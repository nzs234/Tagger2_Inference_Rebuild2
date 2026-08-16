import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowRight,
  Archive,
  CheckCircle2,
  Database,
  Pause,
  Pin,
  PinOff,
  Play,
  RefreshCw,
  RotateCcw,
  Square,
  Undo2,
  Upload,
  Wrench,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Button, DialogLayer, EmptyState, Field, HelpPopover, Notice, Panel, StatusBadge } from '../components/ui'
import { api, ApiError } from '../lib/api'
import { copyFor } from '../lib/workflowCopy'
import { useWorkflowEvents } from '../hooks/useWorkflowEvents'
import { usePreferences } from '../store/app'
import type {
  WorkflowCountDecision,
  WorkflowExportFormat,
  WorkflowRepairReport,
  WorkflowTokenReviewAction,
  WorkflowTokenReviewItem,
  WorkflowImportPreview,
  ModelProfile,
  WorkflowPreflightReport,
  WorkflowProfile,
  WorkflowResource,
  WorkflowWorkMode,
  WorkflowPathBinding,
  WorkflowPathBindingPreview,
} from '../types'

const DEFAULT_CLASSIFY_RESOURCE_ID = 'classify-e621-20260812-v1'
const DEFAULT_REPLACEMENT_RESOURCE_ID = 'replace-e621-pass-drop-v2'
const DEFAULT_OCR_RESOURCE_ID = 'ocr-paddleocr-2-9-1-cpu-v1'
const DEFAULT_TOKENIZER_RESOURCE_ID = 'tokenizer-qwen3-0-6b-tokenizer-v1'

interface JobDraft {
  profile: WorkflowProfile
  workMode: WorkflowWorkMode
  captionModelId?: string
  sourcePath: string
  outputPath: string
  sourceRootId: string
  sourceRelativePath: string
  outputRootId: string
  outputRelativePath: string
  exportFormat: WorkflowExportFormat
  recursive: boolean
  classifyEnabled: boolean
  classifyResourceId: string
  replaceEnabled: boolean
  replaceResourceId: string
  ocrEnabled: boolean
  ocrResourceId: string
  ocrMinConfidence: number
  nlEnabled: boolean
  nlProviderId: string
  nlModel: string
  nlReuseOriginal: boolean
  nlUseImage: boolean
  nlUseFullJson: boolean
  nlPromptPreset: 'general' | 'style' | 'character'
  nlLength: 'short' | 'medium' | 'long'
  policyEnabled: boolean
  policySeed: string
  policyArtistDropout: number
  policyQualityDropout: number
  policySoloDropNl: number
  policySoloDropAppearance: number
  policyNonSoloDropNl: number
  policyNonSoloDropAppearance: number
  tokenBudgetEnabled: boolean
  tokenizerResourceId: string
  tokenMaxTokens: number
}

const emptyDraft: JobDraft = {
  profile: 'e621',
  workMode: 'full_copy',
  sourcePath: '',
  outputPath: '',
  sourceRootId: '',
  sourceRelativePath: '',
  outputRootId: '',
  outputRelativePath: '',
  exportFormat: 'both',
  recursive: false,
  classifyEnabled: true,
  classifyResourceId: DEFAULT_CLASSIFY_RESOURCE_ID,
  replaceEnabled: true,
  replaceResourceId: DEFAULT_REPLACEMENT_RESOURCE_ID,
  ocrEnabled: false,
  ocrResourceId: DEFAULT_OCR_RESOURCE_ID,
  ocrMinConfidence: 0.5,
  nlEnabled: false,
  nlProviderId: '',
  nlModel: '',
  nlReuseOriginal: true,
  nlUseImage: true,
  nlUseFullJson: false,
  nlPromptPreset: 'general',
  nlLength: 'medium',
  policyEnabled: false,
  policySeed: 'workflow-default-v1',
  policyArtistDropout: 0,
  policyQualityDropout: 0,
  policySoloDropNl: 0.7,
  policySoloDropAppearance: 0.05,
  policyNonSoloDropNl: 0.05,
  policyNonSoloDropAppearance: 0.7,
  tokenBudgetEnabled: false,
  tokenizerResourceId: DEFAULT_TOKENIZER_RESOURCE_ID,
  tokenMaxTokens: 512,
}

// Mirrors COUNT_VALUES in backend/tagger2/workflow/count_review.py; the API rejects anything else.
const COUNT_VALUES = ['solo', 'duo', 'trio', 'group'] as const
const EMPTY_WORKFLOW_RESOURCES: WorkflowResource[] = []
const REVIEW_PAGE_SIZE = 50
const TERMINAL_WORKFLOW_STATUSES = new Set(['completed', 'failed', 'cancelled', 'interrupted', 'rollback_required'])

type WorkflowStageId = 'dataset' | 'caption' | 'classify' | 'replace' | 'ocr' | 'nl' | 'count' | 'policy' | 'token' | 'export'

const WORKFLOW_STAGE_IDS: WorkflowStageId[] = [
  'dataset',
  'caption',
  'classify',
  'replace',
  'ocr',
  'nl',
  'count',
  'policy',
  'token',
  'export',
]

export function DatasetWorkflow() {
  const language = usePreferences((state) => state.workflowLanguage)
  const setLanguage = usePreferences((state) => state.setWorkflowLanguage)
  const setPage = usePreferences((state) => state.setPage)
  const text = copyFor(language)
  const queryClient = useQueryClient()

  const [draft, setDraft] = useState<JobDraft>(emptyDraft)
  const [activeStage, setActiveStage] = useState<WorkflowStageId>('dataset')
  const [selectedJobId, setSelectedJobId] = useState<string>()
  const [countPage, setCountPage] = useState(0)
  const [tokenPage, setTokenPage] = useState(0)
  const [preflight, setPreflight] = useState<WorkflowPreflightReport>()
  const [preflightError, setPreflightError] = useState<string>()
  const [createNotice, setCreateNotice] = useState<string>()
  const [pathBindingPreview, setPathBindingPreview] = useState<WorkflowPathBindingPreview>()
  const [pathBinding, setPathBinding] = useState<WorkflowPathBinding>()
  const [pathBindingError, setPathBindingError] = useState<string>()
  const [pathCreationConfirmOpen, setPathCreationConfirmOpen] = useState(false)
  const [continuePreflightAfterBinding, setContinuePreflightAfterBinding] = useState(false)
  const [importForm, setImportForm] = useState({ rootId: '', relativePath: '', resourceId: '' })
  const [importPreview, setImportPreview] = useState<WorkflowImportPreview>()
  const [importError, setImportError] = useState<string>()
  const [countError, setCountError] = useState<string>()
  const [repair, setRepair] = useState<WorkflowRepairReport>()
  const [tokenError, setTokenError] = useState<string>()
  const [tokenDraft, setTokenDraft] = useState<Record<number, string>>({})
  const captionSelectionSource = useRef<'auto' | 'user' | null>(null)

  const roots = useQuery({ queryKey: ['roots'], queryFn: api.roots, retry: false })
  const models = useQuery({ queryKey: ['models'], queryFn: api.models, retry: false })
  const providers = useQuery({ queryKey: ['providers'], queryFn: api.providers, retry: false })
  const providerModels = useQuery({
    queryKey: ['provider-models', draft.nlProviderId],
    queryFn: () => api.providerModels(draft.nlProviderId),
    enabled: Boolean(draft.nlEnabled && draft.nlProviderId),
    retry: false,
  })
  const capabilities = useQuery({
    queryKey: ['workflow', 'capabilities'],
    queryFn: api.workflowCapabilities,
    retry: false,
  })
  const resources = useQuery({
    queryKey: ['workflow', 'resources'],
    queryFn: () => api.workflowResources(),
    retry: false,
  })
  const jobs = useQuery({
    queryKey: ['workflow', 'jobs'],
    queryFn: () => api.workflowJobs(),
    retry: false,
    refetchInterval: (query) => {
      const listedJobs = query.state.data
      return !listedJobs || listedJobs.some(
        (job) => !TERMINAL_WORKFLOW_STATUSES.has(job.status),
      ) ? 5_000 : false
    },
  })
  const countReview = useQuery({
    queryKey: ['workflow', 'count-review', selectedJobId, countPage],
    queryFn: () => api.workflowCountReview(selectedJobId as string, {
      limit: REVIEW_PAGE_SIZE,
      offset: countPage * REVIEW_PAGE_SIZE,
    }),
    enabled: Boolean(selectedJobId),
    retry: false,
  })
  const tokenReview = useQuery({
    queryKey: ['workflow', 'token-review', selectedJobId, tokenPage],
    queryFn: () => api.workflowTokenReview(selectedJobId as string, {
      limit: REVIEW_PAGE_SIZE,
      offset: tokenPage * REVIEW_PAGE_SIZE,
    }),
    enabled: Boolean(selectedJobId),
    retry: false,
  })
  const jobReport = useQuery({
    queryKey: ['workflow', 'report', selectedJobId],
    queryFn: () => api.workflowJobReport(selectedJobId as string),
    enabled: Boolean(selectedJobId),
    retry: false,
  })
  const issues = useQuery({
    queryKey: ['workflow', 'issues', selectedJobId],
    queryFn: () => api.workflowIssues(selectedJobId as string),
    enabled: Boolean(selectedJobId),
    retry: false,
  })

  const inputRoots = useMemo(
    () => (roots.data?.items ?? []).filter((root) => root.kind === 'input'),
    [roots.data?.items],
  )
  const loadedModels: ModelProfile[] = useMemo(
    () => (models.data?.items ?? []).filter((model) => model.loaded),
    [models.data],
  )
  const workflowResources = resources.data ?? EMPTY_WORKFLOW_RESOURCES
  const classifyResources = useMemo(
    () => workflowResources.filter((resource) => resource.category === 'classify' || resource.category === 'classification'),
    [workflowResources],
  )
  const replacementResources = useMemo(
    () =>
      workflowResources.filter(
        (resource) => resource.category === 'replace' || resource.category === 'replacement_index',
      ),
    [workflowResources],
  )
  const ocrResources = useMemo(
    () => workflowResources.filter((resource) => resource.category === 'ocr' || resource.category === 'ocr_runtime'),
    [workflowResources],
  )
  const tokenizerResources = useMemo(
    () => workflowResources.filter((resource) => resource.category === 'tokenizer'),
    [workflowResources],
  )
  const enabledProviders = useMemo(
    () => (providers.data?.items ?? []).filter((provider) => provider.enabled !== false),
    [providers.data?.items],
  )

  const legacyReplacementResource = workflowResources.find((resource) => resource.category === 'replacement_index')

  const updateDraft = (patch: Partial<JobDraft>) => {
    setPreflight(undefined)
    setPreflightError(undefined)
    setCreateNotice(undefined)
    if ('sourcePath' in patch || 'outputPath' in patch || 'workMode' in patch) {
      setPathBinding(undefined)
      setPathBindingPreview(undefined)
      setPathBindingError(undefined)
    }
    setDraft((current) => ({ ...current, ...patch }))
  }

  useEffect(() => {
    if (!resources.data) return
    setDraft((current) => ({
      ...current,
      classifyResourceId:
        classifyResources.some((resource) => resource.resource_id === current.classifyResourceId)
          ? current.classifyResourceId
          : classifyResources[0]?.resource_id ?? '',
      replaceResourceId:
        replacementResources.some((resource) => resource.resource_id === current.replaceResourceId)
          ? current.replaceResourceId
          : replacementResources.find((resource) => resource.resource_id === DEFAULT_REPLACEMENT_RESOURCE_ID)?.resource_id
            ?? legacyReplacementResource?.resource_id
            ?? replacementResources[0]?.resource_id
            ?? '',
      ocrResourceId:
        ocrResources.some((resource) => resource.resource_id === current.ocrResourceId)
          ? current.ocrResourceId
          : ocrResources[0]?.resource_id ?? '',
      tokenizerResourceId:
        tokenizerResources.some((resource) => resource.resource_id === current.tokenizerResourceId)
          ? current.tokenizerResourceId
          : tokenizerResources[0]?.resource_id ?? '',
      nlProviderId:
        enabledProviders.some((provider) => provider.id === current.nlProviderId)
          ? current.nlProviderId
          : enabledProviders[0]?.id ?? '',
    }))
  }, [resources.data, classifyResources, replacementResources, ocrResources, tokenizerResources, legacyReplacementResource, enabledProviders])

  useEffect(() => {
    if (!draft.nlProviderId || !providerModels.data) return
    const available = providerModels.data.items
    if (!available.some((model) => model.id === draft.nlModel)) {
      const provider = enabledProviders.find((item) => item.id === draft.nlProviderId)
      updateDraft({ nlModel: available[0]?.id ?? provider?.primary_model ?? '' })
    }
  }, [draft.nlProviderId, draft.nlModel, providerModels.data, enabledProviders])

  useEffect(() => {
    setDraft((current) => {
      if (loadedModels.length === 0) {
        captionSelectionSource.current = null
        return current.captionModelId ? { ...current, captionModelId: undefined } : current
      }
      if (loadedModels.length === 1) {
        const model = loadedModels[0]!
        if (current.captionModelId === model.id) return current
        captionSelectionSource.current = 'auto'
        return { ...current, captionModelId: model.id }
      }
      const hasValidSelection = loadedModels.some((model) => model.id === current.captionModelId)
      if (hasValidSelection && captionSelectionSource.current === 'user') return current
      // A selection made automatically while only one model was loaded must not
      // silently become the choice once the runtime exposes multiple models.
      captionSelectionSource.current = null
      return current.captionModelId ? { ...current, captionModelId: '' } : current
    })
  }, [loadedModels])

  const bindPathsMutation = useMutation({
    mutationFn: (createOutput: boolean) => api.workflowBindPaths({
      source_path: draft.sourcePath.trim(),
      ...(draft.workMode === 'full_copy' && draft.outputPath.trim()
        ? { output_path: draft.outputPath.trim() }
        : {}),
      work_mode: draft.workMode,
      create_output: createOutput,
    }),
    onSuccess: (result) => {
      setPathBinding(result)
      setPathBindingError(undefined)
      setPathCreationConfirmOpen(false)
      if (result.output_created) setContinuePreflightAfterBinding(true)
      updateDraft({
        sourceRootId: result.source.root_id,
        sourceRelativePath: result.source.relative_path,
        outputRootId: result.output?.root_id ?? '',
        outputRelativePath: result.output?.relative_path ?? '',
      })
    },
    onError: (error: Error) => setPathBindingError(error.message),
  })

  const jobConfig = useMemo(() => {
    const config: Record<string, unknown> = {
      profile: draft.profile,
      work_mode: draft.workMode,
      overwrite_mode: 'incremental',
      source_root: { root_id: draft.sourceRootId, relative_path: draft.sourceRelativePath },
      recursive: draft.recursive,
      // Caption reuses the model already loaded by the Models/Workbench
      // runtime.  The workflow API resolves the canonical opaque model id and
      // preserves that model's thresholds and preprocessing profile.
      caption: {
        enabled: true,
        input_txt_mode: 'tag',
        ...(draft.captionModelId ? { model_id: draft.captionModelId } : {}),
      },
      classify: draft.classifyEnabled
        ? {
            enabled: true,
            ...(draft.classifyResourceId ? { resource_id: draft.classifyResourceId } : {}),
          }
        : { enabled: false },
      replace: draft.replaceEnabled
        ? { enabled: true, resource_id: draft.replaceResourceId }
        : { enabled: false },
      ocr: draft.ocrEnabled
        ? {
            enabled: true,
            ...(draft.ocrResourceId ? { resource_id: draft.ocrResourceId } : {}),
            min_confidence: draft.ocrMinConfidence,
          }
        : { enabled: false },
      nl: draft.nlEnabled
        ? {
            enabled: true,
            api_enabled: true,
            ...(draft.nlProviderId ? { provider_id: draft.nlProviderId } : {}),
            ...(draft.nlModel ? { model: draft.nlModel } : {}),
            reuse_original_nl: draft.nlReuseOriginal,
            use_image: draft.nlUseImage,
            use_full_json: draft.nlUseFullJson,
            prompt_preset: draft.nlPromptPreset,
            length: draft.nlLength,
          }
        : { enabled: false },
      // Production UI jobs always stop at the explicit Count Review gate.
      // Legacy/API callers may opt out for deterministic compatibility tests.
      count_review: { enabled: true },
      policy: draft.policyEnabled
        ? {
            enabled: true,
            seed: draft.policySeed.trim() || 'workflow-default-v1',
            directory_to_artist: true,
            artist_dropout: draft.policyArtistDropout,
            quality_dropout: draft.policyQualityDropout,
            appearance_nl_solo_drop_nl: draft.policySoloDropNl,
            appearance_nl_solo_drop_appearance: draft.policySoloDropAppearance,
            appearance_nl_non_solo_drop_nl: draft.policyNonSoloDropNl,
            appearance_nl_non_solo_drop_appearance: draft.policyNonSoloDropAppearance,
          }
        : { enabled: false },
      token_budget: draft.tokenBudgetEnabled
        ? {
            enabled: true,
            ...(draft.tokenizerResourceId
              ? { tokenizer_resource_id: draft.tokenizerResourceId }
              : {}),
            max_tokens: draft.tokenMaxTokens,
          }
        : { enabled: false },
      export: { format: draft.exportFormat },
    }
    if (draft.workMode === 'full_copy') {
      config.output_root = { root_id: draft.outputRootId, relative_path: draft.outputRelativePath }
    }
    return config
  }, [draft])

  const effectiveJobConfig = useMemo(() => {
    if (!pathBinding) return jobConfig
    return {
      ...jobConfig,
      source_root: pathBinding.source,
      ...(pathBinding.output ? { output_root: pathBinding.output } : {}),
    }
  }, [jobConfig, pathBinding])

  const invalidateWorkflowJob = (jobId = selectedJobId) => {
    void queryClient.invalidateQueries({ queryKey: ['workflow', 'jobs'] })
    if (!jobId) return
    void queryClient.invalidateQueries({ queryKey: ['workflow', 'count-review', jobId] })
    void queryClient.invalidateQueries({ queryKey: ['workflow', 'token-review', jobId] })
    void queryClient.invalidateQueries({ queryKey: ['workflow', 'report', jobId] })
    void queryClient.invalidateQueries({ queryKey: ['workflow', 'issues', jobId] })
  }

  const preflightMutation = useMutation({
    mutationFn: async () => {
      const preview = await api.workflowPreviewPathBinding({
        source_path: draft.sourcePath.trim(),
        ...(draft.workMode === 'full_copy' && draft.outputPath.trim()
          ? { output_path: draft.outputPath.trim() }
          : {}),
        work_mode: draft.workMode,
      })
      setPathBindingPreview(preview)
      if (preview.errors.length) throw new Error(preview.errors.join('; '))
      if (preview.output_create_required) {
        setPathCreationConfirmOpen(true)
        return undefined
      }
      const binding = await api.workflowBindPaths({
        source_path: draft.sourcePath.trim(),
        ...(draft.workMode === 'full_copy' && draft.outputPath.trim()
          ? { output_path: draft.outputPath.trim() }
          : {}),
        work_mode: draft.workMode,
      })
      setPathBinding(binding)
      const boundConfig = {
        ...jobConfig,
        source_root: binding.source,
        ...(binding.output ? { output_root: binding.output } : {}),
      }
      return api.workflowPreflight(boundConfig)
    },
    onMutate: () => {
      setPreflight(undefined)
      setPreflightError(undefined)
      setCreateNotice(undefined)
    },
    onSuccess: (report) => { if (report) setPreflight(report) },
    onError: (error: Error) => setPreflightError(error.message),
  })

  useEffect(() => {
    if (!continuePreflightAfterBinding || !pathBinding) return
    setContinuePreflightAfterBinding(false)
    preflightMutation.mutate()
  }, [continuePreflightAfterBinding, pathBinding, preflightMutation])

  const createMutation = useMutation({
    mutationFn: () => api.workflowCreateJob(effectiveJobConfig),
    onSuccess: (created) => {
      setSelectedJobId(created.job_id)
      setCountPage(0)
      setTokenPage(0)
      setCreateNotice(text.taskCreatedPending)
      invalidateWorkflowJob(created.job_id)
    },
    onError: (error: Error) => setPreflightError(error.message),
  })

  const previewMutation = useMutation({
    mutationFn: () =>
      api.workflowImportPreview({
        root_id: importForm.rootId,
        relative_path: importForm.relativePath,
        resource_id: importForm.resourceId,
        category: 'replace',
      }),
    onMutate: () => {
      setImportPreview(undefined)
      setImportError(undefined)
    },
    onSuccess: (report) => setImportPreview(report),
    onError: (error: Error) => setImportError(error.message),
  })

  const applyMutation = useMutation({
    mutationFn: () =>
      api.workflowImportApply({
        root_id: importForm.rootId,
        relative_path: importForm.relativePath,
        resource_id: importForm.resourceId,
        category: 'replace',
      }),
    onSuccess: () => {
      setImportPreview(undefined)
      void queryClient.invalidateQueries({ queryKey: ['workflow', 'resources'] })
    },
    onError: (error: Error) => setImportError(error.message),
  })

  const resolveCount = useMutation({
    mutationFn: ({ decision, count }: { decision: WorkflowCountDecision; count: string }) =>
      api.workflowResolveCount(selectedJobId as string, {
        sample_id: decision.sample_id,
        expected_version: decision.version,
        count,
        source: 'manual',
      }),
    onMutate: () => setCountError(undefined),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['workflow', 'count-review'] })
    },
    onError: (error: ApiError) => {
      // A 409 means someone else changed the row; say so rather than retrying.
      setCountError(error.status === 409 ? text.countStale : error.message)
    },
  })

  const confirmCount = useMutation({
    mutationFn: () => api.workflowConfirmCount(selectedJobId as string),
    onMutate: () => setCountError(undefined),
    onSuccess: () => {
      invalidateWorkflowJob()
    },
    onError: (error: ApiError) => {
      setCountError(error.status === 409 ? text.countGateBlocked : error.message)
    },
  })

  const jobAction = useMutation({
    mutationFn: (action: 'pause' | 'resume' | 'cancel' | 'recover') =>
      api.workflowJobAction(selectedJobId as string, action),
    onSuccess: () => invalidateWorkflowJob(),
    onError: (error: Error) => setCountError(error.message),
  })

  const startJob = useMutation({
    mutationFn: () => api.workflowStartJob(selectedJobId as string),
    onSuccess: () => invalidateWorkflowJob(),
    onError: (error: Error) => setCountError(error.message),
  })

  const restoreJob = useMutation({
    mutationFn: () => api.workflowRestoreJob(selectedJobId as string),
    onSuccess: () => invalidateWorkflowJob(),
    onError: (error: Error) => setCountError(error.message),
  })

  const discardJob = useMutation({
    mutationFn: (jobId: string) => api.workflowDiscardJob(jobId),
    onSuccess: (_, jobId) => {
      if (selectedJobId === jobId) {
        setSelectedJobId(undefined)
        setCountPage(0)
        setTokenPage(0)
        setRepair(undefined)
        setCountError(undefined)
        setTokenError(undefined)
        setTokenDraft({})
      }
      queryClient.removeQueries({ queryKey: ['workflow', 'count-review', jobId] })
      queryClient.removeQueries({ queryKey: ['workflow', 'token-review', jobId] })
      queryClient.removeQueries({ queryKey: ['workflow', 'report', jobId] })
      queryClient.removeQueries({ queryKey: ['workflow', 'issues', jobId] })
      void queryClient.invalidateQueries({ queryKey: ['workflow', 'jobs'] })
    },
    onError: (error: Error) => setCountError(error.message),
  })

  const pinJob = useMutation({
    mutationFn: (pinned: boolean) => api.workflowPinJob(selectedJobId as string, pinned),
    onSuccess: () => invalidateWorkflowJob(),
    onError: (error: Error) => setCountError(error.message),
  })

  const repairJob = useMutation({
    mutationFn: () => api.workflowRepairJob(selectedJobId as string),
    onSuccess: (report) => {
      setRepair(report)
      invalidateWorkflowJob()
    },
    onError: (error: Error) => setCountError(error.message),
  })

  const reviewToken = useMutation({
    mutationFn: ({
      item,
      action,
      text,
    }: {
      item: WorkflowTokenReviewItem
      action: WorkflowTokenReviewAction
      text?: string
    }) =>
      api.workflowReviewToken(selectedJobId as string, {
        sample_id: item.sample_id,
        action,
        expected_status: item.status,
        ...(text === undefined ? {} : { text }),
      }),
    onMutate: () => setTokenError(undefined),
    onSuccess: () => {
      setTokenDraft({})
      void queryClient.invalidateQueries({ queryKey: ['workflow', 'token-review'] })
    },
    onError: (error: ApiError) => {
      // 409 is a stale status, 503 means the tokenizer resource is missing.
      if (error.status === 409) setTokenError(text.tokenStale)
      else if (error.status === 503) setTokenError(text.tokenUnavailable)
      else setTokenError(error.message)
    },
  })

  const confirmTokenReview = useMutation({
    mutationFn: () => api.workflowConfirmTokenReview(selectedJobId as string),
    onMutate: () => setTokenError(undefined),
    onSuccess: () => {
      invalidateWorkflowJob()
    },
    onError: (error: ApiError) => setTokenError(error.message),
  })

  const selectedJob = jobs.data?.find((job) => job.job_id === selectedJobId)
  const workflowEvents = useWorkflowEvents(selectedJobId, {
    enabled: Boolean(
      selectedJobId && (!selectedJob || !TERMINAL_WORKFLOW_STATUSES.has(selectedJob.status)),
    ),
    onEvent: (event) => {
      // Event replay is the low-latency path; the regular queries remain the
      // source of truth for status controls, review gates, reports, and issues.
      invalidateWorkflowJob(event.job_id)
    },
  })

  // A repair report and review state belong to one job, so clear them on switch.
  function selectJob(jobId: string) {
    if (jobId === selectedJobId) return
    setSelectedJobId(jobId)
    setCountPage(0)
    setTokenPage(0)
    setRepair(undefined)
    setCountError(undefined)
    setTokenError(undefined)
    setTokenDraft({})
  }

  const supportedProfiles = capabilities.data?.profiles ?? []
  const supportedWorkModes = capabilities.data?.work_modes ?? []
  const selectedResourcesAvailable =
    (!draft.classifyEnabled || classifyResources.some((resource) => resource.resource_id === draft.classifyResourceId)) &&
    (!draft.replaceEnabled || replacementResources.some((resource) => resource.resource_id === draft.replaceResourceId)) &&
    (!draft.ocrEnabled || ocrResources.some((resource) => resource.resource_id === draft.ocrResourceId)) &&
    (!draft.tokenBudgetEnabled || tokenizerResources.some((resource) => resource.resource_id === draft.tokenizerResourceId))
  const canPreflight = Boolean(
    capabilities.data &&
      resources.data &&
      supportedProfiles.includes(draft.profile) &&
      supportedWorkModes.includes(draft.workMode) &&
      selectedResourcesAvailable &&
      draft.sourcePath.trim() &&
      draft.captionModelId &&
      (!draft.nlEnabled || (draft.nlProviderId && draft.nlModel)) &&
      (!draft.policyEnabled || draft.policySeed.trim()) &&
      (draft.workMode === 'in_place' || draft.outputPath.trim()),
  )

  const stageTitle: Record<WorkflowStageId, string> = {
    dataset: text.stageDataset,
    caption: text.stageCaption,
    classify: text.stageClassify,
    replace: text.stageReplace,
    ocr: text.stageOcr,
    nl: text.stageNl,
    count: text.stageCount,
    policy: text.stagePolicy,
    token: text.stageToken,
    export: text.stageExport,
  }
  const stageDescription: Record<WorkflowStageId, string> = {
    dataset: text.stageDatasetHint,
    caption: text.stageCaptionHint,
    classify: text.stageClassifyHint,
    replace: text.stageReplaceHint,
    ocr: text.stageOcrHint,
    nl: text.enableNl,
    count: text.stageCountHint,
    policy: text.policyHint,
    token: text.stageTokenHint,
    export: text.stageExportHint,
  }
  const fieldHelp = (key: string) => text.help[key]
  const selectedCaptionModel = loadedModels.find((model) => model.id === draft.captionModelId)
  const enabledStageNames = [
    draft.classifyEnabled && text.stageClassify,
    draft.replaceEnabled && text.stageReplace,
    draft.ocrEnabled && text.stageOcr,
    draft.nlEnabled && text.stageNl,
    text.stageCount,
    draft.policyEnabled && text.stagePolicy,
    draft.tokenBudgetEnabled && text.stageToken,
  ].filter(Boolean).join(' · ')
  const selectedResourceIds = [
    draft.classifyEnabled && draft.classifyResourceId,
    draft.replaceEnabled && draft.replaceResourceId,
    draft.ocrEnabled && draft.ocrResourceId,
    draft.tokenBudgetEnabled && draft.tokenizerResourceId,
  ].filter(Boolean).join(' · ')

  return (
    <div className="page page-dataset-workflow" lang={language === 'zh' ? 'zh-CN' : 'en'}>
      <div className="page-heading">
        <div className="page-heading-copy">
          <p className="eyebrow">DATASET WORKFLOW</p>
          <h1>{text.title}</h1>
          <p className="page-subtitle">{text.subtitle}</p>
        </div>
        <div className="workflow-language" role="group" aria-label={text.languageLabel}>
          <Button
            variant={language === 'zh' ? 'primary' : 'quiet'}
            onClick={() => setLanguage('zh')}
            aria-pressed={language === 'zh'}
          >
            {text.chinese}
          </Button>
          <Button
            variant={language === 'en' ? 'primary' : 'quiet'}
            onClick={() => setLanguage('en')}
            aria-pressed={language === 'en'}
          >
            {text.english}
          </Button>
        </div>
      </div>

      <Notice tone="info">
        <strong>{text.compatibilityTitle}</strong>
        <div>{text.compatibilityBody}</div>
      </Notice>
      {(capabilities.isError || resources.isError) && (
        <Notice tone="danger">
          {language === 'zh'
            ? '无法读取工作流能力或资源目录。为避免提交后端不支持的配置，任务预检已停用。'
            : 'Workflow capabilities or resources are unavailable. Preflight is disabled to avoid submitting an unsupported configuration.'}
        </Notice>
      )}

      <div className="workflow-step-layout">
        <nav className="workflow-step-nav" aria-label={text.workflowSteps}>
          <p className="eyebrow">{text.workflowSteps}</p>
          {WORKFLOW_STAGE_IDS.map((stage, index) => (
            <button
              key={stage}
              type="button"
              className={`workflow-step-button${activeStage === stage ? ' is-active' : ''}`}
              aria-current={activeStage === stage ? 'step' : undefined}
              onClick={() => {
                setActiveStage(stage)
                document.getElementById(`workflow-stage-${stage}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
              }}
            >
              <span className="workflow-step-number">{index + 1}</span>
              <span>
                <strong>{stageTitle[stage]}</strong>
                <small>{stageDescription[stage]}</small>
              </span>
            </button>
          ))}
        </nav>
        <div className="workflow-step-content">
          <Notice tone="info">
            <strong>{stageTitle[activeStage]}</strong>
            <div>{stageDescription[activeStage]}</div>
          </Notice>

      <details className="workflow-advanced-resources">
        <summary>
          <span>
            <strong>{text.advancedResources}</strong>
            <small>{text.advancedResourcesHint}</small>
          </span>
        </summary>
      <Panel title={text.importTitle} eyebrow="Resources">
        <div className="form-grid">
          <Field label={text.importRootId} help={fieldHelp('importRoot')} helpLabels={text.helpLabels}>
            <select aria-label={text.importRootId}
              value={importForm.rootId}
              onChange={(event) => setImportForm({ ...importForm, rootId: event.target.value })}
            >
              <option value="">—</option>
              {inputRoots.map((root) => (
                <option key={root.id} value={root.id}>
                  {root.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label={text.importRelativePath} help={fieldHelp('importRelativePath')} helpLabels={text.helpLabels}>
            <input aria-label={text.importRelativePath}
              value={importForm.relativePath}
              onChange={(event) => setImportForm({ ...importForm, relativePath: event.target.value })}
              placeholder="e621_general_tag_replacement_index.csv"
            />
          </Field>
          <Field label={text.importResourceId} help={fieldHelp('importResourceId')} helpLabels={text.helpLabels}>
            <input aria-label={text.importResourceId}
              value={importForm.resourceId}
              onChange={(event) => setImportForm({ ...importForm, resourceId: event.target.value })}
              placeholder="replace-e621-local-v1"
            />
          </Field>
        </div>
        <div className="workflow-button-row">
          <Button
            icon={<RefreshCw size={15} />}
            onClick={() => previewMutation.mutate()}
            disabled={
              previewMutation.isPending ||
              !importForm.rootId ||
              !importForm.relativePath ||
              !importForm.resourceId
            }
          >
            {text.importPreview}
          </Button>
          <Button
            variant="primary"
            icon={<Upload size={15} />}
            onClick={() => applyMutation.mutate()}
            disabled={applyMutation.isPending || !importPreview?.valid}
          >
            {text.importApply}
          </Button>
        </div>

        {importError && <Notice tone="danger">{importError}</Notice>}
        {importPreview && (
          <Notice tone={importPreview.valid ? 'success' : 'danger'}>
            <strong>
              {importPreview.valid ? text.importPreviewOk : text.importPreviewFailed}
            </strong>
            <div>
              {text.importRuleCount}: {importPreview.rule_count} · {text.importPassthrough}:{' '}
              {importPreview.passthrough_count}
            </div>
            {importPreview.fingerprint && (
              <div className="workflow-mono">
                {text.importFingerprint}: {importPreview.fingerprint.slice(0, 16)}…
              </div>
            )}
            {importPreview.warnings.map((warning) => (
              <div key={warning}>{warning}</div>
            ))}
            {importPreview.errors.slice(0, 10).map((error) => (
              <div key={error}>{error}</div>
            ))}
          </Notice>
        )}
      </Panel>

      <Panel title={text.resourcesTitle} eyebrow="Catalog">
        {resources.data && resources.data.length > 0 ? (
          <table className="workflow-table">
            <thead>
              <tr>
                <th>{text.resourceId}</th>
                <th>{text.resourceCategory}</th>
                <th>{text.resourceFingerprint}</th>
              </tr>
            </thead>
            <tbody>
              {resources.data.map((resource) => (
                <tr key={resource.resource_id}>
                  <td>{resource.resource_id}</td>
                  <td>{resource.category}</td>
                  <td className="workflow-mono">{resource.fingerprint.slice(0, 16)}…</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState icon={<Database size={20} />} title={text.resourcesEmpty} />
        )}
      </Panel>
      </details>

      <aside className="workflow-task-summary" aria-label={text.taskSummary}>
        <p className="eyebrow">{text.taskSummary}</p>
        <strong>{text.summaryOutputMode}</strong>
        <span>{draft.workMode === 'full_copy' ? text.workModeFullCopy : text.workModeInPlace}</span>
        <strong>{text.summarySource}</strong>
        <span>{draft.sourcePath || '—'}</span>
        <strong>{text.summaryCaption}</strong>
        <span>{selectedCaptionModel?.name ?? '—'}</span>
        <strong>{text.summarySteps}</strong>
        <span>{enabledStageNames}</span>
        <strong>{text.summaryResources}</strong>
        <span>{selectedResourceIds || '—'}</span>
        <strong>{text.summaryReview}</strong>
        <span>{text.stageCount}</span>
      </aside>

      <div className="workflow-pipeline-panel" id="workflow-pipeline">
        <Panel title={text.createJobTitle} eyebrow="Pipeline">
          <Panel id="workflow-stage-dataset" title={text.stageDataset} className="workflow-stage-panel">
          <div className="form-grid">
          <Field label={text.profile} help={fieldHelp('profile')} helpLabels={text.helpLabels}>
            <select aria-label={text.profile}
              value={supportedProfiles.includes(draft.profile) ? draft.profile : ''}
              disabled={!capabilities.data}
              onChange={(event) =>
                updateDraft({ profile: event.target.value as WorkflowProfile })
              }
            >
              <option value="">—</option>
              {supportedProfiles.map((profile) => <option key={profile} value={profile}>{profile}</option>)}
            </select>
          </Field>
          <Field label={text.workMode} help={fieldHelp('workMode')} helpLabels={text.helpLabels}>
            <select aria-label={text.workMode}
              value={supportedWorkModes.includes(draft.workMode) ? draft.workMode : ''}
              disabled={!capabilities.data}
              onChange={(event) =>
                updateDraft({ workMode: event.target.value as WorkflowWorkMode })
              }
            >
              <option value="">—</option>
              {supportedWorkModes.map((mode) => (
                <option key={mode} value={mode}>
                  {mode === 'full_copy' ? text.workModeFullCopy : text.workModeInPlace}
                </option>
              ))}
            </select>
          </Field>
          {draft.workMode === 'in_place' && <Notice tone="warning">{text.inPlaceWarning}</Notice>}
          <Field label={text.sourcePath} help={fieldHelp('sourceRoot')} helpLabels={text.helpLabels}>
            <input
              aria-label={text.sourcePath}
              value={draft.sourcePath}
              placeholder="E:\\datasets\\train"
              onChange={(event) => updateDraft({ sourcePath: event.target.value })}
            />
          </Field>
          {pathBindingPreview?.status === 'ready' && !pathBindingError && (
            <Notice tone="success">{text.pathReady}</Notice>
          )}
          {pathBindingError && <Notice tone="danger">{pathBindingError}</Notice>}
          {draft.workMode === 'full_copy' && (
            <>
              <Field label={text.outputPath} help={fieldHelp('outputRoot')} helpLabels={text.helpLabels}>
                <input
                  aria-label={text.outputPath}
                  value={draft.outputPath}
                  placeholder="E:\\datasets\\train_processed"
                  onChange={(event) => updateDraft({ outputPath: event.target.value })}
                />
              </Field>
            </>
          )}
          <Field label={text.exportFormat} help={fieldHelp('exportFormat')} helpLabels={text.helpLabels}>
            <select aria-label={text.exportFormat}
              value={draft.exportFormat}
              onChange={(event) =>
                updateDraft({ exportFormat: event.target.value as WorkflowExportFormat })
              }
            >
              <option value="both">{text.exportBoth}</option>
              <option value="json">{text.exportJson}</option>
              <option value="txt">{text.exportTxt}</option>
            </select>
          </Field>
          </div>
          </Panel>

          <Panel id="workflow-stage-caption" title={text.stageCaption} className="workflow-stage-panel">
          <div className="form-grid">
          <Field label={text.captionModel} help={fieldHelp('captionModel')} helpLabels={text.helpLabels}>
            {loadedModels.length === 0 ? (
              <Notice tone="warning">
                <div>{text.noLoadedModel}</div>
                <Button type="button" size="sm" variant="secondary" icon={<ArrowRight size={14} />} onClick={() => setPage('models')}>
                  {text.chooseModel}
                </Button>
              </Notice>
            ) : loadedModels.length > 1 ? (
              <select aria-label={text.captionModel}
                value={draft.captionModelId ?? ''}
                onChange={(event) => {
                  captionSelectionSource.current = 'user'
                  updateDraft({ captionModelId: event.target.value })
                }}
              >
                <option value="">—</option>
                {loadedModels.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}
              </select>
            ) : (
              <select aria-label={text.captionModel} value={draft.captionModelId ?? ''} disabled>
                <option value={loadedModels[0]?.id}>{loadedModels[0]?.name}</option>
              </select>
            )}
          </Field>
          <div className="workflow-hint">{loadedModels.length > 1 ? text.multipleModels : text.captionRuntimeHint}</div>
          {selectedCaptionModel && <div className="workflow-hint">{selectedCaptionModel.backend.toUpperCase()} / {text.captionModelDetails}: {selectedCaptionModel.threshold_source ?? 'model'}</div>}
          </div>
          </Panel>

          <Panel id="workflow-stage-classify" title={text.stageClassify} className="workflow-stage-panel">
          <div className="form-grid">
          <Field label={text.enableClassify} help={fieldHelp('classifyEnabled')} helpLabels={text.helpLabels}>
            <select aria-label={text.enableClassify}
              value={draft.classifyEnabled ? 'yes' : 'no'}
              onChange={(event) =>
                updateDraft({ classifyEnabled: event.target.value === 'yes' })
              }
            >
              <option value="no">{text.no}</option>
              <option value="yes">{text.yes}</option>
            </select>
          </Field>
          {draft.classifyEnabled && (
              <Field label={text.classifyResource} help={fieldHelp('classifyResource')} helpLabels={text.helpLabels}>
              <select aria-label={text.classifyResource}
                value={draft.classifyResourceId}
                onChange={(event) => updateDraft({ classifyResourceId: event.target.value })}
              >
                <option value="">—</option>
                {classifyResources.map((resource) => (
                  <option key={resource.resource_id} value={resource.resource_id}>
                    {resource.resource_id}
                  </option>
                ))}
              </select>
            </Field>
          )}
          </div>
          </Panel>

          <Panel id="workflow-stage-replace" title={text.stageReplace} className="workflow-stage-panel">
          <div className="form-grid">
          <Field label={text.enableReplace} help={fieldHelp('replaceEnabled')} helpLabels={text.helpLabels}>
            <select aria-label={text.enableReplace}
              value={draft.replaceEnabled ? 'yes' : 'no'}
              onChange={(event) =>
                updateDraft({ replaceEnabled: event.target.value === 'yes' })
              }
            >
              <option value="yes">{text.yes}</option>
              <option value="no">{text.no}</option>
            </select>
          </Field>
          {draft.replaceEnabled && (
            <>
              <Field label={text.replaceResource} help={fieldHelp('replaceResource')} helpLabels={text.helpLabels}>
                <select aria-label={text.replaceResource}
                  value={draft.replaceResourceId}
                  onChange={(event) => updateDraft({ replaceResourceId: event.target.value })}
                >
                  <option value="">—</option>
                  {replacementResources.map((resource) => (
                      <option key={resource.resource_id} value={resource.resource_id}>
                        {resource.resource_id}
                      </option>
                    ))}
                </select>
              </Field>
              {draft.replaceResourceId === DEFAULT_REPLACEMENT_RESOURCE_ID && (
                <Notice tone="info">{text.replacePassDropNotice}</Notice>
              )}
            </>
          )}
          </div>
          </Panel>

          <Panel id="workflow-stage-ocr" title={text.stageOcr} className="workflow-stage-panel">
          <div className="form-grid">
          <Field label={text.enableOcr} help={fieldHelp('ocrEnabled')} helpLabels={text.helpLabels}>
            <select aria-label={text.enableOcr}
              value={draft.ocrEnabled ? 'yes' : 'no'}
              onChange={(event) =>
                updateDraft({ ocrEnabled: event.target.value === 'yes' })
              }
            >
              <option value="no">{text.no}</option>
              <option value="yes">{text.yes}</option>
            </select>
          </Field>
          {draft.ocrEnabled && (
            <>
              <Field label={text.ocrResource} help={fieldHelp('ocrResource')} helpLabels={text.helpLabels}>
                <select aria-label={text.ocrResource}
                  value={draft.ocrResourceId}
                  onChange={(event) => updateDraft({ ocrResourceId: event.target.value })}
                >
                  <option value="">—</option>
                  {ocrResources.map((resource) => (
                    <option key={resource.resource_id} value={resource.resource_id}>
                      {resource.resource_id}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={text.ocrMinConfidence} help={fieldHelp('ocrMinConfidence')} helpLabels={text.helpLabels}>
                <input aria-label={text.ocrMinConfidence}
                  type="number"
                  min={0}
                  max={1}
                  step={0.05}
                  value={draft.ocrMinConfidence}
                  onChange={(event) =>
                    updateDraft({ ocrMinConfidence: Number(event.target.value) })
                  }
                />
              </Field>
            </>
          )}
          </div>
          </Panel>

          <Panel id="workflow-stage-nl" title={text.stageNl} className="workflow-stage-panel">
          <div className="form-grid">
          <Field label={text.enableNl}>
            <select aria-label={text.enableNl}
              value={draft.nlEnabled ? 'yes' : 'no'}
              onChange={(event) => updateDraft({ nlEnabled: event.target.value === 'yes' })}
            >
              <option value="no">{text.no}</option>
              <option value="yes">{text.yes}</option>
            </select>
          </Field>
          {draft.nlEnabled && enabledProviders.length === 0 && (
            <Notice tone="warning">
              <div>{text.nlProvider}: —</div>
              <Button type="button" size="sm" variant="secondary" icon={<ArrowRight size={14} />} onClick={() => setPage('providers')}>
                {text.nlProvider}
              </Button>
            </Notice>
          )}
          {draft.nlEnabled && enabledProviders.length > 0 && (
            <>
              <Field label={text.nlProvider}>
                <select aria-label={text.nlProvider}
                  value={draft.nlProviderId}
                  onChange={(event) => updateDraft({ nlProviderId: event.target.value, nlModel: '' })}
                >
                  <option value="">—</option>
                  {enabledProviders.map((provider) => (
                    <option key={provider.id} value={provider.id}>{provider.name}</option>
                  ))}
                </select>
              </Field>
              <Field label={text.nlModel}>
                <select aria-label={text.nlModel}
                  value={draft.nlModel}
                  onChange={(event) => updateDraft({ nlModel: event.target.value })}
                >
                  <option value="">—</option>
                  {(providerModels.data?.items ?? []).map((model) => (
                    <option key={model.id} value={model.id}>{model.name ?? model.id}</option>
                  ))}
                  {draft.nlModel && !(providerModels.data?.items ?? []).some((model) => model.id === draft.nlModel) && (
                    <option value={draft.nlModel}>{draft.nlModel}</option>
                  )}
                </select>
              </Field>
              <Field label={text.nlPreset}>
                <select aria-label={text.nlPreset} value={draft.nlPromptPreset}
                  onChange={(event) => updateDraft({ nlPromptPreset: event.target.value as JobDraft['nlPromptPreset'] })}
                >
                  <option value="general">general</option>
                  <option value="style">style</option>
                  <option value="character">character</option>
                </select>
              </Field>
              <Field label={text.nlLength}>
                <select aria-label={text.nlLength} value={draft.nlLength}
                  onChange={(event) => updateDraft({ nlLength: event.target.value as JobDraft['nlLength'] })}
                >
                  <option value="short">short</option>
                  <option value="medium">medium</option>
                  <option value="long">long</option>
                </select>
              </Field>
              <Field label={text.nlReuseOriginal}>
                <select aria-label={text.nlReuseOriginal} value={draft.nlReuseOriginal ? 'yes' : 'no'}
                  onChange={(event) => updateDraft({ nlReuseOriginal: event.target.value === 'yes' })}
                >
                  <option value="yes">{text.yes}</option>
                  <option value="no">{text.no}</option>
                </select>
              </Field>
              <Field label={text.nlUseImage}>
                <select aria-label={text.nlUseImage} value={draft.nlUseImage ? 'yes' : 'no'}
                  onChange={(event) => updateDraft({ nlUseImage: event.target.value === 'yes' })}
                >
                  <option value="yes">{text.yes}</option>
                  <option value="no">{text.no}</option>
                </select>
              </Field>
              <Field label={text.nlUseFullJson}>
                <select aria-label={text.nlUseFullJson} value={draft.nlUseFullJson ? 'yes' : 'no'}
                  onChange={(event) => updateDraft({ nlUseFullJson: event.target.value === 'yes' })}
                >
                  <option value="no">{text.no}</option>
                  <option value="yes">{text.yes}</option>
                </select>
              </Field>
            </>
          )}
          </div>
          </Panel>

          <Panel id="workflow-stage-policy" title={text.stagePolicy} className="workflow-stage-panel">
          <div className="form-grid">
            <Field label={text.enablePolicy}>
              <select aria-label={text.enablePolicy}
                value={draft.policyEnabled ? 'yes' : 'no'}
                onChange={(event) => updateDraft({ policyEnabled: event.target.value === 'yes' })}
              >
                <option value="no">{text.no}</option>
                <option value="yes">{text.yes}</option>
              </select>
            </Field>
            {draft.policyEnabled && (
              <Field label={text.policySeed}>
                <input aria-label={text.policySeed} value={draft.policySeed}
                  onChange={(event) => updateDraft({ policySeed: event.target.value })}
                />
              </Field>
            )}
          </div>
          {draft.policyEnabled && (
            <>
              <Notice tone="info">{text.policyHint}</Notice>
              <details className="workflow-policy-advanced">
                <summary>{text.policyAdvanced}</summary>
                <div className="form-grid">
                  {([
                    ['policyArtistDropout', text.policyArtistDropout],
                    ['policyQualityDropout', text.policyQualityDropout],
                    ['policySoloDropNl', text.policySoloDropNl],
                    ['policySoloDropAppearance', text.policySoloDropAppearance],
                    ['policyNonSoloDropNl', text.policyNonSoloDropNl],
                    ['policyNonSoloDropAppearance', text.policyNonSoloDropAppearance],
                  ] as const).map(([key, label]) => (
                    <Field key={key} label={label}>
                      <input aria-label={label} type="number" min={0} max={1} step={0.05}
                        value={draft[key]}
                        onChange={(event) => updateDraft({ [key]: Number(event.target.value) })}
                      />
                    </Field>
                  ))}
                </div>
              </details>
            </>
          )}
          </Panel>

          <Panel id="workflow-stage-token" title={text.stageToken} className="workflow-stage-panel">
          <div className="form-grid">
          <Field label={text.enableTokenBudget} help={fieldHelp('tokenBudgetEnabled')} helpLabels={text.helpLabels}>
            <select aria-label={text.enableTokenBudget}
              value={draft.tokenBudgetEnabled ? 'yes' : 'no'}
              onChange={(event) =>
                updateDraft({ tokenBudgetEnabled: event.target.value === 'yes' })
              }
            >
              <option value="no">{text.no}</option>
              <option value="yes">{text.yes}</option>
            </select>
          </Field>
          {draft.tokenBudgetEnabled && (
            <>
              <Field label={text.tokenizerResource} help={fieldHelp('tokenizerResource')} helpLabels={text.helpLabels}>
                <select aria-label={text.tokenizerResource}
                  value={draft.tokenizerResourceId}
                  onChange={(event) =>
                    updateDraft({ tokenizerResourceId: event.target.value })
                  }
                >
                  <option value="">—</option>
                  {tokenizerResources.map((resource) => (
                    <option key={resource.resource_id} value={resource.resource_id}>
                      {resource.resource_id}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={text.tokenMaxTokens} help={fieldHelp('tokenMaxTokens')} helpLabels={text.helpLabels}>
                <input aria-label={text.tokenMaxTokens}
                  type="number"
                  min={1}
                  max={8192}
                  value={draft.tokenMaxTokens}
                  onChange={(event) =>
                    updateDraft({ tokenMaxTokens: Number(event.target.value) })
                  }
                />
              </Field>
            </>
          )}
          <Field label={text.recursive} help={fieldHelp('recursive')} helpLabels={text.helpLabels}>
            <select aria-label={text.recursive}
              value={draft.recursive ? 'yes' : 'no'}
              onChange={(event) => updateDraft({ recursive: event.target.value === 'yes' })}
            >
              <option value="no">{text.no}</option>
              <option value="yes">{text.yes}</option>
            </select>
          </Field>
          </div>
          </Panel>

          <Panel id="workflow-stage-export" title={text.stageExport} className="workflow-stage-panel">
        <div className="workflow-summary-card">
          <strong>{text.stageExportHint}</strong>
          <div>{text.summaryOutputMode}: {draft.workMode === 'full_copy' ? text.workModeFullCopy : text.workModeInPlace}</div>
          <div>{text.summarySource}: {draft.sourcePath || '—'}</div>
          {draft.workMode === 'full_copy' && <div>{text.summaryDestination}: {draft.outputPath || '—'}</div>}
          <div>{text.summaryCaption}: {selectedCaptionModel?.name ?? '—'}{selectedCaptionModel ? ` · ${selectedCaptionModel.backend.toUpperCase()}` : ''}</div>
          <div>{text.summarySteps}: {enabledStageNames}</div>
          <div>{text.summaryResources}: {selectedResourceIds || '—'}</div>
          <div>{text.summaryReview}: {text.stageCount}</div>
        </div>

        <div className="workflow-button-row">
          <Button
            onClick={() => preflightMutation.mutate()}
            disabled={preflightMutation.isPending || !canPreflight}
          >
            {text.checkSettings}
          </Button>
          <HelpPopover label={text.checkSettings} help={fieldHelp('preflight')!} labels={text.helpLabels} />
          <Button
            variant="primary"
            onClick={() => createMutation.mutate()}
            disabled={createMutation.isPending || !preflight?.valid}
          >
            {text.createPendingTask}
          </Button>
          <HelpPopover label={text.createPendingTask} help={fieldHelp('createJob')!} labels={text.helpLabels} />
        </div>

        {preflightError && <Notice tone="danger">{preflightError}</Notice>}
        {createNotice && <Notice tone="success">{createNotice}</Notice>}
        {!preflight && <div className="workflow-hint">{text.summaryNotChecked}</div>}
        {preflight && (
          <Notice tone={preflight.valid ? 'success' : 'danger'}>
            <strong>
              {preflight.valid ? text.preflightOk : text.preflightFailed}
            </strong>
            {preflight.warnings.length > 0 && (
              <div>
                {text.warnings}: {preflight.warnings.join('; ')}
              </div>
            )}
            {preflight.errors.map((error) => (
              <div key={error}>{error}</div>
            ))}
          </Notice>
        )}
      </Panel>
      </Panel>
      </div>
        </div>
      </div>

      {pathCreationConfirmOpen && (
        <DialogLayer className="workflow-dialog-backdrop" onClose={() => setPathCreationConfirmOpen(false)}>
          <div
            className="workflow-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="workflow-create-output-title"
          >
            <h2 id="workflow-create-output-title">{text.pathCreateConfirm}</h2>
            <p>{text.pathCreateRequired}</p>
            <code>{draft.outputPath}</code>
            <div className="workflow-actions">
              <Button variant="quiet" onClick={() => setPathCreationConfirmOpen(false)}>
                {text.pathCreateCancel}
              </Button>
              <Button
                variant="primary"
                onClick={() => bindPathsMutation.mutate(true)}
                disabled={bindPathsMutation.isPending}
              >
                {text.pathCreateOutput}
              </Button>
            </div>
          </div>
        </DialogLayer>
      )}

      <Panel
        title={text.jobsTitle}
        eyebrow="Jobs"
        actions={
          <Button
            variant="quiet"
            icon={<RefreshCw size={15} />}
            onClick={() => void jobs.refetch()}
          >
            {text.refresh}
          </Button>
        }
      >
        {jobs.data && jobs.data.length > 0 ? (
          <table className="workflow-table">
            <thead>
              <tr>
                <th>{text.jobId}</th>
                <th>{text.status}</th>
                <th>{text.profile}</th>
                <th>{text.samples}</th>
                <th>{text.module}</th>
                <th>{text.created}</th>
                <th>{text.pinJob}</th>
              </tr>
            </thead>
            <tbody>
              {jobs.data.map((job) => (
                <tr
                  key={job.job_id}
                  className={job.job_id === selectedJobId ? 'row-active' : ''}
                >
                  <td className="workflow-mono">
                    <button
                      type="button"
                      className="workflow-job-select"
                      aria-pressed={job.job_id === selectedJobId}
                      onClick={() => selectJob(job.job_id)}
                    >
                      {job.job_id.slice(0, 12)}…
                    </button>
                  </td>
                  <td>
                    <StatusBadge state={job.status} />
                  </td>
                  <td>{job.profile}</td>
                  <td>
                    {job.processed_samples}/{job.total_samples}
                  </td>
                  <td>{job.current_module_id ?? '—'}</td>
                  <td className="workflow-mono">{job.created_at}</td>
                  <td>
                    {job.pinned ? (
                      <Pin size={15} aria-label={text.pinnedJob} />
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState icon={<Database size={20} />} title={text.jobsEmpty} />
        )}
      </Panel>

      {selectedJob && (
        <Panel title={text.jobControlsTitle} eyebrow={selectedJob.job_id.slice(0, 12)}>
          {countError && <Notice tone="danger">{countError}</Notice>}
          <div className="workflow-actions">
            <StatusBadge state={selectedJob.status} />
            <Button
              variant="quiet"
              onClick={() => pinJob.mutate(!selectedJob.pinned)}
              disabled={pinJob.isPending}
              aria-pressed={Boolean(selectedJob.pinned)}
              title={selectedJob.pinned ? text.unpinJob : text.pinJob}
            >
              {selectedJob.pinned ? (
                <PinOff size={15} aria-hidden="true" />
              ) : (
                <Pin size={15} aria-hidden="true" />
              )}
              {selectedJob.pinned ? text.unpinJob : text.pinJob}
            </Button>
            {selectedJob.status === 'pending' && (
              <>
                <Button
                  onClick={() => startJob.mutate()}
                  disabled={startJob.isPending}
                >
                  <Play size={15} aria-hidden="true" />
                  {text.startJob}
                </Button>
                <HelpPopover label={text.startJob} help={fieldHelp('startJob')!} labels={text.helpLabels} />
              </>
            )}
            {selectedJob.status === 'running' && (
              <Button
                variant="secondary"
                onClick={() => jobAction.mutate('pause')}
                disabled={jobAction.isPending}
              >
                <Pause size={15} aria-hidden="true" />
                {text.pauseJob}
              </Button>
            )}
            {selectedJob.status === 'paused' && (
              <Button onClick={() => jobAction.mutate('resume')} disabled={jobAction.isPending}>
                <Play size={15} aria-hidden="true" />
                {text.resumeJob}
              </Button>
            )}
            {[
              'queued',
              'running',
              'waiting_count_review',
              'waiting_token_review',
              'pausing',
              'paused',
            ].includes(selectedJob.status) && (
              <Button
                variant="danger"
                onClick={() => jobAction.mutate('cancel')}
                disabled={jobAction.isPending}
              >
                <Square size={14} aria-hidden="true" />
                {text.cancelJob}
              </Button>
            )}
            {!selectedJob.discarded_at &&
              ['interrupted', 'failed', 'rollback_required'].includes(selectedJob.status) && (
              <Button
                variant="secondary"
                onClick={() => jobAction.mutate('recover')}
                disabled={jobAction.isPending}
              >
                <RotateCcw size={15} aria-hidden="true" />
                {text.recoverJob}
              </Button>
            )}
            {!selectedJob.discarded_at &&
              selectedJob.work_mode === 'in_place' &&
              ['completed', 'failed', 'cancelled', 'interrupted', 'rollback_required'].includes(selectedJob.status) && (
                <Button
                  variant="outline"
                  onClick={() => restoreJob.mutate()}
                  disabled={restoreJob.isPending}
                >
                  <Undo2 size={15} aria-hidden="true" />
                  {text.restoreJob}
                </Button>
              )}
            {!selectedJob.discarded_at &&
              ['completed', 'failed', 'cancelled', 'interrupted', 'rollback_required'].includes(selectedJob.status) && (
              <Button
                variant="quiet"
                onClick={() => discardJob.mutate(selectedJob.job_id)}
                disabled={discardJob.isPending || selectedJob.pinned === true}
                title={selectedJob.pinned ? text.discardPinnedHint : undefined}
              >
                <Archive size={15} aria-hidden="true" />
                {text.discardJob}
              </Button>
            )}
            {!selectedJob.discarded_at && (
              <Button
                variant="quiet"
                onClick={() => repairJob.mutate()}
                disabled={repairJob.isPending}
              >
                <Wrench size={15} aria-hidden="true" />
                {text.repairJob}
              </Button>
            )}
          </div>
          {selectedJob.pinned && (
            <Notice tone="info">
              <Pin size={14} aria-hidden="true" /> {text.discardPinnedHint}
            </Notice>
          )}
          {repair && (
            <dl className="workflow-summary" aria-label={text.repairResult}>
              <div>
                <dt>{text.reclaimed}</dt>
                <dd>{repair.reclaimed_samples}</dd>
              </div>
              <div>
                <dt>{text.parked}</dt>
                <dd>{repair.parked_samples}</dd>
              </div>
              <div>
                <dt>{text.resumableSamples}</dt>
                <dd>{repair.resumable_samples}</dd>
              </div>
              <div>
                <dt>{text.committedFiles}</dt>
                <dd>{repair.committed_files}</dd>
              </div>
              <div>
                <dt>{text.journalState}</dt>
                <dd className="workflow-mono">{repair.journal_state}</dd>
              </div>
            </dl>
          )}
        </Panel>
      )}

      {selectedJobId && (
        <Panel
          title={text.eventsTitle}
          eyebrow={`${text.eventCursor} ${workflowEvents.cursor}`}
        >
          {workflowEvents.error && <Notice tone="warning">{text.eventsError}</Notice>}
          {workflowEvents.events.length === 0 ? (
            <EmptyState icon={<RefreshCw size={20} />} title={text.eventsEmpty} />
          ) : (
            <div className="workflow-review-list" aria-live="polite">
              {workflowEvents.events.map((event) => (
                <article key={`${event.event_id ?? event.seq ?? 0}-${event.event_type}`} className="workflow-review-card">
                  <div className="workflow-actions">
                    <strong>{event.event_type ?? 'workflow'}</strong>
                    {event.to_status && <StatusBadge state={event.to_status} />}
                    <span className="workflow-mono">#{event.event_id ?? event.seq ?? '—'}</span>
                  </div>
                  <div className="workflow-hint">
                    {event.from_status && event.to_status
                      ? `${event.from_status} → ${event.to_status}`
                      : event.to_status ?? event.from_status ?? ''}
                    {event.created_at ? ` · ${event.created_at}` : ''}
                  </div>
                </article>
              ))}
            </div>
          )}
        </Panel>
      )}

      {selectedJobId && countReview.data?.items && (
        <Panel
          id="workflow-stage-count"
          title={text.countReviewTitle}
          eyebrow={`${text.countReviewPending} ${countReview.data.pending ?? 0}`}
          actions={<HelpPopover label={text.countReviewTitle} help={fieldHelp('countReview')!} labels={text.helpLabels} />}
        >
          {countError && <Notice tone="danger">{countError}</Notice>}
          {countReview.data.items.length === 0 ? (
            <EmptyState icon={<CheckCircle2 size={20} />} title={text.countReviewEmpty} />
          ) : (
            <div className="workflow-review-list">
              {countReview.data.items.map((decision) => (
                <article key={decision.sample_id} className="workflow-review-card">
                  <header>
                    <span className="workflow-mono">{decision.relative_image_path}</span>
                    {decision.status === 'confirmed' && (
                      <StatusBadge state={text.countConfirmed} />
                    )}
                    {decision.conflict && <Notice tone="warning">{text.countConflict}</Notice>}
                  </header>
                  <dl className="workflow-summary">
                    <div>
                      <dt>{text.countProposed}</dt>
                      <dd>{decision.proposed_count || '—'}</dd>
                    </div>
                    <div>
                      <dt>{text.countSource}</dt>
                      <dd>{decision.selected_source}</dd>
                    </div>
                    <div>
                      <dt>{text.countEvidence}</dt>
                      <dd>
                        {[
                          decision.original_normalized
                            ? `json:${decision.original_normalized}`
                            : null,
                          decision.wiki_value ? `wiki:${decision.wiki_value}` : null,
                          decision.matched_tags.length
                            ? `tags:${decision.matched_tags.join(' ')}`
                            : null,
                        ]
                          .filter(Boolean)
                          .join(' · ') || '—'}
                      </dd>
                    </div>
                  </dl>
                  <div className="workflow-actions">
                    {COUNT_VALUES.map((value) => (
                      <Button
                        key={value}
                        variant={value === decision.count_value ? 'primary' : 'secondary'}
                        onClick={() =>
                          resolveCount.mutate({ decision, count: value })
                        }
                        disabled={resolveCount.isPending}
                      >
                        {text.countApply} {value}
                      </Button>
                    ))}
                  </div>
                </article>
              ))}
            </div>
          )}
          {(countPage > 0 || countReview.data.items.length === REVIEW_PAGE_SIZE) && (
            <nav className="workflow-pagination" aria-label={language === 'zh' ? '数量审核分页' : 'Count review pagination'}>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                disabled={countPage === 0 || countReview.isFetching}
                onClick={() => setCountPage((page) => Math.max(0, page - 1))}
              >
                {language === 'zh' ? '上一页' : 'Previous'}
              </Button>
              <span aria-live="polite">{language === 'zh' ? `第 ${countPage + 1} 页` : `Page ${countPage + 1}`}</span>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                disabled={countReview.data.items.length < REVIEW_PAGE_SIZE || countReview.isFetching}
                onClick={() => setCountPage((page) => page + 1)}
              >
                {language === 'zh' ? '下一页' : 'Next'}
              </Button>
            </nav>
          )}
          <div className="workflow-actions">
            <Button
              onClick={() => confirmCount.mutate()}
              disabled={confirmCount.isPending || (countReview.data.pending ?? 0) > 0}
            >
              <CheckCircle2 size={15} aria-hidden="true" />
              {text.countConfirm}
            </Button>
          </div>
        </Panel>
      )}

      {selectedJobId && tokenReview.data?.items && (
        <Panel
          title={text.tokenReviewTitle}
          eyebrow={`${text.tokenReviewUnresolved} ${tokenReview.data.unresolved ?? 0}`}
        >
          {tokenError && <Notice tone="danger">{tokenError}</Notice>}
          {tokenReview.data.items.length === 0 ? (
            <EmptyState icon={<CheckCircle2 size={20} />} title={text.tokenReviewEmpty} />
          ) : (
            <>
              <Notice tone="info">{text.tokenNotApplied}</Notice>
              <div className="workflow-review-list">
                {tokenReview.data.items.map((item) => (
                  <article key={item.sample_id} className="workflow-review-card">
                    <header>
                      <StatusBadge state={item.status} />
                      <span className="workflow-mono">#{item.sample_id}</span>
                    </header>
                    <dl className="workflow-summary">
                      <div>
                        <dt>{text.tokenCount}</dt>
                        <dd>{item.token_count}</dd>
                      </div>
                      <div>
                        <dt>{text.tokenLimit}</dt>
                        <dd>{item.token_limit}</dd>
                      </div>
                      <div>
                        <dt>{text.tokenOverBy}</dt>
                        <dd>{item.over_by}</dd>
                      </div>
                      <div>
                        <dt>{text.tokenProposal}</dt>
                        <dd>
                          {item.proposal_text === null
                            ? '—'
                            : `${item.proposal_text} (${item.proposal_token_count ?? '?'})`}
                        </dd>
                      </div>
                    </dl>
                    <Field label={text.tokenCaption}>
                      <textarea
                        rows={3}
                        value={tokenDraft[item.sample_id] ?? item.proposal_text ?? item.nl_text}
                        onChange={(event) =>
                          setTokenDraft({ ...tokenDraft, [item.sample_id]: event.target.value })
                        }
                      />
                    </Field>
                    <div className="workflow-actions">
                      <Button
                        variant="secondary"
                        onClick={() =>
                          reviewToken.mutate({
                            item,
                            action: 'edit',
                            text: tokenDraft[item.sample_id] ?? item.proposal_text ?? item.nl_text,
                          })
                        }
                        disabled={reviewToken.isPending || item.status === 'applied'}
                      >
                        {text.tokenEdit}
                      </Button>
                      <Button
                        variant="secondary"
                        onClick={() =>
                          reviewToken.mutate({
                            item,
                            action: 'rewrite_short',
                            text: tokenDraft[item.sample_id] ?? item.proposal_text ?? item.nl_text,
                          })
                        }
                        disabled={reviewToken.isPending || item.status === 'applied'}
                      >
                        {text.tokenRewriteShort}
                      </Button>
                      <Button
                        variant="quiet"
                        onClick={() => reviewToken.mutate({ item, action: 'recount' })}
                        disabled={reviewToken.isPending || item.status === 'applied'}
                      >
                        {text.tokenRecount}
                      </Button>
                      <Button
                        onClick={() => reviewToken.mutate({ item, action: 'apply' })}
                        disabled={
                          reviewToken.isPending ||
                          item.status === 'applied' ||
                          item.proposal_text === null
                        }
                      >
                        {text.tokenApply}
                      </Button>
                    </div>
                  </article>
                ))}
              </div>
            </>
          )}
          {(tokenPage > 0 || tokenReview.data.items.length === REVIEW_PAGE_SIZE) && (
            <nav className="workflow-pagination" aria-label={language === 'zh' ? 'Token 审核分页' : 'Token review pagination'}>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                disabled={tokenPage === 0 || tokenReview.isFetching}
                onClick={() => setTokenPage((page) => Math.max(0, page - 1))}
              >
                {language === 'zh' ? '上一页' : 'Previous'}
              </Button>
              <span aria-live="polite">{language === 'zh' ? `第 ${tokenPage + 1} 页` : `Page ${tokenPage + 1}`}</span>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                disabled={tokenReview.data.items.length < REVIEW_PAGE_SIZE || tokenReview.isFetching}
                onClick={() => setTokenPage((page) => page + 1)}
              >
                {language === 'zh' ? '下一页' : 'Next'}
              </Button>
            </nav>
          )}
          <div className="workflow-actions">
            <Button
              onClick={() => confirmTokenReview.mutate()}
              disabled={confirmTokenReview.isPending || (tokenReview.data.unresolved ?? 0) > 0}
            >
              <CheckCircle2 size={15} aria-hidden="true" />
              {text.tokenConfirm}
            </Button>
          </div>
        </Panel>
      )}

      {selectedJobId && jobReport.data?.available && jobReport.data.report?.ocr && (
        <Panel title={text.ocrTitle} eyebrow="OCR">
          {(() => {
            const ocr = jobReport.data.report?.ocr ?? {}
            const processed = ocr.processed ?? 0
            const failed = ocr.failed ?? 0
            const regions = ocr.regions ?? 0
            if (processed === 0 && failed === 0) {
              return <p className="workflow-empty">{text.ocrEmpty}</p>
            }
            return (
              <>
                <dl className="workflow-summary">
                  <div>
                    <dt>{text.ocrProcessed}</dt>
                    <dd>{processed}</dd>
                  </div>
                  <div>
                    <dt>{text.ocrFailed}</dt>
                    <dd>{failed}</dd>
                  </div>
                  <div>
                    <dt>{text.ocrRegions}</dt>
                    <dd>{regions}</dd>
                  </div>
                </dl>
                <p className="workflow-hint">{text.ocrUnavailableHint}</p>
              </>
            )
          })()}
        </Panel>
      )}

      {selectedJobId && (
        <Panel title={text.issuesTitle} eyebrow={selectedJobId.slice(0, 12)}>
          {issues.data && issues.data.length > 0 ? (
            <table className="workflow-table">
              <thead>
                <tr>
                  <th>{text.severity}</th>
                  <th>{text.blocking}</th>
                  <th>{text.module}</th>
                  <th>{text.code}</th>
                  <th>{text.message}</th>
                </tr>
              </thead>
              <tbody>
                {issues.data.map((issue) => (
                  <tr key={issue.issue_id}>
                    <td>
                      {issue.severity === 'error' ? (
                        <AlertTriangle size={15} aria-hidden="true" />
                      ) : null}
                      {issue.severity}
                    </td>
                    <td>{issue.blocking ? text.yes : text.no}</td>
                    <td>{issue.module_id}</td>
                    <td className="workflow-mono">{issue.code}</td>
                    <td>{issue.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <EmptyState icon={<CheckCircle2 size={20} />} title={text.issuesEmpty} />
          )}
        </Panel>
      )}
    </div>
  )
}
