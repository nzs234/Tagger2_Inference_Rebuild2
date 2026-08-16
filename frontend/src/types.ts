export type AppPage = 'workbench' | 'image-generation' | 'video-prompts' | 'batch' | 'dataset-workflow' | 'providers' | 'models' | 'settings'
export type ThemeMode = 'light' | 'dark' | 'system'
export type JobMode = 'local' | 'online'
export type JobState =
  | 'queued'
  | 'running'
  | 'paused'
  | 'cancelling'
  | 'cancelled'
  | 'succeeded'
  | 'failed'
  | 'interrupted'

export type QueueState = 'ready' | 'uploading' | 'queued' | 'processing' | 'done' | 'error'

export interface TagItem {
  text: string
  category: string
  score?: number | null
  source: string
  model_id: string
}

export interface AnimaPayload {
  quality: string[]
  count: string
  character: string
  series: string
  artist: string
  appearance: string[]
  tags: string[]
  environment: string[]
  nl: string
}

export interface Artifact {
  kind: 'json' | 'txt' | 'log' | string
  name: string
  path?: string
  download_url?: string
}

export interface ImageResult {
  image_id: string
  file_name: string
  status: 'succeeded' | 'failed' | 'skipped' | string
  model_id?: string | null
  tags: TagItem[]
  caption?: string | null
  anima?: AnimaPayload | null
  artifacts: Artifact[]
  warnings: string[]
  timing: Record<string, number>
  model_results?: ModelResult[]
  error?: string | null
}

export interface ModelResult {
  model_id: string
  model_name: string
  tags: TagItem[]
}

export interface JobEvent {
  seq: number
  job_id: string
  state: JobState
  phase: string
  processed: number
  total: number
  succeeded: number
  skipped: number
  failed: number
  current_item?: string | null
  rate?: number | null
  eta?: number | null
  error?: string | null
}

export interface JobSummary extends JobEvent {
  id: string
  mode: JobMode
  hybrid?: boolean
  created_at: string
  updated_at?: string
  provider_id?: string | null
  model_ids?: string[]
}

export interface JobListResponse {
  items: JobSummary[]
  total: number
}

export interface JobResultsResponse {
  items: ImageResult[]
  total: number
}

export interface UploadResponse {
  upload_id: string
  files: Array<{ id: string; name: string; size: number }>
}

export interface ScanItem {
  id?: string
  relative_path: string
  file_name: string
  size?: number
  modified_at?: string
}

export interface ScanResponse {
  scan_id?: string
  items: ScanItem[]
  total: number
  next_cursor?: string | null
}

export interface RootInfo {
  id: string
  name: string
  kind: 'input' | 'output' | 'model' | string
  path_hint?: string
  writable?: boolean
}

export interface ModelProfile {
  id: string
  name: string
  backend: 'pytorch' | 'onnx' | 'safetensors' | string
  architecture?: string
  input_size?: number | number[]
  loaded: boolean
  device?: string | null
  memory_mb?: number | null
  threshold?: number
  thresholds?: Record<string, number>
  preset_thresholds?: Record<string, number>
  threshold_source?: 'model' | 'custom'
  trusted_pickle?: boolean
  adapters?: Array<{ id: string; name: string; type: string; enabled: boolean; weight: number }>
  classifiers?: string[]
  status?: string
}

export interface ModelDownload {
  id: string
  repo_id: string
  revision?: string | null
  status: 'queued' | 'running' | 'succeeded' | 'failed'
  phase: 'queued' | 'downloading' | 'registering' | 'completed' | 'interrupted' | string
  model_ids: string[]
  loaded_model_ids: string[]
  load_errors: string[]
  error?: string | null
  created_at: string
  updated_at: string
}

export interface ClassifierIssue {
  classifier: 'aesthetic' | string
  code: string
  message: string
  retryable: boolean
}

