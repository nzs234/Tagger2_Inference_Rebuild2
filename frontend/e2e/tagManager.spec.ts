import { expect, test, type Page } from '@playwright/test'
import type { TagManagerImageDetail, TagManagerImageSummary, TagManagerSession } from '../src/lib/tagManager'

// ---------------------------------------------------------------------------
// Tag Manager e2e. The long interaction chains are desktop-only (the batch
// bar and the editor drawer cover the grid at the 375px mobile viewport,
// which makes long chains flaky there); the mobile project additionally runs
// a compact select/edit/save smoke and the filtered batch payload check, so
// the mobile build is actually exercised instead of silently skipped.
//
// All API traffic is mocked through a single `page.route('**/api/v1/**')`
// handler (same pattern as e2e/app.spec.ts). Network assertions use the
// record-into-array pattern: route handlers push request payloads into
// `recorded`, tests poll the arrays with `expect.poll`.
// ---------------------------------------------------------------------------

// --- Mock data: same shapes as the vitest harness (tests/tagManager.test.tsx) ---

const session: TagManagerSession = {
  id: 'ds-1',
  name: 'cats',
  root_id: 'in',
  relative_path: 'cats',
  profile: 'e621',
  recursive: true,
  status: 'ready',
  error: null,
  image_count: 3,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
}

function summary(id: number, fileName: string, sidecarKind: TagManagerImageSummary['sidecar_kind'], tagCount: number): TagManagerImageSummary {
  return {
    id,
    relative_path: fileName,
    file_name: fileName,
    image_format: 'png',
    sidecar_kind: sidecarKind,
    mtime: 1_000 + id,
    width: 64,
    height: 64,
    tag_count: tagCount,
    tags: [],
  }
}

const imageItems: TagManagerImageSummary[] = [
  summary(1, 'a.png', 'tag_txt', 2),
  summary(2, 'b.png', 'none', 0),
  summary(3, 'c.png', 'tags_json', 4),
]

const SIDECAR_MTIME = 1_725_148_800

// Per-image sidecar content behind GET .../images/{id}: id 1 is a tag_txt
// sidecar (the main-chain editor target), id 2 has no sidecar yet, id 3 is a
// tags_json sidecar.
const imageContents: Record<number, TagManagerImageDetail['content']> = {
  1: { kind: 'tag_txt', tags: ['solo', 'long_hair'] },
  2: { kind: 'none' },
  3: { kind: 'tags_json', tags: [{ text: 'solo', category: 'general', score: 0.92 }, { text: 'long_hair', category: 'general' }] },
}

function detailFor(imageId: number): TagManagerImageDetail {
  const base = imageItems.find((item) => item.id === imageId) ?? imageItems[0]
  return { ...base, content: imageContents[imageId] ?? { kind: 'none' }, sidecar_mtime: SIDECAR_MTIME }
}

const tagDbEntries = [
  { name: '1girl', category: 'general', post_count: 4_000_000, alias_of: null },
  { name: 'long_hair', category: 'general', post_count: 1_200_000, alias_of: null },
  { name: 'hakurei_reimu', category: 'character', post_count: 90_000, alias_of: null },
]

// 1x1 PNG served for every thumbnail request. Thumbnails are fetched as blobs
// through the authorized client, so the route must fulfill with a binary body.
const PNG_1PX = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9JvFIAAAAASUVORK5CYII=',
  'base64',
)

// --- Mock plumbing ---

interface RecordedApi {
  /** Query strings of every GET .../images request, e.g. "?offset=0&limit=60&include_tags=1girl". */
  imageQueries: string[]
  /** `query` parameter of every tag-db autocomplete lookup. */
  tagDbQueries: string[]
  batchBodies: Array<Record<string, unknown>>
  patchBodies: Array<Record<string, unknown>>
  undoCalls: number
  redoCalls: number
  thumbnailIds: number[]
}

