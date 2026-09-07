"""Deterministic two-level taxonomy for the tag-wiki catalog.

The catalog directory (see ``scripts/build_tag_wiki_catalog.py`` and the
``/catalog`` API) groups every tag into two levels:

1. **category** — the official booru category from the classification
   snapshot (``general`` / ``artist`` / ``character`` / ``copyright`` /
   ``species`` / ``meta`` / ...). Non-general categories map 1:1 onto a
   stable group so the sidebar never shows an empty second level.
2. **group_key** — for ``general`` tags, a semantic group derived from the
   tag name by maintenance-friendly English token rules plus explicit
   overrides (``appearance_body``, ``action_pose``, ``species``, ``sexual``,
   ``clothing``, ``body_part``, ``other_general``).

Everything here is pure and deterministic: no model calls, no I/O, no
randomness. The rules are ordered by precedence — a tag is assigned to the
first group whose keywords match, so ``long_penis`` lands in ``sexual``
before the ``body_part`` rule can claim it. To extend the taxonomy, add
keywords or overrides and bump ``TAXONOMY_VERSION`` so maintainers know to
regenerate the catalog.
"""

from __future__ import annotations

# Bump when group rules/labels change so catalog consumers can detect stale
# generated databases (the build CLI records this into catalog_meta).
TAXONOMY_VERSION = 1

# -- group vocabulary --------------------------------------------------------

# Stable group keys. The first block is derived from the tag NAME (general
# category only); the second maps official categories onto groups verbatim.
NAME_GROUPS: tuple[str, ...] = (
    "sexual",
    "body_part",
    "clothing",
    "appearance_body",
    "action_pose",
    "other_general",
)

CATEGORY_GROUPS: tuple[str, ...] = (
    "artist",
    "character",
    "copyright",
    "species",
    "meta",
    "contributor",
    "lore",
    "invalid",
    "rating",
)

GROUP_LABELS: dict[str, str] = {
    "sexual": "性内容",
    "body_part": "身体部位",
    "clothing": "服装服饰",
    "appearance_body": "外观与体型",
    "action_pose": "动作与姿势",
    "other_general": "其他通用",
    "artist": "作者",
    "character": "角色",
    "copyright": "作品",
    "species": "物种",
    "meta": "元数据",
    "contributor": "贡献者",
    "lore": "设定",
    "invalid": "无效",
    "rating": "分级",
}

CATEGORY_LABELS: dict[str, str] = {
    "general": "通用",
    "artist": "作者",
    "character": "角色",
    "copyright": "作品",
    "species": "物种",
    "meta": "元数据",
    "contributor": "贡献者",
    "lore": "设定",
    "invalid": "无效",
    "rating": "分级",
}

# Sidebar order: the semantic general groups first (most browsing value),
# then the official non-general categories in a stable order.
GROUP_ORDER: dict[str, int] = {
    "appearance_body": 0,
    "action_pose": 1,
    "body_part": 2,
    "clothing": 3,
    "species": 4,
    "sexual": 5,
    "character": 6,
    "copyright": 7,
    "other_general": 8,
    "artist": 9,
    "meta": 10,
    "contributor": 11,
    "lore": 12,
    "rating": 13,
    "invalid": 14,
}


# -- general-tag token rules --------------------------------------------------
#
# English keywords matched against the tag's underscore tokens (and the raw
# name as a fallback for hyphenated/compound spellings). Keep the vocabulary
# conservative: a wrong hit is worse than ``other_general``.

_SEXUAL_KEYWORDS = frozenset(
    {
        "sex", "sexual", "penis", "vagina", "anal", "oral", "cum", "cumshot",
        "creampie", "nipple", "nipples", "areola", "nude", "naked", "nudity",
        "erection", "masturbation", "penetrated", "penetrating", "penetration",
        "fellatio", "cunnilingus", "intercourse", "orgasm", "horny", "erect",
        "clit", "clitoris", "testicles", "balls", "genitals", "genital",
        "hentai", "explicit", "foreplay", "bulge", "crotch", "groin",
        "yiff", "rimjob", "handjob", "breasts", "breast", "boobs", "tit",
        "tits", "cleavage", "topless", "bottomless", "uncensored",
    }
)

