/**
 * Tag category styling shared by the Tag Manager grid, editor, and stats panel.
 * e621 and danbooru expose different category vocabularies; anything unknown
 * (e.g. a tag that is not in the local tag database) renders with the general
 * palette and the label 未知.
 */
export type TagCategoryKey =
  | 'general'
  | 'artist'
  | 'character'
  | 'copyright'
  | 'species'
  | 'meta'
  | 'contributor'
  | 'lore'
  | 'invalid'
  | 'rating'

const categoryLabels: Record<TagCategoryKey, string> = {
  general: '通用',
  artist: '作者',
  character: '角色',
  copyright: '作品',
  species: '物种',
  meta: '元数据',
  contributor: '贡献者',
  lore: '设定',
  invalid: '无效',
  rating: '分级',
}

const e621Categories: TagCategoryKey[] = [
  'general', 'artist', 'character', 'copyright', 'species', 'meta', 'contributor', 'lore', 'invalid', 'rating',
]

const danbooruCategories: TagCategoryKey[] = ['general', 'artist', 'character', 'copyright', 'meta', 'rating']

export const categoryOrder: Record<TagCategoryKey, number> = {
  artist: 0,
  character: 1,
  copyright: 2,
  species: 3,
  general: 4,
  meta: 5,
  rating: 6,
  contributor: 7,
  lore: 8,
  invalid: 9,
}

export function profileCategories(profile: 'e621' | 'danbooru' | string): TagCategoryKey[] {
  return profile === 'danbooru' ? danbooruCategories : e621Categories
}

export function isTagCategoryKey(value?: string | null): value is TagCategoryKey {
  return value != null && Object.hasOwn(categoryLabels, value)
}

/** Maps a raw category string onto a known key; unknown categories become `general`. */
export function normalizeCategory(category?: string | null): TagCategoryKey {
  return isTagCategoryKey(category) ? category : 'general'
}

/** Chinese label; unknown categories read as 未知. */
export function tagCategoryLabel(category?: string | null): string {
  return isTagCategoryKey(category) ? categoryLabels[category] : '未知'
}

/** CSS modifier used by `.tm-pill` / `.tm-cat` elements; unknown → general palette. */
export function tagCategoryClass(category?: string | null): string {
  return `tm-cat-${normalizeCategory(category)}`
}

/** Sort helper for grouped category lists. */
export function compareCategories(left: string, right: string): number {
  const leftOrder = isTagCategoryKey(left) ? categoryOrder[left] : 50
  const rightOrder = isTagCategoryKey(right) ? categoryOrder[right] : 50
  return leftOrder - rightOrder || left.localeCompare(right)
}
