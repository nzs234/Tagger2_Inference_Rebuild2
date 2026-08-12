/**
 * Bilingual copy for the Dataset Workflow module.
 *
 * Only this module is bilingual; the rest of the application stays Chinese.
 * Every key must exist in both languages so a missing string cannot fall back
 * to a raw identifier in the UI.
 */
import type { WorkflowLanguage } from '../types'

export interface WorkflowCopy {
  navLabel: string
  navHint: string
  title: string
  subtitle: string
  languageLabel: string
  chinese: string
  english: string
  resourcesTitle: string
  resourcesEmpty: string
  resourceId: string
  resourceCategory: string
  resourceFingerprint: string
  importTitle: string
  importRootId: string
  importRelativePath: string
  importResourceId: string
  importPreview: string
  importApply: string
  importPreviewOk: string
  importPreviewFailed: string
  importRuleCount: string
  importPassthrough: string
  importFingerprint: string
  jobsTitle: string
  jobsEmpty: string
  createJobTitle: string
  profile: string
  workMode: string
  workModeInPlace: string
  workModeFullCopy: string
  sourceRoot: string
  outputRoot: string
  relativePath: string
  exportFormat: string
  exportJson: string
  exportTxt: string
  exportBoth: string
  recursive: string
  enableReplace: string
  enableOcr: string
  ocrMinConfidence: string
  ocrTitle: string
  ocrProcessed: string
  ocrFailed: string
  ocrRegions: string
  ocrEmpty: string
  ocrUnavailableHint: string
  preflight: string
  createJob: string
  startJob: string
  cancelJob: string
  recoverJob: string
  restoreJob: string
  discardJob: string
  pinJob: string
  preflightOk: string
  preflightFailed: string
  warnings: string
  jobId: string
  status: string
  samples: string
  progress: string
  module: string
  created: string
  issuesTitle: string
  issuesEmpty: string
  severity: string
  blocking: string
  code: string
  message: string
  refresh: string
  yes: string
  no: string
  compatibilityTitle: string
  compatibilityBody: string
  countReviewTitle: string
  countReviewEmpty: string
  countReviewPending: string
  countProposed: string
  countSource: string
  countEvidence: string
  countConflict: string
  countApply: string
  countConfirm: string
  countConfirmed: string
  countStale: string
  countGateBlocked: string
  pauseJob: string
  resumeJob: string
  repairJob: string
  repairResult: string
  reclaimed: string
  parked: string
  journalState: string
  wikiUnavailable: string
  jobControlsTitle: string
  resumableSamples: string
  committedFiles: string
  tokenReviewTitle: string
  tokenReviewEmpty: string
  tokenReviewUnresolved: string
  tokenCaption: string
  tokenCount: string
  tokenLimit: string
  tokenOverBy: string
  tokenProposal: string
  tokenEdit: string
  tokenRecount: string
  tokenRewriteShort: string
  tokenApply: string
  tokenConfirm: string
  tokenStale: string
  tokenUnavailable: string
  tokenNotApplied: string
}

