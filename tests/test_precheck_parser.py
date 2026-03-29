from __future__ import annotations

import unittest

from app.receipt_parser import _parse_precheck_numbered_eq, parse_receipt_text_with_variant


PRECHECK_TEXT = """
1) Салат Цезарь
=450,00 Руб.
2) Борщ с говядиной
=390,00 Руб.
3) Паста Карбонара
=620,00 Руб.
4) Лимонад
=250,00 Руб.
5) Чай облепиховый
=370,00 Руб.
6) Пицца Маргарита
=780,00 Руб.
7) Стейк из говядины
=1350,00 Руб.
8) Десерт Медовик
=420,00 Руб.
Итого к оплате 4630,00
""".strip()


class PrecheckParserTests(unittest.TestCase):
    def test_precheck_variant_selected(self) -> None:
        items, variant = parse_receipt_text_with_variant(PRECHECK_TEXT)
        self.assertEqual(variant, "rule_v1_precheck_eq")
        base_items = [x for x in items if "корректировка по итогу" not in x.name.lower()]
        self.assertEqual(len(base_items), 8)
        self.assertTrue(any("Салат Цезарь" in x.name for x in base_items))
        self.assertTrue(any("Стейк из говядины" in x.name for x in base_items))

    def test_precheck_ignores_qty_noise(self) -> None:
        text = """
        1) Ребра BBQ
        230,00 * 2,000 порц
        =460,00 Руб.
        """.strip()
        items = _parse_precheck_numbered_eq(text)
        self.assertEqual(len(items), 1)
        self.assertIn("Ребра", items[0].name)
        self.assertNotIn("порц", items[0].name.lower())
        self.assertEqual(items[0].price, 460)


if __name__ == "__main__":
    unittest.main()