async function mockTagManagerApi(page: Page): Promise<RecordedApi> {
  const recorded: RecordedApi = {
    imageQueries: [],
    tagDbQueries: [],
    batchBodies: [],
    patchBodies: [],
    undoCalls: 0,
    redoCalls: 0,
    thumbnailIds: [],
  }
  // Per-image sidecar mtime: every successful PATCH rewrites the sidecar and
  // the next GET reports the fresh value, mirroring the real backend.
  const mtimes: Record<number, number> = {}
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const pathname = url.pathname
    const method = request.method()
    const json = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

    // Landing page (workbench) and app-shell background queries.
    if (pathname.endsWith('/health')) return json({ status: 'ok', version: '2.0.0' })
    if (pathname.endsWith('/providers') || pathname.endsWith('/models') || pathname.endsWith('/classifiers')) {
      return json({ items: [] })
    }
    if (pathname.endsWith('/roots')) {
      return json({ items: [{ id: 'in', name: '训练图片', kind: 'input', path_hint: 'D:\\datasets', writable: true }] })
    }

    // Tag database: autocomplete lookups plus the display-bar info query.
    if (pathname.endsWith('/tag-manager/tag-db/info')) {
      return json({ available: { e621: ['e621'], danbooru: ['danbooru'] }, loaded: { e621: true, danbooru: true } })
    }
    if (pathname.endsWith('/tag-manager/tag-db')) {
      recorded.tagDbQueries.push(url.searchParams.get('query') ?? '')
      const query = (url.searchParams.get('query') ?? '').toLowerCase()
      return json({ profile: 'e621', items: tagDbEntries.filter((entry) => entry.name.includes(query)) })
    }

    // Session-scoped tag-manager endpoints.
    if (/\/tag-manager\/datasets\/ds-1\/batch$/.test(pathname) && method === 'POST') {
      recorded.batchBodies.push(request.postDataJSON() as Record<string, unknown>)
      return json({ affected: 2, journal_id: 7 })
    }
    if (/\/tag-manager\/datasets\/ds-1\/undo$/.test(pathname) && method === 'POST') {
      recorded.undoCalls += 1
      return json({ journal_id: 8 })
    }
    if (/\/tag-manager\/datasets\/ds-1\/redo$/.test(pathname) && method === 'POST') {
      recorded.redoCalls += 1
      return json({ journal_id: 9 })
    }
    if (/\/tag-manager\/datasets\/ds-1\/refresh$/.test(pathname) && method === 'POST') return json(session)
    if (/\/tag-manager\/datasets\/ds-1\/tags\/stats$/.test(pathname)) {
      return json({ items: [{ tag: 'solo', category: 'general', count: 3 }] })
    }
    if (/\/tag-manager\/datasets\/ds-1$/.test(pathname)) return json(session)
    if (/\/tag-manager\/datasets$/.test(pathname)) return json({ items: [session] })

    // Thumbnails must be matched before the bare image detail route.
    const thumbnailMatch = /\/tag-manager\/datasets\/ds-1\/images\/(\d+)\/thumbnail$/.exec(pathname)
    if (thumbnailMatch) {
      recorded.thumbnailIds.push(Number(thumbnailMatch[1]))
      return route.fulfill({ status: 200, contentType: 'image/jpeg', body: PNG_1PX })
    }
    const detailMatch = /\/tag-manager\/datasets\/ds-1\/images\/(\d+)$/.exec(pathname)
    if (detailMatch) {
      const imageId = Number(detailMatch[1])
      if (method === 'PATCH') {
        recorded.patchBodies.push(request.postDataJSON() as Record<string, unknown>)
        mtimes[imageId] = (mtimes[imageId] ?? SIDECAR_MTIME) + 5
        return json({ image_id: imageId, journal_id: 100 + imageId, sidecar_kind: 'tag_txt', sidecar_mtime: mtimes[imageId] })
      }
      return json({ ...detailFor(imageId), sidecar_mtime: mtimes[imageId] ?? SIDECAR_MTIME })
    }
    if (/\/tag-manager\/datasets\/ds-1\/images$/.test(pathname)) {
      recorded.imageQueries.push(url.search)
      return json({ items: imageItems, total: imageItems.length })
    }

    return json({ items: [] })
  })
  return recorded
}

