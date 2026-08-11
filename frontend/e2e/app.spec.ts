import { expect, test, type Page } from '@playwright/test'
import path from 'node:path'

const pages = ['工作台', '视频提示词', '批量任务', '在线模型', '本地模型', '设置'] as const

test.beforeEach(async ({ page }) => {
  await mockApi(page)
  await page.addInitScript(() => localStorage.clear())
})

test('all routes fit the viewport and produce review screenshots', async ({ page }, testInfo) => {
  await page.goto('/')
  for (const name of pages) {
    await navigate(page, name)
    await expect(page.getByRole('heading', { name, level: 1 })).toBeVisible()
    await expectNoHorizontalOverflow(page)
    await page.screenshot({
      path: path.join('test-results', 'screenshots', `${testInfo.project.name}-${routeSlug(name)}.png`),
      fullPage: true,
    })
  }
})

test('video prompt desk generates, restores, and clears in-memory revisions', async ({ page }) => {
  const payloads: string[] = []
  await page.route('**/api/v1/video-prompts/generate', async (route) => {
    const payload = route.request().postData() ?? ''
    payloads.push(payload)
    const fl2va = payload.includes('name="prompt_mode"') && payload.includes('fl2va')
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(fl2va ? fl2vaPromptPackage() : videoPromptPackage()) })
  })
  await page.goto('/')
  await navigate(page, '视频提示词')
  await expect(page.getByRole('combobox', { name: 'Provider' })).not.toHaveValue('')
  await page.locator('input[type="file"]').setInputFiles([
    {
      name: 'reference-1.png',
      mimeType: 'image/png',
      buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9JvFIAAAAASUVORK5CYII=', 'base64'),
    },
    {
      name: 'reference-2.png',
      mimeType: 'image/png',
      buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9JvFIAAAAASUVORK5CYII=', 'base64'),
    },
  ])
  await expect(page.getByText('<Picture 2>', { exact: true })).toBeVisible()
  await page.getByRole('textbox', { name: '生成要求' }).fill('让镜头缓慢推进，人物轻轻眨眼。')
  await page.getByRole('button', { name: '生成提示词' }).click()
  await expect(page.getByText('已生成稳定的缓慢推进镜头。')).toBeVisible()
  await expect(page.getByRole('article').getByText('第 1 版 · 当前基线')).toBeVisible()

  await page.getByRole('textbox', { name: '生成要求' }).fill('把镜头改为固定机位。')
  await page.getByRole('button', { name: '生成新版本' }).click()
  await expect(page.getByRole('article').getByText('第 2 版 · 当前基线')).toBeVisible()
  expect(payloads[1]).toContain('current_package_json')
  await expect(page.getByText('subject_definitions', { exact: true })).toBeVisible()
  await expect(page.getByText('[Shot 2] At 00:03.500,', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: '查看第 1 版' }).click()
  await expect(page.getByText('第 1 版 · 历史版本')).toBeVisible()
  await page.getByRole('button', { name: '恢复第 1 版' }).click()
  await expect(page.getByRole('article').getByText('第 1 版 · 当前基线')).toBeVisible()

  page.once('dialog', (dialog) => dialog.accept())
  await page.getByRole('button', { name: '清空任务' }).click()
  await expect(page.getByText('上传 1 到 9 张参考图片')).toBeVisible()
  await page.getByRole('tab', { name: 'FL2VA', exact: true }).click()
  await expect(page.getByText('H3 T2VA PROMPT DESK', { exact: true })).toBeVisible()
  await page.locator('input[type="file"]').setInputFiles([
    {
      name: 'fl2va-first.png',
      mimeType: 'image/png',
      buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9JvFIAAAAASUVORK5CYII=', 'base64'),
    },
    {
      name: 'fl2va-last.png',
      mimeType: 'image/png',
      buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9JvFIAAAAASUVORK5CYII=', 'base64'),
    },
  ])
  await page.getByRole('textbox', { name: '生成要求' }).fill('让主体连续运动并停在稳定的结束状态。')
  await page.getByRole('button', { name: '生成提示词' }).click()
  await expect(page.getByText('integrated_multimodal_description', { exact: true })).toBeVisible()
  expect(payloads.at(-1)).toContain('fl2va')
  expect(payloads.at(-1)?.match(/name="images"/g)).toHaveLength(2)
  await expectNoHorizontalOverflow(page)
})

test('classifier and adapter controls are visible and keyboard focusable', async ({ page }, testInfo) => {
  await page.goto('/')
  const workbenchUnderscores = page.getByRole('checkbox', { name: '下划线替空格', exact: true })
  await expect(workbenchUnderscores).toBeVisible()
  await expect(workbenchUnderscores).not.toBeChecked()
  const workbenchRating = page.getByRole('checkbox', { name: '输出 Rating 标签', exact: true })
  const workbenchParentheses = page.getByRole('checkbox', { name: '括号转义', exact: true })
  await expect(workbenchRating).toBeVisible()
  await expect(workbenchRating).not.toBeChecked()
  await expect(workbenchParentheses).toBeVisible()
  await expect(workbenchParentheses).toBeChecked()
  await workbenchUnderscores.focus()
  await page.keyboard.press('Space')
  await expect(workbenchUnderscores).toBeChecked()
  await expect(page.getByText('WD EVA02 Large Tagger', { exact: true })).toBeVisible()
  await expect(page.getByText('ConvNeXt V2 Tagger', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '调节 WD EVA02 Large Tagger 阈值' })).toBeVisible()
  const aesthetic = page.getByRole('checkbox', { name: '启用美学评分' })
  await expect(aesthetic).toBeVisible()
  await aesthetic.focus()
  await expect(aesthetic).toBeFocused()
  await page.keyboard.press('Space')
  await expect(aesthetic).toBeChecked()
  await expectNoHorizontalOverflow(page)
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: path.join('test-results', 'screenshots', `${testInfo.project.name}-workbench-local.png`), fullPage: true })

  await navigate(page, '批量任务')
  const batchUnderscores = page.getByRole('checkbox', { name: '下划线替空格', exact: true })
  await expect(batchUnderscores).toBeVisible()
  await expect(batchUnderscores).not.toBeChecked()
  await batchUnderscores.focus()
  await page.keyboard.press('Space')
  await expect(batchUnderscores).toBeChecked()
  await page.getByRole('tab', { name: '本地模型' }).click()
  await expect(page.getByRole('checkbox', { name: '输出 Rating 标签', exact: true })).not.toBeChecked()
  await expect(page.getByRole('checkbox', { name: '括号转义', exact: true })).toBeChecked()
  await expect(page.getByRole('checkbox', { name: '启用美学评分' })).toBeVisible()
  await expectNoHorizontalOverflow(page)
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: path.join('test-results', 'screenshots', `${testInfo.project.name}-batch-local.png`), fullPage: true })

  await navigate(page, '本地模型')
  await expect(page.getByRole('heading', { name: '美学评分模型' })).toBeVisible()
  await page.getByRole('button', { name: '模型设置' }).first().click()
  await expect(page.getByRole('slider', { name: '通用阈值' })).toHaveValue('0.35')
  await expect(page.getByRole('slider', { name: '角色阈值' })).toHaveValue('0.85')
  const adapterType = page.getByRole('combobox', { name: 'Adapter 类型' })
  await expect(adapterType).toBeVisible()
  await adapterType.focus()
  await expect(adapterType).toBeFocused()
  await adapterType.selectOption('lora')
  await expect(page.getByRole('textbox', { name: 'Adapter 相对路径' })).toBeVisible()
  await expectNoHorizontalOverflow(page)
  await page.screenshot({ path: path.join('test-results', 'screenshots', `${testInfo.project.name}-model-adapter.png`) })
})

