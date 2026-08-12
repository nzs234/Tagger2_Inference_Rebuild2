import type {
  CreateJobRequest,
  ClassifierProfile,
  JobListResponse,
  JobResultsResponse,
  JobSummary,
  ModelProfile,
  ModelDownload,
  ProviderProfile,
  ProviderKind,
  ProviderProtocol,
  PromptDefaults,
  RootInfo,
  RuntimeSettings,
  ScanResponse,
  UploadResponse,
  WorkflowCapabilities,
  WorkflowCountReviewPage,
  WorkflowImportPreview,
  WorkflowIssue,
  WorkflowJobReport,
  WorkflowJobEvent,
  WorkflowJobSummary,
  WorkflowPreflightReport,
  WorkflowRepairReport,
  WorkflowResource,
  WorkflowJobStatus,
  WorkflowTokenReviewAction,
  WorkflowTokenReviewItem,
  WorkflowTokenReviewPage,
  WorkflowTokenReviewStatus,
  Fl2vaSingleImageRole,
  VideoPromptMode,
  VideoPromptPackage,
} from '../types'

export const API_BASE = (import.meta.env.VITE_API_BASE_URL || '/api/v1').replace(/\/$/, '')

export class ApiError extends Error {
  code: string
  status: number
  requestId?: string
  retryable: boolean

  constructor(message: string, status: number, code = 'request_failed', requestId?: string, retryable = false) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.requestId = requestId
    this.retryable = retryable
  }
}