// --- Helpers ---

async function openTagManager(page: Page) {
  await page.goto('/')
  await page.locator('.page h1').waitFor({ state: 'visible' })
  // At the mobile viewport the sidebar is off-canvas: open it first (same
  // pattern as e2e/app.spec.ts navigate()).
  const width = page.viewportSize()?.width ?? 1440
  const sidebarOpen = await page.locator('.sidebar').evaluate((element) => element.classList.contains('sidebar-open'))
  if (width <= 980 && !sidebarOpen) {
    await page.getByRole('button', { name: '打开导航' }).click()
  }
  await page.locator('.sidebar').getByRole('button', { name: '标签管理' }).click()
  await expect(page.getByRole('heading', { name: '标签管理', level: 1 })).toBeVisible()
}

function cardBody(page: Page, fileName: string) {
  return page.locator(`.tm-card-body[title="${fileName}"]`)
}

test.beforeEach(async ({ page }) => {
  // Fresh view state: drop the persisted active session, filter and preferences.
  await page.addInitScript(() => localStorage.clear())
})

/** Long interaction chains stay desktop-only; see the file header. */
function requireDesktop() {
  test.skip(test.info().project.name !== 'desktop', 'long interaction chain: desktop project only')
}

test('main chain: filter, select, batch replace, edit, save-and-next, undo', async ({ page }) => {
  requireDesktop()
  const recorded = await mockTagManagerApi(page)
  await openTagManager(page)

  // Grid renders the three mocked images and their blob thumbnails decode.
  await expect(page.locator('img.tm-thumb[alt="a.png"]')).toBeVisible()
  await expect(page.locator('img.tm-thumb[alt="b.png"]')).toBeVisible()
  await expect(page.locator('img.tm-thumb[alt="c.png"]')).toBeVisible()
  await expect(page.locator('.tm-badge-missing')).toContainText('无 sidecar')

  // --- Include-tag filter through the debounced autocomplete ---
  const includeInput = page.getByRole('combobox', { name: '包含标签' })
  await includeInput.fill('1g')
  await expect(page.getByRole('option', { name: /1girl/ })).toBeVisible()
  await includeInput.press('Enter')
  await expect(page.getByRole('button', { name: '移除筛选 1girl' })).toBeVisible()
  await expect.poll(() => recorded.imageQueries.at(-1)).toContain('include_tags=1girl')

  // Removing the chip restores the unfiltered view. The app's QueryClient uses
  // a 15s staleTime and the empty-filter key is structurally identical to the
  // initial one, so no request fires until the key changes: flip the sort to
  // force one and prove on the wire that include_tags is gone.
  await page.getByRole('button', { name: '移除筛选 1girl' }).click()
  await expect(page.getByRole('button', { name: '移除筛选 1girl' })).toHaveCount(0)
  await page.getByRole('combobox', { name: '排序' }).selectOption('mtime')
  await expect.poll(() => recorded.imageQueries.at(-1)).toContain('sort=mtime')
  expect(recorded.imageQueries.at(-1)).not.toContain('include_tags')

  // --- A single card click toggles the selection, a second click reverts it ---
  await cardBody(page, 'a.png').click()
  await expect(page.locator('.heading-stats')).toContainText('1 已选')
  await cardBody(page, 'a.png').click()
  await expect(page.locator('.heading-stats')).toContainText('0 已选')

  // --- Checkbox selection feeds the batch bar's selected scope ---
  await page.getByRole('checkbox', { name: '选择 a.png' }).check()
  await page.getByRole('checkbox', { name: '选择 b.png' }).check()
  await expect(page.getByRole('button', { name: '选中图片（2）' })).toBeVisible()

  await page.getByRole('combobox', { name: '批量操作类型' }).selectOption('replace')
  const batchTagInput = page.getByRole('combobox', { name: '批量标签' })
  await batchTagInput.fill('1g')
  await expect(page.getByRole('option', { name: /1girl/ })).toBeVisible()
  await batchTagInput.press('Enter')
  await expect(page.getByRole('button', { name: '移除 1girl' })).toBeVisible()
  await page.getByRole('textbox', { name: '替换为' }).fill('dog')
  // The toggle skins the native input (opacity 0), so force the check like
  // e2e/app.spec.ts does for the workbench toggles.
  await page.getByRole('checkbox', { name: '使用正则表达式' }).check({ force: true })

  await page.getByRole('button', { name: '执行', exact: true }).click()
  const confirm = page.getByRole('alertdialog', { name: '对 2 张图片执行「替换」？' })
  await expect(confirm).toBeVisible()
  await expect(confirm).toContainText('选中的 2 张图片')
  await confirm.getByRole('button', { name: '确认执行' }).click()

  await expect.poll(() => recorded.batchBodies).toHaveLength(1)
  expect(recorded.batchBodies[0]).toEqual({
    op: 'replace',
    tags: ['1girl'],
    replacement: 'dog',
    use_regex: true,
    image_ids: [1, 2],
  })
  await expect(page.getByText('批量操作完成，影响 2 张图片')).toBeVisible()

  // The confirm dialog closes itself on confirmation (progress shows on the
  // 执行 button and in the notice queue), so no stale scope count lingers.
  await expect(page.getByRole('alertdialog')).toHaveCount(0)

  // --- Editor: double click opens the drawer, pill removal + save hits PATCH ---
  await cardBody(page, 'a.png').dblclick()
  const editor = page.getByRole('dialog', { name: 'a.png' })
  await expect(editor).toBeVisible()
  await editor.getByRole('button', { name: '移除 solo' }).click()
  await expect(editor.getByRole('button', { name: '移除 solo' })).toHaveCount(0)
  await editor.getByRole('button', { name: '保存', exact: true }).click()

  await expect.poll(() => recorded.patchBodies).toHaveLength(1)
  expect(recorded.patchBodies[0]).toMatchObject({
    content: { kind: 'tag_txt', tags: ['long_hair'] },
    expected_sidecar_mtime: SIDECAR_MTIME,
  })
  // The removed pill must not survive into the written sidecar.
  const patchTags = (recorded.patchBodies[0].content as { tags: string[] }).tags
  expect(patchTags).not.toContain('solo')
  await expect(page.getByText('标签已保存', { exact: true })).toBeVisible()

  // --- Save and move to the next image in the current page ---
  await editor.getByRole('button', { name: '保存并下一张' }).click()
  await expect(page.getByRole('dialog', { name: 'b.png' })).toBeVisible()
  await expect.poll(() => recorded.patchBodies).toHaveLength(2)

  // --- Clean draft closes straight through; undo posts to the journal ---
  await page.getByRole('dialog', { name: 'b.png' })
    .locator('.tm-drawer-footer')
    .getByRole('button', { name: '关闭' })
    .click()
  await expect(page.getByRole('dialog', { name: 'b.png' })).toHaveCount(0)

  await page.getByRole('button', { name: '撤销' }).click()
  await expect.poll(() => recorded.undoCalls).toBe(1)
  await expect(page.getByText('已撤销上一次操作')).toBeVisible()
})