export interface ClassifierProfile {
  id: 'aesthetic'
  enabled: boolean
  loaded: boolean
  error?: ClassifierIssue | null
}

export type ProviderKind = 'custom' | 'gemini' | 'openai' | 'xai' | 'claude' | 'lmstudio' | 'antigravity'
export type ProviderProtocol = 'openai' | 'gemini' | 'claude'

export interface ProviderProfile {
  id: string
  name: string
  kind: ProviderKind
  protocol: ProviderProtocol
  base_url: string
  primary_model: string
  fallback_model?: string | null
  temperature: number
  top_p: number
  top_k?: number | null
  max_tokens: number
  timeout_seconds: number
  retries: number
  configured: boolean
  key_hint?: string | null
  enabled?: boolean
  last_test?: { ok: boolean; message: string; at: string } | null
  image_enabled?: boolean
  image_family?: 'auto' | 'google_gemini' | 'openai_gpt_image' | 'xai_grok_image' | 'unknown'
  image_base_url?: string | null
  image_api_style?: 'auto' | 'native' | 'openai_images' | 'openai_chat'
}

export type ImageGenerationFamily = 'google_gemini' | 'openai_gpt_image' | 'xai_grok_image' | 'unknown'
export type ImageGenerationState = 'queued' | 'running' | 'cancelling' | 'cancelled' | 'succeeded' | 'partial' | 'failed' | 'interrupted' | 'deleting'

export interface ImageGenerationCapability {
  schema_version: string
  verified_at: string
  provider_id?: string
  api_style?: 'native' | 'openai_images' | 'openai_chat'
  model: string
  family: ImageGenerationFamily
  label: string
  known: boolean
  operations: Array<'generate' | 'edit'>
  parameters: string[]
  enums: Record<string, string[]>
  defaults: Record<string, string | number | boolean>
  max_references: number
  max_outputs: number
  source_url?: string
  notes: string
}

export interface ImageGenerationArtifact {
  id: string
  ordinal: number
  mime_type: string
  width?: number | null
  height?: number | null
  size_bytes: number
  sha256: string
  source: string
  download_url: string
}

export interface ImageGenerationJob {
  id: string
  state: ImageGenerationState
  phase: string
  provider_id: string
  model: string
  family: ImageGenerationFamily
  operation: 'generate' | 'edit'
  requested_count: number
  completed_count: number
  attempt_counts: Record<string, number>
  config: Record<string, unknown>
  config_hash: string
  reference_count: number
  artifacts: ImageGenerationArtifact[]
  created_at: string
  updated_at: string
  started_at?: string | null
  finished_at?: string | null
  error_code?: string | null
}

export interface ImageGenerationListResponse {
  items: ImageGenerationJob[]
  total: number
  next_cursor?: number | null
}

export interface RuntimeSettings {
  input_root_id?: string
  output_root_id?: string
  default_mode: JobMode
  default_threshold: number
  default_json: boolean
  default_txt: boolean
  bind_host: string
  lan_enabled: boolean
  access_token_configured: boolean
  production: boolean
  max_upload_mb: number
  max_image_pixels: number
}

export interface QueueItem {
  id: string
  file: File
  previewUrl: string
  state: QueueState
  progress: number
  result?: ImageResult
  error?: string
}

export interface ApiErrorEnvelope {
  code: string
  message: string
  fields?: Record<string, string[]>
  request_id?: string
  retryable?: boolean
}

export interface CreateJobRequest {
  mode: JobMode
  hybrid?: boolean
  source:
    | { type: 'upload'; upload_id: string }
    | { type: 'scan'; root_id: string; relative_path: string; recursive: boolean; patterns?: string[] }
  output?: {
    root_id?: string
    relative_path?: string
    json: boolean
    txt: boolean
    txt_include_tags?: boolean
    replace_underscores?: boolean
    include_rating?: boolean
    escape_parentheses?: boolean
    conflict: 'validate-skip' | 'overwrite' | 'rename'
  }
  provider_id?: string
  provider_model?: string
  model_ids?: string[]
  thresholds?: Record<string, number | Record<string, number>>
  classifiers?: Array<'aesthetic'>
  separate_models?: boolean
  tag_prompt?: string
  nl_prompt?: string
  json_prompt?: string
  online_response?: 'json' | 'nl' | 'nl_tags'
  online_concurrency?: number
}

