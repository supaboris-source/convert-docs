#!/usr/bin/env python3
"""Распознавание страниц через dots.mocr (mlx-vlm) на Apple Silicon.

Один модуль для пакетного `dots_batch.py` из скилла convert-docs и других запускающих программ.
Модель грузится один раз на процесс (4,4 ГБ весов, ~6–7 ГБ памяти), страницы по одной.

Почему dots.mocr: по буквам русского печатного текста это лучшая открытая модель
(MDPBench 2026, табл. 3: edit distance 0,030 — лучше PaddleOCR-VL и Qwen3-VL),
по разметке страницы — тоже (PageIoU 92,9). Проигрывает на фото с телефона
(−13…−15 пунктов, PureDocBench/MDPBench) и на таблицах; на рукописи, как все
документные OCR-модели, ненадёжна и может молча выдумать текст.

Чего модель НЕ гарантирует: как всякая VLM она иногда «исправляет» опечатки
оригинала и подменяет близкие слова. Цитаты и цифры сверять по оригиналу.

Устройство страницы (версия 15.10.2026, v3):
  0a. выпрямление: Apple Vision (dewarp.py) находит четырёхугольник листа на фото и
     Core Image натягивает его на прямоугольник — убирает трапецию и наклон строк;
  0. ориентация: Apple Vision (orientation.py) определяет, лежит ли страница боком
     или вверх ногами, и картинка поворачивается ДО распознавания;
  1. промпт разметки (JSON с блоками); генерация потоковая, каждые LOOP_CHECK_EVERY
     токенов хвост проверяется на зацикливание (сжимаемость + новизна n-грамм +
     точные повторы) — петля обрывается сразу, а не после 12 000 токенов;
  2. если разметка не собралась или зациклилась — второй промпт «просто текст»;
  3. если и он зациклился — второе мнение другой модели по очереди (SECOND_OPINIONS; по
     умолчанию Xiaomi-OCR-0 → HunyuanOCR-1.5 → GLM-OCR, что есть на машине), потому что
     петли у разных моделей случаются на разных страницах;
  4. всё, что осталось зациклённым, обрезается до первого повтора и помечается;
  5. полнота (coverage.py): строки, которые видит Apple Vision, а в тексте их нет (и это
     настоящие слова по орфографии, не рукопись), дочитываются по полосам страницы;
  6. если ни один движок не дал текста — резервное чтение Apple Vision (без разметки);
  7. QR-коды (barcodes.py) дописываются строками «‹QR› …».
  Перед всем этим — один проход Apple Vision по картинке, которая пойдёт в OCR (строки, уверенность,
  признаки page_features): он же кормит полноту, резервное чтение и сверку, второй раз Vision не
  запускается. Снимок без текста (Vision < 3 строк, ≤ 5 областей текста, не бумага, нет QR) в OCR
  не отдаётся: mode «empty», в .md — что прочитал Vision (замер 17.10.2026: 84 с впустую на кадр).
Так же устроен официальный `dots_ocr/parser.py` (разметка → текст), остальное — наше.
HunyuanOCR-1.5 как запасной движок идёт через llama.cpp (llama-mtmd-cli), не через mlx-vlm.
"""
from __future__ import annotations

import json
import math
import os
import re
import signal
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

try:
    import orientation  # соседний модуль (Apple Vision); без pyobjc — None
except Exception:  # noqa: BLE001
    orientation = None
try:
    import dewarp  # выпрямление перспективы фото (Vision + Core Image)
except Exception:  # noqa: BLE001
    dewarp = None
try:
    import coverage  # полнота по строкам Vision и дочитывание пропущенных областей
except Exception:  # noqa: BLE001
    coverage = None
try:
    import crosscheck  # резервное чтение Vision, когда движки не справились
except Exception:  # noqa: BLE001
    crosscheck = None
try:
    import barcodes  # QR-коды на странице
except Exception:  # noqa: BLE001
    barcodes = None
try:
    import page_features  # признаки страницы (синяя ручка, резкость) для _ocr.json
except Exception:  # noqa: BLE001
    page_features = None
try:
    import numpy as np
except Exception:  # noqa: BLE001
    np = None

MODEL_REPO = "mlx-community/dots.mocr-8bit"

# Сетка визуального энкодера: патч 14 × merge 2 = 28 px на токен.
IMAGE_FACTOR = 28
MIN_PIXELS = 3136
# Официальный предел 11 289 600 (3360×3360) даёт до 14 тыс. токенов на
# фото-страницу и минуты prefill. A4 при 200 DPI — 3,9 Мпикс, его предел не
# трогает; ужимаются только фото с телефона и большие развороты.
MAX_PIXELS = 2560 * 1840
RENDER_DPI = 200          # как в официальном парсере
# Нормальная страница — 1 000–1 900 токенов (Jina-OCR, 2609.03181), на боевых
# прогонах максимум без предупреждений 3 853. 6 000 — уже брак, не длинная страница
# (стр. 285 длинного документа: 8 000 токенов выдумки без петли стоили 100 с впустую).
MAX_TOKENS = 6000
PAGE_TIMEOUT = 300        # секунд на одну генерацию; каждая попытка отдельно
# локальные веса запасных движков; другое место — переменная CONVERT_DOCS_MODELS
MODELS_DIR = os.path.expanduser(os.environ.get("CONVERT_DOCS_MODELS", "~/models"))

# --- детектор петли на лету ---
LOOP_CHECK_EVERY = 96     # токенов между проверками хвоста
LOOP_TAIL = 3000          # символов хвоста для оценки
LOOP_ZLIB = 0.06          # zlib-коэффициент хвоста ниже — петля (норма 0,2–0,5; LightOnOCR берёт 0,13)
LOOP_NOVELTY = 0.05       # доля уникальных 12-грамм хвоста ниже — петля
ORIENT_MIN_SURE = 0.3     # уверенность детектора ориентации, ниже которой не крутим

