import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  Database,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Square,
  Undo2,
  Upload,
  Wrench,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import { Button, EmptyState, Field, Notice, Panel, StatusBadge } from '../components/ui'
import { api, ApiError } from '../lib/api'
import { copyFor } from '../lib/workflowCopy'
import { usePreferences } from '../store/app'
import type {
  WorkflowCountDecision,
  WorkflowExportFormat,
  WorkflowRepairReport,
  WorkflowTokenReviewAction,
  WorkflowTokenReviewItem,
  WorkflowImportPreview,
  WorkflowPreflightReport,
  WorkflowProfile,
  WorkflowWorkMode,
} from '../types'

interface JobDraft {
  profile: WorkflowProfile
  workMode: WorkflowWorkMode
  sourceRootId: string
  sourceRelativePath: string
  outputRootId: string
  outputRelativePath: string
  exportFormat: WorkflowExportFormat
  recursive: boolean
  replaceEnabled: boolean
  replaceResourceId: string
  ocrEnabled: boolean
  ocrMinConfidence: number
}

const emptyDraft: JobDraft = {
  profile: 'e621',
  workMode: 'full_copy',
  sourceRootId: '',
  sourceRelativePath: '',
  outputRootId: '',
  outputRelativePath: '',
  exportFormat: 'both',
  recursive: false,
  replaceEnabled: true,
  replaceResourceId: '',
  ocrEnabled: false,
  ocrMinConfidence: 0.5,
}

// Mirrors COUNT_VALUES in backend/tagger2/workflow/count_review.py; the API rejects anything else.
const COUNT_VALUES = ['solo', 'duo', 'trio', 'group'] as const