test('batch hybrid mode submits one local job with the required combined outputs', async ({ page }, testInfo) => {
  const bodies: Array<Record<string, unknown>> = []
  await page.route('**/api/v1/scans', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      items: [{ id: 'image-1', relative_path: 'sample.png', file_name: 'sample.png', size: 128 }],
      total: 1,
    }),
  }))
  await page.route('**/api/v1/jobs', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    bodies.push(route.request().postDataJSON() as Record<string, unknown>)
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ...jobSummary('running'), id: `hybrid-${bodies.length}`, mode: 'local', hybrid: true }),
    })
  })

  await page.goto('/')
  await navigate(page, '批量任务')
  await page.getByRole('textbox', { name: '输入文件夹路径' }).fill('E:\\6_santabear1')
  await page.getByRole('button', { name: '扫描目录' }).click()
  await expect(page.getByText('扫描完成：找到 1 张')).toBeVisible()
  await page.getByRole('tab', { name: '本地 + 在线' }).click()
  await expect(page.getByText('WD EVA02 Large Tagger', { exact: true })).toBeVisible()
  await expect(page.getByText('ConvNeXt V2 Tagger', { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: '调节 WD EVA02 Large Tagger 阈值' }).click()
  const thresholdDialog = page.getByRole('dialog', { name: 'WD EVA02 Large Tagger' })
  await thresholdDialog.getByRole('slider', { name: '通用阈值' }).fill('0.42')
  await thresholdDialog.getByRole('button', { name: '应用到本次任务' }).click()
  await expect(page.getByText(/本次自定义/)).toBeVisible()
  await page.screenshot({ path: path.join('test-results', 'screenshots', `${testInfo.project.name}-batch-hybrid.png`), fullPage: true })
  await expect(page.getByRole('combobox', { name: '在线输出' })).toHaveValue('nl')
  await page.getByRole('button', { name: '创建批量任务' }).click()
  await expect.poll(() => bodies.length).toBe(1)
  expect(bodies[0]).toMatchObject({
    mode: 'local',
    hybrid: true,
    source: { type: 'scan', root_id: 'direct-input', relative_path: '' },
    provider_id: 'gemini-main',
    model_ids: ['model_demo_01'],
    thresholds: { model_demo_01: { general: 0.42 } },
    online_response: 'nl',
    output: { json: false, txt: true, txt_include_tags: false },
  })

  await page.getByRole('combobox', { name: '在线输出' }).selectOption('json')
  await page.getByRole('button', { name: '创建批量任务' }).click()
  await expect.poll(() => bodies.length).toBe(2)
  expect(bodies[1]).toMatchObject({
    mode: 'local',
    hybrid: true,
    online_response: 'json',
    output: { json: true, txt: true, txt_include_tags: false },
  })
  await expectNoHorizontalOverflow(page)
})

test('workbench applies a threshold override to only the selected loaded model', async ({ page }, testInfo) => {
  const jobBodies: Array<Record<string, unknown>> = []
  await page.route('**/api/v1/models', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ items: [
      { id: 'model_demo_01', name: 'WD EVA02 Large Tagger', backend: 'onnx', architecture: 'eva02_large_patch14', input_size: [448, 448], loaded: true, device: 'cuda', memory_mb: 1940, threshold: 0.35, thresholds: { default: 0.35, general: 0.35, character: 0.85, rating: 0.5 }, preset_thresholds: { default: 0.35, general: 0.35, character: 0.85, rating: 0.5 }, threshold_source: 'model', trusted_pickle: false, adapters: [], classifiers: ['aesthetic'] },
      { id: 'model_demo_02', name: 'ConvNeXt V2 Tagger', backend: 'safetensors', architecture: 'convnextv2_huge', input_size: [384, 384], loaded: true, device: 'cuda', memory_mb: 2200, threshold: 0.4, thresholds: { default: 0.4, general: 0.4, character: 0.72 }, preset_thresholds: { default: 0.4, general: 0.4, character: 0.72 }, threshold_source: 'model', trusted_pickle: false, adapters: [], classifiers: ['aesthetic'] },
    ] }),
  }))
  await page.route('**/api/v1/jobs', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    jobBodies.push(route.request().postDataJSON() as Record<string, unknown>)
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...jobSummary('running'), id: 'job-e2e', mode: 'local' }) })
  })
  await page.goto('/')
  await expect(page.getByRole('button', { name: '调节 WD EVA02 Large Tagger 阈值' })).toBeVisible()
  await page.getByRole('button', { name: '调节 WD EVA02 Large Tagger 阈值' }).click()
  const dialog = page.getByRole('dialog', { name: 'WD EVA02 Large Tagger' })
  await page.screenshot({ path: path.join('test-results', 'screenshots', `${testInfo.project.name}-workbench-quick-threshold.png`), fullPage: true })
  await dialog.getByRole('slider', { name: '通用阈值' }).fill('0.42')
  await dialog.getByRole('button', { name: '应用到本次任务' }).click()
  await expect(page.getByText(/本次自定义/)).toBeVisible()
  await page.getByRole('checkbox', { name: '输出 Rating 标签', exact: true }).check({ force: true })
  await page.getByRole('checkbox', { name: '括号转义', exact: true }).uncheck({ force: true })
  await page.locator('input[type="file"]').setInputFiles({
    name: 'task.png', mimeType: 'image/png',
    buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64'),
  })
  await page.getByRole('button', { name: '开始打标' }).click()
  await expect.poll(() => jobBodies.length).toBe(1)
  const body = jobBodies[0]
  expect(body).toMatchObject({
    model_ids: ['model_demo_01', 'model_demo_02'],
    separate_models: true,
    output: { include_rating: true, escape_parentheses: false },
  })
  expect(body.thresholds).toMatchObject({ model_demo_01: { general: 0.42 } })
  expect(body.thresholds).not.toHaveProperty('model_demo_02')
  await expectNoHorizontalOverflow(page)
})

