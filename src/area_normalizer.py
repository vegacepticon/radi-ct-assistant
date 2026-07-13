"""
Нормализация областей исследования для RadiCT Assistant.

Решает три задачи:
1. Нормализация aliases (ОГК = грудная клетка = органы грудной клетки).
2. Multi-area matching: reference с [ОГК, ОБП, ОМТ] находится по любому из элементов.
3. Canonical names для metadata filtering.

Используется в OHS retriever и prompt builder.
"""
from __future__ import annotations

import re


# --- Canonical area names ---

CANONICAL_AREAS = {
    "ОГК",
    "ГМ",
    "ОБП",
    "ОМТ",
    "Шейный отдел позвоночника",
    "Грудной отдел позвоночника",
    "Поясничный отдел позвоночника",
    "позвоночник",
}


# --- Alias → canonical mapping ---

AREA_ALIASES: dict[str, str] = {
    # ОГК
    "огк": "ОГК",
    "грудная клетка": "ОГК",
    "органы грудной клетки": "ОГК",
    "chest": "ОГК",
    "chest ct": "ОГК",
    "грудь": "ОГК",
    # ГМ
    "гм": "ГМ",
    "головной мозг": "ГМ",
    "голова": "ГМ",
    "head": "ГМ",
    "head ct": "ГМ",
    "brain": "ГМ",
    "мозг": "ГМ",
    # ОБП
    "обп": "ОБП",
    "органы брюшной полости": "ОБП",
    "брюшная полость": "ОБП",
    "брюшко": "ОБП",
    "abdomen": "ОБП",
    "abdominal": "ОБП",
    # ОМТ
    "омт": "ОМТ",
    "органы малого таза": "ОМТ",
    "малый таз": "ОМТ",
    "таз": "ОМТ",
    "pelvis": "ОМТ",
    "pelvic": "ОМТ",
    # Шейный отдел
    "шейный отдел": "Шейный отдел позвоночника",
    "шейный отдел позвоночника": "Шейный отдел позвоночника",
    "cervical spine": "Шейный отдел позвоночника",
    "cervical": "Шейный отдел позвоночника",
    "шея": "Шейный отдел позвоночника",
    # Грудной отдел
    "грудной отдел": "Грудной отдел позвоночника",
    "грудной отдел позвоночника": "Грудной отдел позвоночника",
    "thoracic spine": "Грудной отдел позвоночника",
    "thoracic": "Грудной отдел позвоночника",
    # Поясничный отдел
    "поясничный отдел": "Поясничный отдел позвоночника",
    "поясничный отдел позвоночника": "Поясничный отдел позвоночника",
    "lumbar spine": "Поясничный отдел позвоночника",
    "lumbar": "Поясничный отдел позвоночника",
    "поясница": "Поясничный отдел позвоночника",
    # Общий
    "позвоночник": "позвоночник",
    "spine": "позвоночник",
    "позвоночный столб": "позвоночник",
}


def normalize_area(area: str) -> str:
    """
    Нормализовать название области к canonical name.

    'огк' → 'ОГК'
    'грудная клетка' → 'ОГК'
    'ГМ' → 'ГМ'
    'шейный отдел' → 'Шейный отдел позвоночника'
    """
    if not area:
        return ""
    normalized = area.strip().lower()
    # Проверяем точное совпадение в lowercase
    if normalized in AREA_ALIASES:
        return AREA_ALIASES[normalized]
    # Проверяем без lower (для уже-canonical форм)
    if area.strip() in CANONICAL_AREAS:
        return area.strip()
    # Проверяем в canonical aliases без lower
    for alias, canonical in AREA_ALIASES.items():
        if alias == normalized:
            return canonical
    # Не найдено — возвращаем как есть
    return area.strip()


def normalize_areas(areas: list[str]) -> list[str]:
    """
    Нормализовать список областей, убирая дубликаты.

    ['огк', 'ОБП', 'грудная клетка'] → ['ОГК', 'ОБП']
    """
    if not areas:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for area in areas:
        canonical = normalize_area(area)
        if canonical and canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


def areas_match(query_area: str, reference_areas: list[str]) -> bool:
    """
    Проверить, совпадает ли запрашиваемая область с любой из областей reference.

    Использует normalized comparison: 'огк' matches reference ['ОГК', 'ОБП'].

    'ОГК' vs ['ОГК', 'ОБП', 'ОМТ'] → True
    'ОБП' vs ['ОГК', 'ОБП', 'ОМТ'] → True
    'ГМ' vs ['ОГК', 'ОБП'] → False
    '' vs ['ОГК'] → True (empty query = match all)
    'ОГК' vs [] → False (no reference areas = no match unless query empty)
    """
    if not query_area:
        return True  # empty query matches everything

    if not reference_areas:
        return False  # no areas in reference = no match

    normalized_query = normalize_area(query_area)
    normalized_refs = normalize_areas(reference_areas)

    return normalized_query in normalized_refs


def any_area_match(
    query_areas: list[str],
    reference_areas: list[str],
) -> bool:
    """
    Проверить, совпадает ли любая из запрашиваемых областей с любой из областей reference.

    ['ОГК', 'ГМ'] vs ['ОГК', 'ОБП'] → True (ОГК matches)
    ['ГМ'] vs ['ОГК', 'ОБП'] → False
    [] vs ['ОГК'] → True (empty query = match all)
    ['ОГК'] vs [] → False (no reference areas)
    """
    if not query_areas:
        return True  # empty query matches everything
    if not reference_areas:
        return False

    normalized_query = normalize_areas(query_areas)
    normalized_refs = normalize_areas(reference_areas)

    return bool(set(normalized_query) & set(normalized_refs))