export const workflowCopy: Record<WorkflowLanguage, WorkflowCopy> = {
  zh: {
    navLabel: '数据集工作流',
    navHint: '事务化标注流水线',
    title: '数据集工作流',
    subtitle: 'Caption → Classify → Replace → OCR → NL → 复核 → Policy → Token → 导出',
    languageLabel: '界面语言',
    chinese: '中文',
    english: 'English',

    resourcesTitle: '资源',
    resourcesEmpty: '尚未注册资源。先导入替换索引 CSV。',
    resourceId: '资源 ID',
    resourceCategory: '类别',
    resourceFingerprint: '指纹',
    importTitle: '导入替换索引',
    importRootId: '根 ID',
    importRelativePath: '相对路径',
    importResourceId: '资源 ID',
    importPreview: '预览',
    importApply: '应用导入',
    importPreviewOk: '校验通过',
    importPreviewFailed: '校验失败',
    importRuleCount: '可执行规则',
    importPassthrough: '恒等透传',
    importFingerprint: '指纹',

    jobsTitle: '任务',
    jobsEmpty: '暂无工作流任务。',
    createJobTitle: '新建任务',
    profile: '配置档',
    workMode: '工作模式',
    workModeInPlace: '原地处理（先备份）',
    workModeFullCopy: '完整副本',
    sourceRoot: '输入根',
    outputRoot: '输出根',
    relativePath: '相对路径',
    exportFormat: '导出格式',
    exportJson: '仅 JSON',
    exportTxt: '仅扁平 TXT',
    exportBoth: '两者',
    recursive: '递归子目录',
    enableReplace: '启用替换',
    enableOcr: '启用 OCR',
    ocrMinConfidence: 'OCR 最低置信度',
    ocrTitle: 'OCR 结果',
    ocrProcessed: '已识别',
    ocrFailed: '失败',
    ocrRegions: '文本区域',
    ocrEmpty: '本次任务没有 OCR 结果。',
    ocrUnavailableHint: 'OCR 运行时缺失或识别失败时只记录警告，不会中断流程。',
    preflight: '预检',
    createJob: '创建任务',
    startJob: '开始任务',
    cancelJob: '取消任务',
    recoverJob: '恢复任务',
    restoreJob: '恢复数据集',
    discardJob: '丢弃工作区',
    pinJob: '固定任务',
    preflightOk: '预检通过',
    preflightFailed: '预检失败',
    warnings: '警告',

    jobId: '任务 ID',
    status: '状态',
    samples: '样本',
    progress: '进度',
    module: '当前阶段',
    created: '创建时间',
    issuesTitle: '问题',
    issuesEmpty: '没有未解决的问题。',
    severity: '级别',
    blocking: '阻断',
    code: '代码',
    message: '说明',
    refresh: '刷新',
    yes: '是',
    no: '否',

    compatibilityTitle: '兼容性说明',
    compatibilityBody:
      '纯规则阶段（导入、替换、九字段规范化、导出、备份与提交）与源项目行为一致。分类词典、LSE14-5k 质量模型与 Danbooru 正式资源不可获得，相关阶段在资源缺失时显示为不可用，不会静默回退。',
    countReviewTitle: '数量复核',
    countReviewEmpty: '没有待复核的数量。',
    countReviewPending: '待复核',
    countProposed: '建议值',
    countSource: '来源',
    countEvidence: '依据',
    countConflict: '来源冲突',
    countApply: '采用',
    countConfirm: '确认完成复核',
    countConfirmed: '已确认',
    countStale: '该条目已被他处修改，请刷新后重试。',
    countGateBlocked: '仍有条目未复核，导出被阻止。',
    pauseJob: '暂停',
    resumeJob: '继续',
    repairJob: '修复中断',
    repairResult: '修复结果',
    reclaimed: '已回收',
    parked: '已搁置',
    journalState: '提交日志状态',
    wikiUnavailable: 'e621 wiki 快照不可用，数量规则回退到原始标注值，不会凭空推断。',
    jobControlsTitle: '任务控制',
    resumableSamples: '可恢复样本',
    committedFiles: '已提交文件',
    tokenReviewTitle: 'Token 预算复核',
    tokenReviewEmpty: '没有超出预算的样本。',
    tokenReviewUnresolved: '待处理',
    tokenCaption: '当前描述',
    tokenCount: 'Token 数',
    tokenLimit: '预算上限',
    tokenOverBy: '超出',
    tokenProposal: '候选文本',
    tokenEdit: '手动改写',
    tokenRecount: '重新计数',
    tokenRewriteShort: '改写为短版',
    tokenApply: '应用候选',
    tokenConfirm: '确认完成 Token 复核',
    tokenStale: '该条目已被他处修改，请刷新后重试。',
    tokenUnavailable: '未注册 Tokenizer 资源，无法计数，相关操作已停用。',
    tokenNotApplied: '候选文本在应用前不会写入最终 JSON。',
  },
  en: {
    navLabel: 'Dataset Workflow',
    navHint: 'Transactional annotation pipeline',
    title: 'Dataset Workflow',
    subtitle: 'Caption → Classify → Replace → OCR → NL → Review → Policy → Token → Export',
    languageLabel: 'Interface language',
    chinese: '中文',
    english: 'English',

    resourcesTitle: 'Resources',
    resourcesEmpty: 'No resources registered yet. Import a replacement index CSV first.',
    resourceId: 'Resource ID',
    resourceCategory: 'Category',
    resourceFingerprint: 'Fingerprint',
    importTitle: 'Import replacement index',
    importRootId: 'Root ID',
    importRelativePath: 'Relative path',
    importResourceId: 'Resource ID',
    importPreview: 'Preview',
    importApply: 'Apply import',
    importPreviewOk: 'Validation passed',
    importPreviewFailed: 'Validation failed',
    importRuleCount: 'Executable rules',
    importPassthrough: 'Identity passthrough',
    importFingerprint: 'Fingerprint',

    jobsTitle: 'Jobs',
    jobsEmpty: 'No workflow jobs yet.',
    createJobTitle: 'Create job',
    profile: 'Profile',
    workMode: 'Work mode',
    workModeInPlace: 'In place (backs up first)',
    workModeFullCopy: 'Full copy',
    sourceRoot: 'Source root',
    outputRoot: 'Output root',
    relativePath: 'Relative path',
    exportFormat: 'Export format',
    exportJson: 'JSON only',
    exportTxt: 'Flat TXT only',
    exportBoth: 'Both',
    recursive: 'Recurse subdirectories',
    enableReplace: 'Enable replace',
    enableOcr: 'Enable OCR',
    ocrMinConfidence: 'OCR min confidence',
    ocrTitle: 'OCR results',
    ocrProcessed: 'Recognized',
    ocrFailed: 'Failed',
    ocrRegions: 'Text regions',
    ocrEmpty: 'This job produced no OCR results.',
    ocrUnavailableHint: 'A missing OCR runtime or a failed image is recorded as a warning and never blocks the run.',
    preflight: 'Preflight',
    createJob: 'Create job',
    startJob: 'Start job',
    cancelJob: 'Cancel job',
    recoverJob: 'Recover job',
    restoreJob: 'Restore dataset',
    discardJob: 'Discard workspace',
    pinJob: 'Pin job',
    preflightOk: 'Preflight passed',
    preflightFailed: 'Preflight failed',
    warnings: 'Warnings',

    jobId: 'Job ID',
    status: 'Status',
    samples: 'Samples',
    progress: 'Progress',
    module: 'Current stage',
    created: 'Created',
    issuesTitle: 'Issues',
    issuesEmpty: 'No unresolved issues.',
    severity: 'Severity',
    blocking: 'Blocking',
    code: 'Code',
    message: 'Message',
    refresh: 'Refresh',
    yes: 'Yes',
    no: 'No',

    compatibilityTitle: 'Compatibility notes',
    compatibilityBody:
      'The rule-only stages (import, replace, nine-field normalization, export, backup and commit) match the source project. The classification dictionary, the LSE14-5k quality model and the formal Danbooru resources are unavailable, so those stages report as unavailable when their resource is missing rather than falling back silently.',
    countReviewTitle: 'Count review',
    countReviewEmpty: 'No counts awaiting review.',
    countReviewPending: 'Pending',
    countProposed: 'Proposed',
    countSource: 'Source',
    countEvidence: 'Evidence',
    countConflict: 'Source conflict',
    countApply: 'Apply',
    countConfirm: 'Confirm review complete',
    countConfirmed: 'Confirmed',
    countStale: 'This entry changed elsewhere. Refresh and try again.',
    countGateBlocked: 'Entries are still unreviewed, so export is blocked.',
    pauseJob: 'Pause',
    resumeJob: 'Resume',
    repairJob: 'Repair interrupted run',
    repairResult: 'Repair result',
    reclaimed: 'Reclaimed',
    parked: 'Parked',
    journalState: 'Commit journal state',
    wikiUnavailable: 'The e621 wiki snapshot is unavailable, so count rules fall back to the original annotation instead of inferring a value.',
    jobControlsTitle: 'Job controls',
    resumableSamples: 'Resumable samples',
    committedFiles: 'Committed files',
    tokenReviewTitle: 'Token budget review',
    tokenReviewEmpty: 'No samples exceed the budget.',
    tokenReviewUnresolved: 'Unresolved',
    tokenCaption: 'Current caption',
    tokenCount: 'Tokens',
    tokenLimit: 'Budget',
    tokenOverBy: 'Over by',
    tokenProposal: 'Proposal',
    tokenEdit: 'Edit manually',
    tokenRecount: 'Recount',
    tokenRewriteShort: 'Rewrite short',
    tokenApply: 'Apply proposal',
    tokenConfirm: 'Confirm token review complete',
    tokenStale: 'This entry changed elsewhere. Refresh and try again.',
    tokenUnavailable: 'No tokenizer resource is registered, so counting actions are disabled.',
    tokenNotApplied: 'A proposal is never written into the final JSON until it is applied.',
  },
}

export function copyFor(language: WorkflowLanguage): WorkflowCopy {
  return workflowCopy[language]
}
