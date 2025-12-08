"""
Модуль проверки наличия ключевых слов в HTML.
"""
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class KeywordCheckResult:
    """Результат проверки ключевых слов."""
    found: bool
    missing_keyword: Optional[str] = None
    found_keywords: Optional[List[str]] = None


def check_keywords(html_content: str, keywords: List[str]) -> KeywordCheckResult:
    """
    Проверяет наличие всех ключевых слов в HTML-контенте.

    Args:
        html_content: HTML-контент страницы
        keywords: Список ключевых слов для поиска

    Returns:
        KeywordCheckResult с результатами проверки
    """
    if not keywords:
        return KeywordCheckResult(found=True, found_keywords=[])

    found_keywords = []
    for keyword in keywords:
        if keyword in html_content:
            found_keywords.append(keyword)
        else:
            return KeywordCheckResult(
                found=False,
                missing_keyword=keyword,
                found_keywords=found_keywords
            )

    return KeywordCheckResult(found=True, found_keywords=found_keywords)


def check_keywords_any(html_content: str, keywords: List[str]) -> KeywordCheckResult:
    """
    Проверяет наличие хотя бы одного ключевого слова в HTML-контенте.

    Args:
        html_content: HTML-контент страницы
        keywords: Список ключевых слов для поиска

    Returns:
        KeywordCheckResult с результатами проверки
    """
    if not keywords:
        return KeywordCheckResult(found=True, found_keywords=[])

    found_keywords = []
    for keyword in keywords:
        if keyword in html_content:
            found_keywords.append(keyword)

    if found_keywords:
        return KeywordCheckResult(found=True, found_keywords=found_keywords)

    return KeywordCheckResult(
        found=False,
        missing_keyword=keywords[0] if keywords else None,
        found_keywords=[]
    )