# --- снимок без текста: в OCR не отдаётся (замер 17.10.2026, реальное дело) ---
# «Фото помещения»: 8 страниц по 84 с впустую (dots выдумывает текст по фото и ловит петлю).
# Vision на них: 0–2 строки, детектор областей текста 0–4. Рукопись (материал проверки стр. 19, 44):
# 61 и 29 строк, 20 и 15 областей; вывеска и скриншоты кадастра: 6–11 строк, 3–8 областей.
NOTEXT_LINES = 3          # Vision распознал меньше строк
NOTEXT_RECTS = 5          # детектор областей текста (VNDetectTextRectangles) нашёл не больше
# Доля «бумажных» пикселей (яркие, почти без цвета, близко к 90-му перцентилю яркости; картинка 700 px):
# снимки помещения 0,21–0,40, любая страница документа даже почти пустая (печать, штамп, подпись) 0,65–0,99.
# Бумажная страница в пропуск не идёт никогда: потерять строку подписи или штамп хуже, чем 10 секунд OCR.
# Условие «ink.blue_px < 150» из первой версии правила не годится для снимков: синего в самом кадре
# (стул, небо, краска) 900–10 000 пикселей на 7 из 8 страниц, а рукопись и так закрыта числом областей.
NOTEXT_PAPER = 0.5

PROMPT_LAYOUT = """Please output the layout information from the PDF image, including each layout element's bbox, its category, and the corresponding text content within the bbox.

1. Bbox format: [x1, y1, x2, y2]

2. Layout Categories: The possible categories are ['Caption', 'Footnote', 'Formula', 'List-item', 'Page-footer', 'Page-header', 'Picture', 'Section-header', 'Table', 'Text', 'Title'].

3. Text Extraction & Formatting Rules:
    - Picture: For the 'Picture' category, the text field should be omitted.
    - Formula: Format its text as LaTeX.
    - Table: Format its text as HTML.
    - All Others (Text, Title, etc.): Format their text as Markdown.

4. Constraints:
    - The output text must be the original text from the image, with no translation.
    - All layout elements must be sorted according to human reading order.

5. Final Output: The entire output must be a single JSON object.
"""
PROMPT_OCR = "Extract the text content from this image."

# Второе мнение: ключ → (репозиторий HF или локальный путь, промпт, repetition_penalty).
# Включается переменной DOTS_SECOND="glm,hunyuan15" или аргументом DotsEngine(second_opinions=[...]).
SECOND_OPINIONS = {
    # Xiaomi-OCR-0 (0,8B, Qwen3.5): в замере 15.10.2026 ни одной петли на 16 трудных
    # страницах, 4–18 с/стр, 2,5 ГБ, на фото под углом 0,77 (dots-разметка 0,75), без
    # латиницы в словах. Конвертируется сам: python -m mlx_vlm.convert --hf-path
    # SeerRay-Lab/Xiaomi-OCR-0 --mlx-path ~/models/Xiaomi-OCR-0-mlx-8bit -q --q-bits 8
    "xiaomi": (os.path.join(MODELS_DIR, "Xiaomi-OCR-0-mlx-8bit"),
               "Extract all information from the main body of the document image and represent it in markdown format, "
               "ignoring headers and footers. Tables should be expressed in OTSL format, formulas in the document should be "
               "represented using LATEX format, and the parsing should be organized according to the reading order.", None),
    "glm": ("mlx-community/GLM-OCR-bf16", "Text Recognition:", None),
    # WeVisDoc-4B (Qwen3-VL, Apache 2.0): фото под углом 0,80, но 50 с/стр, 7 ГБ и петли на
    # страницах боком; конвертируется только с правкой config (tie_word_embeddings=false).
    # Не в очереди по умолчанию — включать через DOTS_SECOND=xiaomi,wevisdoc.
    "wevisdoc": (os.path.join(MODELS_DIR, "WeVisDoc-4B-mlx-8bit"), "Convert this document image to Markdown.", None),
    # HunyuanOCR-1.5 (1B, Tencent): в mlx-vlm 0.7.8 не грузится, работает через llama.cpp
    # (brew install llama.cpp; GGUF в ~/models/HunyuanOCR-1.5-gguf, см. УСТАНОВКА.md).
    # Официальные параметры: temperature 0, repetition_penalty 1.08. Путь «llama:модель|mmproj».
    "hunyuan15": ("llama:" + os.path.join(MODELS_DIR, "HunyuanOCR-1.5-gguf/HunyuanOCR.Q8_0.gguf")
                  + "|" + os.path.join(MODELS_DIR, "HunyuanOCR-1.5-gguf/HunyuanOCR.mmproj-f16.gguf"),
                  "提取文档图片中正文的所有信息用markdown格式表示，其中页眉、页脚部分忽略，表格用html格式表达，文档中公式用latex格式表示，按照阅读顺序组织进行解析。", 1.08),
}

CATEGORIES = {"Caption", "Footnote", "Formula", "List-item", "Page-footer", "Page-header",
              "Picture", "Section-header", "Table", "Text", "Title"}


def model_path(repo: str = MODEL_REPO) -> str:
    """Папка снапшота модели в кэше HF (или переопределение через DOTS_MODEL)."""
    if repo == MODEL_REPO:
        override = os.environ.get("DOTS_MODEL")
        if override:
            return override
    if repo.startswith("/") or repo.startswith("~"):
        return os.path.expanduser(repo)
    from huggingface_hub import snapshot_download
    return snapshot_download(repo, local_files_only=True)


SECOND_FAMILY = {"glm": "mlx_vlm.models.glm_ocr", "hunyuan15": "mlx_vlm.models.hunyuan_vl", "xiaomi": "mlx_vlm.models.qwen3_5", "wevisdoc": "mlx_vlm.models.qwen3_vl"}


def llama_bin() -> str | None:
    """llama-mtmd-cli: из PATH, а под launchd (там PATH короткий, без /opt/homebrew/bin) — из каталогов Homebrew."""
    import shutil
    found = shutil.which("llama-mtmd-cli")
    if found:
        return found
    for p in ("/opt/homebrew/bin/llama-mtmd-cli", "/usr/local/bin/llama-mtmd-cli"):
        if os.path.exists(p):
            return p
    return None


