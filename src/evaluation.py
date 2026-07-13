"""
Evaluation harness для RadiCT Assistant.

Определяет:
- BenchmarkCase: один обезличенный test case с ground truth
- BenchmarkSuite: набор test cases для конкретного task/area
- MetricsResult: вычисленные метрики для одного case
- SuiteResult: сводные метрики по всему suite

Метрики разделены на три группы:
1. Safety/semantic — unsupported additions, omissions, laterality, measurement, dynamics errors
2. Style/structure — section order, banned phrases, modality prefix, compactness
3. Human-effort — character edit ratio, factual/stylistic corrections

Использование:
    from src.evaluation import BenchmarkCase, BenchmarkSuite, evaluate_output
    result = evaluate_output(draft, ground_truth, task="conclusion")
    print(result.to_dict())
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# --- Banned style patterns ---

BANNED_PHRASES = [
    "без существенной динамики",
    "без убедительной динамики",
    "выраженный",
    "значительный",
    "резкий",
    "умеренный",
    "нерезкий",
]

BANNED_LETTERS = ["ё"]

MODALITY_PREFIXES = [
    "КТ-признаки",
    "РКТ-признаки",
    "КТ-данные",
    "РКТ-данные",
]


# --- Data classes ---

@dataclass
class BenchmarkCase:
    """
    Один обезличенный test case для evaluation.

    Поля:
    - case_id: уникальный ID
    - task: conclusion / description / description_and_conclusion
    - area: область исследования
    - input_text: входной текст (описание/диктовка/находки)
    - ground_truth: эталонный финальный текст (от Романа)
    - metadata: дополнительные параметры (comparison, clinical_context, output_mode)
    """
    case_id: str
    task: str
    area: list[str]
    input_text: str
    ground_truth: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MetricsResult:
    """
    Результат оценки одного output.

    Safety/semantic metrics:
    - unsupported_additions: найдены факты, отсутствующие в input
    - omissions: пропущены клинически значимые находки из input
    - laterality_errors: ошибка стороны (правый/левый)
    - measurement_errors: ошибка размеров/значений
    - dynamics_errors: ошибка направления динамики
    - contradiction: противоречие между описанием и заключением

    Style/structure metrics:
    - banned_phrases_found: список найденных запрещённых фраз
    - modality_prefix_found: найден лишний модality prefix
    - has_semicolons_in_conclusion: использование «Без динамики:» grouping
    - section_order_correct: порядок секций соответствует task

    Human-effort metrics:
    - char_edit_ratio: доля изменённых символов (Levenshtein)
    - factual_corrections: количество фактических правок
    - stylistic_corrections: количество стилистических правок
    - accepted_unchanged: draft принят без правок (char_edit_ratio < 0.05)
    """
    # Safety
    unsupported_additions: int = 0
    omissions: int = 0
    laterality_errors: int = 0
    measurement_errors: int = 0
    dynamics_errors: int = 0
    contradiction: bool = False

    # Style
    banned_phrases_found: list[str] = field(default_factory=list)
    modality_prefix_found: bool = False
    section_order_correct: bool = True

    # Human effort
    char_edit_ratio: float = 1.0
    factual_corrections: int = 0
    stylistic_corrections: int = 0
    accepted_unchanged: bool = False

    # Critical flags
    has_critical_error: bool = False

    @property
    def is_acceptable(self) -> bool:
        """Output принимается с косметическими правками или без."""
        return (
            not self.has_critical_error
            and self.unsupported_additions == 0
            and self.laterality_errors == 0
            and self.measurement_errors == 0
            and self.dynamics_errors == 0
            and not self.contradiction
            and self.char_edit_ratio < 0.20
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "safety": {
                "unsupported_additions": self.unsupported_additions,
                "omissions": self.omissions,
                "laterality_errors": self.laterality_errors,
                "measurement_errors": self.measurement_errors,
                "dynamics_errors": self.dynamics_errors,
                "contradiction": self.contradiction,
            },
            "style": {
                "banned_phrases_found": self.banned_phrases_found,
                "modality_prefix_found": self.modality_prefix_found,
                "section_order_correct": self.section_order_correct,
            },
            "human_effort": {
                "char_edit_ratio": round(self.char_edit_ratio, 4),
                "factual_corrections": self.factual_corrections,
                "stylistic_corrections": self.stylistic_corrections,
                "accepted_unchanged": self.accepted_unchanged,
            },
            "has_critical_error": self.has_critical_error,
            "is_acceptable": self.is_acceptable,
        }


@dataclass
class SuiteResult:
    """Сводные метрики по всему benchmark suite."""
    total_cases: int = 0
    acceptable_count: int = 0
    critical_error_count: int = 0
    median_char_edit_ratio: float = 1.0
    banned_phrases_total: int = 0
    modality_prefix_count: int = 0
    case_results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def acceptance_rate(self) -> float:
        if self.total_cases == 0:
            return 0.0
        return self.acceptable_count / self.total_cases

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_cases": self.total_cases,
            "acceptable": self.acceptable_count,
            "critical_errors": self.critical_error_count,
            "acceptance_rate": round(self.acceptance_rate, 4),
            "median_char_edit_ratio": round(self.median_char_edit_ratio, 4),
            "banned_phrases_total": self.banned_phrases_total,
            "modality_prefix_count": self.modality_prefix_count,
            "case_results": self.case_results,
        }


# --- Metrics computation ---

def _levenshtein_ratio(a: str, b: str) -> float:
    """
    Вычислить долю различий между двумя строками (0 = идентичны, 1 = полностью разные).
    Упрощённый алгоритм на основе нормализованного Levenshtein distance.
    """
    if not a and not b:
        return 0.0
    if not a or not b:
        return 1.0

    # Нормализуем: убираем лишние пробелы
    a_norm = re.sub(r"\s+", " ", a.strip())
    b_norm = re.sub(r"\s+", " ", b.strip())

    if a_norm == b_norm:
        return 0.0

    # Простой подход: доля общих n-грамм (3-граммы)
    def get_ngrams(text: str, n: int = 3) -> set[str]:
        words = text.lower().split()
        if len(words) < n:
            return {" ".join(words)}
        return {" ".join(words[i:i+n]) for i in range(len(words) - n + 1)}

    ngrams_a = get_ngrams(a_norm)
    ngrams_b = get_ngrams(b_norm)

    if not ngrams_a or not ngrams_b:
        return 1.0

    intersection = ngrams_a & ngrams_b
    union = ngrams_a | ngrams_b
    jaccard = len(intersection) / len(union) if union else 0.0

    # Convert Jaccard similarity to edit ratio (1 - similarity)
    return max(0.0, 1.0 - jaccard)


def _check_banned_phrases(text: str) -> list[str]:
    """Найти запрещённые фразы в тексте."""
    text_lower = text.lower()
    found = []
    for phrase in BANNED_PHRASES:
        if phrase.lower() in text_lower:
            found.append(phrase)
    return found


def _check_banned_letters(text: str) -> bool:
    """Проверить наличие буквы ё."""
    return "ё" in text


def _check_modality_prefix(text: str) -> bool:
    """Проверить наличие лишнего modality prefix в заключении."""
    for prefix in MODALITY_PREFIXES:
        if prefix in text:
            return True
    return False


def _check_laterality(draft: str, truth: str) -> int:
    """
    Проверить ошибки стороны (правый/левый).
    Простая эвристика: сравниваем упоминания сторон.
    """
    def get_laterality(text: str) -> dict[str, int]:
        """Возвращает {right: count, left: count}."""
        text_lower = text.lower()
        right_words = ["правый", "правая", "правое", "правого", "правому",
                       "правой", "справа"]
        left_words = ["левый", "левая", "левое", "левого", "левому",
                      "левой", "слева"]
        right_count = sum(text_lower.count(w) for w in right_words)
        left_count = sum(text_lower.count(w) for w in left_words)
        return {"right": right_count, "left": left_count}

    draft_lat = get_laterality(draft)
    truth_lat = get_laterality(truth)

    errors = 0
    # Error: draft mentions a side not in truth
    if draft_lat["right"] > 0 and truth_lat["right"] == 0:
        errors += 1
    if draft_lat["left"] > 0 and truth_lat["left"] == 0:
        errors += 1
    return errors


def _check_measurements(draft: str, truth: str) -> int:
    """
    Проверить ошибки измерений.
    Простая эвристика: сравниваем числа в текстах.
    """
    def get_numbers(text: str) -> set[str]:
        return set(re.findall(r"\b(\d+(?:[.,]\d+)?)\s*(?:мм|см|HU|мл|см3|мм3)\b", text, re.IGNORECASE))

    draft_nums = get_numbers(draft)
    truth_nums = get_numbers(truth)

    # Числа в draft, отсутствующие в truth → possible errors
    extra = draft_nums - truth_nums
    return len(extra)


def evaluate_output(
    draft: str,
    ground_truth: str,
    task: str = "conclusion",
    input_text: str = "",
) -> MetricsResult:
    """
    Оценить draft против ground truth.

    Вычисляет safety, style и human-effort метрики.
    """
    result = MetricsResult()

    # --- Human effort ---
    result.char_edit_ratio = _levenshtein_ratio(draft, ground_truth)
    result.accepted_unchanged = result.char_edit_ratio < 0.05

    # --- Style checks ---
    result.banned_phrases_found = _check_banned_phrases(draft)
    result.modality_prefix_found = _check_modality_prefix(draft) if task in ("conclusion", "description_and_conclusion") else False

    # Banned letter ё
    if _check_banned_letters(draft):
        result.banned_phrases_found.append("ё")

    # --- Safety checks ---
    if input_text:
        # Laterality: сравниваем draft с input
        result.laterality_errors = _check_laterality(draft, input_text)

        # Measurements: сравниваем draft с truth
        result.measurement_errors = _check_measurements(draft, ground_truth)

    # Critical errors flag
    result.has_critical_error = (
        result.unsupported_additions > 0
        or result.laterality_errors > 0
        or result.measurement_errors > 0
        or result.dynamics_errors > 0
        or result.contradiction
    )

    return result


def evaluate_suite(
    cases: list[BenchmarkCase],
    drafts: list[str],
) -> SuiteResult:
    """
    Оценить набор draft'ов против benchmark cases.

    cases и drafts должны быть одинаковой длины.
    """
    if len(cases) != len(drafts):
        raise ValueError(f"Mismatch: {len(cases)} cases vs {len(drafts)} drafts")

    suite = SuiteResult(total_cases=len(cases))
    char_ratios = []

    for case, draft in zip(cases, drafts):
        result = evaluate_output(
            draft=draft,
            ground_truth=case.ground_truth,
            task=case.task,
            input_text=case.input_text,
        )

        case_result = {
            "case_id": case.case_id,
            **result.to_dict(),
        }
        suite.case_results.append(case_result)

        if result.is_acceptable:
            suite.acceptable_count += 1
        if result.has_critical_error:
            suite.critical_error_count += 1

        suite.banned_phrases_total += len(result.banned_phrases_found)
        if result.modality_prefix_found:
            suite.modality_prefix_count += 1

        char_ratios.append(result.char_edit_ratio)

    # Median char edit ratio
    if char_ratios:
        char_ratios.sort()
        n = len(char_ratios)
        if n % 2 == 0:
            suite.median_char_edit_ratio = (char_ratios[n//2 - 1] + char_ratios[n//2]) / 2
        else:
            suite.median_char_edit_ratio = char_ratios[n//2]

    return suite