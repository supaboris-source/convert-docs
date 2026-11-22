#!/usr/bin/env python3
"""Постпроверка готовых .md: что распозналось впустую, криво или выдумано.

    python3 postcheck.py <корень_вывода> [--json report.json] [--no-write-json]

Зачем. Конвертация «прошла без ошибок» и конвертация «дала пригодный текст» —
разные вещи, и разрыв между ними обнаруживается уже в анализе, когда поздно.
На реальном деле (95 файлов) молча случилось три беды сразу:

  * семь файлов превратились в одну строку `![](_page_0_Figure_3.jpeg)` —
    скриншоты кадастровой карты, текста в них нет вовсе. Пустой .md выглядел
    как «сделано», и ни один из семи не попал в разбор дела;
  * четыре чертежа дали кракозябры («ɍɫɥɨɜɧɵɟɨɛɨɡɧɚɱɟɧɢɹ») из-за шрифта
    без ToUnicode;
  * в одном файле marker ВЫДУМАЛ таблицу американских чиновников
    («Seal of the Commonwealth of Massachusetts», «Senator: John McCarthy»)
    поверх нечитаемого штампа русской схемы.

Скрипт ищет ровно эти следы. Он ничего не чинит и не удаляет — только
показывает, что нельзя отдавать в работу не посмотрев.

С 15.10.2026 он ещё собирает статистику OCR из `<имя>_ocr.json` (петли, обрывы,
второй движок, повороты, непроверенные имена и числа из сверки вторым чтением)
и пишет `<корень>/_ПРОВЕРИТЬ.md` — пофайловый список страниц, которые надо
прочитать глазами или сверить по оригиналу. Это и есть «что осталось человеку».

С 17.10.2026 правила «прочитать глазами» построены по разметке 81 страницы реального дела
(отчёт B2-детектор.md: найдено 62 из 71 плохих страниц против 58, ложных 2 из 10 против 4):
пропуски строк от 2 (было 5), любое дочитывание (REREAD), иероглифы и английские фразы (≥ 4 слов подряд)
в русском тексте, петля или обрыв — только при неуверенном втором чтении (conf < 0,45) или пропусках,
иначе в «сверить» (4 из 11 петель оказались ложной тревогой), плохо читаемая страница по Vision
(conf < 0,40, мало настоящих слов). Синие чернила (`ink.blue_px` из page_features.py) сами по себе рукописью
не считаются (на другом реальном деле «синие» 120 страниц из 130: подписи, печати, ЭЦП), они добавляют страницы
только в документе, где рукопись уже видна по Vision. Документ, где страницы с провалами составляют ≥ 30 %
(и не меньше 3), помечается «ПЛОХОЕ КАЧЕСТВО» (`quality` в `_ocr.json`, раздел в начале `_ПРОВЕРИТЬ.md`;
переименование и удаление json — finalize.py). Повторные снимки одного листа группируются, на чтение
глазами идёт самый резкий. Реквизиты проверяет requisites.py. Снимки без текста, пропущенные движком
(`mode: "empty"`), не считаются пустыми страницами и в «прочитать глазами» не попадают.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BAD_SUFFIX, md_for_json   # пометка «плохое качество» в имени и поиск .md — общие для скриптов
try:
    import requisites          # контрольные суммы ИНН/ОГРН/счетов, сумма прописью; без него раздел пропускается
except Exception:  # noqa: BLE001
    requisites = None

# Диапазоны, которых в русском юридическом тексте быть не может.
# IPA Extensions + Latin Extended-B: туда попадают глифы битых CAD-шрифтов.
MOJIBAKE = re.compile(r"[ɀ-ʯ]")

# Иероглифы, кана, хангыль, полуширинные формы: VLM на рукописи и размытом фото сыплет ими
# («паспорт на分子», китайские знаки вместо рукописных строк).
HIEROGLYPHS = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff66-\uff9f]")

# Латинская «официальщина» посреди русского документа — почерк галлюцинации
# vision-модели на нечитаемом штампе или печати.
INVENTED = re.compile(
    r"\b(Commonwealth|Senator|Commissioner|Chief Counsel|Co-President|"
    r"President|Secretary of State|Notary Public|Attorney General)\b"
)

# Английская фраза: ≥ 4 латинских слов подряд (LATIN_MIN_WORDS) (в слове ≥ 2 букв), из них ≥ 2 набраны строчными.
# Так отсекаются аббревиатуры («PDF USB SIM»), названия («Microsoft Office Word»); URL, e-mail и домены вырезаются.
# Взято с разметки B2: «each other in the page», «which are responsible for their own activities»,
# «not shorter than 1 day» — старая проверка по списку слов их не видела (стр. 19, 33, 35, 47 материала).
LATIN_RUN = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё0-9_])[A-Za-z][A-Za-z'’-]+(?:[ \t]+[A-Za-z][A-Za-z'’-]+){2,}(?![A-Za-zА-Яа-яЁё0-9_-])")
URLISH = re.compile(
    r"https?://\S+|www\.\S+|\S+@\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:ru|com|org|net|su|рф|info|io|me|gov|edu)\b\S*", re.I)
LATIN_MIN_WORDS = 4         # 3 слов давало ложные «Push to open» в ведомостях оборудования (реальное дело); у выдумок B2 серии по 5–9 слов
LATIN_MIN_LOWER = 2
RUSSIAN_MIN_LETTERS = 100   # меньше кириллицы — документ не русский, английский текст в нём в порядке вещей
BILINGUAL_LATIN = 0.25      # на странице латиница ≥ 25 % букв — явно двуязычная (договор с английским столбцом): английские фразы не выдумка

# Картинка отдельной строкой: `![](x)` и `![C:\Users\…\Scan_0034.jpg](data:image/jpeg;base64...)`
# (markitdown кладёт имя файла в подпись — прежнее `^!\[\]\(` такие строки не узнавало,
# и .md из пяти картинок docx проходил как «с текстом»).
IMG_ONLY = re.compile(r"^!\[[^\n]*\]\([^\n]*\)$")
IMG_PLACEHOLDER = re.compile(r"^!\[[^\n]*\]\((?:рисунок|figure|picture)\)$", re.I)   # пометка OCR «здесь была картинка»

# HTML-комментарии — это пометки, которые оставляем мы сами («здесь marker
# выдумал таблицу Commonwealth of Massachusetts»). Их нельзя проверять как
# содержимое документа: иначе объяснение находки само становится находкой.
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
HTML_TAG = re.compile(r"</?(?:td|tr|th|table|thead|tbody|br|div|span|p)[^>]*>", re.I)
IMG_LINK = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")

TINY_BYTES = 300          # меньше — распознавать было нечего
TEXT_PARA_MIN_LETTERS = 20   # абзац короче этого не считается «текстом» при сравнении с картинками
IMAGES_MORE_THAN_TEXT_MIN = 2

# Слово, в котором перемешаны кириллица и латиница: «Мoсква» с латинской «o».
# MDPBench (2026) называет это типовой ошибкой VLM на русском — буквы,
# одинаковые в двух алфавитах, декодируются латиницей. Глазом не видно,
# поиск по тексту такое слово не найдёт.
MIXED = re.compile(r"(?<![A-Za-zА-Яа-яЁё])(?=[A-Za-zА-Яа-яЁё]*[А-Яа-яЁё])(?=[A-Za-zА-Яа-яЁё]*[A-Za-z])[A-Za-zА-Яа-яЁё]{3,}(?![A-Za-zА-Яа-яЁё])")
MIXED_MIN = 3             # единичные бывают и в норме (Wi-Fi, iPhone)
MOJIBAKE_CHARS = 5        # единичный глиф бывает и в норме
CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")


def latin_phrases(text: str) -> list[str]:
    """Английские фразы (≥ 3 латинских слов подряд) в тексте, без URL, e-mail и аббревиатур."""
    t = URLISH.sub(" ", text)
    out = []
    for m in LATIN_RUN.finditer(t):
        ws = m.group(0).split()
        if len(ws) >= LATIN_MIN_WORDS and sum(1 for w in ws if w.islower()) >= LATIN_MIN_LOWER:
            out.append(" ".join(ws))
    return out


def strip_service(text: str) -> str:
    """Текст без служебных строк конвейера: «## Страница N», подписи «Распознано: …», заметок «> ⚠» / «> ✎» /
    «> Примечание», ‹QR›, первого заголовка с именем файла и HTML-комментариев. Их кириллица — не язык документа."""
    t = HTML_COMMENT.sub("", text)
    t = re.sub(r"\A\s*# [^\n]*\n", "", t)
    t = re.sub(r"(?m)^---\nРаспознано:[^\n]*$", "", t)
    t = re.sub(r"(?m)^## Страница \d+\s*$", "", t)
    return "\n".join(l for l in t.splitlines()
                     if not l.lstrip().startswith(SERVICE_QUOTE) and not l.lstrip().startswith("‹QR›"))


def is_russian(text: str) -> bool:
    return len(CYRILLIC.findall(strip_service(text))) >= RUSSIAN_MIN_LETTERS


def is_bilingual(text: str) -> bool:
    """Латиница — не меньше BILINGUAL_LATIN букв текста (служебные строки и ссылки не считаются)."""
    t = URLISH.sub(" ", strip_service(text))
    lat, cyr = len(re.findall(r"[A-Za-z]", t)), len(CYRILLIC.findall(t))
    return lat + cyr > 0 and lat / (lat + cyr) >= BILINGUAL_LATIN


def look(md: Path) -> list[str]:
    """Список претензий к одному файлу (пустой — всё в порядке)."""
    try:
        raw = md.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [f"не читается: {e}"]

    text = HTML_COMMENT.sub("", raw)
    flags: list[str] = []
    body = [l.strip() for l in text.splitlines() if l.strip()]
    size = md.stat().st_size

    if not body:
        return ["ПУСТО — файла как будто и нет"]

    imgs = [l for l in body if IMG_ONLY.match(l)]
    real_imgs = [l for l in imgs if not IMG_PLACEHOLDER.match(l)]
    paras = [l for l in body if not IMG_ONLY.match(l) and not l.startswith("#") and not l.startswith(">")
             and not l.startswith("---") and len(CYRILLIC.findall(l)) + len(re.findall(r"[A-Za-z]", l)) >= TEXT_PARA_MIN_LETTERS]

    if all(IMG_ONLY.match(l) or l.startswith("#") for l in body):
        flags.append("ТОЛЬКО КАРТИНКА — текста не извлечено ни строки (%d картинок); "
                     "если это скан/скриншот/карта, читать надо глазами или распознавать (OCR), а не из .md" % len(imgs))
    elif len(real_imgs) >= IMAGES_MORE_THAN_TEXT_MIN and len(real_imgs) > len(paras):
        flags.append(f"КАРТИНОК БОЛЬШЕ, ЧЕМ ТЕКСТА — {len(real_imgs)} картинок и {len(paras)} абзацев текста: "
                     "содержимое, видимо, в картинках (скан внутри docx/pdf); распознать их или читать глазами")
    elif size < TINY_BYTES:
        seen, pages_js = 0, []
        js = md.with_name(md.stem.removesuffix(BAD_SUFFIX) + "_ocr.json")
        if js.exists():
            try:
                pages_js = json.loads(js.read_text(encoding="utf-8")).get("page_stats", [])
                seen = max((((p.get("crosscheck") or {}).get("vision_lines") or 0) for p in pages_js), default=0)
            except Exception:
                seen = 0
        if pages_js and all(p.get("mode") == "empty" for p in pages_js):
            pass    # движок сам пропустил снимки без текста (NO_TEXT_SKIPPED) — это не потеря, список в _ПРОВЕРИТЬ.md
        elif seen >= 10:
            flags.append(f"ПОЧТИ ПУСТО, но второе чтение видит {seen} строк текста — страница ПОТЕРЯНА, прочитать глазами")
        else:
            flags.append(f"ПОЧТИ ПУСТО — {size} б, {len(body)} строк (текста на странице, видимо, нет: фото, схема)")

    if PAGE_RE.search(text):
        js = md.with_name(md.stem.removesuffix(BAD_SUFFIX) + "_ocr.json")
        covered: set[int] = set()
        if js.exists():
            try:
                covered = {p.get("page") for p in json.loads(js.read_text(encoding="utf-8")).get("page_stats", [])}
            except Exception:
                covered = set()
        pages = split_pages(text)
        blank = [n for n, t in pages.items() if n not in covered and _alnum(clean_page(t)) < EMPTY_PAGE_CHARS]
        if blank and len(pages) >= 2:
            flags.append(f"ПУСТЫЕ СТРАНИЦЫ — стр. {_pages_str(blank)}: текста нет и OCR их не читал (скан внутри PDF "
                         "с текстом или пустой лист). Распознать эти страницы или посмотреть оригинал")

    moj = len(MOJIBAKE.findall(text))
    if moj >= MOJIBAKE_CHARS:
        cyr = len(CYRILLIC.findall(text))
        if moj * 4 >= cyr:
            hint = "шрифт без ToUnicode; переделать с --force_ocr"
        else:
            # Битым шрифтом набрана малая часть — обычно заголовок. force_ocr
            # тут отнимет больше, чем вернёт (замер по газетной полосе:
            # кириллицы 4732 → 1933). Восстанавливать такое руками по картинке.
            hint = (f"битый шрифт лишь в части текста ({moj} против {cyr} читаемых букв) — "
                    "force_ocr обеднит документ, восстановить эти слова по исходнику")
        flags.append(f"КРАКОЗЯБРЫ — {moj} симв.; {hint}")

    mixed = sorted(set(MIXED.findall(text)))
    if len(mixed) >= MIXED_MIN:
        flags.append(f"СМЕШАНЫ АЛФАВИТЫ — {len(mixed)} слов с латиницей внутри кириллицы: "
                     f"{', '.join(mixed[:6])}. Поиск по тексту их не найдёт, поправить")

    hier = HIEROGLYPHS.findall(text)
    if hier and CYRILLIC.search(text):
        flags.append(f"ИЕРОГЛИФЫ — {len(hier)} симв. ({''.join(dict.fromkeys(hier))[:8]}) в русском документе: "
                     "почерк выдумки OCR на рукописи или размытом фото; найти и прочитать по оригиналу")

    if CYRILLIC.search(text):
        clean = IMG_LINK.sub(" ", text)
        hits = sorted(set(INVENTED.findall(clean)))
        phrases = latin_phrases(clean) if is_russian(text) and not is_bilingual(clean) else []
        if hits or phrases:
            parts = []
            if hits:
                parts.append("английская «официальщина»: " + ", ".join(hits[:5]))
            if phrases:
                parts.append(f"английские фразы ({len(phrases)}): «{phrases[0][:60]}»" + (" …" if len(phrases) > 1 else ""))
            flags.append("ВОЗМОЖНО ВЫДУМАНО — " + "; ".join(parts) + " в русском документе. Сверить с оригиналом")

    return flags


def stale_meta(root: Path) -> list[Path]:
    """_meta.json от marker, переживший замену .md другим движком.

    Метаданные утверждают «сделано marker, 92 страницы», а рядом лежит вывод
    markitdown. Через месяц по папке уже не понять, чем получен текст.
    """
    out = []
    for meta in root.rglob("*_meta.json"):
        stem = meta.name[: -len("_meta.json")]
        md = meta.with_name(stem + ".md")
        # Если рядом лежит «<имя> (marker).md», значит вывод marker подменили
        # после сверки цифр, и метаданные описывают именно его — они на месте.
        if meta.with_name(f"{stem} (marker).md").exists():
            continue
        if md.exists() and md.stat().st_mtime > meta.stat().st_mtime + 60:
            out.append(meta)
    return out


NUM = re.compile(r'\b\d{1,3}(?:[  ]\d{3})+\b|\b\d{4,}\b')


def numbers(text: str) -> set[str]:
    """Числа как токены: «1 350 000» и «1350000» — одно и то же.

    Убирать ВСЕ пробелы перед поиском нельзя: соседние ячейки таблицы
    склеятся в несуществующие числа вроде «000019359», и сверка утонет
    в ложных потерях.
    """
    return {re.sub(r'[  ]', '', m) for m in NUM.findall(text)}


def compare_engines(plan_path: Path, log=print) -> list[dict]:
    """Кто сохранил больше цифр — marker или отложенный замер markitdown.

    Пороги Шага 1 системно переоценивают marker: вертикальный список
    «поле — значение» (выписка ЕГРН) и чертёж с размерами в столбик выглядят
    как рассыпавшаяся структура, хотя текстовый слой цел. Marker, пересобирая
    макет, выбрасывает значения — молча и без единого признака брака.

    Замерено на двух делах: из выписки ЕГРН пропала кадастровая стоимость
    691 200 руб. (ячейка осталась пустой), из проекта реконструкции — 52 числа
    и две трети текста. Оба раза markitdown читал файл правильно.

    Поэтому решает не эвристика, а факт: сколько числовых токенов из
    `pdftotext` дошло до каждого варианта. Разница меньше MIN_GAIN считается
    шумом — движки не обязаны совпадать посимвольно.
    """
    MIN_GAIN = 3      # разница в потерянных числах, ниже которой это шум
    MIN_TEXT = 0.90   # и при этом markitdown не должен растерять текст
    data = json.loads(plan_path.read_text())
    swapped = []
    for row in data.get("plan", []):
        probe = row.get("probe")
        if not probe or row.get("engine") not in ("marker", "ocr"):
            continue
        out, src, probe = Path(row["out"]), Path(row["src"]), Path(probe)
        if not (out.exists() and probe.exists() and src.exists()):
            continue
        ref = subprocess.run(["pdftotext", str(src), "-"],
                             capture_output=True, text=True).stdout
        want = numbers(ref)
        if not want:
            continue
        t_marker = out.read_text(errors="replace")
        t_probe = probe.read_text(errors="replace")
        lost_marker = len(want - numbers(t_marker))
        lost_probe = len(want - numbers(t_probe))
        # Одних чисел мало. На отчёте об оценке (208 страниц) marker потерял
        # 5 номеров из блока об оценщике, зато сам распознал таблицы-картинки,
        # которых нет в текстовом слое: 343 649 букв против 293 847. Подмена
        # ради пяти номеров стоила бы 50 000 знаков — так делать нельзя.
        cyr_marker = len(CYRILLIC.findall(t_marker))
        cyr_probe = len(CYRILLIC.findall(t_probe))
        keeps_text = cyr_probe >= cyr_marker * MIN_TEXT
        if lost_marker - lost_probe >= MIN_GAIN and keeps_text:
            keep = out.with_name(f"{out.stem} (marker){out.suffix}")
            out.rename(keep)
            probe.rename(out)
            swapped.append({"file": row.get("rel", str(out)),
                            "lost_marker": lost_marker, "lost_markitdown": lost_probe})
            log(f"  ЗАМЕНЕНО на markitdown: {row.get('rel', out.name)} "
                f"(marker терял {lost_marker} чисел, markitdown — {lost_probe}); "
                f"версия marker рядом как «{keep.name}»")
        elif lost_marker - lost_probe >= MIN_GAIN:
            # markitdown бы вернул числа, но отдал бы слишком много текста —
            # не подменяем, а зовём посмотреть: обычно правильный ответ здесь
            # «взять основной вариант и дописать в него выпавший кусок».
            log(f"  СПОРНО: {row.get('rel', out.name)} — marker терял "
                f"{lost_marker} чисел против {lost_probe} у markitdown, но текста "
                f"у markitdown лишь {cyr_probe / max(1, cyr_marker):.0%}. "
                f"Замер оставлен рядом как «{probe.name}», сверить руками")
        else:
            probe.unlink(missing_ok=True)   # marker не хуже — замер не нужен
    return swapped


# ---------------------------------------------------------------------------
# Страницы OCR: что читать глазами, что сверять, какой документ плох целиком
# ---------------------------------------------------------------------------

# Рукопись на печатном бланке Vision читает уверенно (бланк), и такие страницы уходили только в «сверить».
# Замер 15.10.2026 на 21 стр.: несловарных неподтверждённых имён на чистых сканах 0, на анкетах-рукописях 2 и 10,
# на странице-«разбеге» длинного документа 8, на плохом фото договора 1.
RARE_HAND = 2
SILENT = {"JSON_REPAIRED", "ROTATED", "DEWARPED"}   # технические детали, в список «прочитать глазами» не выносятся

# Петля и обрыв по лимиту токенов: «мягкие» — в «прочитать глазами» только если и второе чтение их подтверждает.
# На реальном деле 16.10.2026 из 11 таких страниц 4 были верны (договор 32, материал 53 и 58, доверенность 1): у всех
# четырёх conf Vision 0,45–0,96 и пропусков 0–1, у всех семи испорченных conf < 0,45 или пропусков ≥ 2.
SOFT_LOOP = {"LOOP_DETECTED", "LOOP_DETECTED_2", "MAX_TOKENS_HIT", "MAX_TOKENS_HIT_2"}
# Сбой движка: текста страницы нет или он неполон безусловно.
EMPTY_PAGE_CHARS = 20     # меньше букв и цифр в разделе «## Страница N» — страница пустая
HARD_FAIL = {"TIMEOUT", "LAYOUT_ERROR", "SECOND_LOOP", "SECOND_ERROR", "SECOND_TIMEOUT", "ERROR"}
EYES = SOFT_LOOP | HARD_FAIL      # прежнее имя: все предупреждения «OCR оборвался или зациклился»

LOOP_CONF = 0.45          # ниже — Vision сам читает плохо, петля не ложная
GAPS_MISSING = 2          # Vision видит ≥ 2 настоящих строк, которых нет в тексте (было 5; B2: ≥ 2 даёт 44 стр., 43 плохих)
LOW_CONF = 0.40           # conf Vision ниже при ≥ LOW_CONF_LINES строк — страница не читается (две пустые страницы дали 0,30 при 1–3 строках)
LOW_CONF_LINES = 10
LOW_CRED = 0.6            # доля подтверждённых настоящих слов среди оценённых строк Vision
LOW_CRED_SCORED = 10
# Синие чернила (ink.blue_px из page_features.py: пиксели на A4 при 60 dpi, поля 5 % не считаются).
# САМОСТОЯТЕЛЬНЫМ признаком рукописи он быть не может: на 61 странице материала проверки порог 15 отделяет 24 страницы с рукописью
# (от 19 до тысяч) от 37 без неё (не больше 12), но на другом реальном деле «синие» 120 страниц из 130 (подписи и синие печати
# договоров, штампы ЭЦП, скриншоты с синим интерфейсом), у снимков помещения тоже сотни и тысячи. Поэтому синий учитывается
# только в документе, где рукопись уже видна по чтению Vision (BLUE_DOC_FRAC страниц и не меньше BLUE_DOC_MIN
# помечены «вероятно рукопись» по другим признакам): там страницы с синим (≥ BLUE_HAND_PX) тоже идут глазами.
BLUE_HAND_PX = 15
BLUE_DOC_FRAC = 0.30
BLUE_DOC_MIN = 3
# «OCR не короче Vision»: символов (букв и цифр) в тексте страницы / в чтении Vision. B2: ≥ 1,0 у 20 страниц, 18 с выдумкой.
LEN_RATIO = 1.0
USE_LEN = False           # включается только если даёт пойманные плохие без лишних ложных (замер автора)
LEN_MIN_VISION_LINES = 10

# Документ плохого качества: доля страниц, прочитанных с провалами (рукопись сама по себе не считается)
BAD_DOC_FRAC = 0.30
BAD_DOC_MIN = 3

# Повторные снимки одного листа: сходство множеств словесных 3-грамм (Жаккар) по тексту OCR страницы
# и по чтению Vision (берётся большее)
SAME_SHEET_SIM = 0.5      # кандидаты в «возможно, снимки одного листа»
SAME_SHEET_SURE = 0.55    # уверенно: сходство ≥ 0,55, на каждой странице ≥ 3 числа, и числа совпадают не меньше чем наполовину
SAME_SHEET_NUM_JAC = 0.5
SAME_SHEET_MIN_NUMS = 3
SAME_SHEET_MIN_TRI = 30   # меньше 3-грамм — страница слишком короткая для сравнения

KIND_LABEL = {
    "loop": "OCR зациклился или оборвался",
    "hard": "сбой движка (таймаут, ошибка)",
    "vfb": "движки не дали текста (резервное чтение Vision)",
    "gaps": "пропущены строки текста",
    "reread": "текст дописан по областям (в замере всегда выдумка)",
    "invent": "выдуманный текст (иероглифы, английские фразы)",
    "second": "понадобился запасной движок",
}

PAGE_RE = re.compile(r"(?m)^## Страница (\d+)\s*$")
TRAILER_RE = re.compile(r"(?m)^---\nРаспознано:")
SERVICE_QUOTE = ("> ⚠", "> ✎", "> Примечание")


def split_pages(md: str) -> dict[int, str]:
    """Разделы `## Страница N` → текст страницы (без концовки «Распознано: …»)."""
    heads = list(PAGE_RE.finditer(md))
    out: dict[int, str] = {}
    if not heads:
        # документ из одного снимка (фото, скриншот): `.md` без разделов «## Страница» — вся страница под заголовком
        t = TRAILER_RE.search(md)
        body = md[:t.start()] if t else md
        return {1: re.sub(r"\A\s*# [^\n]*\n", "", body)}
    for i, m in enumerate(heads):
        if i + 1 < len(heads):
            end = heads[i + 1].start()
        else:
            t = TRAILER_RE.search(md, m.end())
            end = t.start() if t else len(md)
        out[int(m.group(1))] = md[m.end():end]
    return out


def clean_page(t: str) -> str:
    """Текст страницы без служебных пометок движка, HTML-разметки и ссылок на картинки."""
    t = HTML_COMMENT.sub("", t)
    t = "\n".join(l for l in t.splitlines()
                  if not l.lstrip().startswith(SERVICE_QUOTE) and not l.lstrip().startswith("‹QR›"))
    t = IMG_LINK.sub(" ", t).replace("‹колонтитул›", " ")
    return HTML_TAG.sub(" ", t)


def _alnum(s: str) -> int:
    return len(re.findall(r"[0-9A-Za-zА-Яа-яЁё]", s))


def _flat(s) -> str:
    """Для поиска числа или имени в тексте: нижний регистр, ё→е, без пробелов, дефисов и знаков."""
    return re.sub(r"[^0-9a-zа-я.,/:]+", "", str(s).lower().replace("ё", "е"))


def classify_page(ps: dict, text: str, russian: bool) -> dict:
    """Что с одной страницей: `eyes` — прочитать глазами по признакам плохого текста, `hand` — вероятно рукопись
    или плохое фото (тоже глазами), `verify` — сверить по оригиналу, `second`, `rotated`.
    Причины — списки (вид, сообщение); `bad` — виды причин, говорящие о плохой картинке (не о рукописи)."""
    w = ps.get("warnings") or []
    keys = {x.split(":")[0] for x in w}
    if ps.get("mode") == "empty" or "NO_TEXT_SKIPPED" in keys:
        # снимок без текста, OCR пропущен движком: не «почти пусто» и не «прочитать глазами» (см. раздел «пропущено без OCR»)
        return {"eyes": [], "hand": [], "verify": [], "second": None, "rotated": ps.get("rotation") or 0,
                "bad": set(), "empty": True}
    cc = ps.get("crosscheck") or {}
    cov = ps.get("coverage") or {}
    ok_cc = bool(cc) and not cc.get("error")
    conf = cc.get("vision_conf") if ok_cc else None
    vlines = (cc.get("vision_lines") or 0) if ok_cc else 0
    measured = bool(cov)   # покрытие не измерено (прогон с --no-reread, REREAD_ERROR): coverage null
    missing = (cov.get("missing") or 0) if cov else 0
    eyes: list[tuple[str, str]] = []
    hand: list[tuple[str, str]] = []
    verify: list[str] = []

    # 1. петли, обрывы, сбои
    hard = [x for x in w if x.split(":")[0] in HARD_FAIL]
    soft = [x for x in w if x.split(":")[0] in SOFT_LOOP]
    if hard:
        eyes.append(("hard", ", ".join(hard)))
    if soft:
        label = ", ".join(soft)
        if conf is None:
            eyes.append(("loop", f"{label}; второго чтения нет, проверить нечем"))
        elif conf < LOOP_CONF:
            eyes.append(("loop", f"{label}; второе чтение тоже неуверенное (conf {conf})"))
        elif not measured:
            eyes.append(("loop", f"{label}; покрытие второго чтения не измерено — проверить нечем"))
        elif missing >= GAPS_MISSING:
            eyes.append(("loop", f"{label}; второе чтение видит строки, которых нет в тексте (их {missing})"))
        else:
            verify.append(f"{label}, но второе чтение уверено (conf {conf}) и пропусков {missing} — вероятно ложная тревога, "
                          "сверить имена и числа")
    if "VISION_FALLBACK" in keys:
        eyes.append(("vfb", "движки не дали полного текста, в конце страницы резервное чтение Apple Vision — сверить с картинкой"))

    # 2. пропуски, дочитывание (если страницу ещё и «дочитали» — одна запись: блок в конце страницы — тоже выдумка)
    ml = cov.get("missing_lines") or []
    gap_msg = f"второе чтение видит строки, которых нет в тексте (их {missing})" + (f"; напр.: {'; '.join(ml[:2])}" if ml else "")
    if "REREAD" in keys:
        n = next((x.split(":")[1] for x in w if x.startswith("REREAD:")), "?")
        eyes.append(("reread", f"дочитано областей — {n}: блок в конце страницы — выдумка (11 из 11 в замере), в тексте ему не верить"
                     + (f"; {gap_msg}" if missing >= GAPS_MISSING else "")))
    elif missing >= GAPS_MISSING:
        eyes.append(("gaps", gap_msg + ", дочитать не удалось"))

    # 3. выдумка в тексте страницы
    cj = HIEROGLYPHS.findall(text)
    if cj:
        eyes.append(("invent", f"иероглифы/кана/хангыль в тексте ({len(cj)} симв.: {''.join(dict.fromkeys(cj))[:8]})"))
    if russian:
        hits = sorted(set(INVENTED.findall(text)))
        if hits:
            eyes.append(("invent", f"английская «официальщина»: {', '.join(hits[:4])}"))
        ph = [] if is_bilingual(text) else latin_phrases(text)   # двуязычная страница: английский столбец — не выдумка
        if ph:
            eyes.append(("invent", f"английская фраза в русском тексте: «{ph[0][:70]}»" + (f" (и ещё {len(ph) - 1})" if len(ph) > 1 else "")))

    # 4. вероятно рукопись или плохое фото
    names, nums = cc.get("names_unconfirmed") or [], cc.get("numbers_unconfirmed") or []
    if ok_cc:
        if not cc.get("reliable") and (len(names) >= 3 or (ps.get("gen_tokens", 0) < 400 and vlines >= 10)):
            hand.append(("hand", f"второе чтение неуверенное (conf {conf}), непроверенных имён {len(names)} — похоже на рукопись или плохое фото"))
        elif len(cc.get("names_rare") or []) >= RARE_HAND:
            rare = cc["names_rare"]
            hand.append(("hand", f"второе чтение не подтвердило {len(rare)} несловарных имён "
                         f"({', '.join(rare[:5])}{' …' if len(rare) > 5 else ''}) — похоже на рукопись на бланке или плохое фото"))
        if conf is not None and conf < LOW_CONF and vlines >= LOW_CONF_LINES and not any(k == "hand" for k, _ in hand):
            hand.append(("conf", f"второе чтение почти не читает страницу (conf {conf} при {vlines} строках)"))
    scored = (cov.get("scored") or 0) if cov else 0
    if scored >= LOW_CRED_SCORED and (cov.get("credible") or 0) / scored < LOW_CRED:
        hand.append(("cred", f"из {scored} строк второго чтения настоящие слова лишь в {cov.get('credible') or 0} — "
                     "рукопись, печать, кракозябры или плохое фото"))
    if USE_LEN and ok_cc and vlines >= LEN_MIN_VISION_LINES:
        v = ps.get("vision_text")
        va = _alnum(v) if v else (cc.get("vision_chars") or 0) * 0.85
        if va and _alnum(text) / va >= LEN_RATIO:
            hand.append(("len", f"OCR не короче второго чтения ({_alnum(text)} против {int(va)} знаков) — признак выдумки"))

    # 5. сверить имена и числа (только если страницу не читать глазами). Число или имя, которого в тексте
    # страницы уже нет (исправлено по картинке), не перечислять: иначе исправленное «висит» в списке
    flat = _flat(text)
    names = [x for x in names if _flat(x) in flat]
    nums = [x for x in nums if _flat(x) in flat]
    if not eyes and not hand and ok_cc and (names or nums):
        verify.append("; ".join(
            ([f"имена — {', '.join(names[:10])}" + (" …" if len(names) > 10 else "")] if names else [])
            + ([f"числа — {', '.join(nums[:10])}" + (" …" if len(nums) > 10 else "")] if nums else [])))
    # сбой самой сверки: без этой строки страница выглядела бы проверенной («замечаний нет»)
    if not eyes and not hand and cc.get("error"):
        verify.append(f"сверка вторым чтением не выполнена ({str(cc['error'])[:80]}) — "
                      "имена и числа не проверены, сверить по оригиналу")

    bad = {k for k, _ in eyes}
    if missing >= GAPS_MISSING:
        bad.add("gaps")
    second = "SECOND" in keys
    if second:
        bad.add("second")
    return {"eyes": eyes, "hand": hand, "verify": verify, "second": ps.get("engine") if second else None,
            "rotated": ps.get("rotation") or 0, "bad": bad, "empty": False}


# -- повторные снимки одного листа --

def _tri(text: str) -> set:
    ws = re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е"))
    return set(zip(ws, ws[1:], ws[2:]))


def _jac(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def number_set(text: str) -> set[str]:
    return {re.sub(r"[  ]", "", m) for m in NUM.findall(text)}


def same_sheet_groups(info: dict[int, dict]) -> list[dict]:
    """Группы страниц одного документа, похожих как повторные фото одного листа.

    `info[page]` = {"text": текст OCR, "vision": чтение Vision, "sharp": резкость или None}.
    Агломерация по полной связи: страница входит в группу, только если похожа на каждую из неё (≥ SAME_SHEET_SIM).
    Для каждой группы — `sure`: уверенно ли это один лист (см. SAME_SHEET_SURE).
    Одной текстовой схожести мало: договоры одного арендодателя на одном шаблоне дают 0,5–0,65 между РАЗНЫМИ листами
    (реальное дело: стр. 6 и 38, 44 и 50, 37 и 48, анкеты-протоколы разных лиц в материале проверки), поэтому уверенными
    считаются только пары с числами (реквизиты, суммы, даты), совпадающими наполовину: у разных договоров и разных лиц
    числа отличаются, у двух снимков одного листа — нет (кроме ошибок OCR). Листы без чисел уверенными не бывают.
    На изогнутых фото, где Vision читает на 0,3 (conf), а OCR зацикливается, сходство падает до 0,0–0,3 даже у одного
    листа (стр. 2/11, 29/33, 30/34 «договоров»): такие пары не находятся, страницы читаются все."""
    tri_o = {p: _tri(v["text"]) for p, v in info.items()}
    tri_v = {p: _tri(v.get("vision") or "") for p, v in info.items()}
    pages = sorted(p for p in info if len(tri_o[p]) >= SAME_SHEET_MIN_TRI or len(tri_v[p]) >= SAME_SHEET_MIN_TRI)
    sims: dict[tuple[int, int], float] = {}
    for a, b in itertools.combinations(pages, 2):
        s = 0.0
        for T in (tri_o, tri_v):
            x, y = T[a], T[b]
            if len(x) >= SAME_SHEET_MIN_TRI and len(y) >= SAME_SHEET_MIN_TRI and min(len(x), len(y)) >= SAME_SHEET_SIM * max(len(x), len(y)):
                s = max(s, _jac(x, y))
        if s >= SAME_SHEET_SIM:
            sims[(a, b)] = s

    def sim(a: int, b: int) -> float:
        return sims.get((a, b) if a < b else (b, a), 0.0)

    groups: list[set] = []
    where: dict[int, int] = {}
    for (a, b), _s in sorted(sims.items(), key=lambda kv: -kv[1]):
        ga, gb = where.get(a), where.get(b)
        if ga is None and gb is None:
            groups.append({a, b})
            where[a] = where[b] = len(groups) - 1
        elif ga is not None and gb is None:
            if all(sim(x, b) >= SAME_SHEET_SIM for x in groups[ga]):
                groups[ga].add(b)
                where[b] = ga
        elif ga is None and gb is not None:
            if all(sim(x, a) >= SAME_SHEET_SIM for x in groups[gb]):
                groups[gb].add(a)
                where[a] = gb
        elif ga != gb:
            if all(sim(x, y) >= SAME_SHEET_SIM for x in groups[ga] for y in groups[gb]):
                groups[ga] |= groups[gb]
                for p in groups[gb]:
                    where[p] = ga
                groups[gb] = set()
    nums = {p: number_set(info[p]["text"]) for p in pages}
    out = []
    for g in groups:
        if len(g) < 2:
            continue
        ps_ = sorted(g)
        pair = [(a, b) for a, b in itertools.combinations(ps_, 2)]
        smin = min(sim(a, b) for a, b in pair)
        sure = all(
            min(len(nums[a]), len(nums[b])) >= SAME_SHEET_MIN_NUMS
            and sim(a, b) >= SAME_SHEET_SURE and _jac(nums[a], nums[b]) >= SAME_SHEET_NUM_JAC
            for a, b in pair)
        sharp = {p: info[p].get("sharp") for p in ps_}
        ref = max(ps_, key=lambda p: sharp[p]) if all(v is not None for v in sharp.values()) else None
        out.append({"pages": ps_, "sim_min": round(smin, 2), "sim_max": round(max(sim(a, b) for a, b in pair), 2),
                    "sure": sure, "ref": ref, "sharp": sharp})
    return sorted(out, key=lambda g: g["pages"][0])


def _present(text: str, vision: str) -> set[str]:
    return number_set(text) | number_set(vision or "")


def number_diff(a: int, b: int, info: dict[int, dict]) -> tuple[list[str], list[str]]:
    """Числа ≥ 4 цифр: есть в тексте снимка a и нет в тексте и чтении Vision снимка b — и наоборот."""
    na, nb = number_set(info[a]["text"]), number_set(info[b]["text"])
    pa, pb = _present(info[a]["text"], info[a].get("vision")), _present(info[b]["text"], info[b].get("vision"))
    return sorted(na - pb), sorted(nb - pa)


def doc_quality(cls: dict[int, dict], total: int) -> dict | None:
    """«Плохое качество»: доля страниц, прочитанных с провалами (петли, пропуски, дочитывание, выдумка, запасной
    движок), не меньше BAD_DOC_FRAC и не меньше BAD_DOC_MIN страниц. Рукопись сама по себе качеством не считается."""
    bad = sorted(p for p, c in cls.items() if c["bad"])
    if not total or len(bad) < BAD_DOC_MIN or len(bad) / total < BAD_DOC_FRAC:
        return None
    cnt: Counter = Counter()
    for p in bad:
        for k in cls[p]["bad"]:
            cnt[k] += 1
    parts = [f"{KIND_LABEL[k]} — {n}" for k, n in cnt.most_common()]
    why = f"{len(bad)} из {total} страниц ({round(100 * len(bad) / total)} %) прочитаны с провалами ({'; '.join(parts)})"
    return {"bad": True, "why": why, "pages": bad}


def analyze_doc(d: dict, md_text: str) -> dict:
    """Разбор одного документа по `_ocr.json` (d) и тексту его .md. Ничего не пишет."""
    pages_txt = {n: clean_page(t) for n, t in split_pages(md_text).items()}
    russian = is_russian(md_text)
    stats = d.get("page_stats") or []
    cls: dict[int, dict] = {}
    for ps in stats:
        n = ps.get("page")
        cls[n] = classify_page(ps, pages_txt.get(n, ""), russian)
    # синие чернила — только в документе, где рукопись уже видна по чтению Vision (иначе подписи и печати дают сотни ложных)
    live = [ps for ps in stats if not cls[ps["page"]]["empty"]]
    vis_hand = [ps["page"] for ps in live if any(k in ("hand", "conf", "cred") for k, _ in cls[ps["page"]]["hand"])]
    blue_doc = bool(live) and len(vis_hand) >= max(BLUE_DOC_MIN, math.ceil(BLUE_DOC_FRAC * len(live)))
    if blue_doc:
        for ps in live:
            b = (ps.get("ink") or {}).get("blue_px")
            if b is not None and b >= BLUE_HAND_PX:
                c = cls[ps["page"]]
                c["hand"].append(("blue", f"синие чернила (blue_px {b}) — в этом документе рукопись видна и по чтению Vision, страница, вероятно, с рукописными вставками"))
                # пропуски, дочитывание и выдумка на странице с рукописью — следствие рукописи, а не плохой картинки
                c["bad"] &= {"loop", "hard", "vfb", "second"}
    info = {ps["page"]: {"text": pages_txt.get(ps["page"], ""), "vision": ps.get("vision_text") or "",
                         "sharp": ps.get("sharp")} for ps in live}
    groups = same_sheet_groups(info) if len(live) >= 2 else []
    # самый резкий снимок остаётся на чтении глазами, остальные уверенной группы — «сверить с ним»
    demoted: dict[int, int] = {}
    read = {n for n, c in cls.items() if c["eyes"] or c["hand"]}
    eyed = {ps["page"] for ps in stats if ps.get("eyes")}
    empty = [ps for ps in stats if cls[ps["page"]]["empty"]]
    for g in groups:
        if not g["sure"] or g["ref"] is None:
            continue
        for p in g["pages"]:
            if p != g["ref"] and p in read and p not in eyed:
                demoted[p] = g["ref"]
    return {"cls": cls, "info": info, "groups": groups, "demoted": demoted, "eyed": eyed, "empty": empty,
            "quality": doc_quality(cls, len(live)), "total": len(stats), "blue_doc": blue_doc}


def _pages_str(ps: list[int]) -> str:
    return ", ".join(str(p) for p in sorted(ps))


def doc_block(md_rel: str, d: dict, an: dict) -> tuple[list[str], dict]:
    """Строки раздела `### файл` в _ПРОВЕРИТЬ.md и счётчики для сводки."""
    cls, demoted, eyed = an["cls"], an["demoted"], an["eyed"]
    stats = {ps["page"]: ps for ps in d.get("page_stats", [])}
    cnt = {"eyes": 0, "second": 0, "rotated": 0, "verify": 0, "handwriting": 0, "reread": 0, "gaps": 0,
           "dewarped": 0, "qr": 0, "demoted": len(demoted), "empty": len(an["empty"])}
    sec_loop, sec_gap, sec_hand, sec_second, sec_verify, sec_rot, sec_empty = [], [], [], [], [], [], []
    read_pages: list[int] = []
    for n in sorted(cls):
        c, ps = cls[n], stats.get(n, {})
        if c.get("empty"):
            vt = " ".join((ps.get("vision_text") or "").split())[:80]
            sec_empty.append(f"стр. {n}" + (f" (Apple Vision прочитал: «{vt}»)" if vt else ""))
            continue
        if ps.get("dewarp"):
            cnt["dewarped"] += 1
        if ps.get("qr"):
            cnt["qr"] += 1
        if n in eyed:
            continue                      # прочитана глазами и вставлена в .md (merge_eyes.py)
        if c["rotated"]:
            sec_rot.append(f"стр. {n}: на {c['rotated']}°")
        if n in demoted:
            continue                      # повторный снимок: в разделе «Снимки одного листа»
        if c["eyes"] or c["hand"]:
            read_pages.append(n)
        for kind, msg in c["eyes"]:
            if kind in ("loop", "hard", "vfb"):
                sec_loop.append(f"стр. {n}: {msg}")
            else:
                sec_gap.append(f"стр. {n}: {msg}")
                if kind == "reread":
                    cnt["reread"] += 1
                elif kind == "gaps":
                    cnt["gaps"] += 1
        if c["hand"]:
            sec_hand.append(f"стр. {n}: " + "; ".join(m for _, m in c["hand"]))
        if c["second"]:
            sec_second.append(f"стр. {n}: {c['second']}")
        for v in c["verify"]:
            sec_verify.append(f"стр. {n}: {v}")
    cnt["eyes"] = len({n for n in read_pages if cls[n]["eyes"]})
    cnt["handwriting"] = len({n for n in read_pages if cls[n]["hand"] and not cls[n]["eyes"]})
    cnt["second"], cnt["verify"], cnt["rotated"] = len(sec_second), len(sec_verify), len(sec_rot)

    lines: list[str] = []
    if eyed:
        lines.append(f"Прочитано глазами и вставлено в .md (помечено ✎, сверять фамилии и числа): стр. {_pages_str(list(eyed))}")
    if read_pages:
        lines.append(f"**Прочитать глазами — {len(read_pages)} стр.:** {_pages_str(read_pages)}")
    if sec_loop:
        lines.append("**Прочитать глазами (OCR оборвался или зациклился):**")
        lines += [f"- {x}" for x in sec_loop]
    if sec_gap:
        lines.append("**Прочитать глазами (пропущены строки, дочитывание или выдумка):**")
        lines += [f"- {x}" for x in sec_gap]
    if sec_hand:
        lines.append("**Вероятно рукопись или плохое фото — прочитать глазами:**")
        lines += [f"- {x}" for x in sec_hand]
    if sec_second:
        lines.append("**Прочитано запасным движком (сверить порядок и таблицы):**")
        lines += [f"- {x}" for x in sec_second]
    if an["groups"]:
        lines += same_sheet_lines(an, d)
    if sec_empty:
        lines.append("**Пропущено без OCR (на снимке нет текста; нужен ли снимок как доказательство — решать человеку):** "
                     + "; ".join(sec_empty))
    if sec_verify:
        lines.append("**Не подтверждены вторым чтением — сверить по оригиналу:**")
        lines += [f"- {x}" for x in sec_verify]
    if sec_rot:
        lines.append("Повёрнуто перед OCR: " + "; ".join(sec_rot))
    return lines, cnt


def _sim_str(g: dict) -> str:
    return f"{g['sim_min']}" if g["sim_min"] == g["sim_max"] else f"{g['sim_min']}–{g['sim_max']}"


def same_sheet_lines(an: dict, d: dict) -> list[str]:
    info, cls, demoted, eyed = an["info"], an["cls"], an["demoted"], an["eyed"]

    def relevant(g: dict) -> bool:
        # группа интересна, только если в ней есть что читать глазами или что снято с чтения
        return any(p in demoted or (p not in eyed and (cls[p]["eyes"] or cls[p]["hand"])) for p in g["pages"])

    sure = [g for g in an["groups"] if g["sure"] and relevant(g)]
    maybe = [g for g in an["groups"] if not g["sure"] and relevant(g)]
    out: list[str] = []
    if sure:
        out.append("**Снимки одного листа (повторные фото одной страницы; на чтение глазами идёт самый резкий):**")
        for g in sure:
            pg = g["pages"]
            sharp = ", ".join(f"стр. {p} — {g['sharp'][p]:g}" for p in pg) if g["ref"] is not None else "резкость не измерена (page_features.py)"
            if g["ref"] is None:
                out.append(f"- стр. {_pages_str(pg)} (сходство {_sim_str(g)}): {sharp}; читать глазами все, у кого есть пометка")
                continue
            ref = g["ref"]
            others = [p for p in pg if p != ref]
            if ref in an["eyed"]:
                tail = f"стр. {ref} уже прочитана глазами; остальные сверить с ней"
            elif cls[ref]["eyes"] or cls[ref]["hand"]:
                tail = f"читать глазами стр. {ref}, остальные сверить с ней"
            else:
                tail = f"стр. {ref} без пометок читать не нужно; остальные сверить с ней"
            out.append(f"- стр. {_pages_str(pg)} (сходство {_sim_str(g)}); резкость: {sharp}; {tail}")
            for p in others:
                only_ref, only_p = number_diff(ref, p, info)
                if only_ref or only_p:
                    out.append(f"  - числа стр. {p} и стр. {ref} расходятся: только на стр. {p} — {', '.join(only_p[:8]) or '—'}; "
                               f"только на стр. {ref} — {', '.join(only_ref[:8]) or '—'}")
    if maybe:
        out.append("**Возможно, снимки одного листа (не подтверждено: шаблонные страницы разных договоров похожи так же) — читать оба:**")
        for g in maybe:
            pg = g["pages"]
            sh = ""
            if all(g["sharp"][p] is not None for p in pg):
                ref = max(pg, key=lambda p: g["sharp"][p])
                sh = f"; резче стр. {ref}"
            out.append(f"- стр. {_pages_str(pg)} (сходство {_sim_str(g)}{sh})")
    return out


def review_docs(root: Path, write_json: bool = True) -> tuple[dict, dict]:
    """По каждому `_ocr.json` под root: разбор, блок для _ПРОВЕРИТЬ.md, поле `quality` в json.
    Возвращает ({md_rel: {"lines", "quality", "md_text"}}, сводка)."""
    docs: dict = {}
    summary = {"files": 0, "pages": 0, "eyes": 0, "second": 0, "rotated": 0, "verify": 0, "handwriting": 0,
               "reread": 0, "gaps": 0, "dewarped": 0, "qr": 0, "demoted": 0, "bad_docs": 0, "empty": 0}
    for js in sorted(root.rglob("*_ocr.json")):
        try:
            d = json.loads(js.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        md = md_for_json(js)
        md_text = md.read_text(encoding="utf-8", errors="replace") if md else ""
        md_rel = str((md or js.with_name(js.name.replace("_ocr.json", ".md"))).relative_to(root))
        an = analyze_doc(d, md_text)
        lines, cnt = doc_block(md_rel, d, an)
        summary["files"] += 1
        summary["pages"] += an["total"]
        for k, v in cnt.items():
            summary[k] += v
        q = an["quality"]
        if q:
            summary["bad_docs"] += 1
        if write_json and d.get("quality") != q:
            if q:
                d["quality"] = q
            else:
                d.pop("quality", None)
            js.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        docs[md_rel] = {"lines": lines, "quality": q, "md_text": md_text, "has_json": True}
    return docs, summary


def ocr_review(root: Path) -> tuple[list[str], dict]:
    """Прежний интерфейс: (строки отчёта по OCR-документам, сводка)."""
    docs, summary = review_docs(root, write_json=False)
    out: list[str] = []
    for rel, v in docs.items():
        if v["lines"]:
            out += [f"### {rel}"] + v["lines"] + [""]
    return out, summary


def requisites_lines(md_text: str) -> list[str]:
    """Находки requisites.check_md для одного файла (включая страницы, прочитанные глазами)."""
    if requisites is None:
        return []
    try:
        found = requisites.check_md(md_text)
    except Exception as e:  # noqa: BLE001
        return [f"проверка реквизитов упала: {type(e).__name__}: {e}"]
    out = []
    for f in found[:15]:
        page = f"стр. {f['page']}" if f.get("page") is not None else "без страницы"
        out.append(f"{page}: {f.get('kind', '')} {f.get('value', '')} — {f.get('problem', '')}".replace("  ", " "))
    if len(found) > 15:
        out.append(f"… и ещё {len(found) - 15} (python3 requisites.py <файл>)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--json", default=None)
    ap.add_argument("--plan", default=None,
                    help="plan.json от route.py: сверить цифры и выбрать лучший движок")
    ap.add_argument("--no-write-json", action="store_true",
                    help="не писать поле quality в _ocr.json (пробный прогон)")
    a = ap.parse_args()

    root = Path(a.root).resolve()
    if not root.exists():
        print(f"нет пути: {root}", file=sys.stderr)
        return 2

    swapped = []
    if a.plan:
        print("Сверка цифр с оригиналами (marker против markitdown):")
        swapped = compare_engines(Path(a.plan))
        print(f"  заменено файлов: {len(swapped)}\n" if swapped else "  замен не потребовалось\n")

    mds = sorted(p for p in root.rglob("*.md")
                 if not p.name.startswith("_") and not p.name.endswith(".markitdown.md")
                 and "_ПРОВЕРИТЬ" not in p.name)
    problems: list[dict] = []
    for md in mds:
        flags = look(md)
        if flags:
            problems.append({"file": str(md.relative_to(root)), "flags": flags})

    print(f"Проверено .md: {len(mds)}   с замечаниями: {len(problems)}\n")
    for p in problems:
        print(f"  {p['file']}")
        for fl in p["flags"]:
            print(f"      • {fl}")

    docs, summ = review_docs(root, write_json=not a.no_write_json)

    # реквизиты — по каждому файлу, в том числе прочитанному глазами
    req: dict[str, list[str]] = {}
    for md in mds:
        rel = str(md.relative_to(root))
        text = docs[rel]["md_text"] if rel in docs else md.read_text(encoding="utf-8", errors="replace")
        r = requisites_lines(text)
        if r:
            req[rel] = r

    # файловые замечания: у OCR-документов иероглифы и английские фразы названы постранично, дубль не нужен
    file_flags: list[tuple[str, list[str]]] = []
    for p in problems:
        fl = p["flags"]
        if p["file"] in docs:
            fl = [x for x in fl if not x.startswith(("ИЕРОГЛИФЫ", "ВОЗМОЖНО ВЫДУМАНО"))]
        if fl:
            file_flags.append((p["file"], fl))

    bad_docs = [(rel, v["quality"]) for rel, v in docs.items() if v["quality"]]
    block_files = sorted(set(rel for rel, v in docs.items() if v["lines"]) | set(req))
    if summ["files"] or file_flags or req:
        print(f"\nСтатистика OCR: файлов {summ['files']}, страниц {summ['pages']}; "
              f"читать глазами {summ['eyes'] + summ['handwriting']}, запасным движком {summ['second']}, "
              f"сверить имена/числа {summ['verify']}, повторных снимков (сверить с резким) {summ['demoted']}, "
              f"без OCR (снимки без текста) {summ['empty']}, выпрямлено {summ['dewarped']}, QR {summ['qr']}; "
              f"документов плохого качества {summ['bad_docs']}")
        head = ["# Что проверить человеку после OCR", "",
                f"Корень: `{root}`. Страниц с OCR: {summ['pages']} в {summ['files']} файлах.",
                "Разделы: «прочитать глазами» — OCR оборвался или зациклился, пропустил строки, выдумал текст (иероглифы, английские фразы) "
                "либо страница похожа на рукопись или плохое фото (синие чернила, второе чтение её не читает); "
                "«запасной движок» — страница прочитана другой моделью, сверить порядок и таблицы; "
                "«снимки одного листа» — повторные фото страницы: читать глазами самый резкий, остальные сверить с ним; "
                "«сверить» — имена и числа, которых не увидело независимое второе чтение (Apple Vision): это не ошибка, а список первоочередной сверки; "
                "«реквизиты не сходятся» — контрольные суммы ИНН/ОГРН/счетов, сумма цифрами и прописью, дроби и проценты. "
                "Ни одну цифру, фамилию или цитату из OCR нельзя ставить в документ без сверки с оригиналом.", ""]
        body: list[str] = []
        if bad_docs:
            body += ["## ДОКУМЕНТЫ ПЛОХОГО КАЧЕСТВА", "",
                     "Исходник такой, что OCR на нём систематически сбивается: читать из него цитаты нельзя, а всё важное "
                     "(фамилии, числа, даты, пункты) надо сверять с оригиналом. При выдаче такие .md получат суффикс "
                     f"«{BAD_SUFFIX.strip()}» и предупреждением в начале файла.", ""]
            for rel, q in bad_docs:
                body.append(f"- `{rel}` — {q['why']}. Страницы: {_pages_str(q['pages'])}")
            body.append("")
        if file_flags:
            body += ["## Файлы с замечаниями (пусто, только картинки, кракозябры, смешанные алфавиты)", ""]
            for rel, fl in file_flags:
                body.append(f"### {rel}")
                body += [f"- {x}" for x in fl]
                body.append("")
        if block_files:
            body += ["## По файлам", ""]
            for rel in block_files:
                body.append(f"### {rel}")
                if rel in docs:
                    body += docs[rel]["lines"]
                if rel in req:
                    body.append("**Реквизиты не сходятся (проверить по оригиналу):**")
                    body += [f"- {x}" for x in req[rel]]
                body.append("")
        (root / "_ПРОВЕРИТЬ.md").write_text("\n".join(head + body) + "\n", encoding="utf-8")
        print(f"  список: {root / '_ПРОВЕРИТЬ.md'}")
        for l in body[:60]:
            print("  " + l)
        if len(body) > 60:
            print(f"  … ещё {len(body) - 60} строк в _ПРОВЕРИТЬ.md")

    stale = stale_meta(root)
    if stale:
        print(f"\nУстаревшие _meta.json ({len(stale)}) — .md переделан другим движком,"
              f"\nа метаданные остались от marker. Удалить, чтобы не вводили в заблуждение:")
        for m in stale:
            print(f"  {m.relative_to(root)}")

    if not problems and not stale and not req:
        print("Замечаний нет.")

    if a.json:
        Path(a.json).write_text(json.dumps(
            {"checked": len(mds), "problems": problems, "swapped": swapped,
             "stale_meta": [str(m.relative_to(root)) for m in stale],
             "bad_quality": [{"file": rel, **q} for rel, q in bad_docs],
             "requisites": req},
            ensure_ascii=False, indent=2))
        print(f"\nОтчёт: {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