export function DatasetWorkflow() {
  const language = usePreferences((state) => state.workflowLanguage)
  const setLanguage = usePreferences((state) => state.setWorkflowLanguage)
  const text = copyFor(language)
  const queryClient = useQueryClient()

  const [draft, setDraft] = useState<JobDraft>(emptyDraft)
  const [selectedJobId, setSelectedJobId] = useState<string>()
  const [preflight, setPreflight] = useState<WorkflowPreflightReport>()
  const [preflightError, setPreflightError] = useState<string>()
  const [importForm, setImportForm] = useState({ rootId: '', relativePath: '', resourceId: '' })
  const [importPreview, setImportPreview] = useState<WorkflowImportPreview>()
  const [importError, setImportError] = useState<string>()
  const [countError, setCountError] = useState<string>()
  const [repair, setRepair] = useState<WorkflowRepairReport>()
  const [tokenError, setTokenError] = useState<string>()
  const [tokenDraft, setTokenDraft] = useState<Record<number, string>>({})

  const roots = useQuery({ queryKey: ['roots'], queryFn: api.roots, retry: false })
  const resources = useQuery({
    queryKey: ['workflow', 'resources'],
    queryFn: () => api.workflowResources(),
    retry: false,
  })
  const jobs = useQuery({
    queryKey: ['workflow', 'jobs'],
    queryFn: () => api.workflowJobs(),
    retry: false,
    refetchInterval: 5_000,
  })
  const countReview = useQuery({
    queryKey: ['workflow', 'count-review', selectedJobId],
    queryFn: () => api.workflowCountReview(selectedJobId as string, { limit: 50 }),
    enabled: Boolean(selectedJobId),
    retry: false,
  })
  const tokenReview = useQuery({
    queryKey: ['workflow', 'token-review', selectedJobId],
    queryFn: () => api.workflowTokenReview(selectedJobId as string, { limit: 50 }),
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

  const rootOptions = roots.data?.items ?? []

  const jobConfig = useMemo(() => {
    const config: Record<string, unknown> = {
      profile: draft.profile,
      work_mode: draft.workMode,
      overwrite_mode: 'incremental',
      source_root: { root_id: draft.sourceRootId, relative_path: draft.sourceRelativePath },
      recursive: draft.recursive,
      caption: { enabled: false, input_txt_mode: 'tag' },
      classify: { enabled: false },
      replace: draft.replaceEnabled
        ? { enabled: true, resource_id: draft.replaceResourceId }
        : { enabled: false },
      ocr: draft.ocrEnabled
        ? { enabled: true, min_confidence: draft.ocrMinConfidence }
        : { enabled: false },
      nl: { enabled: false },
      // Production UI jobs always stop at the explicit Count Review gate.
      // Legacy/API callers may opt out for deterministic compatibility tests.
      count_review: { enabled: true },
      token_budget: { enabled: false },
      export: { format: draft.exportFormat },
    }
    if (draft.workMode === 'full_copy') {
      config.output_root = { root_id: draft.outputRootId, relative_path: draft.outputRelativePath }
    }
    return config
  }, [draft])

  const preflightMutation = useMutation({
    mutationFn: () => api.workflowPreflight(jobConfig),
    onMutate: () => {
      setPreflight(undefined)
      setPreflightError(undefined)
    },
    onSuccess: (report) => setPreflight(report),
    onError: (error: Error) => setPreflightError(error.message),
  })

  const createMutation = useMutation({
    mutationFn: () => api.workflowCreateJob(jobConfig),
    onSuccess: (created) => {
      setSelectedJobId(created.job_id)
      void queryClient.invalidateQueries({ queryKey: ['workflow', 'jobs'] })
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
      void queryClient.invalidateQueries({ queryKey: ['workflow', 'count-review'] })
    },
    onError: (error: ApiError) => {
      setCountError(error.status === 409 ? text.countGateBlocked : error.message)
    },
  })

  const jobAction = useMutation({
    mutationFn: (action: 'pause' | 'resume' | 'cancel' | 'recover') =>
      api.workflowJobAction(selectedJobId as string, action),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['workflow', 'jobs'] })
    },
    onError: (error: Error) => setCountError(error.message),
  })

  const startJob = useMutation({
    mutationFn: () => api.workflowStartJob(selectedJobId as string),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['workflow', 'jobs'] })
    },
    onError: (error: Error) => setCountError(error.message),
  })

  const restoreJob = useMutation({
    mutationFn: () => api.workflowRestoreJob(selectedJobId as string),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['workflow', 'jobs'] })
    },
    onError: (error: Error) => setCountError(error.message),
  })

  const discardJob = useMutation({
    mutationFn: () => api.workflowDiscardJob(selectedJobId as string),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['workflow', 'jobs'] })
    },
    onError: (error: Error) => setCountError(error.message),
  })

  const repairJob = useMutation({
    mutationFn: () => api.workflowRepairJob(selectedJobId as string),
    onSuccess: (report) => {
      setRepair(report)
      void queryClient.invalidateQueries({ queryKey: ['workflow', 'jobs'] })
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
      void queryClient.invalidateQueries({ queryKey: ['workflow', 'token-review'] })
    },
    onError: (error: ApiError) => setTokenError(error.message),
  })

  const selectedJob = jobs.data?.find((job) => job.job_id === selectedJobId)

  // A repair report and a review error belong to one job, so clear them on switch.
  function selectJob(jobId: string) {
    if (jobId === selectedJobId) return
    setSelectedJobId(jobId)
    setRepair(undefined)
    setCountError(undefined)
    setTokenError(undefined)
    setTokenDraft({})
  }

  const canPreflight = Boolean(
    draft.sourceRootId && (draft.workMode === 'in_place' || draft.outputRootId),
  )

  return (
    <div className="page page-dataset-workflow">
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

      <Panel title={text.importTitle} eyebrow="Resources">
        <div className="form-grid">
          <Field label={text.importRootId}>
            <select
              value={importForm.rootId}
              onChange={(event) => setImportForm({ ...importForm, rootId: event.target.value })}
            >
              <option value="">—</option>
              {rootOptions.map((root) => (
                <option key={root.id} value={root.id}>
                  {root.name} ({root.kind})
                </option>
              ))}
            </select>
          </Field>
          <Field label={text.importRelativePath}>
            <input
              value={importForm.relativePath}
              onChange={(event) => setImportForm({ ...importForm, relativePath: event.target.value })}
              placeholder="e621_general_tag_replacement_index.csv"
            />
          </Field>
          <Field label={text.importResourceId}>
            <input
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

      <Panel title={text.createJobTitle} eyebrow="Pipeline">
        <div className="form-grid">
          <Field label={text.profile}>
            <select
              value={draft.profile}
              onChange={(event) =>
                setDraft({ ...draft, profile: event.target.value as WorkflowProfile })
              }
            >
              <option value="e621">e621</option>
              <option value="danbooru">danbooru</option>
            </select>
          </Field>
          <Field label={text.workMode}>
            <select
              value={draft.workMode}
              onChange={(event) =>
                setDraft({ ...draft, workMode: event.target.value as WorkflowWorkMode })
              }
            >
              <option value="full_copy">{text.workModeFullCopy}</option>
              <option value="in_place">{text.workModeInPlace}</option>
            </select>
          </Field>
          <Field label={text.sourceRoot}>
            <select
              value={draft.sourceRootId}
              onChange={(event) => setDraft({ ...draft, sourceRootId: event.target.value })}
            >
              <option value="">—</option>
              {rootOptions.map((root) => (
                <option key={root.id} value={root.id}>
                  {root.name} ({root.kind})
                </option>
              ))}
            </select>
          </Field>
          <Field label={`${text.sourceRoot} · ${text.relativePath}`}>
            <input
              value={draft.sourceRelativePath}
              onChange={(event) => setDraft({ ...draft, sourceRelativePath: event.target.value })}
            />
          </Field>
          {draft.workMode === 'full_copy' && (
            <>
              <Field label={text.outputRoot}>
                <select
                  value={draft.outputRootId}
                  onChange={(event) => setDraft({ ...draft, outputRootId: event.target.value })}
                >
                  <option value="">—</option>
                  {rootOptions
                    .filter((root) => root.writable)
                    .map((root) => (
                      <option key={root.id} value={root.id}>
                        {root.name} ({root.kind})
                      </option>
                    ))}
                </select>
              </Field>
              <Field label={`${text.outputRoot} · ${text.relativePath}`}>
                <input
                  value={draft.outputRelativePath}
                  onChange={(event) =>
                    setDraft({ ...draft, outputRelativePath: event.target.value })
                  }
                />
              </Field>
            </>
          )}
          <Field label={text.exportFormat}>
            <select
              value={draft.exportFormat}
              onChange={(event) =>
                setDraft({ ...draft, exportFormat: event.target.value as WorkflowExportFormat })
              }
            >
              <option value="both">{text.exportBoth}</option>
              <option value="json">{text.exportJson}</option>
              <option value="txt">{text.exportTxt}</option>
            </select>
          </Field>
          <Field label={text.enableReplace}>
            <select
              value={draft.replaceEnabled ? 'yes' : 'no'}
              onChange={(event) =>
                setDraft({ ...draft, replaceEnabled: event.target.value === 'yes' })
              }
            >
              <option value="yes">{text.yes}</option>
              <option value="no">{text.no}</option>
            </select>
          </Field>
          {draft.replaceEnabled && (
            <Field label="Replace resource">
              <select
                value={draft.replaceResourceId}
                onChange={(event) => setDraft({ ...draft, replaceResourceId: event.target.value })}
              >
                <option value="">—</option>
                {(resources.data ?? [])
                  // Existing catalogs use `replacement_index`; older imports
                  // used `replace`. Both are valid replacement resources.
                  .filter(
                    (resource) =>
                      resource.category === 'replace' || resource.category === 'replacement_index',
                  )
                  .map((resource) => (
                    <option key={resource.resource_id} value={resource.resource_id}>
                      {resource.resource_id}
                    </option>
                  ))}
              </select>
            </Field>
          )}
          <Field label={text.enableOcr}>
            <select
              value={draft.ocrEnabled ? 'yes' : 'no'}
              onChange={(event) =>
                setDraft({ ...draft, ocrEnabled: event.target.value === 'yes' })
              }
            >
              <option value="no">{text.no}</option>
              <option value="yes">{text.yes}</option>
            </select>
          </Field>
          {draft.ocrEnabled && (
            <Field label={text.ocrMinConfidence}>
              <input
                type="number"
                min={0}
                max={1}
                step={0.05}
                value={draft.ocrMinConfidence}
                onChange={(event) =>
                  setDraft({ ...draft, ocrMinConfidence: Number(event.target.value) })
                }
              />
            </Field>
          )}
          <Field label={text.recursive}>
            <select
              value={draft.recursive ? 'yes' : 'no'}
              onChange={(event) => setDraft({ ...draft, recursive: event.target.value === 'yes' })}
            >
              <option value="no">{text.no}</option>
              <option value="yes">{text.yes}</option>
            </select>
          </Field>
        </div>

        <div className="workflow-button-row">
          <Button
            onClick={() => preflightMutation.mutate()}
            disabled={preflightMutation.isPending || !canPreflight}
          >
            {text.preflight}
          </Button>
          <Button
            variant="primary"
            onClick={() => createMutation.mutate()}
            disabled={createMutation.isPending || !preflight?.valid}
          >
            {text.createJob}
          </Button>
        </div>

        {preflightError && <Notice tone="danger">{preflightError}</Notice>}
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
              </tr>
            </thead>
            <tbody>
              {jobs.data.map((job) => (
                <tr
                  key={job.job_id}
                  onClick={() => selectJob(job.job_id)}
                  className={job.job_id === selectedJobId ? 'row-active' : ''}
                >
                  <td className="workflow-mono">{job.job_id.slice(0, 12)}…</td>
                  <td>
                    <StatusBadge state={job.status} />
                  </td>
                  <td>{job.profile}</td>
                  <td>
                    {job.processed_samples}/{job.total_samples}
                  </td>
                  <td>{job.current_module_id ?? '—'}</td>
                  <td className="workflow-mono">{job.created_at}</td>
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
            {selectedJob.status === 'pending' && (
              <Button
                onClick={() => startJob.mutate()}
                disabled={startJob.isPending}
              >
                <Play size={15} aria-hidden="true" />
                {text.startJob}
              </Button>
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
            {['interrupted', 'failed', 'rollback_required'].includes(selectedJob.status) && (
              <Button
                variant="secondary"
                onClick={() => jobAction.mutate('recover')}
                disabled={jobAction.isPending}
              >
                <RotateCcw size={15} aria-hidden="true" />
                {text.recoverJob}
              </Button>
            )}
            {selectedJob.work_mode === 'in_place' &&
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
            {['completed', 'failed', 'cancelled', 'interrupted', 'rollback_required'].includes(selectedJob.status) && (
              <Button
                variant="quiet"
                onClick={() => discardJob.mutate()}
                disabled={discardJob.isPending}
              >
                <Archive size={15} aria-hidden="true" />
                {text.discardJob}
              </Button>
            )}
            <Button
              variant="quiet"
              onClick={() => repairJob.mutate()}
              disabled={repairJob.isPending}
            >
              <Wrench size={15} aria-hidden="true" />
              {text.repairJob}
            </Button>
          </div>
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

      {selectedJobId && countReview.data?.items && (
        <Panel
          title={text.countReviewTitle}
          eyebrow={`${text.countReviewPending} ${countReview.data.pending ?? 0}`}
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