test('workbench disables local inference when no models are loaded', async ({ page }) => {
  await page.route('**/api/v1/models', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ items: [
      { id: 'model_demo_01', name: 'WD EVA02 Large Tagger', backend: 'onnx', loaded: false, threshold: 0.35, thresholds: { default: 0.35, general: 0.35 }, threshold_source: 'model' },
    ] }),
  }))
  await page.goto('/')
  await expect(page.getByText('没有已加载模型', { exact: true })).toBeVisible()
  await expect(page.getByText('WD EVA02 Large Tagger', { exact: true })).toHaveCount(0)
  await page.locator('input[type="file"]').setInputFiles({
    name: 'task.png', mimeType: 'image/png',
    buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64'),
  })
  await expect(page.getByRole('button', { name: '开始打标' })).toBeDisabled()
  await page.getByRole('button', { name: '前往本地模型' }).click()
  await expect(page.getByRole('heading', { name: '本地模型', level: 1 })).toBeVisible()
  await expectNoHorizontalOverflow(page)
})

test('Hugging Face model download reports completion', async ({ page }, testInfo) => {
  await page.goto('/')
  await navigate(page, '本地模型')
  await page.getByRole('button', { name: '下载 Hugging Face 模型' }).click()
  const dialog = page.getByRole('dialog', { name: '下载模型' })
  const iconInputs = dialog.locator('.input-with-icon input')
  await expect(iconInputs).toHaveCount(2)
  for (const input of await iconInputs.all()) {
    expect(Number.parseFloat(await input.evaluate((element) => getComputedStyle(element).paddingLeft))).toBeGreaterThanOrEqual(36)
  }
  await dialog.getByRole('textbox', { name: 'Hugging Face 仓库地址' }).fill('https://huggingface.co/owner/tagger')
  await dialog.getByRole('textbox', { name: 'Revision（可选）' }).fill('main')
  await dialog.getByRole('button', { name: '开始下载' }).click()
  await expect(dialog.getByText('已注册 1 个模型')).toBeVisible()
  await expect(page.getByText('owner/tagger 下载、注册并自动加载完成')).toBeVisible()
  await expectNoHorizontalOverflow(page)
  await page.screenshot({ path: path.join('test-results', 'screenshots', `${testInfo.project.name}-model-download.png`) })
})

test('provider editor discovers and selects an available model', async ({ page }, testInfo) => {
  await page.goto('/')
  await navigate(page, '在线模型')
  await page.getByRole('button', { name: '编辑配置' }).first().click()
  const dialog = page.getByRole('dialog', { name: '编辑 Provider' })
  await dialog.getByRole('button', { name: '获取可用模型' }).click()
  const available = dialog.getByRole('combobox', { name: '可用模型' })
  await expect(available).toBeVisible()
  await available.selectOption('gemini-2.5-pro')
  await expect(dialog.getByRole('textbox', { name: '主模型' })).toHaveValue('gemini-2.5-pro')
  await expectNoHorizontalOverflow(page)
  await page.screenshot({ path: path.join('test-results', 'screenshots', `${testInfo.project.name}-provider-model-discovery.png`) })
})

test('new provider can discover models before it is saved', async ({ page }, testInfo) => {
  await page.goto('/')
  await navigate(page, '在线模型')
  await page.getByRole('button', { name: '添加 Provider' }).first().click()
  const dialog = page.getByRole('dialog', { name: '添加 Provider' })
  await dialog.getByRole('button', { name: '自定义 API', exact: true }).click()
  await dialog.getByRole('textbox', { name: '名称' }).fill('Unsaved gateway')
  await dialog.getByRole('textbox', { name: 'Base URL' }).fill('https://gateway.example.test/v1')
  await dialog.getByRole('textbox', { name: 'API Key / 密钥池' }).fill('temporary-key')
  const discover = dialog.getByRole('button', { name: '获取可用模型' })
  await expect(discover).toBeEnabled()
  await discover.click()
  const available = dialog.getByRole('combobox', { name: '可用模型' })
  await expect(available).toBeVisible()
  await available.selectOption('gateway-vision')
  await expect(dialog.getByRole('textbox', { name: '主模型' })).toHaveValue('gateway-vision')
  await expectNoHorizontalOverflow(page)
  await page.screenshot({ path: path.join('test-results', 'screenshots', `${testInfo.project.name}-provider-unsaved-discovery.png`) })
})

test('online prompt templates are editable and shared with the workbench', async ({ page }) => {
  await page.goto('/')
  await navigate(page, '在线模型')
  await expect(page.getByRole('heading', { name: '提示词模板' })).toBeVisible()
  await page.getByRole('textbox', { name: 'TAG Prompt' }).fill('SHARED TAG PROMPT')
  await page.getByRole('textbox', { name: 'NL Prompt' }).fill('SHARED NL PROMPT')
  await page.getByRole('textbox', { name: 'JSON Prompt' }).fill('SHARED JSON PROMPT')

  await navigate(page, '工作台')
  await expect(page.getByRole('textbox', { name: 'NL Prompt' })).toHaveValue('SHARED NL PROMPT')
  await expect(page.getByRole('textbox', { name: 'TAG Prompt' })).toHaveCount(0)
  await expect(page.getByRole('textbox', { name: 'JSON Prompt' })).toHaveCount(0)
  await expectNoHorizontalOverflow(page)
})

test('online concurrency is configured per batch task instead of per provider', async ({ page }) => {
  await page.goto('/')
  await navigate(page, '在线模型')
  await page.getByRole('button', { name: '编辑配置' }).first().click()
  const providerDialog = page.getByRole('dialog', { name: '编辑 Provider' })
  await expect(providerDialog.getByRole('spinbutton', { name: '并发' })).toHaveCount(0)
  await expect(providerDialog.getByRole('textbox', { name: '提示词 Profile' })).toHaveCount(0)
  await providerDialog.getByRole('button', { name: '关闭' }).click()

  await navigate(page, '批量任务')
  const concurrency = page.getByRole('spinbutton', { name: '并发' })
  await expect(concurrency).toBeVisible()
  await concurrency.fill('6')
  await expect(concurrency).toHaveValue('6')
  await expectNoHorizontalOverflow(page)
})

