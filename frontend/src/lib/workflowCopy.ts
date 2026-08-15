/**
 * Bilingual copy for the Dataset Workflow module.
 *
 * Only this module is bilingual; the rest of the application stays Chinese.
 * Every key must exist in both languages so a missing string cannot fall back
 * to a raw identifier in the UI.
 */
import type { WorkflowLanguage } from '../types'

export interface WorkflowHelp {
  purpose: string
  recommended: string
  note: string
}

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
  captionModel: string
  captionModelDetails: string
  sourceRoot: string
  outputRoot: string
  sourcePath: string
  outputPath: string
  pathReady: string
  pathCreateRequired: string
  pathCreateConfirm: string
  pathCreateCancel: string
  pathCreateOutput: string
  pathBindingError: string
  relativePath: string
  sourceRelativePath: string
  outputRelativePath: string
  exportFormat: string
  exportJson: string
  exportTxt: string
  exportBoth: string
  recursive: string
  enableClassify: string
  classifyResource: string
  enableReplace: string
  replaceResource: string
  replacePassDropNotice: string
  enableOcr: string
  ocrResource: string
  ocrMinConfidence: string
  enableTokenBudget: string
  tokenizerResource: string
  tokenMaxTokens: string
  captionRuntimeHint: string
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
  unpinJob: string
  pinnedJob: string
  discardPinnedHint: string
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
  eventsTitle: string
  eventsEmpty: string
  eventsError: string
  eventCursor: string
  workflowSteps: string
  stageDataset: string
  stageCaption: string
  stageClassify: string
  stageReplace: string
  stageOcr: string
  stageCount: string
  stageToken: string
  stageExport: string
  stageDatasetHint: string
  stageCaptionHint: string
  stageClassifyHint: string
  stageReplaceHint: string
  stageOcrHint: string
  stageCountHint: string
  stageTokenHint: string
  stageExportHint: string
  advancedResources: string
  advancedResourcesHint: string
  enabledStages: string
  checkSettings: string
  createPendingTask: string
  noLoadedModel: string
  chooseModel: string
  multipleModels: string
  taskCreatedPending: string
  inPlaceWarning: string
  taskSummary: string
  summaryOutputMode: string
  summarySource: string
  summaryDestination: string
  summaryCaption: string
  summarySteps: string
  summaryResources: string
  summaryReview: string
  summaryNotChecked: string
  summaryChecked: string
  helpLabels: {
    button: string
    purpose: string
    recommended: string
    note: string
    close: string
  }
  help: Record<string, WorkflowHelp>
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
    resourceId: '内部标识',
    resourceCategory: '类别',
    resourceFingerprint: '内容校验码',
    importTitle: '导入自定义标签替换表',
    importRootId: 'CSV 所在目录',
    importRelativePath: 'CSV 文件位置',
    importResourceId: '保存名称',
    importPreview: '检查 CSV',
    importApply: '确认导入',
    importPreviewOk: '校验通过',
    importPreviewFailed: '校验失败',
    importRuleCount: '可执行规则',
    importPassthrough: '原样保留（旧规则）',
    importFingerprint: '内容校验码',

    jobsTitle: '任务',
    jobsEmpty: '暂无工作流任务。',
    createJobTitle: '新建任务',
    profile: '标签规范',
    workMode: '输出方式',
    workModeInPlace: '更新原数据集（自动备份）',
    workModeFullCopy: '生成新数据集（推荐）',
    captionModel: 'Caption 模型',
    sourceRoot: '源数据集完整路径',
    outputRoot: '新数据集完整路径',
    sourcePath: '源数据集完整路径',
    outputPath: '新数据集完整路径',
    pathReady: '目录已准备好，可以检查设置。',
    pathCreateRequired: '输出目录尚不存在，需要确认后创建。',
    pathCreateConfirm: '创建输出目录',
    pathCreateCancel: '暂不创建',
    pathCreateOutput: '创建目录并继续检查',
    pathBindingError: '目录绑定失败',
    relativePath: '子文件夹',
    sourceRelativePath: '源数据集子文件夹',
    outputRelativePath: '输出子文件夹',
    exportFormat: '导出格式',
    exportJson: '仅 JSON',
    exportTxt: '仅扁平 TXT',
    exportBoth: '两者',
    recursive: '包含子文件夹中的图片',
    enableClassify: '启用分类',
    classifyResource: '分类快照',
    enableReplace: '启用替换',
    replaceResource: '替换索引',
    replacePassDropNotice: '当前 e621 推荐索引会把原索引中标记为 pass 的标签改为 drop，并从最终标签中删除。',
    enableOcr: '启用 OCR',
    ocrResource: 'OCR 运行时',
    ocrMinConfidence: 'OCR 最低置信度',
    enableTokenBudget: '启用 Token 预算',
    tokenizerResource: 'Tokenizer 资源',
    tokenMaxTokens: 'Token 预算上限',
    captionRuntimeHint: 'Caption 使用 Models/Workbench 中当前已加载的本地模型；创建时会冻结该模型 ID。',
    captionModelDetails: '后端 / 阈值来源',
    ocrTitle: 'OCR 结果',
    ocrProcessed: '已识别',
    ocrFailed: '失败',
    ocrRegions: '文本区域',
    ocrEmpty: '本次任务没有 OCR 结果。',
    ocrUnavailableHint: 'OCR 运行时缺失或识别失败时只记录警告，不会中断流程。',
    preflight: '检查设置',
    createJob: '创建任务',
    startJob: '开始处理',
    cancelJob: '取消任务',
    recoverJob: '恢复任务',
    restoreJob: '恢复数据集',
    discardJob: '丢弃工作区',
    pinJob: '固定任务',
    unpinJob: '取消固定',
    pinnedJob: '已固定',
    discardPinnedHint: '任务已固定；取消固定后才能丢弃工作区。',
    preflightOk: '检查通过',
    preflightFailed: '检查未通过',
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
      '纯规则阶段（导入、替换、九字段规范化、导出、备份与提交）与源项目行为一致。当前 e621 分类快照、替换索引、Tokenizer 与 CPU OCR 已登记；LSE14-5k 质量模型和 Danbooru 正式资源仍不可获得，缺失时会明确阻断，不会静默回退。',
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
    eventsTitle: '事件',
    eventsEmpty: '尚无持久化事件。',
    eventsError: '事件暂时不可用，正在重试。',
    eventCursor: '游标',
    workflowSteps: '处理步骤',
    stageDataset: '数据集',
    stageCaption: 'Caption',
    stageClassify: '标签分类',
    stageReplace: '标签替换',
    stageOcr: 'OCR 文字识别',
    stageCount: '数量复核',
    stageToken: 'Token 长度控制',
    stageExport: '导出与创建',
    stageDatasetHint: '选择源数据集、输出方式和处理范围。',
    stageCaptionHint: '沿用本地模型页面中已加载的 Caption 模型。',
    stageClassifyHint: '按 e621 规则整理标签字段。',
    stageReplaceHint: '应用已登记的标签替换规则。',
    stageOcrHint: '可选：识别图片中的文字。',
    stageCountHint: '导出前必须人工确认数量。',
    stageTokenHint: '可选：按指定 Tokenizer 检查文本长度。',
    stageExportHint: '检查设置、创建任务，然后显式开始处理。',
    advancedResources: '资源维护（高级）',
    advancedResourcesHint: '日常任务无需修改；这里用于导入自定义替换表。',
    noLoadedModel: '没有已加载的本地模型，请先前往本地模型。',
    chooseModel: '前往本地模型',
    multipleModels: '检测到多个已加载模型，请明确选择。',
    taskCreatedPending: '任务已创建，尚未开始。请在任务控制中点击“开始处理”。',
    inPlaceWarning: '更新原数据集会先创建备份，并直接覆盖原标注文件；建议先用“生成新数据集”验证。',
    taskSummary: '任务摘要',
    summaryOutputMode: '输出方式',
    summarySource: '源数据集',
    summaryDestination: '输出位置',
    summaryCaption: 'Caption 模型',
    summarySteps: '处理步骤',
    summaryResources: '资源',
    summaryReview: '人工复核',
    summaryNotChecked: '尚未检查设置',
    summaryChecked: '检查设置已通过',
    enabledStages: '已开启步骤',
    checkSettings: '检查设置',
    createPendingTask: '创建任务',
    helpLabels: { button: '帮助', purpose: '作用', recommended: '推荐', note: '注意', close: '关闭帮助' },
    help: {
      profile: { purpose: '选择标签字段和分类规则使用的规范。', recommended: '处理 e621 数据时选择 e621。', note: 'Danbooru 资源尚未完整配置，选择后可能在检查设置时阻断。' },
      workMode: { purpose: '决定结果写回原数据集，还是写入一个新的副本。', recommended: '第一次处理选择“生成新数据集”，原目录不会被修改。', note: '更新原数据集会先创建备份；仍建议先用小批量验证。' },
      sourceRoot: { purpose: '填写要读取的数据集完整路径。', recommended: '例如 E:\\datasets\\train；必须是已授权且存在的目录。', note: '不能填写相对路径、盘符根目录、.. 或模型目录。' },
      sourceRelativePath: { purpose: '该字段仅用于兼容旧任务配置。', recommended: '新任务请直接填写完整路径。', note: '界面不会再显示相对路径输入。' },
      outputRoot: { purpose: '填写生成新数据集的完整保存路径。', recommended: '例如 E:\\datasets\\train_processed；建议使用新的空目录。', note: '目录不存在时检查设置会先请求确认创建；不能与源目录重叠。' },
      outputRelativePath: { purpose: '该字段仅用于兼容旧任务配置。', recommended: '新任务请直接填写完整路径。', note: '界面不会再显示相对路径输入。' },
      exportFormat: { purpose: '决定每张图片生成 JSON、TXT，还是两种文件。', recommended: '训练数据通常选择 JSON + TXT。', note: 'TXT 是扁平标签文本；JSON 保存完整九字段结构。' },
      recursive: { purpose: '把源目录下的子文件夹也纳入扫描范围。', recommended: '图片按子文件夹组织时开启。', note: '开启后处理样本数可能大幅增加，请先检查设置。' },
      captionModel: { purpose: '选择 Caption 使用的本地模型。', recommended: '选择 Models/Workbench 中已加载的模型，并沿用其阈值和预处理设置。', note: '没有已加载模型时无法通过检查设置；多个模型时必须明确选择。' },
      classifyEnabled: { purpose: '使用 e621 分类快照整理 quality、character、artist 等字段。', recommended: '需要结构化九字段时开启。', note: '开启后必须有兼容的分类快照资源。' },
      classifyResource: { purpose: '指定用于标签分类的 e621 词典快照。', recommended: '使用已登记的 e621 分类快照。', note: '资源内容会在任务创建时冻结，资源变化不会静默混用。' },
      replaceEnabled: { purpose: '按替换规则统一标签名称、删除规则标签或保留原标签。', recommended: 'e621 标注清洗通常开启。', note: '替换只写入工作区/新副本，确认提交前不会修改原目录。' },
      replaceResource: { purpose: '选择要应用的标签替换索引。', recommended: 'e621 默认使用 pass→drop 清理索引；原 pass 标签会被删除。', note: '自定义索引需先在页面底部的资源维护区导入。' },
      ocrEnabled: { purpose: '识别图片中的文字，并把结果作为后续标注上下文。', recommended: '漫画对白、截图文字较多时开启。', note: 'OCR 会增加处理时间；普通插画可关闭。' },
      ocrResource: { purpose: '选择隔离的 CPU OCR 运行时。', recommended: '使用已登记的 PaddleOCR CPU 运行时。', note: '运行时缺失会在检查设置时阻断，不会自动下载系统 Python 依赖。' },
      ocrMinConfidence: { purpose: '设置 OCR 结果进入输出的最低置信度。', recommended: '从 0.5 开始。', note: '调低会保留更多文字但可能增加误识别；调高会漏掉浅色或模糊文字。' },
      countReview: { purpose: '在导出前人工确认 solo、duo、trio 或 group 数量。', recommended: '保持开启，尤其是训练数据首次整理时。', note: '未完成复核不会提交最终数据集。' },
      tokenBudgetEnabled: { purpose: '检查最终训练文本是否超过指定 Token 上限。', recommended: '只有训练器有明确 token 限制时开启。', note: '开启后必须选择可用 Tokenizer，并可能产生人工复核。' },
      tokenizerResource: { purpose: '为 Token 计数提供确定的本地 tokenizer。', recommended: '使用已登记的 Qwen tokenizer 资源。', note: '不能使用猜测值或自动下载的 tokenizer 代替。' },
      tokenMaxTokens: { purpose: '设置每条最终训练文本允许的最大 token 数。', recommended: '按实际训练器上下文限制填写，例如 512。', note: '过小会频繁触发改写；过大可能超过训练器限制。' },
      preflight: { purpose: '只读检查目录、资源、样本数量和预计输出。', recommended: '每次修改设置后都重新检查。', note: '检查设置不会创建任务，也不会写入目标目录。' },
      createJob: { purpose: '保存一份待处理的任务配置和资源快照。', recommended: '检查设置通过后创建。', note: '创建任务不会自动开始处理，需要再点击“开始处理”。' },
      startJob: { purpose: '将已创建的待处理任务加入队列并开始处理。', recommended: '确认配置和输出位置无误后点击。', note: '开始后会按批次读取暂停、取消和人工复核状态。' },
      importRoot: { purpose: '选择自定义替换 CSV 所在的已授权输入目录。', recommended: '只在导入自定义索引时使用。', note: '它不是模型选择器，也不是新任务的数据集选择器。' },
      importRelativePath: { purpose: '填写 CSV 相对于所选输入目录的路径。', recommended: '例如 e621_general_tag_replacement_index.csv。', note: '不能填写绝对路径或目录外路径。' },
      importResourceId: { purpose: '为导入后的替换规则设置唯一内部名称。', recommended: '使用能说明 profile 和版本的名称。', note: '同名资源会显示覆盖警告；任务会冻结导入时的指纹。' },
    },
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
    resourceId: 'Internal ID',
    resourceCategory: 'Category',
    resourceFingerprint: 'Content checksum',
    importTitle: 'Import custom tag replacement table',
    importRootId: 'CSV directory',
    importRelativePath: 'CSV file location',
    importResourceId: 'Save as',
    importPreview: 'Check CSV',
    importApply: 'Confirm import',
    importPreviewOk: 'Validation passed',
    importPreviewFailed: 'Validation failed',
    importRuleCount: 'Executable rules',
    importPassthrough: 'Identity passthrough',
    importFingerprint: 'Content checksum',

    jobsTitle: 'Jobs',
    jobsEmpty: 'No workflow jobs yet.',
    createJobTitle: 'Create job',
    profile: 'Tag standard',
    workMode: 'Output mode',
    workModeInPlace: 'Update source dataset (backup first)',
    workModeFullCopy: 'Generate new dataset (recommended)',
    captionModel: 'Caption model',
    sourceRoot: 'Full source dataset path',
    outputRoot: 'Full output dataset path',
    sourcePath: 'Full source dataset path',
    outputPath: 'Full output dataset path',
    pathReady: 'Directories are ready for settings checks.',
    pathCreateRequired: 'The output directory does not exist and needs confirmation.',
    pathCreateConfirm: 'Create output directory',
    pathCreateCancel: 'Not now',
    pathCreateOutput: 'Create directory and continue',
    pathBindingError: 'Directory binding failed',
    relativePath: 'Subfolder',
    sourceRelativePath: 'Source subfolder',
    outputRelativePath: 'Output subfolder',
    exportFormat: 'Export format',
    exportJson: 'JSON only',
    exportTxt: 'Flat TXT only',
    exportBoth: 'Both',
    recursive: 'Include images in subfolders',
    enableClassify: 'Enable classification',
    classifyResource: 'Classification snapshot',
    enableReplace: 'Enable replace',
    replaceResource: 'Replacement index',
    replacePassDropNotice: 'The recommended e621 index converts every original pass rule to drop, removing those tags from the final annotation.',
    enableOcr: 'Enable OCR',
    ocrResource: 'OCR runtime',
    ocrMinConfidence: 'OCR min confidence',
    enableTokenBudget: 'Enable token budget',
    tokenizerResource: 'Tokenizer resource',
    tokenMaxTokens: 'Token budget limit',
    captionRuntimeHint: 'Caption uses the local model currently loaded in Models/Workbench; its model ID is frozen when the job is created.',
    captionModelDetails: 'Backend / threshold source',
    ocrTitle: 'OCR results',
    ocrProcessed: 'Recognized',
    ocrFailed: 'Failed',
    ocrRegions: 'Text regions',
    ocrEmpty: 'This job produced no OCR results.',
    ocrUnavailableHint: 'A missing OCR runtime or a failed image is recorded as a warning and never blocks the run.',
    preflight: 'Check settings',
    createJob: 'Create job',
    startJob: 'Start processing',
    cancelJob: 'Cancel job',
    recoverJob: 'Recover job',
    restoreJob: 'Restore dataset',
    discardJob: 'Discard workspace',
    pinJob: 'Pin job',
    unpinJob: 'Unpin job',
    pinnedJob: 'Pinned',
    discardPinnedHint: 'This job is pinned. Unpin it before discarding the workspace.',
    preflightOk: 'Checks passed',
    preflightFailed: 'Checks failed',
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
      'The rule-only stages (import, replace, nine-field normalization, export, backup and commit) match the source project. The e621 classification snapshot, replacement index, tokenizer and CPU OCR runtime are registered locally; the LSE14-5k quality model and formal Danbooru resources remain unavailable and fail closed rather than falling back silently.',
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
    eventsTitle: 'Events',
    eventsEmpty: 'No durable events yet.',
    eventsError: 'Events are temporarily unavailable; retrying.',
    eventCursor: 'Cursor',
    workflowSteps: 'Processing steps',
    stageDataset: 'Dataset',
    stageCaption: 'Caption',
    stageClassify: 'Tag classification',
    stageReplace: 'Tag replacement',
    stageOcr: 'OCR text recognition',
    stageCount: 'Count review',
    stageToken: 'Token length control',
    stageExport: 'Export and create',
    stageDatasetHint: 'Choose the source, output mode and processing scope.',
    stageCaptionHint: 'Reuse a Caption model already loaded in the local model page.',
    stageClassifyHint: 'Organize tag fields using e621 rules.',
    stageReplaceHint: 'Apply a registered tag replacement index.',
    stageOcrHint: 'Optional: recognize text in images.',
    stageCountHint: 'Manual count confirmation is required before export.',
    stageTokenHint: 'Optional: check text length with a registered tokenizer.',
    stageExportHint: 'Check settings, create the job, then start it explicitly.',
    advancedResources: 'Resource maintenance (advanced)',
    advancedResourcesHint: 'Not needed for ordinary jobs; import custom replacement tables here.',
    noLoadedModel: 'No local model is loaded. Open Local Models first.',
    chooseModel: 'Open Local Models',
    multipleModels: 'Multiple loaded models found; choose one explicitly.',
    taskCreatedPending: 'Job created and waiting. Open Job controls and click Start processing.',
    inPlaceWarning: 'Updating in place creates a backup and overwrites source annotation files; use Generate new dataset for the first run.',
    taskSummary: 'Task summary',
    summaryOutputMode: 'Output mode',
    summarySource: 'Source dataset',
    summaryDestination: 'Output location',
    summaryCaption: 'Caption model',
    summarySteps: 'Processing steps',
    summaryResources: 'Resources',
    summaryReview: 'Manual review',
    summaryNotChecked: 'Settings not checked yet',
    summaryChecked: 'Settings checks passed',
    enabledStages: 'Enabled steps',
    checkSettings: 'Check settings',
    createPendingTask: 'Create job',
    helpLabels: { button: 'help', purpose: 'Purpose', recommended: 'Recommended', note: 'Note', close: 'Close help' },
    help: {
      profile: { purpose: 'Chooses the tag fields and classification rules used by the job.', recommended: 'Choose e621 for e621 data.', note: 'Danbooru resources are not complete and may fail preflight.' },
      workMode: { purpose: 'Decides whether results update the source dataset or a new copy.', recommended: 'Choose Generate new dataset for a first run.', note: 'Updating in place creates a backup, but a small trial is still recommended.' },
      sourceRoot: { purpose: 'Enter the full dataset directory to read.', recommended: 'For example E:\\datasets\\train; it must exist and be authorized.', note: 'Relative paths, drive roots, .. and model directories are rejected.' },
      sourceRelativePath: { purpose: 'Compatibility field for legacy job configurations.', recommended: 'Enter a full path for new jobs.', note: 'The new UI no longer exposes a relative-path field.' },
      outputRoot: { purpose: 'Enter the full directory for a generated dataset copy.', recommended: 'For example E:\\datasets\\train_processed; use a new empty directory.', note: 'If missing, settings checks ask before creating it; it may not overlap the source.' },
      outputRelativePath: { purpose: 'Compatibility field for legacy job configurations.', recommended: 'Enter a full path for new jobs.', note: 'The new UI no longer exposes a relative-path field.' },
      exportFormat: { purpose: 'Chooses JSON, TXT, or both for each image.', recommended: 'Use JSON + TXT for training datasets.', note: 'TXT is flat tag text; JSON retains the complete nine-field structure.' },
      recursive: { purpose: 'Includes subfolders below the source directory.', recommended: 'Enable it when images are organized in subfolders.', note: 'The sample count may increase substantially; check settings first.' },
      captionModel: { purpose: 'Selects the local model used by Caption.', recommended: 'Use a model loaded in Models/Workbench so its thresholds and preprocessing are reused.', note: 'Preflight is blocked with no loaded model; multiple loaded models require an explicit choice.' },
      classifyEnabled: { purpose: 'Uses the e621 classification snapshot to organize quality, character and artist fields.', recommended: 'Enable it when structured nine-field output is required.', note: 'A compatible classification snapshot is required.' },
      classifyResource: { purpose: 'Selects the e621 dictionary snapshot used for classification.', recommended: 'Use the registered e621 classification snapshot.', note: 'The resource is frozen at job creation and cannot silently change mid-run.' },
      replaceEnabled: { purpose: 'Normalizes tag names, applies drops and preserves configured passthrough rules.', recommended: 'Usually enable it for e621 cleanup.', note: 'Changes are staged or written to a new copy until commit; the source stays untouched.' },
      replaceResource: { purpose: 'Selects the replacement index to apply.', recommended: 'The e621 default uses the pass-to-drop cleanup index; passthrough tags are removed.', note: 'Import a custom index in the resource maintenance section first.' },
      ocrEnabled: { purpose: 'Recognizes text in images for downstream annotation context.', recommended: 'Enable it for comics, screenshots or images with visible text.', note: 'OCR adds processing time; ordinary illustrations can leave it off.' },
      ocrResource: { purpose: 'Selects the isolated CPU OCR runtime.', recommended: 'Use the registered PaddleOCR CPU runtime.', note: 'A missing runtime blocks preflight; the system Python is never used as a fallback.' },
      ocrMinConfidence: { purpose: 'Sets the minimum confidence for OCR text to enter the output.', recommended: 'Start at 0.5.', note: 'Lower values keep more text but may add false detections; higher values may miss faint text.' },
      countReview: { purpose: 'Requires manual confirmation of solo, duo, trio or group before export.', recommended: 'Keep it enabled, especially for a first dataset cleanup.', note: 'The final dataset is not committed until review is complete.' },
      tokenBudgetEnabled: { purpose: 'Checks final training text against a token limit.', recommended: 'Enable it only when the trainer has a defined token limit.', note: 'A usable tokenizer is required and manual review may be created.' },
      tokenizerResource: { purpose: 'Provides deterministic local token counting.', recommended: 'Use the registered Qwen tokenizer resource.', note: 'Guessed counts and automatic downloads are not accepted.' },
      tokenMaxTokens: { purpose: 'Sets the maximum tokens allowed in final training text.', recommended: 'Match the trainer context limit, for example 512.', note: 'A value that is too low causes frequent rewrites; too high may exceed the trainer limit.' },
      preflight: { purpose: 'Read-only checks paths, resources, sample counts and planned output.', recommended: 'Run it after every setting change.', note: 'It creates no job and writes nothing to the target dataset.' },
      createJob: { purpose: 'Saves a pending job configuration and resource snapshot.', recommended: 'Create only after preflight passes.', note: 'Creating a job does not start it; use Start processing separately.' },
      startJob: { purpose: 'Queues a pending job and starts processing.', recommended: 'Start after confirming the configuration and output location.', note: 'Pause, cancel and review states are checked at batch boundaries.' },
      importRoot: { purpose: 'Selects the authorized input directory containing a custom replacement CSV.', recommended: 'Use this only when importing a custom index.', note: 'This is not a model selector or a dataset task selector.' },
      importRelativePath: { purpose: 'Specifies the CSV path relative to the selected input directory.', recommended: 'For example e621_general_tag_replacement_index.csv.', note: 'Absolute paths and paths outside the root are rejected.' },
      importResourceId: { purpose: 'Assigns a unique internal name to the imported replacement rules.', recommended: 'Use a name that identifies the profile and version.', note: 'An existing name shows a warning; jobs freeze the import fingerprint.' },
    },
  },
}

export function copyFor(language: WorkflowLanguage): WorkflowCopy {
  return workflowCopy[language]
}