test('editor warns before closing with unsaved changes and cancel keeps the draft', async ({ page }) => {
  requireDesktop()
  const recorded = await mockTagManagerApi(page)
  await openTagManager(page)
  await expect(page.locator('img.tm-thumb[alt="a.png"]')).toBeVisible()

  await cardBody(page, 'a.png').dblclick()
  const editor = page.getByRole('dialog', { name: 'a.png' })
  await expect(editor).toBeVisible()

  // Dirty the draft, then try to close: the unsaved-changes guard steps in.
  await editor.getByRole('button', { name: '移除 solo' }).click()
  await editor.locator('.tm-drawer-footer').getByRole('button', { name: '关闭' }).click()

  const confirm = page.getByRole('alertdialog')
  await expect(confirm).toBeVisible()
  await expect(confirm).toContainText('未保存')
  expect(recorded.patchBodies).toHaveLength(0)

  // Cancel keeps the editor open with the draft intact.
  await confirm.getByRole('button', { name: '取消' }).click()
  await expect(confirm).toHaveCount(0)
  await expect(page.getByRole('dialog', { name: 'a.png' })).toBeVisible()
  await expect(editor.getByRole('button', { name: '移除 long_hair' })).toBeVisible()
  await expect(editor.getByRole('button', { name: '移除 solo' })).toHaveCount(0)
})