def second_available(name: str) -> bool:
    """Веса в кэше И архитектура известна этой версии mlx-vlm (в 0.4.4 GLM-OCR нет);
    для «llama:…» — оба GGUF на месте и llama-mtmd-cli в PATH."""
    try:
        import importlib.util
        repo = SECOND_OPINIONS[name][0]
        if repo.startswith("llama:"):
            m, mm = repo[6:].split("|")
            return bool(llama_bin()) and os.path.exists(m) and os.path.exists(mm)
        fam = SECOND_FAMILY.get(name)
        if fam and importlib.util.find_spec(fam) is None:
            return False
        path = model_path(SECOND_OPINIONS[name][0])
        return bool(path) and os.path.exists(path)
    except Exception:  # noqa: BLE001
        return False


# --- размер картинки: копия smart_resize из dots_ocr/utils/image_utils.py ---

def _round(n, f): return round(n / f) * f
def _ceil(n, f): return math.ceil(n / f) * f
def _floor(n, f): return math.floor(n / f) * f


def smart_resize(height: int, width: int, factor: int = IMAGE_FACTOR,
                 min_pixels: int = MIN_PIXELS, max_pixels: int = MAX_PIXELS) -> tuple[int, int]:
    h = max(factor, _round(height, factor))
    w = max(factor, _round(width, factor))
    if h * w > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h = max(factor, _floor(height / beta, factor))
        w = max(factor, _floor(width / beta, factor))
    elif h * w < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h = _ceil(height * beta, factor)
        w = _ceil(width * beta, factor)
    return h, w


def fit_image(img: Image.Image, max_pixels: int = MAX_PIXELS) -> Image.Image:
    img = img.convert("RGB")
    h, w = smart_resize(img.height, img.width, max_pixels=max_pixels)
    if (w, h) != img.size:
        img = img.resize((w, h), Image.Resampling.LANCZOS)
    return img


# --- петли ---

_PUNCT_RUN = re.compile(r"([^\w\s])\1{3,}")


def _collapse(text: str) -> str:
    """Линии-заполнители из точек и подчёркиваний на бланках — не петля (2607.24077)."""
    return _PUNCT_RUN.sub(r"\1\1\1", text)


def detect_repeat(text: str, base_max_repeats: int = 4, window_size: int = 500,
                  scaling_factor: float = 3.0) -> bool:
    """Точные повторы хвоста (копия detect_repeat_token из chandra/model/util.py),
    но единицы короче 4 символов без букв и цифр («...», «__») не считаются."""
    text = _collapse(text)
    for seq_len in range(1, window_size // 2 + 1):
        cand = text[-seq_len:]
        if seq_len < 4 and not re.search(r"[0-9A-Za-zА-Яа-яЁё]", cand):
            continue
        max_rep = int(base_max_repeats * (1 + scaling_factor / seq_len))
        if seq_len < 4:
            max_rep = max(max_rep, 30)
        pos = len(text) - seq_len
        if pos < 0:
            continue
        n = 0
        while pos >= 0 and text[pos:pos + seq_len] == cand:
            n += 1
            pos -= seq_len
        if n > max_rep:
            return True
    return False


def tail_scores(text: str, n: int = LOOP_TAIL) -> tuple[float, float]:
    """(zlib-коэффициент, новизна 12-грамм) хвоста. На коротком тексте — (1, 1)."""
    t = _collapse(text[-n:])
    if len(t) < 600:
        return 1.0, 1.0
    b = t.encode("utf-8")
    zr = len(zlib.compress(b, 6)) / len(b)
    k = 12
    grams = [t[i:i + k] for i in range(0, len(t) - k)]
    nov = len(set(grams)) / max(1, len(grams))
    return zr, nov


_JSON_SCAFFOLD = re.compile(r'"bbox"\s*:\s*\[[^\]]*\]|"category"\s*:\s*"[^"]*"|[0-9]+|[\[\]{},:"]')
LOOP_RELAX_TOKENS = 4000   # нормальная страница короче (макс. без предупреждений 3 853); дальше порог мягче
LOOP_ZLIB_RELAXED = 0.12   # порог LightOnOCR для текста (2601.14251: 0,13)
LOOP_NOVELTY_RELAXED = 0.12


def looks_looped(text: str, n_tokens: int = 0) -> bool:
    """Замер 15.10.2026 на 128 выходах семи моделей: все петли (обрыв по лимиту)
    дали zlib ≤ 0,056 и новизну ≤ 0,081; нормальные страницы — zlib ≥ 0,074,
    типично 0,2–0,5. Точные повторы ловятся и на коротком тексте. Хвост
    оценивается и как есть, и без каркаса JSON разметки (bbox, цифры), потому что
    меняющиеся координаты маскируют повтор одного и того же блока. После
    LOOP_RELAX_TOKENS токенов порог мягче: такой длины у нормальных страниц не бывает."""
    if detect_repeat(text):
        return True
    if len(text) < LOOP_TAIL:
        return False
    zr, nov = tail_scores(text)
    zr2, nov2 = tail_scores(re.sub(r"\s+", " ", _JSON_SCAFFOLD.sub(" ", text[-LOOP_TAIL * 2:])))
    z, v = min(zr, zr2), min(nov, nov2)
    if z < LOOP_ZLIB or v < LOOP_NOVELTY:
        return True
    if n_tokens >= LOOP_RELAX_TOKENS and (z < LOOP_ZLIB_RELAXED or v < LOOP_NOVELTY_RELAXED):
        return True
    return False


def trim_loop(text: str) -> str:
    """Обрезать зациклившийся хвост: оставить текст до начала повторов."""
    if len(text) < 600:
        return text
    # 1) где хвост впервые стал «сжимаемым»
    cut = None
    for i in range(min(len(text), LOOP_TAIL), len(text) + 1, 400):
        zr, nov = tail_scores(text[:i])
        if zr < LOOP_ZLIB or nov < LOOP_NOVELTY:
            cut = max(600, i - LOOP_TAIL + 400)
            break
    if cut is not None:
        text = text[:cut]
    # 2) точный повтор единицы в конце — оставить одно вхождение
    for unit_len in (200, 120, 60, 30):
        if len(text) <= unit_len * 3:
            continue
        unit = text[-unit_len:]
        first = text.find(unit)
        if 0 <= first < len(text) - unit_len:
            text = text[:first + unit_len]
            break
    return text.rstrip()


# --- разбор JSON разметки ---

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.S)
_DICT = re.compile(r'\{[^{}]*?"bbox"\s*:\s*\[[^\]]*?\][^{}]*?\}', re.S)