_BODY_PART_KEYWORDS = frozenset(
    {
        "ears", "ear", "tail", "tails", "paw", "paws", "pawpads", "muzzle",
        "snout", "beak", "hooves", "hoof", "horns", "horn", "antlers",
        "antler", "wings", "wing", "fur", "hair", "mane", "whiskers",
        "claws", "claw", "fangs", "fang", "scales", "feathers", "feather",
        "eyes", "eye", "mouth", "tongue", "teeth", "hands", "feet", "toes",
        "fingers", "legs", "arms", "neck", "shoulders", "back", "belly",
        "chest", "torso", "hips", "thighs", "butt", "stomach", "navel",
        "eyebrows", "eyelashes", "lips", "nose", "cheeks", "forehead",
    }
)

_CLOTHING_KEYWORDS = frozenset(
    {
        "clothing", "clothes", "uniform", "dress", "skirt", "shirt", "pants",
        "jeans", "shorts", "socks", "stockings", "panties", "bra", "underwear",
        "lingerie", "bikini", "swimsuit", "bodysuit", "leotard", "jacket",
        "coat", "hoodie", "sweater", "vest", "gloves", "boots", "shoes",
        "sneakers", "sandals", "hat", "cap", "helmet", "mask", "scarf",
        "tie", "necktie", "bowtie", "apron", "cape", "robe", "kimono",
        "armor", "armour", "glasses", "eyewear", "headphones", "footwear",
        "nightwear", "pajamas", "thong", "corset", "suspenders", "raincoat",
    }
)

_APPEARANCE_BODY_KEYWORDS = frozenset(
    {
        "hair", "blonde", "blond", "brunette", "redhead", "pixie", "bob",
        "ponytail", "twintails", "braid", "braids", "bangs", "curls", "afro",
        "muscular", "muscles", "abs", "stocky", "slender", "plump", "chubby",
        "fat", "thin", "slim", "tall", "short", "tallgirl", "tallmale",
        "anthro", "feral", "human", "humanoid", "taur", "cyborg", "robot",
        "android", "monster", "dragon", "wolf", "fox", "cat", "dog", "bird",
        "colors", "colored", "tan", "freckles", "scar", "tattoo", "tattoos",
        "piercing", "piercings", "makeup", "eyeshadow", "skin", "complexion",
        "eyes", "heterochromia", "old", "young", "elderly", "child", "teen",
        "adult", "middle-aged", "loli", "shota", "milf", "handsome",
        "beautiful", "cute", "pretty",
    }
)

_ACTION_POSE_KEYWORDS = frozenset(
    {
        "sitting", "standing", "lying", "kneeling", "kneel", "crouching",
        "walking", "running", "jumping", "jump", "dancing", "dance",
        "sleeping", "sleep", "hugging", "hug", "hugging", "kissing", "kiss",
        "holding", "carrying", "riding", "flying", "floating", "falling",
        "swimming", "fighting", "fight", "combat", "duel", "sparring",
        "smiling", "smile", "laughing", "crying", "crying", "shouting",
        "yelling", "whispering", "waving", "pointing", "saluting", "bowing",
        "leaning", "stretching", "yawning", "eating", "drinking", "cooking",
        "reading", "writing", "playing", "singing", "swinging", "throwing",
        "catching", "climbing", "crawling", "squatting", "splits", "handstand",
        "spreading", "raising", "crossed", "arms", "legs", "pose", "posing",
        "gesture", "gesturing", "reaching", "looking", "staring", "gazing",
        "peeking", "hiding", "escaping", "chasing", "pouncing", "mounting",
        "humping", "grinding", "dancing", "victory", "peace", "thumbs",
    }
)

# Precedence: rule list order is the match order. Sexual content must win
# over body_part (e.g. "penis", "nipples"), body_part over appearance (a tag
# like "long_ears" is anatomy, not looks).
_RULES: tuple[tuple[str, frozenset[str]], ...] = (
    ("sexual", _SEXUAL_KEYWORDS),
    ("body_part", _BODY_PART_KEYWORDS),
    ("clothing", _CLOTHING_KEYWORDS),
    ("appearance_body", _APPEARANCE_BODY_KEYWORDS),
    ("action_pose", _ACTION_POSE_KEYWORDS),
)