test('grid keyboard: arrows move focus, space selects, enter opens the editor', async ({ page }) => {
  requireDesktop()
  await mockTagManagerApi(page)
  await openTagManager(page)
  await expect(page.locator('img.tm-thumb[alt="a.png"]')).toBeVisible()

  // Clicking the card body focuses the roving tab stop (and selects the card).
  await cardBody(page, 'a.png').click()
  await page.keyboard.press('ArrowRight')
  await expect(cardBody(page, 'b.png')).toBeFocused()

  // Space toggles the focused card's selection without opening the editor.
  await page.keyboard.press('Space')
  await expect(page.getByRole('checkbox', { name: '选择 b.png' })).toBeChecked()
  await expect(page.getByRole('dialog')).toHaveCount(0)

  // Enter opens the editor for the focused card.
  await page.keyboard.press('Enter')
  await expect(page.getByRole('dialog', { name: 'b.png' })).toBeVisible()
})

test('sidecar-less image creates a tag_txt sidecar and saves it', async ({ page }) => {
  requireDesktop()
  const recorded = await mockTagManagerApi(page)
  await openTagManager(page)
  await expect(page.locator('img.tm-thumb[alt="b.png"]')).toBeVisible()

  await cardBody(page, 'b.png').dblclick()
  const editor = page.getByRole('dialog', { name: 'b.png' })
  await expect(editor).toBeVisible()
  await expect(editor.getByText('暂无 sidecar')).toBeVisible()

  // tag_txt is the preselected format; materialise an empty editable draft.
  await editor.getByRole('radio', { name: /tag_txt/ }).check()
  await editor.getByRole('button', { name: '创建', exact: true }).click()

  const addInput = editor.getByRole('combobox', { name: '添加标签' })
  await addInput.fill('hakurei')
  const suggestion = page.getByRole('option', { name: /hakurei_reimu/ })
  await expect(suggestion).toBeVisible()
  await suggestion.getByRole('button').click()
  await expect(editor.getByRole('button', { name: '移除 hakurei_reimu' })).toBeVisible()

  await editor.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => recorded.patchBodies).toHaveLength(1)
  expect(recorded.patchBodies[0]).toEqual({
    content: { kind: 'tag_txt', tags: ['hakurei_reimu'] },
    expected_sidecar_mtime: SIDECAR_MTIME,
  })
})