def parse_cells(text: str) -> tuple[list[dict], bool]:
    """(блоки, пришлось ли чинить). Пустой список — разбор не удался."""
    raw = _FENCE.sub("", text.strip())
    try:
        cells = json.loads(raw)
        if isinstance(cells, list) and all(isinstance(c, dict) and "category" in c for c in cells):
            return cells, False
    except Exception:
        pass
    cells = []
    for m in _DICT.finditer(raw):
        try:
            c = json.loads(m.group(0))
            if isinstance(c, dict) and "category" in c:
                cells.append(c)
        except Exception:
            continue
    return cells, True


_PLAIN_TEX = [(r"\{([,.])\}", r"\1"),     # десятичная запятая «5{,}5»
              (r"\\(?:text|mathrm|textrm|mbox)\s*\{([^{}]*)\}", r"\1"),
              (r"\\(?:,|;|:|!|quad|qquad|\s)", " "), (r"\\(?:cdot|times)", "×"), (r"\\div", "÷"),
              (r"\\%", "%"), (r"\\left\s*([(\[])", r"\1"), (r"\\right\s*([)\]])", r"\1"),
              (r"\\approx", "≈"), (r"\\(?:ldots|dots|cdots)", "…"), (r"\\leq?", "≤"), (r"\\geq?", "≥")]


def _plain_formula(t: str) -> str | None:
    """Простая арифметика из документа («2\\,034+350+360 = 4100 \\text{(тыс. руб.)}») — обычным текстом,
    чтобы числа находились поиском и читались юристом. Дроби, степени, индексы, корни, суммы и
    многострочные формулы остаются LaTeX (None)."""
    body = t.strip()
    for a, b in (("$$", "$$"), ("\\[", "\\]"), ("$", "$")):
        if body.startswith(a) and body.endswith(b) and len(body) > len(a) + len(b):
            body = body[len(a):-len(b)].strip()
            break
    if not body or "\n" in body.strip() or "\\\\" in body:
        return None
    for pat, rep in _PLAIN_TEX:
        body = re.sub(pat, rep, body)
    if re.search(r"[\\^_{}]", body):
        return None
    return re.sub(r"[ \t]+", " ", body).strip() or None


def _formula(text: str) -> str:
    t = text.strip()
    plain = _plain_formula(t)
    if plain:
        return plain
    if t.startswith("$$") and t.endswith("$$"):
        return t
    if t.startswith("\\[") and t.endswith("\\]"):
        return f"$$\n{t[2:-2].strip()}\n$$"
    if "$" in t:
        return t
    return f"$$\n{t}\n$$"


def cells_to_markdown(cells: list[dict]) -> str:
    """Порядок чтения — как отдала модель. Колонтитулы не выбрасываем (в
    судебных документах там номер дела и листа), а помечаем."""
    parts = []
    for c in cells:
        cat = c.get("category", "Text")
        text = (c.get("text") or "").strip()
        if cat == "Picture":
            parts.append("![](рисунок)")
        elif cat == "Formula":
            parts.append(_formula(text))
        elif cat in ("Page-header", "Page-footer"):
            if text:
                parts.append(f"‹колонтитул› {text}")
        elif text:
            parts.append(text)
    return "\n\n".join(parts)


class PageTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise PageTimeout()


@dataclass
class PageResult:
    markdown: str
    mode: str                      # layout | layout-repaired | ocr | second:<имя> | timeout | error
    warnings: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    gen_tokens: int = 0
    seconds: float = 0.0
    blocks: int = 0
    image_size: tuple[int, int] = (0, 0)
    rotation: int = 0              # на сколько градусов по часовой повернули перед OCR
    engine: str = "dots.mocr"      # кто дал итоговый текст
    dewarp: dict | None = None     # четырёхугольник листа, если перспектива выправлялась (conf, skew_deg, area, inset)
    coverage: dict | None = None   # полнота по строкам Vision: сколько строк не покрыто, что дочитано
    qr: list = field(default_factory=list)   # QR-коды/штрихкоды со страницы
    image: object = None           # картинка, которую видел движок (после выпрямления, поворота, ужатия)
    vision: tuple | None = None    # (текст, средняя уверенность, строк) Apple Vision по той же картинке; None — Vision недоступен
    vision_obs: list | None = None # строки Vision с координатами (для сверки в dots_batch, в json не пишется)
    features: dict | None = None   # page_features.features(): {"ink": {"blue_px"}, "sharp"}
    notext: dict | None = None     # признаки правила «снимок без текста», если оно проверялось: lines, rects, paper


