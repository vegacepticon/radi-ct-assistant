"""
Тесты для evaluation harness.

Используют только synthetic no-PHI fixtures.
"""
import unittest

from src.evaluation import (
    BenchmarkCase,
    MetricsResult,
    SuiteResult,
    evaluate_output,
    evaluate_suite,
    _check_banned_phrases,
    _check_modality_prefix,
    _check_laterality,
    _check_measurements,
    _levenshtein_ratio,
)


class TestStyleChecks(unittest.TestCase):
    """Тесты проверки стиля."""

    def test_banned_phrases_detected(self):
        text = "Без существенной динамики. Выраженный отек."
        found = _check_banned_phrases(text)
        self.assertIn("без существенной динамики", found)
        self.assertIn("выраженный", found)

    def test_no_banned_phrases(self):
        text = "Очаг S8 правого легкого без динамики."
        found = _check_banned_phrases(text)
        self.assertEqual(found, [])

    def test_banned_letter_yo(self):
        text = "Решетчатый лабиринт объем."
        # These words contain ё in correct Russian but we use е
        found = _check_banned_phrases(text)
        self.assertNotIn("ё", found)

    def test_modality_prefix_detected(self):
        text = "КТ-признаки очаговых изменений."
        self.assertTrue(_check_modality_prefix(text))

    def test_no_modality_prefix(self):
        text = "Признаки очаговых изменений."
        self.assertFalse(_check_modality_prefix(text))


class TestSafetyChecks(unittest.TestCase):
    """Тесты safety checks."""

    def test_laterality_no_error(self):
        draft = "Очаг правого легкого."
        truth = "Очаг правого легкого."
        self.assertEqual(_check_laterality(draft, truth), 0)

    def test_laterality_error(self):
        draft = "Очаг левого легкого."
        truth = "Очаг правого легкого."
        self.assertGreater(_check_laterality(draft, truth), 0)

    def test_measurement_no_error(self):
        draft = "Очаг 15 мм."
        truth = "Очаг 15 мм."
        self.assertEqual(_check_measurements(draft, truth), 0)

    def test_measurement_error(self):
        draft = "Очаг 20 мм."
        truth = "Очаг 15 мм."
        self.assertGreater(_check_measurements(draft, truth), 0)


class TestLevenshteinRatio(unittest.TestCase):
    """Тесты edit ratio."""

    def test_identical(self):
        self.assertEqual(_levenshtein_ratio("текст", "текст"), 0.0)

    def test_completely_different(self):
        ratio = _levenshtein_ratio("абв", "эюя")
        self.assertGreater(ratio, 0.8)

    def test_similar(self):
        ratio = _levenshtein_ratio(
            "Очаг S8 правого легкого 15 мм",
            "Очаг S8 правого легкого 15 мм без динамики",
        )
        self.assertLess(ratio, 0.5)


class TestEvaluateOutput(unittest.TestCase):
    """Тесты evaluate_output."""

    def test_perfect_match(self):
        """Draft = ground truth → accepted_unchanged, no errors."""
        draft = "Очаг S8 правого легкого без динамики."
        truth = "Очаг S8 правого легкого без динамики."
        result = evaluate_output(draft, truth, task="conclusion")
        self.assertTrue(result.accepted_unchanged)
        self.assertTrue(result.is_acceptable)
        self.assertFalse(result.has_critical_error)

    def test_banned_phrase_in_draft(self):
        """Draft с запрещённой фразой → banned_phrases_found."""
        draft = "Без существенной динамики."
        truth = "Без динамики."
        result = evaluate_output(draft, truth, task="conclusion")
        self.assertIn("без существенной динамики", result.banned_phrases_found)

    def test_modality_prefix_in_conclusion(self):
        """Modality prefix в conclusion → flagged."""
        draft = "КТ-признаки очаговых изменений."
        truth = "Признаки очаговых изменений."
        result = evaluate_output(draft, truth, task="conclusion")
        self.assertTrue(result.modality_prefix_found)

    def test_laterality_error_flagged(self):
        """Ошибка стороны → critical error."""
        draft = "Очаг левого легкого."
        truth = "Очаг правого легкого."
        input_text = "Очаг правого легкого 15 мм."
        result = evaluate_output(draft, truth, task="conclusion", input_text=input_text)
        self.assertGreater(result.laterality_errors, 0)
        self.assertTrue(result.has_critical_error)

    def test_measurement_error_flagged(self):
        """Ошибка размера → critical error."""
        draft = "Очаг 20 мм."
        truth = "Очаг 15 мм."
        input_text = "Очаг 15 мм."
        result = evaluate_output(draft, truth, task="conclusion", input_text=input_text)
        self.assertGreater(result.measurement_errors, 0)
        self.assertTrue(result.has_critical_error)

    def test_to_dict_structure(self):
        """to_dict возвращает ожидаемую структуру."""
        result = evaluate_output("текст", "текст", task="conclusion")
        d = result.to_dict()
        self.assertIn("safety", d)
        self.assertIn("style", d)
        self.assertIn("human_effort", d)
        self.assertIn("has_critical_error", d)
        self.assertIn("is_acceptable", d)


class TestEvaluateSuite(unittest.TestCase):
    """Тесты evaluate_suite."""

    def test_suite_with_all_accepted(self):
        """Все drafts приняты → 100% acceptance rate."""
        cases = [
            BenchmarkCase(
                case_id="synthetic-001",
                task="conclusion",
                area=["ОГК"],
                input_text="Очаг S8 правого легкого 15 мм.",
                ground_truth="Очаг S8 правого легкого без динамики.",
            ),
            BenchmarkCase(
                case_id="synthetic-002",
                task="conclusion",
                area=["ОГК"],
                input_text="Пневмония нижней доли справа.",
                ground_truth="Пневмония нижней доли правого легкого.",
            ),
        ]
        drafts = [
            "Очаг S8 правого легкого без динамики.",
            "Пневмония нижней доли правого легкого.",
        ]
        suite = evaluate_suite(cases, drafts)
        self.assertEqual(suite.total_cases, 2)
        self.assertEqual(suite.acceptable_count, 2)
        self.assertEqual(suite.acceptance_rate, 1.0)

    def test_suite_with_critical_errors(self):
        """Drafts с laterality errors → critical_error_count > 0."""
        cases = [
            BenchmarkCase(
                case_id="synthetic-003",
                task="conclusion",
                area=["ОГК"],
                input_text="Очаг правого легкого.",
                ground_truth="Очаг правого легкого.",
            ),
        ]
        drafts = ["Очаг левого легкого."]
        suite = evaluate_suite(cases, drafts)
        self.assertEqual(suite.critical_error_count, 1)
        self.assertEqual(suite.acceptable_count, 0)

    def test_suite_mismatch_raises(self):
        """Несовпадение длины cases и drafts → ValueError."""
        cases = [BenchmarkCase(case_id="x", task="conclusion", area=[], input_text="", ground_truth="")]
        drafts = ["a", "b"]
        with self.assertRaises(ValueError):
            evaluate_suite(cases, drafts)

    def test_suite_to_dict(self):
        """to_dict возвращает ожидаемую структуру."""
        cases = [
            BenchmarkCase(case_id="x", task="conclusion", area=["ОГК"], input_text="", ground_truth="текст"),
        ]
        drafts = ["текст"]
        suite = evaluate_suite(cases, drafts)
        d = suite.to_dict()
        self.assertIn("total_cases", d)
        self.assertIn("acceptance_rate", d)
        self.assertIn("case_results", d)
        self.assertEqual(len(d["case_results"]), 1)


if __name__ == "__main__":
    unittest.main()