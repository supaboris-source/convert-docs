"""Второе независимое прочтение страницы (Apple Vision) и сверка имён и чисел.

Идея из статей 2608.03617 (согласие двух прочтений как оценка качества) и 2607.24077
(VLM подменяют имена без роста CER): дешёвый классический OCR читает страницу за
0,3 с, и всё, что VLM «увидела», а Vision — нет, помечается как непроверенное.
Это не доказательство ошибки, а список того, что сверять по оригиналу в первую очередь.

Имена сверяются нечётко (VLM и Vision читают слова по-разному), числа — ТОЧНО: цифры
последовательности должны совпасть с чтением Vision (между группами цифр пробелы и дефисы
не в счёт: «40702 810 …» = «40702810…»). Нечёткое сравнение чисел подтверждало ИНН
7704123459 при прочитанном Vision 7704123458: одна цифра из десяти — это сходство 0,9.
Если Vision прочёл рядом число, отличающееся одной-двумя цифрами, оно называется в пометке.
"""
from __future__ import annotations
import difflib
import os
import re
import tempfile
from PIL import Image

try:
    import orientation  # соседний модуль, тот же каталог
except ImportError:  # pragma: no cover
    orientation = None
try:
    import coverage  # соседний модуль: орфофильтр NSSpellChecker
except ImportError:  # pragma: no cover
    coverage = None

NAME = re.compile(r"(?<![«\"(])\b[А-ЯЁ][а-яё]{4,}\b")
NUM = re.compile(r"\b\d{1,2}\.\d{2}\.\d{4}\b|\b\d{4,}\b")
STOP = set("""Московская Область Россия Российской Федерации Федерация Химки Подольск Москва
Начато Окончено Вопрос Ответ Договор Арендатор Арендодатель Объект Стороны Соглашение Собственник
Протокол Осмотр Статья Страница Договора Арендатора Арендодателя Объекта Настоящего Настоящий
Гражданин Гражданка Общество Ограниченной Ответственностью Приложение Заявитель Заявление Решение
Определение Постановление Истец Ответчик Судья Суда Лицензия Пациент Пациента Диагноз Лечение""".split())
LOW_CONF = 0.45   # ниже — Vision сам читает плохо (фото под углом, рукопись), сверка ненадёжна