function authHeaders(): HeadersInit {
  const token = sessionStorage.getItem('tagger2_access_token')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (!(init.body instanceof FormData) && init.body !== undefined) {
    headers.set('Content-Type', 'application/json')
  }
  const auth = authHeaders() as Record<string, string>
  Object.entries(auth).forEach(([key, value]) => headers.set(key, value))
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers })

  if (!response.ok) {
    let body: Record<string, unknown> = {}
    try {
      body = (await response.json()) as Record<string, unknown>
    } catch {
      // A non-JSON proxy error still becomes a stable client error.
    }
    const detail = body.detail && typeof body.detail === 'object' ? body.detail as Record<string, unknown> : undefined
    const message = typeof body.message === 'string'
      ? body.message
      : typeof detail?.message === 'string'
        ? detail.message
        : typeof body.detail === 'string'
          ? body.detail
          : `请求失败 (${response.status})`
    const code = typeof body.code === 'string' ? body.code : typeof detail?.code === 'string' ? detail.code : 'request_failed'
    const requestId = typeof body.request_id === 'string' ? body.request_id : response.headers.get('x-request-id') ?? undefined
    const retryable = body.retryable === true || detail?.retryable === true
    throw new ApiError(
      message,
      response.status,
      code,
      requestId,
      retryable,
    )
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  health: () => request<{ status: string; version?: string }>('/health'),
  promptDefaults: () => request<PromptDefaults>('/prompts/defaults'),
  upload: async (files: File[]): Promise<UploadResponse> => {
    const form = new FormData()
    files.forEach((file) => form.append('files', file, file.name))
    return request<UploadResponse>('/uploads', { method: 'POST', body: form })
  },
  generateVideoPrompt: async (body: {
    images: File[]
    providerId: string
    providerModel?: string
    instruction: string
    promptMode: VideoPromptMode
    fl2vaSingleImageRole?: Fl2vaSingleImageRole
    currentPackage?: VideoPromptPackage
  }, signal?: AbortSignal): Promise<VideoPromptPackage> => {
    const form = new FormData()
    body.images.forEach((image) => form.append('images', image, image.name))
    form.append('provider_id', body.providerId)
    if (body.providerModel?.trim()) form.append('provider_model', body.providerModel.trim())
    form.append('instruction', body.instruction)
    form.append('prompt_mode', body.promptMode)
    if (body.promptMode === 'fl2va') form.append('fl2va_single_image_role', body.fl2vaSingleImageRole ?? 'first')
    if (body.currentPackage) form.append('current_package_json', JSON.stringify(body.currentPackage))
    return request<VideoPromptPackage>('/video-prompts/generate', { method: 'POST', body: form, signal })
  },
  scan: (body: Record<string, unknown>) => request<ScanResponse>('/scans', { method: 'POST', body: JSON.stringify(body) }),
  roots: () => request<{ items: RootInfo[] }>('/roots'),
  addRoot: (body: { name: string; kind: string; path: string }) =>
    request<RootInfo>('/roots', { method: 'POST', body: JSON.stringify(body) }),
  models: () => request<{ items: ModelProfile[] }>('/models'),
  startModelDownload: (body: { url: string; revision?: string }) =>
    request<ModelDownload>('/models/downloads', { method: 'POST', body: JSON.stringify(body) }),
  modelDownload: (id: string) =>
    request<ModelDownload>(`/models/downloads/${encodeURIComponent(id)}`),
  classifiers: () => request<{ items: ClassifierProfile[] }>('/classifiers'),
  loadClassifier: (id: ClassifierProfile['id']) =>
    request<ClassifierProfile>(`/classifiers/${encodeURIComponent(id)}/load`, { method: 'POST', body: '{}' }),
  unloadClassifier: (id: ClassifierProfile['id']) =>
    request<ClassifierProfile>(`/classifiers/${encodeURIComponent(id)}/unload`, { method: 'POST', body: '{}' }),
  loadModel: (id: string, body?: Record<string, unknown>) =>
    request<ModelProfile>(`/models/${encodeURIComponent(id)}/load`, { method: 'POST', body: JSON.stringify(body ?? {}) }),
  unloadModel: (id: string) =>
    request<ModelProfile>(`/models/${encodeURIComponent(id)}/unload`, { method: 'POST', body: '{}' }),
  updateModel: (id: string, body: Record<string, unknown>) =>
    request<ModelProfile>(`/models/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(body) }),
  providers: () => request<{ items: ProviderProfile[] }>('/providers'),
  createProvider: (body: Omit<ProviderProfile, 'id' | 'configured' | 'key_hint' | 'last_test'>) =>
    request<ProviderProfile>('/providers', { method: 'POST', body: JSON.stringify(body) }),
  updateProvider: (id: string, body: Partial<ProviderProfile>) =>
    request<ProviderProfile>(`/providers/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteProvider: (id: string) =>
    request<void>(`/providers/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  setProviderSecret: (id: string, keys: string[]) =>
    request<{ configured: boolean; key_hint?: string }>(`/providers/${encodeURIComponent(id)}/secret`, {
      method: 'POST',
      body: JSON.stringify({ keys }),
    }),
  testProvider: (id: string) =>
    request<{ ok: boolean; message: string; latency_ms?: number }>(`/providers/${encodeURIComponent(id)}/test`, {
      method: 'POST',
      body: '{}',
    }),
  providerModels: (id: string) =>
    request<{ items: Array<{ id: string; name?: string }> }>(`/providers/${encodeURIComponent(id)}/models`),
  discoverProviderModels: (body: {
    kind: ProviderKind
    protocol: ProviderProtocol
    base_url: string
    api_keys: string[]
    timeout_seconds?: number
  }) => request<{ items: Array<{ id: string; name?: string }> }>('/providers/discover-models', {
    method: 'POST',
    body: JSON.stringify(body),
  }),
  createJob: (body: CreateJobRequest) => request<JobSummary>('/jobs', { method: 'POST', body: JSON.stringify(body) }),
  jobs: (limit = 50) => request<JobListResponse>(`/jobs?limit=${limit}`),
  job: (id: string) => request<JobSummary>(`/jobs/${encodeURIComponent(id)}`),
  results: (id: string) => request<JobResultsResponse>(`/jobs/${encodeURIComponent(id)}/results`),
  jobAction: (id: string, action: 'pause' | 'resume' | 'cancel' | 'retry-failed') =>
    request<JobSummary>(`/jobs/${encodeURIComponent(id)}/${action}`, { method: 'POST', body: '{}' }),
  settings: () => request<RuntimeSettings>('/settings'),
  saveSettings: (body: RuntimeSettings) =>
    request<RuntimeSettings>('/settings', { method: 'PUT', body: JSON.stringify(body) }),

  // --- Dataset Workflow ---
  workflowCapabilities: () => request<WorkflowCapabilities>('/workflows/capabilities'),
  workflowResources: (category?: string) =>
    request<WorkflowResource[]>(
      category ? `/workflows/resources?category=${encodeURIComponent(category)}` : '/workflows/resources',
    ),
  workflowImportPreview: (body: {
    root_id: string
    relative_path: string
    resource_id: string
    category: string
  }) =>
    request<WorkflowImportPreview>('/workflows/resources/import/preview', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  workflowImportApply: (body: {
    root_id: string
    relative_path: string
    resource_id: string
    category: string
  }) =>
    request<{ resource_id: string; fingerprint: string; category: string; rule_count: number }>(
      '/workflows/resources/import/apply',
      { method: 'POST', body: JSON.stringify(body) },
    ),
  workflowPreflight: (config: Record<string, unknown>) =>
    request<WorkflowPreflightReport>('/workflows/jobs/preflight', {
      method: 'POST',
      body: JSON.stringify(config),
    }),
  workflowCreateJob: (config: Record<string, unknown>) =>
    request<{ job_id: string; status: WorkflowJobStatus }>('/workflows/jobs', {
      method: 'POST',
      body: JSON.stringify({ config }),
    }),
  /** Creation is intentionally separate from execution. */
  workflowStartJob: (id: string) =>
    request<{ job_id: string; status: WorkflowJobStatus }>(
      `/workflows/jobs/${encodeURIComponent(id)}/start`,
      { method: 'POST', body: '{}' },
    ),
  workflowJobs: (limit = 50) =>
    request<WorkflowJobSummary[]>(`/workflows/jobs?limit=${limit}`),
  workflowJob: (id: string) =>
    request<WorkflowJobSummary>(`/workflows/jobs/${encodeURIComponent(id)}`),
  workflowCountReview: (id: string, params: { limit?: number; offset?: number; pendingOnly?: boolean } = {}) => {
    const query = new URLSearchParams()
    if (params.limit !== undefined) query.set('limit', String(params.limit))
    if (params.offset !== undefined) query.set('offset', String(params.offset))
    if (params.pendingOnly) query.set('pending_only', 'true')
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request<WorkflowCountReviewPage>(
      `/workflows/jobs/${encodeURIComponent(id)}/count-review${suffix}`,
    )
  },
  workflowResolveCount: (
    id: string,
    body: { sample_id: number; expected_version: number; count: string; source?: string },
  ) =>
    request<{ sample_id: number; count_value: string; version: number }>(
      `/workflows/jobs/${encodeURIComponent(id)}/count-review/resolve`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  workflowConfirmCount: (id: string) =>
    request<{ job_id: string; confirmed: boolean; pending: number }>(
      `/workflows/jobs/${encodeURIComponent(id)}/count-review/confirm`,
      { method: 'POST', body: JSON.stringify({ confirmed: true }) },
    ),
  workflowJobAction: (id: string, action: 'pause' | 'resume' | 'cancel' | 'recover') =>
    request<{ job_id: string; status: WorkflowJobStatus }>(
      `/workflows/jobs/${encodeURIComponent(id)}/${action}`,
      { method: 'POST', body: '{}' },
    ),
  workflowRestoreJob: (id: string) =>
    request<{ job_id: string; restored_files: number; root_id?: string }>(
      `/workflows/jobs/${encodeURIComponent(id)}/restore`,
      { method: 'POST', body: '{}' },
    ),
  workflowDiscardJob: (id: string) =>
    request<{ job_id: string; discarded: boolean }>(
      `/workflows/jobs/${encodeURIComponent(id)}/discard`,
      { method: 'POST', body: '{}' },
    ),
  workflowPinJob: (id: string, pinned = true) =>
    request<{ job_id: string; pinned: boolean }>(
      `/workflows/jobs/${encodeURIComponent(id)}/pin`,
      { method: 'POST', body: JSON.stringify({ pinned }) },
    ),
  workflowJobEvents: (id: string, afterEventId = 0, limit = 100) =>
    request<{ job_id: string; events: WorkflowJobEvent[]; next_after_event_id: number; has_more: boolean }>(
      `/workflows/jobs/${encodeURIComponent(id)}/events?after_event_id=${afterEventId}&limit=${limit}`,
    ),
  workflowRepairJob: (id: string) =>
    request<WorkflowRepairReport>(`/workflows/jobs/${encodeURIComponent(id)}/repair`, {
      method: 'POST',
      body: '{}',
    }),
  workflowJobReport: (id: string) =>
    request<WorkflowJobReport>(`/workflows/jobs/${encodeURIComponent(id)}/report`),
  workflowIssues: (id: string, blockingOnly = false) =>
    request<WorkflowIssue[]>(
      `/workflows/jobs/${encodeURIComponent(id)}/issues${blockingOnly ? '?blocking_only=true' : ''}`,
    ),
  workflowTokenReview: (
    id: string,
    params: { limit?: number; offset?: number; unresolvedOnly?: boolean } = {},
  ) => {
    const query = new URLSearchParams()
    if (params.limit !== undefined) query.set('limit', String(params.limit))
    if (params.offset !== undefined) query.set('offset', String(params.offset))
    if (params.unresolvedOnly) query.set('unresolved_only', 'true')
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request<WorkflowTokenReviewPage>(
      `/workflows/jobs/${encodeURIComponent(id)}/token-review${suffix}`,
    )
  },
  workflowReviewToken: (
    id: string,
    body: {
      sample_id: number
      action: WorkflowTokenReviewAction
      expected_status: WorkflowTokenReviewStatus
      text?: string
    },
  ) =>
    request<WorkflowTokenReviewItem>(
      `/workflows/jobs/${encodeURIComponent(id)}/token-review/review`,
      { method: 'POST', body: JSON.stringify(body) },
    ),
  workflowConfirmTokenReview: (id: string) =>
    request<{ job_id: string; confirmed: boolean; unresolved: number }>(
      `/workflows/jobs/${encodeURIComponent(id)}/token-review/confirm`,
      { method: 'POST', body: JSON.stringify({ confirmed: true }) },
    ),
}

export function getSseHeaders(lastEventId?: number): HeadersInit {
  const headers = new Headers({ Accept: 'text/event-stream' })
  const token = sessionStorage.getItem('tagger2_access_token')
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (lastEventId !== undefined) headers.set('Last-Event-ID', String(lastEventId))
  return headers
}
