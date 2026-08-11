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
  preflight: string
  createJob: string
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
    preflight: '预检',
    createJob: '创建任务',
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
    preflight: 'Preflight',
    createJob: 'Create job',
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
  },
}

export function copyFor(language: WorkflowLanguage): WorkflowCopy {
  return workflowCopy[language]
}