# группы цифр, склеенные пробелом или дефисом («40702 810 4 0247», «1234-56»): одно число
_SEP = r"(?:[ \u00a0\u202f]+|[ \u00a0\u202f]*[-\u2010-\u2015][ \u00a0\u202f]*)"
_CHAIN = re.compile(rf"\d+(?:{_SEP}\d+)*")
_MAX_GROUPS = 30     # в длинной строке таблицы из цифр с пробелами — не перебирать все отрезки
_MAX_DIGITS = 40
MONTHS = ("янв", "фев", "мар", "апр", "ма[йя]", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def _norm(s: str) -> str:
    return s.lower().replace("ё", "е")


def vision_read(img: Image.Image) -> tuple[str, float, int]:
    """(текст построчно, средняя уверенность, число строк). Без pyobjc — пусто."""
    if orientation is None:
        return "", 0.0, 0
    im = img.convert("RGB").copy()
    im.thumbnail((2200, 2200))
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as t:
        im.save(t.name)
        p = t.name
    try:
        obs = orientation._observations(p)
    finally:
        try:
            os.unlink(p)
        except OSError:
            pass
    if not obs:
        return "", 0.0, 0
    text = "\n".join(o[5] for o in obs)
    conf = sum(o[4] for o in obs) / len(obs)
    return text, conf, len(obs)


def _tokens(text: str) -> tuple[set, set]:
    names, nums = set(), set()
    for line in text.splitlines():
        ws = NAME.findall(line)
        for w in ws:
            if w in STOP:
                continue
            if line.strip().startswith(w) and len(ws) == 1:
                continue   # одиночное слово с заглавной в начале строки — скорее начало предложения
            names.add(w)
        nums.update(NUM.findall(line))
    return names, nums


_LOOK = str.maketrans({**dict.fromkeys("OОoо", "0"), **dict.fromkeys("Ss", "5"), **dict.fromkeys("Il|", "1"),
                       "З": "3", **dict.fromkeys("BВ", "8"), **dict.fromkeys("Zz", "2")})
_LOOK_TOK = re.compile(r"[\dOОoоSsIl|ЗBВZz]{4,}")


def _lookalikes(line: str) -> str:
    """Vision путает похожие знаки внутри номеров («ИНН S012345678», «5О12…»): в «словах», где цифр
    большинство, буквы-двойники заменяются цифрами. Только для чтения Vision."""
    def fix(m):
        t = m.group()
        d = sum(c.isdigit() for c in t)
        return t.translate(_LOOK) if d >= 3 and d * 2 >= len(t) else t
    return _LOOK_TOK.sub(fix, line)


def _chains(line: str) -> list[list[str]]:
    """Группы цифр строки, склеенные пробелом/дефисом, — по цепочкам: «40702 810 4» → [['40702','810','4']]."""
    return [re.findall(r"\d+", m.group()) for m in _CHAIN.finditer(line)]


def _spans(groups: list[str]) -> set[str]:
    """Все отрезки цепочки по границам групп: ['1234','567890'] → {'1234','567890','1234567890'}."""
    out = set()
    g = groups[:_MAX_GROUPS]
    for i in range(len(g)):
        s = ""
        for j in range(i, len(g)):
            s += g[j]
            if len(s) > _MAX_DIGITS:
                break
            out.add(s)
    return out


def _vision_numbers(vtext: str) -> set[str]:
    """Допустимые «числа» чтения Vision: каждая группа цифр и каждый отрезок цепочек групп,
    склеенных пробелом/дефисом внутри строки. Отрезок не обязан быть всей цепочкой (паспорт
    «1234 № 567890» OCR читает двумя числами), но обязан идти по границам групп: обрезанный
    на цифру номер («770412345» при «7704123459») числом Vision не считается."""
    out: set[str] = set()
    for line in vtext.splitlines():
        for ch in _chains(_lookalikes(line)):
            out |= _spans(ch)
    return out


def _ocr_alts(text: str) -> dict[str, set[str]]:
    """Для чисел OCR, склеенных с соседями пробелом/дефисом («1027700 123456»), — склейка всей
    цепочки: Vision мог прочитать её одним числом. Используется только если само число не нашлось."""
    alts: dict[str, set[str]] = {}
    for line in text.splitlines():
        for ch in _chains(line):
            if len(ch) < 2:
                continue
            full = "".join(ch)[:_MAX_DIGITS]
            for g in ch:
                if len(g) >= 4:
                    alts.setdefault(g, set()).add(full)
    return alts


def _date_re(tok: str):
    d, m, y = tok.split(".")
    mon = MONTHS[int(m) - 1] if 1 <= int(m) <= 12 else "-"
    # «10.07.2021», «10 07 2021», «10» июля 2021
    return re.compile(rf"(?<!\d)0?{int(d)}\W{{0,3}}(?:0?{int(m)}|{mon}\w*)\W{{0,3}}{y}(?!\d)")


def _num_supported(tok: str, vnums: set[str], vtext: str, alts: dict[str, set[str]]) -> bool:
    """Число подтверждено только точным совпадением цифр (без нечёткости)."""
    if re.fullmatch(r"\d{1,2}\.\d{2}\.\d{4}", tok):
        return bool(_date_re(tok).search(_norm(vtext)))
    if tok in vnums:
        return True
    return any(a in vnums for a in alts.get(tok, ()))


def _near_numbers(tok: str, vnums: set[str]) -> list[str]:
    """Числа Vision, отличающиеся от tok одной-двумя цифрами (двумя — только у длинных) или одной
    лишней/пропущенной цифрой: «OCR 7704123459 / Vision 7704123458» — почти наверняка подмена цифры
    (чьей, OCR или Vision, решает оригинал)."""
    digits = re.sub(r"\D", "", tok)
    if len(digits) < 6 or "." in tok:      # даты не сравниваем: у цифр дат слишком много «соседей»
        return []
    maxdiff = 1 if len(digits) < 9 else 2   # у коротких чисел совпадения «через две цифры» случайны
    out = []
    for v in vnums:
        if v == digits or abs(len(v) - len(digits)) > 1 or len(v) < 6:
            continue
        if len(v) == len(digits):
            if sum(a != b for a, b in zip(v, digits)) <= maxdiff:
                out.append(v)
        else:
            a, b = (v, digits) if len(v) > len(digits) else (digits, v)   # a длиннее на цифру
            if any(a[:i] + a[i + 1:] == b for i in range(len(a))):
                out.append(v)
    return sorted(out)[:2]


def _supported(tok: str, ref: str, thr: float) -> bool:
    """Нечёткое вхождение — только для слов и имён (числа: _num_supported)."""
    t = _norm(tok)
    if t in ref:
        return True
    L = len(t)
    step = max(1, L // 4)
    for i in range(0, max(1, len(ref) - L + 1), step):
        if difflib.SequenceMatcher(None, t, ref[i:i + L]).ratio() >= thr:
            return True
    return False


def check(img: Image.Image, ocr_text: str) -> dict:
    """Сверить имена и числа из OCR-текста со вторым чтением той же картинки."""
    vtext, vconf, vlines = vision_read(img)
    # свои пометки движка («> ⚠ Дочитано по областям…») — не текст страницы
    ocr_text = "\n".join(l for l in ocr_text.splitlines() if not l.lstrip().startswith("> ⚠"))
    names, nums = _tokens(ocr_text)
    out = {"vision_conf": round(vconf, 2), "vision_lines": vlines, "vision_chars": len(vtext),
           "names_total": len(names), "numbers_total": len(nums),
           "names_unconfirmed": [], "numbers_unconfirmed": [], "numbers_near": {},
           "reliable": vconf >= LOW_CONF and vlines >= 5}
    if not vtext:
        out["reliable"] = False
        return out
    ref = _norm(vtext)
    out["names_unconfirmed"] = sorted(n for n in names if not _supported(n, ref, 0.85))
    vnums, alts = _vision_numbers(vtext), _ocr_alts(ocr_text)
    out["numbers_unconfirmed"] = sorted(n for n in nums if not _num_supported(n, vnums, vtext, alts))
    # что Vision прочёл вместо них, если отличие в одной-двух цифрах
    out["numbers_near"] = {n: v for n in out["numbers_unconfirmed"] if (v := _near_numbers(n, vnums))}
    if coverage is not None and coverage._checker():
        # несловарные среди неподтверждённых: обычные слова с заглавной Vision часто читает иначе,
        # а два чтения, разошедшиеся на фамилиях и словах-уродцах, — признак рукописи или плохого фото
        out["names_rare"] = [n for n in out["names_unconfirmed"]
                             if not (coverage.word_ok(n.lower()) or coverage.word_ok(n))]
    return out


def note(cc: dict) -> str:
    """Короткая пометка для .md (пусто, если сверять нечего)."""
    if not cc:
        return ""
    parts = []
    if cc.get("names_unconfirmed"):
        parts.append("имена: " + ", ".join(cc["names_unconfirmed"][:12]) + (" …" if len(cc["names_unconfirmed"]) > 12 else ""))
    if cc.get("numbers_unconfirmed"):
        near = cc.get("numbers_near") or {}
        # сначала те, у которых Vision прочёл почти такое же число: подмена одной цифры вероятнее всего
        nums = sorted(cc["numbers_unconfirmed"], key=lambda n: (n not in near, n))
        shown = [f"{n} (Vision: {', '.join(near[n])})" if n in near else n for n in nums[:12]]
        parts.append("числа (цифры в цифру): " + ", ".join(shown) + (" …" if len(nums) > 12 else ""))
    if not parts:
        return ""
    tail = "" if cc.get("reliable") else " (второе чтение ненадёжно: фото низкого качества или рукопись)"
    return "> ⚠ Не подтверждены вторым чтением, сверить по оригиналу — " + "; ".join(parts) + tail + "."