// Runs on BOTH projects: this compact chain is the mobile project's proof of
// life (selection, drawer editing and saving at the 375px viewport), and on
// desktop it doubles as a cross-viewport sanity check. It uses the card body
// as the interaction target: at the mobile viewport the sticky topbar covers
// the card's top edge, where the small corner checkbox lives.
test('mobile smoke: select, edit, save (runs on desktop and mobile)', async ({ page }) => {
  const recorded = await mockTagManagerApi(page)
  await openTagManager(page)
  const smokeCard = cardBody(page, 'a.png')
  await smokeCard.scrollIntoViewIfNeeded()
  await expect(page.locator('img.tm-thumb[alt="a.png"]')).toBeVisible()

  // --- Selection: a plain card click toggles it ---
  await smokeCard.click()
  await expect(page.locator('.heading-stats')).toContainText('1 已选')

  // --- Edit in the drawer: double click opens it at any viewport ---
  await smokeCard.dblclick()
  const editor = page.getByRole('dialog', { name: 'a.png' })
  await expect(editor).toBeVisible()
  await editor.getByRole('button', { name: '移除 solo' }).click()
  await expect(editor.getByRole('button', { name: '移除 solo' })).toHaveCount(0)

  // --- Save hits PATCH with the draft and the current mtime ---
  await editor.getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => recorded.patchBodies).toHaveLength(1)
  expect(recorded.patchBodies[0]).toEqual({
    content: { kind: 'tag_txt', tags: ['long_hair'] },
    expected_sidecar_mtime: SIDECAR_MTIME,
  })
  await expect(page.getByText('标签已保存', { exact: true })).toBeVisible()
  // The drawer stays open on the saved state (no remount, no dirty marker).
  await expect(editor).toBeVisible()
  await expect(editor).not.toContainText('有未保存更改')

  await editor.locator('.tm-drawer-footer').getByRole('button', { name: '关闭' }).click()
  await expect(page.getByRole('dialog', { name: 'a.png' })).toHaveCount(0)
  // The double click that opened the editor toggled the selection twice, so
  // the card is still selected after the drawer closes.
  await expect(page.locator('.heading-stats')).toContainText('1 已选')
})

// Runs on BOTH projects: proves the batch bar sends a filtered-scope payload
// carrying the active include filter (and no explicit image ids).
test('filtered batch sends the filter scope payload (runs on desktop and mobile)', async ({ page }) => {
  const recorded = await mockTagManagerApi(page)
  await openTagManager(page)
  await expect(page.locator('img.tm-thumb[alt="a.png"]')).toBeVisible()

  // Filter the grid first: the batch must inherit this filter on the wire.
  const includeInput = page.getByRole('combobox', { name: '包含标签' })
  await includeInput.fill('1g')
  await expect(page.getByRole('option', { name: /1girl/ })).toBeVisible()
  await includeInput.press('Enter')
  await expect(page.getByRole('button', { name: '移除筛选 1girl' })).toBeVisible()
  await expect.poll(() => recorded.imageQueries.at(-1)).toContain('include_tags=1girl')

  // No selection: the scope follows the filtered result.
  const filteredScope = page.getByRole('button', { name: '当前过滤结果（3）' })
  await expect(filteredScope).toHaveAttribute('aria-pressed', 'true')
  await filteredScope.click()

  const batchTagInput = page.getByRole('combobox', { name: '批量标签' })
  await batchTagInput.fill('1g')
  await expect(page.getByRole('option', { name: /1girl/ })).toBeVisible()
  await batchTagInput.press('Enter')
  await expect(page.getByRole('button', { name: '移除 1girl' })).toBeVisible()

  await page.getByRole('button', { name: '执行', exact: true }).click()
  const confirm = page.getByRole('alertdialog', { name: '对 3 张图片执行「添加」？' })
  await expect(confirm).toBeVisible()
  await expect(confirm).toContainText('当前过滤结果的全部 3 张图片')
  await confirm.getByRole('button', { name: '确认执行' }).click()

  await expect.poll(() => recorded.batchBodies).toHaveLength(1)
  expect(recorded.batchBodies[0]).toEqual({
    op: 'add',
    tags: ['1girl'],
    use_regex: false,
    filter: {
      include_tags: ['1girl'],
      exclude_tags: [],
      include_mode: 'all',
      kind: 'any',
      sidecar: 'any',
    },
  })
  expect(recorded.batchBodies[0].image_ids).toBeUndefined()
  await expect(page.getByText('批量操作完成，影响 2 张图片')).toBeVisible()
})
