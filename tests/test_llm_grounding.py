from __future__ import annotations

import unittest

from app.bot import _apply_llm_grounding
from app.storage import Item


class LLMGroundingTests(unittest.TestCase):
    def test_rejects_hallucinated_item(self) -> None:
        items = [
            Item(name="Салат Цезарь", price=450),
            Item(name="Вымышленный бургер", price=900),
        ]
        ocr_text = "Салат Цезарь 450\nКофе 200\nИтого 650"
        grounded, report = _apply_llm_grounding(items, ocr_text)
        self.assertEqual(len(grounded), 1)
        self.assertEqual(grounded[0].name, "Салат Цезарь")
        self.assertEqual(int(report["unsupported_items"]), 1)
        self.assertGreater(float(report["unsupported_ratio"]), 0.0)

    def test_allows_ocr_normalization_variants(self) -> None:
        items = [Item(name="Ёжик Сola", price=450)]
        ocr_text = "Ежик Cola 450\nИтого 450"
        grounded, report = _apply_llm_grounding(items, ocr_text)
        self.assertEqual(len(grounded), 1)
        self.assertEqual(int(report["unsupported_items"]), 0)
        self.assertLessEqual(float(report["novel_token_ratio"]), 0.05)

    def test_adjustment_row_not_penalized(self) -> None:
        items = [
            Item(name="Салат", price=400),
            Item(name="Корректировка по итогу", price=100),
        ]
        ocr_text = "Салат 400\nИтого 500"
        grounded, report = _apply_llm_grounding(items, ocr_text)
        self.assertEqual(len(grounded), 2)
        self.assertEqual(int(report["unsupported_items"]), 0)


if __name__ == "__main__":
    unittest.main()
