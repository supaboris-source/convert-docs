# Проверки convert-docs

В установку скилла не входят (их исключает `install.sh`). Все данные вымышленные.

| Что | Команда | GPU | Время |
|---|---|---|---|
| Логика без модели: коды выхода и план повтора при нераспознанных страницах, отказ выдачи, новая версия без перезаписи, очистка только по точному разрешению, сканы внутри PDF с текстом, TIFF, HEIC, неподдерживаемые и скрытые файлы, мягкие переносы, постпроверка, формулы, листы миниатюр, установщик | `~/ocr-env2/bin/python -I tests/test_logic.py` | нет | секунды |
| Сложный набор: 18 файлов с известным текстом (сканы боком и вверх ногами, фото таблиц, Word и презентации из сканов, HEIC, PDF с испорченным и с хорошим OCR-слоем, дубль, старый .doc, Excel, архив, аудио) | см. ниже | да | ~8 мин |
| Самопроверка установки (то же, что у пользователя) | `~/ocr-env2/bin/python -I scripts/selftest.py` | да | ~5 мин |

Сложный набор:

```bash
T=~/Desktop/сложный-набор                       # любая новая папка
~/Projects/markitdown-local/.venv/bin/python tests/make_hard_set.py "$T/набор"
python3 scripts/workspace.py prepare "$T/набор" --out "$T/выдача" --service-root "$T/служебное"
RUN=$(ls -d "$T"/служебное/runs/*)
python3 tests/check_hard_set.py "$T" "$RUN" "$T/выдача" --plan-only   # только маршрут, без GPU
~/ocr-env2/bin/python scripts/dots_batch.py --plan "$RUN/plan.json"
python3 scripts/postcheck.py "$RUN/documents" && python3 scripts/workspace.py publish "$RUN"
python3 tests/check_hard_set.py "$T" "$RUN" "$T/выдача"               # все контрольные значения
```

Каждая страница сложного набора несёт свои контрольные значения (шифр, сумма, дата, счёт, фамилия и три числа таблицы); `check_hard_set.py` ищет их в выданных `.md` без учёта пробелов и разницы «ё»/«е».
