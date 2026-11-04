"""
Группы сайтов (уровни поддержки). У сайта может быть несколько групп: например, hosting + vip.

Известные группы имеют эмодзи и название для интерфейса, неизвестные показываются как есть.
"""
from typing import Dict, Iterable, List, Tuple

# Порядок = порядок показа в интерфейсе
KNOWN_GROUPS: Dict[str, Tuple[str, str]] = {
    "hosting": ("🏠", "Хостинг"),
    "vip": ("⭐", "VIP"),
    "premium": ("🟢", "Премиум"),
    "standard": ("🟡", "Стандарт"),
}

DEFAULT_GROUP = "standard"


def normalize_groups(values: Iterable[str]) -> List[str]:
    """Чистит список групп: строчные буквы, без пустых и дубликатов, известные группы впереди."""
    seen: List[str] = []
    for v in values or []:
        g = str(v or "").strip().lower()
        if g in ("", "none") or g in seen:
            continue
        seen.append(g)
    known = [g for g in KNOWN_GROUPS if g in seen]
    other = [g for g in seen if g not in KNOWN_GROUPS]
    return known + other


def group_emoji(group: str) -> str:
    return KNOWN_GROUPS.get(group, ("🏷", group))[0]


def group_title(group: str) -> str:
    return KNOWN_GROUPS.get(group, ("🏷", group))[1]


def group_label(group: str) -> str:
    """"🏠 Хостинг" для известной группы, "🏷 имя" для прочей."""
    return f"{group_emoji(group)} {group_title(group)}"


def groups_label(groups: Iterable[str], empty: str = "без группы") -> str:
    groups = list(groups or [])
    if not groups:
        return empty
    return ", ".join(group_label(g) for g in groups)


def all_groups(sites) -> List[str]:
    """Все группы, встречающиеся у сайтов, плюс известные (в порядке показа)."""
    found: List[str] = []
    for site in sites:
        for g in site.groups:
            if g not in found:
                found.append(g)
    known = [g for g in KNOWN_GROUPS if g in found or True]
    other = [g for g in found if g not in KNOWN_GROUPS]
    return known + other