test('provider types can be created, reconfigured, and deleted', async ({ page }) => {
  await page.goto('/')
  await navigate(page, '在线模型')

  const choices = [
    { type: '自定义 API', name: 'Custom Claude Gateway', protocol: 'claude', base: 'https://gateway.example.test', model: 'claude-proxy' },
    { type: 'OpenAI 官方', name: 'Official OpenAI' },
    { type: 'Gemini 官方', name: 'Official Gemini' },
    { type: 'Claude 官方', name: 'Official Claude' },
  ]
  for (const choice of choices) {
    await page.getByRole('button', { name: '添加 Provider' }).first().click()
    const dialog = page.getByRole('dialog', { name: '添加 Provider' })
    await dialog.getByRole('button', { name: choice.type, exact: true }).click()
    await dialog.getByRole('textbox', { name: '名称' }).fill(choice.name)
    if (choice.protocol) {
      await dialog.getByRole('combobox', { name: '兼容协议' }).selectOption(choice.protocol)
      await dialog.getByRole('textbox', { name: 'Base URL' }).fill(choice.base!)
      await dialog.getByRole('textbox', { name: '主模型' }).fill(choice.model!)
    }
    const secretRequest = choice.type === '自定义 API'
      ? page.waitForRequest((request) => request.url().endsWith('/secret') && request.method() === 'POST')
      : null
    if (secretRequest) await dialog.getByRole('textbox', { name: 'API Key / 密钥池' }).fill('test-key-one\ntest-key-two')
    await dialog.getByRole('button', { name: '创建 Provider' }).click()
    if (secretRequest) expect((await secretRequest).postDataJSON()).toEqual({ keys: ['test-key-one', 'test-key-two'] })
    await expect(page.getByText(choice.name, { exact: true })).toBeVisible()
  }

  const row = page.locator('.provider-row').filter({ hasText: 'Custom Claude Gateway' })
  await row.getByRole('button', { name: '编辑配置' }).click()
  const editor = page.getByRole('dialog', { name: '编辑 Provider' })
  await editor.getByRole('combobox', { name: '连接类型' }).selectOption('openai')
  await expect(editor.getByRole('textbox', { name: 'Base URL' })).toHaveValue('https://api.openai.com/v1')
  await editor.getByRole('button', { name: '保存修改' }).click()

  page.once('dialog', (dialog) => dialog.accept())
  await row.getByRole('button', { name: '删除 Provider' }).click()
  await expect(page.getByText('Custom Claude Gateway', { exact: true })).toHaveCount(0)
  await expectNoHorizontalOverflow(page)
})

test('file selection, drop, paste, and a 1000 item queue stay usable', async ({ page }) => {
  await page.goto('/')
  const input = page.locator('input[type="file"]')
  await input.setInputFiles({
    name: 'selected.png',
    mimeType: 'image/png',
    buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64'),
  })
  await expect(page.getByText('1 张图片')).toBeVisible()

  await page.locator('.dropzone').evaluate((element) => {
    const transfer = new DataTransfer()
    transfer.items.add(new File(['drop'], 'dropped.png', { type: 'image/png', lastModified: 2 }))
    element.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: transfer }))
  })
  await page.evaluate(() => {
    const transfer = new DataTransfer()
    transfer.items.add(new File(['paste'], 'pasted.png', { type: 'image/png', lastModified: 3 }))
    window.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, clipboardData: transfer }))
  })
  await expect(page.getByText('3 张图片')).toBeVisible()

  await page.evaluate(() => {
    const transfer = new DataTransfer()
    for (let index = 0; index < 1000; index += 1) {
      transfer.items.add(new File(['x'], `bulk-${String(index).padStart(4, '0')}.png`, {
        type: 'image/png',
        lastModified: 10 + index,
      }))
    }
    window.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, clipboardData: transfer }))
  })
  await expect(page.getByText('1003 张图片')).toBeVisible()
  await expect.poll(() => page.locator('.queue-row').count()).toBeLessThan(30)
  await expectNoHorizontalOverflow(page)
})

const workbenchScenarios = [
  { slug: 'single-local', title: 'single image with one local model', localModelCount: 1, online: false },
  { slug: 'multi-local', title: 'single image with multiple local models', localModelCount: 2, online: false },
  { slug: 'single-local-online-nl', title: 'single image with one local model and online NL', localModelCount: 1, online: true },
  { slug: 'multi-local-online-nl', title: 'single image with multiple local models and online NL', localModelCount: 2, online: true },
] as const