export interface PromptDefaults {
  tag_prompt: string
  nl_prompt: string
  json_prompt: string
}

export type VideoPromptLanguage = 'both' | 'zh' | 'en'

export type VideoPromptMode = 'ref2va' | 'fl2va'
export type H3BasePromptMode = 't2va' | 'i2va' | 'l2va' | 'fl2va'
export type Fl2vaSingleImageRole = 'first' | 'last'

export interface BilingualText {
  zh: string
  en: string
}

export type H3VisualRetention = 'fully_preserved' | 'partially_preserved' | 'attribute_transfer' | 'weak_reference'

export interface H3SubjectDefinition extends BilingualText {
  subject_number: number
  picture_number: number
}

export interface H3RetentionAnalysis extends BilingualText {
  subject_number: number
  shot_number: number
  visual_retention: H3VisualRetention
}

export interface H3Shot extends BilingualText {
  shot_number: number
  cut_time_seconds: number | null
}

export interface Ref2vaPromptPackage {
  change_summary_zh: string
  subject_definitions: H3SubjectDefinition[]
  summary: BilingualText
  retention_analysis: H3RetentionAnalysis[]
  detailed_description: {
    overview: BilingualText
    shots: H3Shot[]
  }
  overall_soundscape: BilingualText
  non_diegetic_music: BilingualText
  assumptions_zh: string[]
}

export interface Fl2vaPromptPackage {
  change_summary_zh: string
  base_mode: H3BasePromptMode
  reference_alignment: BilingualText | null
  integrated_multimodal_description: BilingualText
  overall_soundscape: BilingualText
  non_diegetic_music: BilingualText
  assumptions_zh: string[]
}

export type VideoPromptPackage = Ref2vaPromptPackage | Fl2vaPromptPackage

export interface VideoPromptRevision {
  id: string
  version: number
  mode: VideoPromptMode
  parent_revision_id?: string
  instruction: string
  package: VideoPromptPackage
  created_at: string
}

// --- Dataset Workflow ---

export type WorkflowLanguage = 'zh' | 'en'
export type WorkflowProfile = 'e621' | 'danbooru'
export type WorkflowWorkMode = 'in_place' | 'full_copy'
export type WorkflowOverwriteMode = 'incremental' | 'rebuild'
export type WorkflowExportFormat = 'json' | 'txt' | 'both'
/**
 * Public workflow state names.  Keep these aligned with the workflow
 * lifecycle rather than collapsing waiting/transition states into `running`:
 * the UI uses them to decide which destructive controls are safe to show.
 */
export type WorkflowJobStatus =
  | 'pending'
  | 'queued'
  | 'running'
  | 'waiting_count_review'
  | 'waiting_token_review'
  | 'committing'
  | 'pausing'
  | 'paused'
  | 'cancelling'
  | 'cancelled'
  | 'interrupted'
  | 'rollback_required'
  | 'restoring'
  | 'completed'
  | 'failed'

export interface WorkflowPathRef {
  root_id: string
  relative_path: string
}

export type WorkflowPathBindingPreviewStatus = 'ready' | 'create_required' | 'not_applicable'

export interface WorkflowPathBindingPreview {
  status: WorkflowPathBindingPreviewStatus
  source_bound: boolean
  output_bound: boolean
  output_create_required: boolean
  warnings: string[]
  errors: string[]
}

export interface WorkflowPathBinding {
  status: 'ready' | 'not_applicable'
  source: WorkflowPathRef
  output?: WorkflowPathRef | null
  output_created: boolean
}

