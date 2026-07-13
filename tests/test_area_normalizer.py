"""
Тесты для area normalizer.

Проверяют:
- normalize_area: aliases → canonical
- normalize_areas: дедупликация
- areas_match: multi-area matching
- any_area_match: списки областей
"""
import unittest

from src.area_normalizer import (
    normalize_area,
    normalize_areas,
    areas_match,
    any_area_match,
)


class TestNormalizeArea(unittest.TestCase):
    """Тесты нормализации отдельных областей."""

    def test_ogk_aliases(self):
        self.assertEqual(normalize_area("ОГК"), "ОГК")
        self.assertEqual(normalize_area("огк"), "ОГК")
        self.assertEqual(normalize_area("грудная клетка"), "ОГК")
        self.assertEqual(normalize_area("органы грудной клетки"), "ОГК")
        self.assertEqual(normalize_area("chest"), "ОГК")

    def test_gm_aliases(self):
        self.assertEqual(normalize_area("ГМ"), "ГМ")
        self.assertEqual(normalize_area("гм"), "ГМ")
        self.assertEqual(normalize_area("головной мозг"), "ГМ")
        self.assertEqual(normalize_area("мозг"), "ГМ")
        self.assertEqual(normalize_area("brain"), "ГМ")

    def test_obp_aliases(self):
        self.assertEqual(normalize_area("ОБП"), "ОБП")
        self.assertEqual(normalize_area("обп"), "ОБП")
        self.assertEqual(normalize_area("органы брюшной полости"), "ОБП")
        self.assertEqual(normalize_area("брюшная полость"), "ОБП")
        self.assertEqual(normalize_area("abdomen"), "ОБП")

    def test_omt_aliases(self):
        self.assertEqual(normalize_area("ОМТ"), "ОМТ")
        self.assertEqual(normalize_area("омт"), "ОМТ")
        self.assertEqual(normalize_area("органы малого таза"), "ОМТ")
        self.assertEqual(normalize_area("таз"), "ОМТ")
        self.assertEqual(normalize_area("pelvis"), "ОМТ")

    def test_spine_aliases(self):
        self.assertEqual(normalize_area("Шейный отдел позвоночника"), "Шейный отдел позвоночника")
        self.assertEqual(normalize_area("шейный отдел"), "Шейный отдел позвоночника")
        self.assertEqual(normalize_area("cervical"), "Шейный отдел позвоночника")
        self.assertEqual(normalize_area("поясничный отдел"), "Поясничный отдел позвоночника")
        self.assertEqual(normalize_area("lumbar"), "Поясничный отдел позвоночника")

    def test_empty(self):
        self.assertEqual(normalize_area(""), "")
        self.assertEqual(normalize_area("  "), "")

    def test_unknown_passthrough(self):
        self.assertEqual(normalize_area("Неизвестная область"), "Неизвестная область")


class TestNormalizeAreas(unittest.TestCase):
    """Тесты нормализации списков областей."""

    def test_dedup(self):
        result = normalize_areas(["ОГК", "огк", "грудная клетка"])
        self.assertEqual(result, ["ОГК"])

    def test_multiple(self):
        result = normalize_areas(["ОГК", "ОБП", "ОМТ"])
        self.assertEqual(result, ["ОГК", "ОБП", "ОМТ"])

    def test_empty(self):
        self.assertEqual(normalize_areas([]), [])

    def test_mixed_case(self):
        result = normalize_areas(["огк", "ГМ", "обп"])
        self.assertEqual(result, ["ОГК", "ГМ", "ОБП"])


class TestAreasMatch(unittest.TestCase):
    """Тесты multi-area matching."""

    def test_exact_match(self):
        self.assertTrue(areas_match("ОГК", ["ОГК", "ОБП", "ОМТ"]))

    def test_partial_match(self):
        """Reference с [ОГК, ОБП, ОМТ] находится по запросу ОБП."""
        self.assertTrue(areas_match("ОБП", ["ОГК", "ОБП", "ОМТ"]))
        self.assertTrue(areas_match("ОМТ", ["ОГК", "ОБП", "ОМТ"]))

    def test_no_match(self):
        self.assertFalse(areas_match("ГМ", ["ОГК", "ОБП"]))

    def test_alias_match(self):
        """'огк' matches reference ['ОГК', 'ОБП']."""
        self.assertTrue(areas_match("огк", ["ОГК", "ОБП"]))
        self.assertTrue(areas_match("грудная клетка", ["ОГК"]))

    def test_empty_query_matches_all(self):
        self.assertTrue(areas_match("", ["ОГК"]))
        self.assertTrue(areas_match("", []))

    def test_empty_reference_no_match(self):
        self.assertFalse(areas_match("ОГК", []))


class TestAnyAreaMatch(unittest.TestCase):
    """Тесты matching для списков запросов."""

    def test_any_match(self):
        self.assertTrue(any_area_match(["ОГК", "ГМ"], ["ОГК", "ОБП"]))

    def test_no_match(self):
        self.assertFalse(any_area_match(["ГМ"], ["ОГК", "ОБП"]))

    def test_empty_query(self):
        self.assertTrue(any_area_match([], ["ОГК"]))

    def test_empty_reference(self):
        self.assertFalse(any_area_match(["ОГК"], []))

    def test_alias_match(self):
        self.assertTrue(any_area_match(["грудная клетка"], ["ОГК"]))


if __name__ == "__main__":
    unittest.main()