for (const scenario of workbenchScenarios) {
  test(`workbench handles ${scenario.title}`, async ({ page }, testInfo) => {
    const jobBodies: Array<Record<string, unknown>> = []
    if (scenario.localModelCount === 2) {
      await page.route('**/api/v1/models', (route) => route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [
          { id: 'model_demo_01', name: 'WD EVA02 Large Tagger', backend: 'onnx', architecture: 'eva02_large_patch14', input_size: [448, 448], loaded: true, device: 'cuda', memory_mb: 1940, threshold: 0.35, thresholds: { default: 0.35, general: 0.35, character: 0.85, rating: 0.5 }, preset_thresholds: { default: 0.35, general: 0.35, character: 0.85, rating: 0.5 }, threshold_source: 'model', trusted_pickle: false, adapters: [], classifiers: ['aesthetic'] },
          { id: 'model_demo_02', name: 'ConvNeXt V2 Tagger', backend: 'safetensors', architecture: 'convnextv2_huge', input_size: [384, 384], loaded: true, device: 'cuda', memory_mb: 2200, threshold: 0.4, thresholds: { default: 0.4, general: 0.4, character: 0.72 }, preset_thresholds: { default: 0.4, general: 0.4, character: 0.72 }, threshold_source: 'model', trusted_pickle: false, adapters: [], classifiers: ['aesthetic'] },
        ] }),
      }))
    }
    await page.route('**/api/v1/jobs', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback()
      const body = route.request().postDataJSON() as Record<string, unknown>
      jobBodies.push(body)
      const mode = body.mode === 'online' ? 'online' : 'local'
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...jobSummary('running'), id: `${mode}-e2e`, mode }) })
    })
    await page.route('**/api/v1/jobs/*/events', (route) => {
      const jobId = route.request().url().includes('online-e2e') ? 'online-e2e' : 'local-e2e'
      const completed = {
        seq: 1, job_id: jobId, state: 'succeeded', phase: 'completed', processed: 1,
        total: 1, succeeded: 1, skipped: 0, failed: 0, current_item: 'task.png',
      }
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: `id: 1\nevent: job\ndata: ${JSON.stringify(completed)}\n\n` })
    })
    await page.route('**/api/v1/jobs/*/results', (route) => {
      const online = route.request().url().includes('online-e2e')
      const localGroups = [
        { model_id: 'model_demo_01', model_name: 'WD EVA02 Large Tagger', tags: [{ text: 'first_model_tag', category: 'general', score: 0.91, source: 'local', model_id: 'model_demo_01' }] },
        { model_id: 'model_demo_02', model_name: 'ConvNeXt V2 Tagger', tags: [{ text: 'second_model_tag', category: 'general', score: 0.82, source: 'local', model_id: 'model_demo_02' }] },
      ].slice(0, scenario.localModelCount)
      const result = online ? {
        image_id: 'image-e2e', file_name: 'task.png', status: 'succeeded', model_id: 'gemini-2.5-flash',
        tags: [], anima: null, caption: 'Online NL caption.', artifacts: [], warnings: [], timing: {},
      } : {
        image_id: 'image-e2e', file_name: 'task.png', status: 'succeeded',
        tags: localGroups.flatMap((group) => group.tags), model_results: localGroups,
        caption: null, anima: null, artifacts: [], warnings: [], timing: {},
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [result], total: 1 }) })
    })
    await page.goto('/')
    await page.locator('input[type="file"]').setInputFiles({
      name: 'task.png',
      mimeType: 'image/png',
      buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64'),
    })
    const selectedModels = scenario.localModelCount === 1
      ? ['model_demo_01']
      : ['model_demo_01', 'model_demo_02']
    if (scenario.online) {
      await page.getByRole('checkbox', { name: '启用在线模型' }).check({ force: true })
      await expect(page.getByRole('textbox', { name: 'TAG Prompt' })).toHaveCount(0)
      await expect(page.getByRole('textbox', { name: 'JSON Prompt' })).toHaveCount(0)
      await page.getByRole('textbox', { name: 'NL Prompt' }).fill('CUSTOM NL PROMPT')
    }
    const runButton = page.getByRole('button', { name: '开始打标' })
    await expect(runButton).toHaveCount(1)
    await expect(runButton).toBeVisible()
    expect(await runButton.evaluate((element) => Boolean(element.closest('.page-heading')))).toBe(true)
    await runButton.click()
    await expect.poll(() => jobBodies.length).toBe(scenario.online ? 2 : 1)

    const localBody = jobBodies.find((body) => body.mode === 'local')
    const onlineBody = jobBodies.find((body) => body.mode === 'online')
    expect(localBody).toMatchObject({
      model_ids: selectedModels,
      separate_models: true,
      output: { json: false, txt: false, include_rating: false, escape_parentheses: true },
    })
    if (scenario.online) {
      expect(onlineBody).toMatchObject({
        provider_id: 'gemini-main',
        nl_prompt: 'CUSTOM NL PROMPT',
        online_response: 'nl',
        output: { json: false, txt: false },
      })
      expect(onlineBody).not.toHaveProperty('tag_prompt')
      expect(onlineBody).not.toHaveProperty('json_prompt')
    } else {
      expect(onlineBody).toBeUndefined()
    }

    const resultPanel = page.locator('.result-panel')
    await expect(resultPanel.getByText('本地模型', { exact: true })).toBeVisible()
    await expect(resultPanel.getByText(`${scenario.localModelCount} 个结果`, { exact: true })).toBeVisible()
    await expect(resultPanel.getByText('WD EVA02 Large Tagger', { exact: true })).toBeVisible()
    const firstTag = resultPanel.locator('.tag-pill').filter({ hasText: 'first_model_tag' })
    await expect(firstTag).toBeVisible()
    expect(await firstTag.evaluate((element) => Number.parseFloat(getComputedStyle(element).fontSize))).toBeGreaterThanOrEqual(11)
    if (scenario.localModelCount === 2) {
      await expect(resultPanel.getByText('ConvNeXt V2 Tagger', { exact: true })).toBeVisible()
      await expect(resultPanel.locator('.tag-pill').filter({ hasText: 'second_model_tag' })).toBeVisible()
    } else {
      await expect(resultPanel.getByText('ConvNeXt V2 Tagger', { exact: true })).toHaveCount(0)
    }
    if (scenario.online) {
      await expect(resultPanel.getByText('在线模型', { exact: true })).toBeVisible()
      await expect(resultPanel.getByText('gemini-2.5-flash', { exact: true })).toBeVisible()
      const nlCaption = resultPanel.getByText('Online NL caption.', { exact: true })
      await expect(nlCaption).toBeVisible()
      expect(await nlCaption.evaluate((element) => Number.parseFloat(getComputedStyle(element).fontSize))).toBeGreaterThanOrEqual(13)
      await expect(resultPanel.getByText('Anima JSON', { exact: true })).toHaveCount(0)
      await expect(resultPanel.getByText('标签预览', { exact: true })).toHaveCount(0)
    } else {
      await expect(resultPanel.getByText('在线模型', { exact: true })).toHaveCount(0)
    }
    await expect(resultPanel.getByRole('button', { name: '下载 JSON' })).toHaveCount(0)
    await expect(page.getByText('任务完成：1 项成功')).toBeVisible()
    await expectNoHorizontalOverflow(page)
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({ path: path.join('test-results', 'screenshots', `${testInfo.project.name}-workbench-${scenario.slug}.png`), fullPage: true })
  })
}

test('workbench reloads the completed result for consecutive runs', async ({ page }) => {
  let run = 0
  let secondTerminalDelivered = false
  const resultRequests = new Map<string, number>()
  const groups = [
    { model_id: 'model_demo_01', model_name: 'WD EVA02 Large Tagger', tags: [{ text: 'first_run_tag', category: 'general', score: 0.91, source: 'local', model_id: 'model_demo_01' }] },
    { model_id: 'model_demo_02', model_name: 'ConvNeXt V2 Tagger', tags: [{ text: 'second_model_tag', category: 'general', score: 0.82, source: 'local', model_id: 'model_demo_02' }] },
  ]
  await page.route('**/api/v1/models', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ items: [
      { id: 'model_demo_01', name: 'WD EVA02 Large Tagger', backend: 'onnx', loaded: true, threshold: 0.35, thresholds: { default: 0.35 } },
      { id: 'model_demo_02', name: 'ConvNeXt V2 Tagger', backend: 'onnx', loaded: true, threshold: 0.35, thresholds: { default: 0.35 } },
    ] }),
  }))
  await page.route('**/api/v1/jobs', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    run += 1
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...jobSummary('running'), id: `consecutive-${run}`, mode: 'local', model_ids: groups.map((group) => group.model_id) }) })
  })
  await page.route('**/api/v1/jobs/consecutive-*/events', async (route) => {
    const jobId = new URL(route.request().url()).pathname.split('/').at(-2) as string
    if (jobId === 'consecutive-2') {
      await new Promise((resolve) => setTimeout(resolve, 150))
      secondTerminalDelivered = true
    }
    const event = { seq: 1, job_id: jobId, state: 'succeeded', phase: 'completed', processed: 1, total: 1, succeeded: 1, skipped: 0, failed: 0 }
    return route.fulfill({ status: 200, contentType: 'text/event-stream', body: `id: 1\nevent: job\ndata: ${JSON.stringify(event)}\n\n` })
  })
  await page.route('**/api/v1/jobs/consecutive-*/results', (route) => {
    const jobId = new URL(route.request().url()).pathname.split('/').at(-2) as string
    const count = (resultRequests.get(jobId) ?? 0) + 1
    resultRequests.set(jobId, count)
    // This is what the stale terminal event would observe on the second run.
    const stale = jobId === 'consecutive-2' && !secondTerminalDelivered
    const result = stale
      ? { image_id: 'image-e2e', file_name: 'task.png', status: 'running', tags: [], model_results: [], artifacts: [], warnings: [], timing: {} }
      : { image_id: 'image-e2e', file_name: 'task.png', status: 'succeeded', tags: groups.flatMap((group) => group.tags), model_results: groups, artifacts: [], warnings: [], timing: {} }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [result], total: 1 }) })
  })

  await page.goto('/')
  await page.locator('input[type="file"]').setInputFiles({
    name: 'task.png',
    mimeType: 'image/png',
    buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64'),
  })
  await page.getByRole('button', { name: '开始打标' }).click()
  const resultPanel = page.locator('.result-panel')
  await expect(resultPanel.getByText('2 个结果', { exact: true })).toBeVisible()
  await expect(resultPanel.locator('.tag-pill').filter({ hasText: 'first_run_tag' })).toBeVisible()

  await page.getByRole('button', { name: '再次运行队列' }).click()
  await expect(resultPanel.locator('.tag-pill').filter({ hasText: 'second_model_tag' })).toBeVisible({ timeout: 30_000 })
  await expect(resultPanel.getByText('2 个结果', { exact: true })).toBeVisible()
  expect(resultRequests.get('consecutive-2')).toBe(1)
})