export interface WorkflowResource {
  resource_id: string
  /** Resource manifests historically used both `replace` and
   * `replacement_index`; both identify the same replacement-index contract. */
  category: 'classify' | 'replace' | 'replacement_index' | 'ocr' | 'tokenizer' | string
  fingerprint: string
  source_url?: string | null
  created_at?: string
}

export interface WorkflowCapabilities {
  profiles: WorkflowProfile[]
  work_modes: WorkflowWorkMode[]
  resources: WorkflowResource[]
}

export interface WorkflowImportPreview {
  valid: boolean
  errors: string[]
  warnings: string[]
  rule_count: number
  action_counts: Record<string, number>
  passthrough_count: number
  fingerprint: string | null
}

export interface WorkflowStageCounts {
  processed?: number
  failed?: number
  regions?: number
}

export interface WorkflowJobReport {
  job_id: string
  available: boolean
  report?: {
    total_samples?: number
    exported_samples?: number
    failed_samples?: number
    committed_files?: number
    ocr?: WorkflowStageCounts
    caption?: Record<string, number>
    replacement?: Record<string, number>
  }
}

export interface WorkflowJobSummary {
  job_id: string
  status: WorkflowJobStatus
  profile: WorkflowProfile
  work_mode: WorkflowWorkMode
  overwrite_mode?: WorkflowOverwriteMode
  source_root_id?: string
  output_root_id?: string | null
  total_samples: number
  processed_samples: number
  succeeded_samples: number
  failed_samples: number
  skipped_samples: number
  current_module_id: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  restored_at?: string | null
  discarded_at?: string | null
  /** Stable public code, never an exception message or traceback. */
  error_code: string | null
  pinned?: boolean
}

export interface WorkflowJobEvent {
  seq?: number
  event_id?: number
  job_id: string
  status?: WorkflowJobStatus
  event_type?: string
  from_status?: WorkflowJobStatus | null
  to_status?: WorkflowJobStatus | null
  payload?: Record<string, unknown>
  module_id?: string | null
  processed_samples?: number
  total_samples?: number
  message?: string | null
  created_at?: string
}

export interface WorkflowIssue {
  issue_id: string
  sample_id: number | null
  relative_image_path: string | null
  module_id: string
  code: string
  severity: 'info' | 'warning' | 'error'
  blocking: boolean
  message: string
  created_at: string | null
}

export interface WorkflowPreflightReport {
  valid: boolean
  errors: string[]
  warnings: string[]
}

export interface WorkflowCountDecision {
  sample_id: number
  count_value: string
  status: 'pending' | 'confirmed'
  updated_at: string
  version: number
  proposed_count: string
  base_value: string
  selected_source: string
  original_normalized: string | null
  wiki_value: string | null
  matched_tags: string[]
  conflict: boolean
  issue_codes: string[]
  warnings: string[]
  applied_lower_bounds: string[]
  blocking_code: string | null
  relative_image_path: string
  nl_observation: Record<string, unknown>
}

export interface WorkflowCountReviewPage {
  items: WorkflowCountDecision[]
  pending: number
}

export interface WorkflowRepairReport {
  job_id: string
  reclaimed_samples: number
  parked_samples: number
  committed_files: number
  journal_state: string
  resumable_samples: number
}

export type WorkflowTokenReviewStatus =
  | 'overflow'
  | 'edited'
  | 'recounted'
  | 'rewritten'
  | 'applied'

export type WorkflowTokenReviewAction = 'edit' | 'recount' | 'rewrite_short' | 'apply'

export interface WorkflowTokenReviewItem {
  sample_id: number
  nl_text: string
  token_count: number
  token_limit: number
  status: WorkflowTokenReviewStatus
  proposal_text: string | null
  proposal_token_count: number | null
  over_by: number
  updated_at: string
}

export interface WorkflowTokenReviewPage {
  items: WorkflowTokenReviewItem[]
  unresolved: number
}