class DotsEngine:
    def __init__(self, max_pixels: int = MAX_PIXELS, max_tokens: int = MAX_TOKENS,
                 page_timeout: int = PAGE_TIMEOUT, log=print, auto_rotate: bool = True,
                 second_opinions: list[str] | None = None, auto_dewarp: bool = False,
                 reread: bool = True, vision_fallback: bool = True, qr: bool = True,
                 skip_empty: bool = True):
        self.max_pixels = max_pixels
        self.max_tokens = max_tokens
        self.page_timeout = page_timeout
        self.log = log
        self.auto_rotate = auto_rotate and orientation is not None
        # Выпрямление по умолчанию ВЫКЛЮЧЕНО: на 21 тестовой странице полнота не изменилась
        # (+0,004), а на синтетическом фото с наклоном 2,4° передискретизация потеряла цифру в
        # 20-значном номере счёта (замер 15.10.2026, вечер). Включать для фото с сильной
        # трапецией: DotsEngine(auto_dewarp=True) / dots_batch.py --dewarp / DOTS_DEWARP=1.
        if not auto_dewarp and os.environ.get("DOTS_DEWARP") == "1":
            auto_dewarp = True
        self.auto_dewarp = auto_dewarp and dewarp is not None
        self.reread = reread and coverage is not None
        self.vision_fallback = vision_fallback and crosscheck is not None
        self.qr = qr and barcodes is not None
        self.skip_empty = skip_empty and orientation is not None and coverage is not None
        if second_opinions is None:
            env = os.environ.get("DOTS_SECOND")
            if env is not None:
                second_opinions = [s.strip() for s in env.split(",") if s.strip()]
            else:
                second_opinions = [k for k in ("xiaomi", "hunyuan15", "glm") if second_available(k)]
        self.second_opinions = [s for s in second_opinions if s in SECOND_OPINIONS]
        self.model = self.processor = self.config = None
        self._prompts: dict[str, str] = {}
        self._second: dict[str, tuple] = {}

    # -- жизненный цикл --
    def load(self):
        if self.model is not None:
            return
        from mlx_vlm import load
        from mlx_vlm.prompt_utils import apply_chat_template
        from mlx_vlm.utils import load_config
        t0 = time.time()
        path = model_path()
        self.model, self.processor = load(path)
        self.config = load_config(path)
        self._prompts = {
            "layout": apply_chat_template(self.processor, self.config, PROMPT_LAYOUT, num_images=1),
            "ocr": apply_chat_template(self.processor, self.config, PROMPT_OCR, num_images=1),
        }
        self.log(f"dots.mocr загружена за {time.time() - t0:.1f} с ({path})"
                 + (f"; второе мнение: {', '.join(self.second_opinions)}" if self.second_opinions else "")
                 + ("; автоповорот: Vision" if self.auto_rotate else "; автоповорот выключен")
                 + ("; выпрямление: Vision" if self.auto_dewarp else "")
                 + ("; дочитывание областей: Vision" if self.reread else ""))

    def _load_second(self, name: str):
        if name in self._second:
            return self._second[name]
        from mlx_vlm import load
        from mlx_vlm.prompt_utils import apply_chat_template
        from mlx_vlm.utils import load_config
        repo, prompt, rep = SECOND_OPINIONS[name]
        t0 = time.time()
        path = model_path(repo)
        model, processor = load(path)
        config = load_config(path)
        formatted = apply_chat_template(processor, config, prompt, num_images=1)
        self._second[name] = (model, processor, formatted, rep)
        self.log(f"    второе мнение «{name}» загружено за {time.time() - t0:.1f} с")
        return self._second[name]

    def unload(self):
        self.model = self.processor = self.config = None
        self._prompts = {}
        self._second = {}
        import gc
        gc.collect()
        try:
            import mlx.core as mx
            (mx.clear_cache if hasattr(mx, "clear_cache") else mx.metal.clear_cache)()
        except Exception:
            pass

    # -- генерация --
    def _stream(self, image_path: str, prompt: str, model=None, processor=None, rep=None,
                max_tokens: int | None = None):
        """(текст, prompt_tok, gen_tok, зациклилось). Генерация потоковая: хвост
        проверяется каждые LOOP_CHECK_EVERY токенов. PageTimeout — если не уложились."""
        from mlx_vlm import stream_generate
        model = model or self.model
        processor = processor or self.processor
        kw = dict(max_tokens=max_tokens or self.max_tokens, temperature=0.0)
        if rep:
            kw["repetition_penalty"] = rep
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(self.page_timeout)
        text, n, ptok, looped = "", 0, 0, False
        gen = None
        try:
            try:
                gen = stream_generate(model, processor, prompt, [image_path], eos_tokens=["<|im_end|>"], **kw)
                it = iter(gen)
                first = next(it, None)
            except TypeError:
                gen = stream_generate(model, processor, prompt, [image_path], **kw)
                it = iter(gen)
                first = next(it, None)
            if first is not None:
                text += first.text or ""
                n = 1
                ptok = int(getattr(first, "prompt_tokens", 0) or 0)
                for r in it:
                    text += r.text or ""
                    n += 1
                    if n % LOOP_CHECK_EVERY == 0 and looks_looped(text, n):
                        looped = True
                        break
        finally:
            signal.alarm(0)
            if gen is not None:
                try:
                    gen.close()
                except Exception:
                    pass
        return text, ptok, n, looped

    def _result(self, md, mode, warnings, ptok, gtok, t0, blocks, img, rotation, engine="dots.mocr"):
        return PageResult(md, mode, warnings, ptok, gtok, time.time() - t0, blocks, img.size, rotation, engine)

    def ocr_page(self, img: Image.Image) -> PageResult:
        """Страница целиком: выпрямление → поворот → ужатие → каскад движков →
        дочитывание пропущенных областей → резервное чтение Vision → QR-коды."""
        self.load()
        t_start = time.time()
        warnings: list[str] = []
        rotation = 0
        dq = None
        if self.auto_dewarp:
            try:
                q = dewarp.detect(img)
                if dewarp.should_fix(q):
                    img = dewarp.correct(img, q)
                    dq = q
                    warnings.append(f"DEWARPED:{q['skew_deg']}")
            except Exception as e:  # noqa: BLE001
                warnings.append(f"DEWARP_ERROR:{type(e).__name__}")
        if self.auto_rotate:
            try:
                rot, sure, _ = orientation.detect(img)
                if rot and sure >= ORIENT_MIN_SURE:
                    img = orientation.fix(img, rot)
                    rotation = rot
                    warnings.append(f"ROTATED:{rot}")
            except Exception as e:  # noqa: BLE001
                warnings.append(f"ORIENT_ERROR:{type(e).__name__}")
        img = fit_image(img, self.max_pixels)
        pre = self._prescan(img)
        skip, info = self._no_text(img, pre) if self.skip_empty else (False, None)
        if skip:
            r = self._empty_result(img, pre, warnings, rotation)
        else:
            r = self._ocr_core(img, warnings, rotation)
        r.dewarp = dq
        r.image = img
        r.vision, r.vision_obs, r.features, r.notext = pre["vision"], pre["obs"], pre["features"], info
        if not skip:
            if self.reread and r.mode not in ("timeout", "error"):
                self._reread(img, r, pre["obs"])
            if self.vision_fallback:
                self._vision_fallback(img, r)
            if self.qr:
                self._qr(img, r, pre["qr"])
        r.seconds = time.time() - t_start
        return r

    # -- один проход Apple Vision и признаки страницы --
    def _prescan(self, img: Image.Image) -> dict:
        """Apple Vision читает ту картинку, что пойдёт в OCR, ровно один раз; результат кормит
        проверку полноты (coverage.check(obs=…)), резервное чтение, сверку в dots_batch и поле
        vision_text в _ocr.json. Без pyobjc vision = None (тогда правило «без текста» не работает)."""
        obs = vision = None
        if orientation is not None and coverage is not None:
            try:
                obs = coverage.vision_lines(img)
                text = "\n".join(o[5] for o in obs)
                conf = sum(o[4] for o in obs) / len(obs) if obs else 0.0
                vision = (text, conf, len(obs))
            except Exception:  # noqa: BLE001
                obs = vision = None
        feats = None
        if page_features is not None:
            try:
                feats = page_features.features(img)
            except Exception:  # noqa: BLE001
                feats = None
        return {"obs": obs, "vision": vision, "features": feats, "qr": None}

    @staticmethod
    def _paper_share(img: Image.Image) -> float | None:
        """Доля «бумажных» пикселей: яркие (не темнее 140 и не дальше 30 от 90-го перцентиля
        яркости), почти без цвета (max−min канала ≤ 50). Картинка ужимается до 700 px по длинной
        стороне. Снимки помещения 0,21–0,40; страницы документов 0,65–0,99; фото документа на столе
        0,29 (там строк Vision много, правило до бумаги не доходит)."""
        if np is None:
            return None
        im = img.convert("RGB")
        k = 700 / max(im.size)
        if k < 1:
            im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.BOX)
        a = np.asarray(im).astype(np.int32)
        gray = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
        ref = np.percentile(gray, 90)
        paper = (gray >= ref - 30) & (gray >= 140) & ((a.max(axis=2) - a.min(axis=2)) <= 50)
        return float(paper.mean())

    def _no_text(self, img: Image.Image, pre: dict) -> tuple[bool, dict | None]:
        """Правило «снимок без текста»: Vision < NOTEXT_LINES строк, детектор областей текста
        ≤ NOTEXT_RECTS, страница не «бумажная» (NOTEXT_PAPER), QR нет. Любая неопределённость
        (нет Vision, ошибка детектора, нет numpy) — НЕ пропускать. Вернёт (пропустить, признаки)."""
        v = pre["vision"]
        if v is None or v[2] >= NOTEXT_LINES:
            return False, None
        info = {"lines": v[2], "rects": None, "paper": None}
        try:
            info["rects"] = orientation.text_rect_count(img)
        except Exception:  # noqa: BLE001
            return False, info
        if info["rects"] > NOTEXT_RECTS:
            return False, info
        paper = self._paper_share(img)
        if paper is None:
            return False, info
        info["paper"] = round(paper, 2)
        if paper >= NOTEXT_PAPER:
            return False, info
        if barcodes is not None:
            try:
                pre["qr"] = barcodes.decode(img)
            except Exception:  # noqa: BLE001
                return False, info
            if pre["qr"]:
                return False, info
        return True, info

    def _empty_result(self, img: Image.Image, pre: dict, warnings: list[str], rotation: int) -> PageResult:
        text = "; ".join(l.strip() for l in pre["vision"][0].splitlines() if l.strip())[:300]
        md = (f"_(на снимке почти нет текста; Apple Vision прочитал: «{text}»)_" if text
              else "_(на снимке текста нет)_")
        return PageResult(md, "empty", warnings + ["NO_TEXT_SKIPPED"], 0, 0, 0.0, 0, img.size, rotation,
                          engine="apple-vision")

    def _ocr_core(self, img: Image.Image, warnings: list[str], rotation: int) -> PageResult:
        """Каскад: разметка dots → «просто текст» → запасные движки."""
        t0 = time.time()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            img.save(tmp.name)
            path = tmp.name
        ptok = gtok = 0
        try:
            # 1. разметка
            cells, repaired, layout_md = [], False, ""
            try:
                text, ptok, gtok, looped = self._stream(path, self._prompts["layout"])
            except PageTimeout:
                return self._result("", "timeout", warnings + ["TIMEOUT"], 0, 0, t0, 0, img, rotation)
            except Exception as e:  # noqa: BLE001
                # Сбой генерации (в mlx-vlm 0.4.4 — UnicodeDecodeError детокенизатора на
                # редком токене) — не приговор странице: второй промпт обычно проходит.
                warnings.append(f"LAYOUT_ERROR:{type(e).__name__}")
                text, looped = "", False
            if looped:
                warnings.append("LOOP_DETECTED")
                text = trim_loop(text)
            if gtok >= self.max_tokens - 5:
                # Нормальная страница — до ~4 000 токенов; упор в лимит без явной петли —
                # это «разбег» с выдумкой (длинный документ, стр. 285). Считаем отказом.
                warnings.append("MAX_TOKENS_HIT")
                looped = True
            if text:
                cells, repaired = parse_cells(text)
            has_text = any((c.get("text") or "").strip() for c in cells)
            if cells and has_text:
                layout_md = cells_to_markdown(cells)
                if not looped:
                    if repaired:
                        warnings.append("JSON_REPAIRED")
                    return self._result(layout_md, "layout-repaired" if repaired else "layout",
                                        warnings, ptok, gtok, t0, len(cells), img, rotation)
            # 2. просто текст (разметка не собралась, или вся страница «картинка», или петля)
            if not looped and not any(w.startswith("LAYOUT_ERROR") for w in warnings):
                warnings.append("PICTURE_ONLY" if cells else "LAYOUT_FAILED")
            try:
                text2, p2, g2, looped2 = self._stream(path, self._prompts["ocr"])
            except PageTimeout:
                warnings.append("TIMEOUT")
                return self._result(layout_md, "timeout", warnings, ptok, gtok, t0, len(cells), img, rotation)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"ERROR:{type(e).__name__}:{e}")
                return self._result(layout_md, "error", warnings, ptok, gtok, t0, len(cells), img, rotation)
            ptok += p2
            gtok += g2
            if g2 >= self.max_tokens - 5:
                warnings.append("MAX_TOKENS_HIT_2")
                looped2 = True
            if looped2:
                if "MAX_TOKENS_HIT_2" not in warnings or looped2:
                    warnings.append("LOOP_DETECTED_2")
                text2 = trim_loop(text2)
            if "PICTURE_ONLY" in warnings and len(text2.strip()) < 40:
                # Действительно картинка без текста — оставляем метку рисунка.
                warnings.remove("PICTURE_ONLY")
                return self._result(layout_md, "layout", warnings, ptok, gtok, t0, len(cells), img, rotation)
            if not looped2 and text2.strip():
                return self._result(text2.strip(), "ocr", warnings, ptok, gtok, t0, 0, img, rotation)
            # 3. второе мнение другой модели: петли у разных моделей — на разных страницах
            best = text2.strip() if len(text2.strip()) >= len(layout_md) else layout_md
            for name in self.second_opinions:
                try:
                    if SECOND_OPINIONS[name][0].startswith("llama:"):
                        t3, p3, g3, looped3 = self._stream_llama(path, name)
                    else:
                        model, processor, prompt, rep = self._load_second(name)
                        t3, p3, g3, looped3 = self._stream(path, prompt, model, processor, rep)
                except PageTimeout:
                    warnings.append(f"SECOND_TIMEOUT:{name}")
                    continue
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"SECOND_ERROR:{name}:{type(e).__name__}")
                    continue
                ptok += p3
                gtok += g3
                if g3 >= self.max_tokens - 5:
                    looped3 = True
                if looped3 or not t3.strip():
                    warnings.append(f"SECOND_LOOP:{name}")
                    t3 = trim_loop(t3)
                    if len(t3.strip()) > len(best):
                        best = t3.strip()
                    continue
                warnings.append(f"SECOND:{name}")
                return self._result(t3.strip(), f"second:{name}", warnings, ptok, gtok, t0, 0, img, rotation, engine=name)
            return self._result(best, "ocr", warnings, ptok, gtok, t0, 0, img, rotation)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    # -- запасной движок через llama.cpp (HunyuanOCR-1.5) --
    def _stream_llama(self, image_path: str, name: str, max_tokens: int | None = None):
        """llama-mtmd-cli печатает токены в stdout по мере генерации (проверено 15.10.2026),
        поэтому хвост проверяется на петлю так же, как у mlx-vlm, и процесс убивается при
        первом повторе. Возвращает (текст, 0, ~токенов, зациклилось)."""
        import select
        import subprocess
        repo, prompt, rep = SECOND_OPINIONS[name]
        model, mmproj = repo[6:].split("|")
        cmd = [llama_bin() or "llama-mtmd-cli", "-m", model, "--mmproj", mmproj, "--image", image_path, "-p", prompt,
               "-n", str(max_tokens or self.max_tokens), "--temp", "0", "-ngl", "99", "--no-warmup"]
        if rep:
            cmd += ["--repeat-penalty", str(rep)]
        t0 = time.time()
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        fd = p.stdout.fileno()
        buf = b""
        looped = False
        last_check = 0
        try:
            while True:
                if time.time() - t0 > self.page_timeout:
                    p.kill()
                    raise PageTimeout()
                ready, _, _ = select.select([fd], [], [], 1.0)
                if ready:
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) - last_check >= 400:
                        last_check = len(buf)
                        text = buf.decode("utf-8", errors="ignore")
                        if looks_looped(text, len(text) // 3):
                            looped = True
                            p.kill()
                            break
                elif p.poll() is not None:
                    break
        finally:
            try:
                if p.poll() is None:
                    p.kill()
                p.wait(timeout=10)
                p.stdout.close()
            except Exception:  # noqa: BLE001
                pass
        text = buf.decode("utf-8", errors="ignore").strip()
        return text, 0, max(1, len(text) // 3), looped

    # -- дочитывание пропущенных областей (coverage.py) --
    REREAD_MAX_BANDS = 4

    def _reread(self, img: Image.Image, r: PageResult, obs: list | None = None) -> None:
        """Строки, которые Apple Vision видит, а в тексте OCR их нет (и это настоящие
        русские слова, а не каша с рукописи), перечитываются промптом «просто текст» по
        полосам страницы; добавляется только то, что действительно ново."""
        try:
            cov = coverage.check(img, r.markdown, obs=obs)
        except Exception as e:  # noqa: BLE001
            r.warnings.append(f"REREAD_ERROR:{type(e).__name__}")
            return
        r.coverage = {k: cov[k] for k in ("vision_lines", "scored", "credible", "non_credible",
                                           "missing", "missing_lines", "bands")}
        page_vision = cov.get("vision_text", "")
        r.coverage["added"] = 0
        if not cov["bands"]:
            return
        added: list[str] = []
        for band in cov["bands"][:self.REREAD_MAX_BANDS]:
            crop = coverage.crop_band(img, band)
            if crop.height < 40 or crop.width < 40:
                continue
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                crop.save(tmp.name)
                cp = tmp.name
            try:
                limit = min(self.max_tokens, 600 + int(5000 * (band[1] - band[0])))
                text, _, _, looped = self._stream(cp, self._prompts["ocr"], max_tokens=limit)
            except PageTimeout:
                r.warnings.append("REREAD_TIMEOUT")
                continue
            except Exception as e:  # noqa: BLE001
                r.warnings.append(f"REREAD_ERROR:{type(e).__name__}")
                continue
            finally:
                try:
                    os.unlink(cp)
                except OSError:
                    pass
            if looped:
                text = trim_loop(text)
            text = text.strip()
            band_text = (cov.get("band_texts") or [""] * len(cov["bands"]))[cov["bands"].index(band)] if band in cov["bands"] else ""
            if text and coverage.novel(text, r.markdown + "\n" + "\n".join(added)) \
                    and coverage.corroborated(text, band_text + "\n" + page_vision):
                added.append(text)
        if added:
            r.markdown = (r.markdown.rstrip()
                          + "\n\n> ⚠ Дочитано по областям: строки ниже второе чтение (Apple Vision) видит на "
                            "странице, а основной проход пропустил; их место в тексте не гарантировано.\n\n"
                          + "\n\n".join(added))
            r.warnings.append(f"REREAD:{len(added)}")
            r.coverage["added"] = len(added)

    # -- резервное чтение Apple Vision --
    def _vision_fallback(self, img: Image.Image, r: PageResult) -> None:
        """Если ни один движок не дал полного текста (пусто, или оба прохода зациклились и
        запасные не спасли), дописывается чтение Vision: без разметки, зато без петель и
        выдумки. Только когда Vision сам читает уверенно (≥ 8 строк, conf ≥ 0,45)."""
        keys = {w.split(":")[0] for w in r.warnings}
        double_fail = bool(keys & {"LOOP_DETECTED_2", "MAX_TOKENS_HIT_2", "TIMEOUT"}) and "SECOND" not in keys
        if r.markdown.strip() and not double_fail:
            return
        try:
            text, conf, n = r.vision if r.vision is not None else crosscheck.vision_read(img)
        except Exception as e:  # noqa: BLE001
            r.warnings.append(f"VISION_ERROR:{type(e).__name__}")
            return
        if n < 8 or conf < 0.45 or not text.strip():
            return
        r.markdown = ((r.markdown.rstrip() + "\n\n") if r.markdown.strip() else "") + \
            "> ⚠ Резервное чтение Apple Vision (движки не дали полного текста); без разметки, строки в порядке страницы:\n\n" + text
        r.warnings.append("VISION_FALLBACK")
        if r.mode in ("timeout", "error") or r.engine == "dots.mocr" and not double_fail:
            r.engine = "apple-vision"

    # -- QR-коды --
    def _qr(self, img: Image.Image, r: PageResult, codes: list | None = None) -> None:
        if codes is None:
            try:
                codes = barcodes.decode(img)
            except Exception:  # noqa: BLE001
                return
        if codes:
            r.qr = codes
            r.markdown = ((r.markdown.rstrip() + "\n\n") if r.markdown.strip() else "") + barcodes.lines(codes)


WARN_NOTES = {
    "MAX_TOKENS_HIT": "страница упёрлась в лимит токенов и могла быть распознана не полностью",
    "LOOP_DETECTED": "обнаружено зацикливание, конец страницы обрезан",
    "LOOP_DETECTED_2": "второй проход тоже зациклился, конец страницы обрезан",
    "MAX_TOKENS_HIT_2": "второй проход тоже упёрся в лимит токенов",
    "TIMEOUT": "превышено время распознавания",
    "JSON_REPAIRED": "разметка страницы пришла битой и была восстановлена по частям",
    "LAYOUT_FAILED": "разметка не собралась, страница прочитана как сплошной текст",
    "PICTURE_ONLY": "разметка сочла страницу картинкой, текст снят вторым проходом без разметки",
    "LAYOUT_ERROR": "первый проход упал с ошибкой генерации, текст снят вторым проходом без разметки",
    "ROTATED": "страница лежала не прямо и была повёрнута перед распознаванием",
    "ORIENT_ERROR": "не удалось проверить ориентацию страницы",
    "SECOND": "основной движок зациклился, страница прочитана запасным",
    "SECOND_LOOP": "запасной движок тоже зациклился",
    "SECOND_ERROR": "запасной движок упал",
    "SECOND_TIMEOUT": "запасной движок не уложился в таймаут",
    "DEWARPED": "перспектива фото выправлена перед распознаванием",
    "DEWARP_ERROR": "не удалось проверить геометрию листа",
    "REREAD": "основной проход пропустил часть строк, они дочитаны по областям (блок в конце страницы)",
    "REREAD_ERROR": "дочитывание пропущенных областей не удалось",
    "REREAD_TIMEOUT": "дочитывание пропущенных областей не уложилось в таймаут",
    "VISION_FALLBACK": "движки не дали полного текста, в конец добавлено резервное чтение Apple Vision",
    "VISION_ERROR": "резервное чтение Apple Vision не удалось",
    "NO_TEXT_SKIPPED": "на снимке почти нет текста, распознавание пропущено",
}
SILENT = {"JSON_REPAIRED", "ROTATED", "DEWARPED", "NO_TEXT_SKIPPED"}   # технические детали, содержимому не вредят


def annotate(result: PageResult, page_num: int) -> str:
    """Текст страницы с явными пометками о проблемах — чтобы читатель .md
    видел, где распознавание споткнулось."""
    def note(w):
        key = w.split(":")[0]
        if key == "ERROR":
            return f"ошибка распознавания ({w[6:][:120]})"
        n = WARN_NOTES.get(key, w)
        if key in ("SECOND", "SECOND_LOOP", "SECOND_ERROR", "SECOND_TIMEOUT"):
            n += f" ({w.split(':', 1)[1]})"
        return n
    if not result.markdown:
        why = note(result.warnings[0]) if result.warnings else "пусто"
        return f"> ⚠ Страница {page_num} не распознана: {why}."
    text = result.markdown
    for w in result.warnings:
        if w.split(":")[0] in SILENT:
            continue
        n = note(w)
        text += f"\n\n> ⚠ {n[0].upper()}{n[1:]}."
    return text