test('workbench preserves local results when the online channel cannot start', async ({ page }) => {
  await page.route('**/api/v1/jobs', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    const body = route.request().postDataJSON() as Record<string, unknown>
    if (body.mode === 'online') {
      return route.fulfill({
        status: 502,
        contentType: 'application/json',
        body: JSON.stringify({ code: 'provider_unavailable', message: 'Provider unavailable', request_id: 'e2e', retryable: true }),
      })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...jobSummary('running'), id: 'local-partial-e2e', mode: 'local' }) })
  })
  await page.route('**/api/v1/jobs/local-partial-e2e/events', (route) => {
    const event = { seq: 1, job_id: 'local-partial-e2e', state: 'succeeded', phase: 'completed', processed: 1, total: 1, succeeded: 1, skipped: 0, failed: 0, current_item: 'task.png' }
    return route.fulfill({ status: 200, contentType: 'text/event-stream', body: `id: 1\nevent: job\ndata: ${JSON.stringify(event)}\n\n` })
  })
  await page.route('**/api/v1/jobs/local-partial-e2e/results', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ items: [{
      image_id: 'image-e2e', file_name: 'task.png', status: 'succeeded',
      tags: [{ text: 'local_survived', category: 'general', score: 0.9, source: 'local', model_id: 'model_demo_01' }],
      model_results: [{ model_id: 'model_demo_01', model_name: 'WD EVA02 Large Tagger', tags: [{ text: 'local_survived', category: 'general', score: 0.9, source: 'local', model_id: 'model_demo_01' }] }],
      caption: null, anima: null, artifacts: [], warnings: [], timing: {},
    }], total: 1 }),
  }))

  await page.goto('/')
  await page.locator('input[type="file"]').setInputFiles({
    name: 'task.png', mimeType: 'image/png',
    buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64'),
  })
  await page.getByRole('checkbox', { name: '启用在线模型' }).check({ force: true })
  await page.getByRole('button', { name: '开始打标' }).click()

  await expect(page.locator('.result-panel').locator('.tag-pill').filter({ hasText: 'local_survived' })).toBeVisible()
  await expect(page.getByText('任务完成：0 项成功，1 项存在失败通道；在线任务创建失败')).toBeVisible()
  await expect(page.locator('.result-file-line').getByText('失败', { exact: true })).toBeVisible()
  await expectNoHorizontalOverflow(page)
})

test('workbench retry reconnects the stream and replaces failed results', async ({ page }) => {
  let eventRequests = 0
  let resultRequests = 0
  await page.route('**/api/v1/jobs', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...jobSummary('running'), id: 'local-retry-e2e', mode: 'local' }) })
  })
  await page.route('**/api/v1/jobs/local-retry-e2e/events', (route) => {
    eventRequests += 1
    const succeeded = eventRequests > 1
    const event = {
      seq: eventRequests, job_id: 'local-retry-e2e', state: succeeded ? 'succeeded' : 'failed', phase: succeeded ? 'completed' : 'failed',
      processed: 1, total: 1, succeeded: succeeded ? 1 : 0, skipped: 0, failed: succeeded ? 0 : 1, current_item: 'task.png',
    }
    return route.fulfill({ status: 200, contentType: 'text/event-stream', body: `id: ${eventRequests}\nevent: job\ndata: ${JSON.stringify(event)}\n\n` })
  })
  await page.route('**/api/v1/jobs/local-retry-e2e/results', (route) => {
    resultRequests += 1
    const succeeded = resultRequests > 1
    const result = {
      image_id: 'image-e2e', file_name: 'task.png', status: succeeded ? 'succeeded' : 'failed',
      tags: succeeded ? [{ text: 'recovered_tag', category: 'general', score: 0.9, source: 'local', model_id: 'model_demo_01' }] : [],
      model_results: succeeded ? [{ model_id: 'model_demo_01', model_name: 'WD EVA02 Large Tagger', tags: [{ text: 'recovered_tag', category: 'general', score: 0.9, source: 'local', model_id: 'model_demo_01' }] }] : [],
      caption: null, anima: null, artifacts: [], warnings: [], timing: {}, error: succeeded ? null : 'initial failure',
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [result], total: 1 }) })
  })
  await page.route('**/api/v1/jobs/local-retry-e2e/retry-failed', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({ ...jobSummary('running'), id: 'local-retry-e2e', mode: 'local' }),
  }))

  await page.goto('/')
  await page.locator('input[type="file"]').setInputFiles({
    name: 'task.png', mimeType: 'image/png',
    buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64'),
  })
  await page.getByRole('button', { name: '开始打标' }).click()
  const retry = page.getByRole('button', { name: '重试失败项' })
  await expect(retry).toBeVisible()
  await retry.click()

  await expect(page.locator('.result-panel').locator('.tag-pill').filter({ hasText: 'recovered_tag' })).toBeVisible()
  await expect(page.getByText('任务完成：1 项成功')).toBeVisible()
  await expect.poll(() => eventRequests).toBe(2)
  await expect.poll(() => resultRequests).toBe(2)
  await expectNoHorizontalOverflow(page)
})