# Explicit per-tag overrides, checked before any rule. Keys are casefolded
# tag names. Keep this list small and well-justified: it exists for the
# high-traffic tags whose meaning the token rules cannot see (e.g. "solo"
# describes a count/composition, not an action).
OVERRIDES: dict[str, str] = {
    "solo": "action_pose",
    "duo": "action_pose",
    "trio": "action_pose",
    "group": "action_pose",
    "couple": "action_pose",
    "portrait": "action_pose",
    "close-up": "action_pose",
    "smile": "action_pose",
    "eyes_closed": "body_part",
    "censored": "sexual",
    "nude": "sexual",
    "naked": "sexual",
    "intersex": "body_part",
    "herm": "body_part",
    "andromorph": "body_part",
    "gynomorph": "body_part",
    "furry": "species",
    "pony": "species",
    "pokemon": "copyright",
    "clothed": "clothing",
    "unclothed": "sexual",
    "long_hair": "appearance_body",
    "short_hair": "appearance_body",
}

# Official categories that bypass the name rules and map to their own group.
_PASSTHROUGH_GROUPS = frozenset(CATEGORY_GROUPS)


def split_tokens(name: str) -> list[str]:
    """Split a tag name into lowercase word tokens.

    Underscores, hyphens, slashes and parentheses all separate; empty tokens
    are dropped so ``"long__ears"`` behaves like ``"long_ears"``.
    """

    token = []
    tokens: list[str] = []
    for char in str(name).casefold():
        if char.isalnum():
            token.append(char)
        elif token:
            tokens.append("".join(token))
            token = []
    if token:
        tokens.append("".join(token))
    return tokens


def _matches_any_keyword(name: str, keywords: frozenset[str]) -> bool:
    """Whether any rule keyword appears as a token or inside one.

    Two match modes on purpose: exact token equality covers most rules,
    while a containment pass catches compounds the tagger community spells
    without separators (``"longhair"``, ``"blueeyes"``).
    """

    tokens = split_tokens(name)
    for keyword in keywords:
        if keyword in tokens:
            return True
    for keyword in keywords:
        if len(keyword) >= 4 and any(keyword in token for token in tokens):
            return True
    return False


def group_for_tag(profile: str, category: str, name: str) -> str:
    """Resolve the stable group key for one tag.

    Official non-general categories map onto their own group verbatim;
    unknown categories (and ``general`` tags matching nothing) fall back to
    ``other_general``. ``general`` tags run through the overrides table and
    then the ordered token rules.
    """

    del profile  # profiles share the taxonomy; the argument documents intent
    if category in _PASSTHROUGH_GROUPS:
        return category
    if category != "general":
        return "other_general"
    key = str(name).strip().casefold().replace(" ", "_")
    override = OVERRIDES.get(key)
    if override is not None:
        return override
    for group, keywords in _RULES:
        if _matches_any_keyword(key, keywords):
            return group
    return "other_general"


def group_label(group_key: str) -> str:
    """Chinese display label for a group key; unknown keys stay raw."""

    return GROUP_LABELS.get(group_key, group_key)


def category_label(category: str) -> str:
    """Chinese display label for an official category; unknown stays raw."""

    return CATEGORY_LABELS.get(category, category)


def group_sort_key(group_key: str) -> tuple[int, str]:
    """Stable sidebar ordering (semantic groups first), then alphabetical."""

    return (GROUP_ORDER.get(group_key, 99), group_key)


__all__ = [
    "CATEGORY_GROUPS",
    "CATEGORY_LABELS",
    "GROUP_LABELS",
    "GROUP_ORDER",
    "NAME_GROUPS",
    "OVERRIDES",
    "TAXONOMY_VERSION",
    "category_label",
    "group_for_tag",
    "group_label",
    "group_sort_key",
    "split_tokens",
]
