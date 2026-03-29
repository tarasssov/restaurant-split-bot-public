# Fixtures for parser regression

В public-репозитории лежат только синтетические текстовые fixtures.

Что здесь есть:
- безопасные OCR-like/precheck тексты без реальных чеков и без персональных данных;
- минимальный набор для smoke/regression прогона `python3 test_parser.py`.

Чего здесь нет:
- реальные фотографии чеков;
- реальные OCR-снимки и внутренние golden datasets.

Если хочешь проверить свой чек:
1. Подготовь собственный OCR-текст в `fixtures/<name>.txt`, либо
2. Запусти `python3 scripts/test_yandex_ocr.py --image /abs/path/to/receipt.jpg`

Базовый регресс:

```bash
python3 test_parser.py
```