test('job stream reconnects with Last-Event-ID and supports cancel and retry', async ({ page }) => {
  await page.goto('/')
  await page.locator('input[type="file"]').setInputFiles({
    name: 'task.png',
    mimeType: 'image/png',
    buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64'),
  })
  await page.getByRole('button', { name: '开始打标' }).click()

  const cancelRequest = page.waitForRequest((request) => request.url().endsWith('/jobs/job-e2e/cancel'))
  const cancel = page.getByRole('button', { name: '取消' })
  await expect(cancel).toBeVisible()
  await cancel.click()
  await cancelRequest

  await expect(page.getByText('连接中断，正在重连')).toBeVisible()
  const retry = page.getByRole('button', { name: '重试失败项' })
  await expect(retry).toBeVisible({ timeout: 8_000 })
  const retryRequest = page.waitForRequest((request) => request.url().endsWith('/jobs/job-e2e/retry-failed'))
  await retry.click()
  await retryRequest
  await expectNoHorizontalOverflow(page)
})

async function navigate(page: Page, name: typeof pages[number]) {
  await page.locator('.page h1').waitFor({ state: 'visible' })
  if ((await page.getByRole('heading', { name, level: 1 }).count()) > 0) return
  const desktopLink = page.locator('.sidebar').getByRole('button', { name })
  if ((page.viewportSize()?.width ?? 1440) <= 980 && !(await page.locator('.sidebar').evaluate((element) => element.classList.contains('sidebar-open')))) {
    await page.getByRole('button', { name: '打开导航' }).click()
  }
  await desktopLink.click()
}

async function expectNoHorizontalOverflow(page: Page) {
  const result = await page.evaluate(() => {
    const root = document.documentElement
    const offenders = [...document.querySelectorAll<HTMLElement>('body *')]
      .filter((element) => {
        if (element.closest('.sidebar:not(.sidebar-open)')) return false
        const rect = element.getBoundingClientRect()
        const style = getComputedStyle(element)
        return style.position !== 'fixed' && rect.width > 0 && (rect.right > root.clientWidth + 1 || rect.left < -1)
      })
      .slice(0, 8)
      .map((element) => `${element.tagName.toLowerCase()}.${element.className}`)
    return { clientWidth: root.clientWidth, scrollWidth: root.scrollWidth, offenders }
  })
  expect(result.scrollWidth, `horizontal overflow: ${result.offenders.join(', ')}`).toBeLessThanOrEqual(result.clientWidth + 1)
  expect(result.offenders, `elements outside viewport: ${result.offenders.join(', ')}`).toEqual([])
}

async function mockApi(page: Page) {
  let eventRequests = 0
  let providerItems = [
    { id: 'gemini-main', name: 'Gemini', kind: 'gemini', protocol: 'gemini', base_url: 'https://generativelanguage.googleapis.com', primary_model: 'gemini-2.5-flash', fallback_model: 'gemini-2.5-pro', temperature: 0.2, top_p: 0.9, top_k: 40, max_tokens: 4096, timeout_seconds: 90, concurrency: 3, retries: 2, prompt_profile: 'anima-v1', configured: true, key_hint: '•••• 7K2A', enabled: true },
    { id: 'studio', name: 'LM Studio', kind: 'lmstudio', protocol: 'openai', base_url: 'http://127.0.0.1:1234/v1', primary_model: 'local-vision', temperature: 0.1, top_p: 0.9, max_tokens: 2048, timeout_seconds: 60, concurrency: 1, retries: 1, configured: false, enabled: true },
  ]
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    const pathname = url.pathname
    const json = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (pathname.endsWith('/health')) return json({ status: 'ok', version: '2.0.0' })
    if (pathname.endsWith('/prompts/defaults')) return json({
      tag_prompt: 'Generate comprehensive booru-style tags.',
      nl_prompt: 'Write a dense natural-language caption.',
      json_prompt: 'Return the strict Anima JSON schema with tags and nl.',
    })
    if (pathname.endsWith('/roots')) {
      if (route.request().method() === 'POST') {
        const body = route.request().postDataJSON()
        return json({ id: 'direct-input', name: body.name, kind: body.kind, path_hint: '已授权', writable: false })
      }
      return json({ items: [
        { id: 'input', name: '训练图片', kind: 'input', path_hint: 'D:\\datasets', writable: false },
        { id: 'output', name: '标注输出', kind: 'output', path_hint: 'D:\\outputs', writable: true },
      ] })
    }
    if (/\/providers\/[^/]+\/models$/.test(pathname)) return json({ items: [
      { id: 'gemini-2.5-flash', name: 'Gemini 2.5 Flash' },
      { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro' },
    ] })
    if (pathname.endsWith('/providers/discover-models') && route.request().method() === 'POST') return json({ items: [
      { id: 'gateway-vision', name: 'Gateway Vision' },
      { id: 'gateway-caption', name: 'Gateway Caption' },
    ] })
    const providerSecretMatch = pathname.match(/\/providers\/([^/]+)\/secret$/)
    if (providerSecretMatch) {
      const id = decodeURIComponent(providerSecretMatch[1])
      providerItems = providerItems.map((item) => item.id === id ? { ...item, configured: true, key_hint: '•••• -two' } : item)
      return json({ configured: true, key_hint: '•••• -two' })
    }
    if (/\/models\/downloads\/[^/]+$/.test(pathname)) return json({
      id: 'download-e2e', repo_id: 'owner/tagger', revision: 'main', status: 'succeeded', phase: 'completed',
      model_ids: ['model-downloaded'], loaded_model_ids: ['model-downloaded'], load_errors: [], error: null, created_at: new Date(0).toISOString(), updated_at: new Date().toISOString(),
    })
    if (pathname.endsWith('/models/downloads')) return route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify({
      id: 'download-e2e', repo_id: 'owner/tagger', revision: 'main', status: 'queued', phase: 'queued',
      model_ids: [], loaded_model_ids: [], load_errors: [], error: null, created_at: new Date(0).toISOString(), updated_at: new Date(0).toISOString(),
    }) })
    if (pathname.endsWith('/models')) return json({ items: [
      { id: 'model_demo_01', name: 'WD EVA02 Large Tagger', backend: 'onnx', architecture: 'eva02_large_patch14', input_size: [448, 448], loaded: true, device: 'cuda', memory_mb: 1940, threshold: 0.35, thresholds: { default: 0.35, general: 0.35, character: 0.85, rating: 0.5 }, preset_thresholds: { default: 0.35, general: 0.35, character: 0.85, rating: 0.5 }, threshold_source: 'model', trusted_pickle: false, adapters: [{ id: 'lora', name: 'LoRA', type: 'lora', enabled: false, weight: 1 }], classifiers: ['aesthetic'] },
      { id: 'model_demo_02', name: 'ConvNeXt V2 Tagger', backend: 'safetensors', architecture: 'convnextv2_huge', input_size: [384, 384], loaded: false, threshold: 0.4, thresholds: { default: 0.4, general: 0.4, character: 0.72 }, preset_thresholds: { default: 0.4, general: 0.4, character: 0.72 }, threshold_source: 'model', trusted_pickle: false, adapters: [], classifiers: ['aesthetic'] },
    ] })
    if (pathname.endsWith('/classifiers')) return json({ items: [
      { id: 'aesthetic', enabled: true, loaded: true, error: null },
    ] })
    if (pathname.includes('/classifiers/')) return json({ id: 'aesthetic', enabled: true, loaded: pathname.endsWith('/load'), error: null })
    if (pathname.endsWith('/providers')) {
      if (route.request().method() === 'GET') return json({ items: providerItems })
      const body = route.request().postDataJSON()
      const created = { ...body, id: `provider-${providerItems.length}`, configured: false, key_hint: null }
      providerItems = [...providerItems, created]
      return json(created)
    }
    const providerMatch = pathname.match(/\/providers\/([^/]+)$/)
    if (providerMatch) {
      const id = decodeURIComponent(providerMatch[1])
      if (route.request().method() === 'DELETE') {
        providerItems = providerItems.filter((item) => item.id !== id)
        return route.fulfill({ status: 204, body: '' })
      }
      const body = route.request().postDataJSON()
      const current = providerItems.find((item) => item.id === id)
      const updated = { ...current, ...body, id }
      providerItems = providerItems.map((item) => item.id === id ? updated : item)
      return json(updated)
    }
    if (pathname.endsWith('/uploads')) return json({ upload_id: 'upload-e2e', files: [{ id: 'image-e2e', name: 'task.png', size: 68 }] })
    if (pathname.endsWith('/jobs') && route.request().method() === 'POST') return json(jobSummary('running'))
    if (pathname.endsWith('/jobs') && route.request().method() === 'GET') return json({ items: [], total: 0 })
    if (pathname.endsWith('/jobs/job-e2e/events')) {
      eventRequests += 1
      const reconnectId = route.request().headers()['last-event-id']
      if (eventRequests > 1 && reconnectId !== '1') {
        return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ code: 'missing_last_event_id', message: 'Last-Event-ID missing' }) })
      }
      const state = eventRequests === 1 ? 'running' : 'failed'
      const event = { seq: eventRequests, job_id: 'job-e2e', state, phase: state, processed: state === 'failed' ? 1 : 0, total: 1, succeeded: 0, skipped: 0, failed: state === 'failed' ? 1 : 0, current_item: 'task.png', error: state === 'failed' ? 'mock failure' : undefined }
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: `id: ${eventRequests}\nevent: job\ndata: ${JSON.stringify(event)}\n\n` })
    }
    if (pathname.endsWith('/jobs/job-e2e/results')) return json({ items: [{ image_id: 'image-e2e', file_name: 'task.png', status: 'failed', tags: [], artifacts: [], warnings: [], timing: {}, error: 'mock failure' }], total: 1 })
    if (pathname.endsWith('/jobs/job-e2e/cancel')) return json(jobSummary('cancelling'))
    if (pathname.endsWith('/jobs/job-e2e/retry-failed')) return json(jobSummary('running'))
    if (pathname.endsWith('/settings')) return json({ input_root_id: 'input', output_root_id: 'output', default_mode: 'online', default_threshold: 0.35, default_json: true, default_txt: false, bind_host: '127.0.0.1', lan_enabled: false, access_token_configured: false, production: true, max_upload_mb: 32, max_image_pixels: 80000000 })
    return json({ items: [] })
  })
}

function jobSummary(state: string) {
  return {
    id: 'job-e2e', mode: 'online', state, phase: state, processed: 0, total: 1,
    succeeded: 0, skipped: 0, failed: 0, created_at: new Date(0).toISOString(),
  }
}

function routeSlug(name: typeof pages[number]) {
  return ({ 工作台: 'workbench', 视频提示词: 'video-prompts', 批量任务: 'batch', 在线模型: 'providers', 本地模型: 'models', 设置: 'settings' })[name]
}

function fl2vaPromptPackage() {
  return {
    change_summary_zh: '已生成 FL2VA 首尾状态之间的连续运动路径。',
    base_mode: 'fl2va',
    reference_alignment: {
      zh: '参考图 1 对齐目标视频 0.00 秒的起始状态；参考图 2 对齐目标视频 5.00 秒的结束状态。',
      en: 'How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 2) aligns with the 5.00-second mark of the target video.',
    },
    integrated_multimodal_description: {
      zh: '[Shot 1] 从参考图中的起始构图开始，主体连续完成动作并逐步接近结束状态。\n[Shot 2] At 00:03.500, 动作收束到参考图 2 的稳定结束状态。',
      en: '[Shot 1] Live-action, cinematic, the visible subject begins in Picture 1 and moves continuously while preserving identity, clothing, props, lighting, and composition.\n[Shot 2] At 00:03.500, the action settles into the stable ending composition established by Picture 2.',
    },
    overall_soundscape: { zh: '安静环境声持续，动作带来轻微的衣料声。', en: 'Quiet ambience continues beneath subtle fabric movement.' },
    non_diegetic_music: { zh: '无非叙事音乐。', en: 'N/A' },
    assumptions_zh: ['未提供第二张参考图，末帧按用户要求推导。'],
  }
}

function videoPromptPackage() {
  return {
    subject_definitions: [{
      subject_number: 1,
      picture_number: 1,
      zh: 'reference image main subject and visible styling',
      en: 'the reference image main subject and visible styling',
    }],
    summary: {
      zh: '[reference generation] Create one connected reference-based video.',
      en: '[reference generation] Create one connected reference-based video.',
    },
    retention_analysis: [{
      subject_number: 1,
      shot_number: 1,
      visual_retention: 'fully_preserved',
      zh: 'Keep the visible appearance and composition stable.',
      en: 'Keep the visible appearance and composition stable.',
    }],
    detailed_description: {
      overview: {
        zh: 'a single continuous video with a stable final frame',
        en: 'a single continuous video with a stable final frame',
      },
      shots: [
        { shot_number: 1, cut_time_seconds: null, zh: 'Start in the reference composition.', en: 'Start in the reference composition.' },
        { shot_number: 2, cut_time_seconds: 3.5, zh: 'Settle on the final frame.', en: 'Settle on the final frame.' },
      ],
    },
    overall_soundscape: { zh: 'Quiet room tone.', en: 'Quiet room tone.' },
    non_diegetic_music: { zh: 'Subtle ambient score.', en: 'Subtle ambient score.' },
    change_summary_zh: '已生成稳定的缓慢推进镜头。',
    assumptions_zh: [],
  }
}
