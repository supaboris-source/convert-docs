#!/usr/bin/env python3
"""Проверка реквизитов и согласованности чисел в готовом тексте `.md`.

OCR и чтение глазами тихо портят цифры: ИНН 7704123459 вместо …458, ОГРНИП 120… вместо 320…,
«1/4» вместо «¾». Сверка с Apple Vision (crosscheck.py) ловит расхождение двух чтений, а эта
проверка — внутреннюю несостыковку самого текста, и ей не нужны ни картинка, ни второе чтение.

    python3 requisites.py <файл.md | папка>        # печатает найденное по файлам и страницам
    python3 requisites.py <папка> --json           # то же в JSON

    from requisites import check_md
    check_md(text) -> [{"page", "kind", "value", "problem", "context"}, …]

Что проверяется (только стандартная библиотека, Python 3.9+):
  * ИНН 10 и 12 цифр, ОГРН 13, ОГРНИП 15, СНИЛС — контрольные цифры; БИК — 9 цифр и «04…»;
    КПП — формат; р/с и к/с — 20 цифр и ключ по БИК (если БИК той же страницы есть);
  * метки с опечатками OCR («ОРГН», «ИНН/КПП», «р/сч», «расчетный счет», «кор. счет»);
    число может быть разбито пробелами; буква-двойник вместо цифры («7704О1475») — тоже находка;
  * сумма цифрами и прописью («75 000 (Семьдесят пять тысяч) рублей»), в том числе наоборот;
  * доля и процент в одном предложении («¾ … 75 %»);
  * один и тот же реквизит, написанный по-разному в разных местах документа.

Находка — повод посмотреть в оригинал, а не доказательство: контрольные суммы не знают о
намеренно выдуманных номерах в образцах и шаблонах. Кадастровые номера и номера записей
регистрации не сравниваются (у них соседние значения законны). Служебные строки «> ⚠», «> ✎»
пропускаются."""
from __future__ import annotations

import json
import re
import sys
from fractions import Fraction
from pathlib import Path

PAGE_RE = re.compile(r"^#{1,4}\s*Страница\s+(\d+)\b", re.I)
SERVICE = ("> ⚠", "> ✎")
# казначейские и бюджетные счета в Банке России (03100…, 40101…, 40102…): ключ у них считается иначе
# (проверено на реквизитах госпошлины: 03100643000000018500 и ЕКС 40102810545370000003 с БИК 017003983)
NO_KEY_PREFIXES = ("03", "401", "402")
CHECKSUM_KINDS = ("ИНН", "ОГРН", "ОГРНИП")   # подмена цифры ломает контрольную сумму сама, отдельная сверка не нужна

# ---------- контрольные суммы ----------

def inn_check(d: str):
    """(верно?, ожидаемые контрольные цифры) для ИНН из 10 или 12 цифр."""
    def ctl(s, w):
        return str(sum(int(a) * b for a, b in zip(s, w)) % 11 % 10)
    if len(d) == 10:
        want = ctl(d[:9], (2, 4, 10, 3, 5, 9, 4, 6, 8))
        return d[9] == want, want
    if len(d) == 12:
        w1 = ctl(d[:10], (7, 2, 4, 10, 3, 5, 9, 4, 6, 8))
        w2 = ctl(d[:11], (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8))
        return d[10:] == w1 + w2, w1 + w2
    return False, ""


def ogrn_check(d: str):
    """ОГРН (13): (первые 12 цифр mod 11) mod 10; ОГРНИП (15): (первые 14 mod 13) mod 10."""
    if len(d) == 13:
        want = str(int(d[:12]) % 11 % 10)
        return d[12] == want, want
    if len(d) == 15:
        want = str(int(d[:14]) % 13 % 10)
        return d[14] == want, want
    return False, ""


def snils_check(d: str):
    if len(d) != 11:
        return False, ""
    s = sum(int(c) * (9 - i) for i, c in enumerate(d[:9]))
    if s in (100, 101):
        s = 0
    elif s > 101:
        s %= 101
        if s in (100, 101):
            s = 0
    want = f"{s:02d}"
    return d[9:] == want, want


def account_key_ok(bik: str, acc: str, kind: str) -> bool:
    """Ключ счёта по БИК. р/с: 3 последние цифры БИК + 20 цифр счёта; к/с: «0» + 5–6-я цифры БИК +
    20 цифр. Веса 7,1,3 повторяются, сумма младших разрядов произведений кратна 10."""
    s = (bik[-3:] if kind == "р/с" else "0" + bik[4:6]) + acc
    w = (7, 1, 3)
    return sum(int(c) * w[i % 3] % 10 for i, c in enumerate(s)) % 10 == 0


# ---------- метки и числа ----------

_L = r"(?<![A-Za-zА-Яа-яЁё])"
_R = r"(?![A-Za-zА-Яа-яЁё])"
LABELS = [
    ("ОГРНИП", r"[ОO][ГР][РГ][НH]\s?[ИI][ПП]"),
    ("ОГРН", r"[ОO][ГР][РГ][НH]"),
    ("ИНН/КПП", r"И[НH][НH]\s?[/,\\]\s?[КK][ПП][ПП]"),
    ("ИНН", r"И[НH][НH]"),
    ("КПП", r"[КK][ПП][ПП]"),
    ("СНИЛС", r"СНИЛС"),
    ("БИК", r"БИК(?:\s+банка)?"),
    ("р/с", r"(?:[РP]\s?/\s?[СC]|расч[её]тн\w*\s+сч[её]т\w*|расч\.?\s*сч[её]т\w*)(?:ч\w*)?"),
    ("к/с", r"(?:[КK]\s?/\s?[СC]|кор(?:р|респондентск\w*)?\.?\s*сч[её]т\w*)(?:ч\w*)?"),
]
LABEL_RE = re.compile("|".join(f"(?P<g{i}>{_L}{p}{_R})" for i, (_, p) in enumerate(LABELS)), re.I)
PASSPORT_RE = re.compile(
    r"паспорт\w*[^\d]{0,60}?(?<!\d)(?P<ser>\d{2}\s?\d{2})\s*(?:№|N|номер\w*)?\s*(?P<num>\d{6})(?!\d)", re.I)

_DIG = "0-9OОoоIlЗSB"          # цифры и буквы-двойники, которые OCR/Vision путают с цифрами
_TOK = re.compile(rf"(?=[^\s\d]*\d)[{_DIG}]+(?:-[{_DIG}]+)*")
_LOOK = str.maketrans({**dict.fromkeys("OОoо", "0"), **dict.fromkeys("Il", "1"), "З": "3",
                       "S": "5", "B": "8"})
_UNREADABLE = re.compile(r"\s*(?:\[\s*(?:неразборчиво|…|\.\.\.)|…|\.\.\.|\?)")
_SKIP = re.compile(r"[\s:№#=.|\-–—]*(?:N|No|Nº|№)?[\s:№#=.|\-–—]*")


def _tokens_after(line: str, pos: int):
    """Цепочка токенов с цифрами после метки: [(токен, начало, конец)]; токены разделены ровно
    одним пробелом. «7704 123456» — одно число, «7704123456 12 июля» — ИНН и дата."""
    m = _SKIP.match(line, pos)
    i = m.end() if m else pos
    out = []
    while True:
        t = _TOK.match(line, i)
        if not t:
            break
        tok, end = t.group(), t.end()
        # «3200000000000от»: «о» — начало слова, не цифра-двойник
        while end < len(line) and line[end].isalpha() and tok and tok[-1] in "OОoоIlЗSB":
            tok, end = tok[:-1], end - 1
        if not re.search(r"\d", tok):
            break
        out.append((tok, t.start(), end))
        j = end
        if j < len(line) and line[j] in " \u00a0\u202f" and (j + 1 < len(line)):
            i = j + 1
        else:
            break
    return out


def _pick(tokens, lengths):
    """Какие токены считать числом. Первый префикс с допустимым числом цифр; иначе первый токен,
    если он длинный (≥ 8 цифр: остальное — соседнее слово/число), иначе вся цепочка."""
    acc, hits = "", []
    for k, (t, _, _) in enumerate(tokens):
        acc += t
        if len(re.sub(r"\D", "", acc.translate(_LOOK))) in lengths:
            hits.append(k + 1)
    if hits:
        # форма из клеток «7 7 2 1 6 9 …»: берём самое длинное допустимое число; иначе — первое
        if len(hits) > 1 and all(len(t) == 1 for t, _, _ in tokens[:hits[-1]]):
            return tokens[:hits[-1]]
        return tokens[:hits[0]]
    if tokens and len(re.sub(r"\D", "", tokens[0][0].translate(_LOOK))) >= 8:
        return tokens[:1]
    return tokens


# КПП: 4 цифры, 2 знака (цифры или латинские буквы), 3 цифры. _TOK на букве обрывается, поэтому КПП
# с буквами в 5–6 позициях («7701A1001») читается по сырой строке, а не по цифрам.
_KPP_LETTERS = re.compile(r"(?<![0-9A-Za-zА-Яа-яЁё])(?=\d{4}[0-9A-Z]{0,2}[A-Z])\d{4}[0-9A-Z]{2}\d{3}(?![0-9A-Za-zА-Яа-яЁё])")


def _kpp_letters(line: str, pos: int):
    """КПП с буквами в 5–6 позициях сразу после метки/позиции pos: (значение, конец) или None."""
    m = _SKIP.match(line, pos)
    k = _KPP_LETTERS.match(line, m.end() if m else pos)
    return (k.group(), k.end()) if k else None


def _raw(tokens) -> str:
    return "".join(t for t, _, _ in tokens)


def _digits(raw: str) -> str:
    return re.sub(r"\D", "", raw.translate(_LOOK))


def _has_lookalike(raw: str) -> bool:
    return bool(re.search(r"[OОoоIlЗSB]", raw)) and bool(re.search(r"\d", raw))


# ---------- прописью ----------

def _norm(s: str) -> str:
    return s.lower().replace("ё", "е")


_NUMERALS = {
    0: "ноль нуль ноля нуля",
    1: "один одна одно одного одной одному одним одну одном одних",
    2: "два две двух двум двумя",
    3: "три трех трем тремя",
    4: "четыре четырех четырем четырьмя",
    5: "пять пяти пятью",
    6: "шесть шести шестью",
    7: "семь семи семью",
    8: "восемь восьми восемью восьмью",
    9: "девять девяти девятью",
    10: "десять десяти десятью",
    20: "двадцать двадцати двадцатью",
    30: "тридцать тридцати тридцатью",
    40: "сорок сорока",
    50: "пятьдесят пятидесяти пятьюдесятью",
    60: "шестьдесят шестидесяти шестьюдесятью",
    70: "семьдесят семидесяти семьюдесятью",
    80: "восемьдесят восьмидесяти восемьюдесятью",
    90: "девяносто девяноста",
    100: "сто ста",
    200: "двести двухсот двумстам двумястами двухстах",
    300: "триста трехсот тремстам тремястами трехстах",
    400: "четыреста четырехсот четыремстам четырьмястами четырехстах",
    500: "пятьсот пятисот пятистам пятьюстами пятистах",
    600: "шестьсот шестисот шестистам шестьюстами шестистах",
    700: "семьсот семисот семистам семьюстами семистах",
    800: "восемьсот восьмисот восьмистам восемьюстами восьмистах",
    900: "девятьсот девятисот девятистам девятьюстами девятистах",
}
_TEENS = {11: "одиннадцат", 12: "двенадцат", 13: "тринадцат", 14: "четырнадцат", 15: "пятнадцат",
          16: "шестнадцат", 17: "семнадцат", 18: "восемнадцат", 19: "девятнадцат"}
WORD_VAL = {}
for _v, _ws in _NUMERALS.items():
    for _w in _ws.split():
        WORD_VAL[_w] = _v
for _v, _stem in _TEENS.items():
    for _end in ("ь", "и", "ью"):
        WORD_VAL[_stem + _end] = _v
SCALE = [(re.compile(r"тысяч\w*"), 10 ** 3), (re.compile(r"миллион\w*"), 10 ** 6),
         (re.compile(r"миллиард\w*"), 10 ** 9), (re.compile(r"триллион\w*"), 10 ** 12)]
# слова рядом с суммой, не меняющие значения
FILLER = re.compile(r"(?:и|руб(?:л\w*|\.)?|коп(?:е\w*|\.)?|процент\w*|цел\w*|рос\w*|"
                    r"евро|доллар\w*|центов?|цент)$")


def words_to_int(words: str):
    """Число из русских числительных (до триллионов, любой падеж и род) или None, если в
    тексте есть слова, не являющиеся числительными. Второй результат — сколько числительных."""
    toks = re.findall(r"[а-яё]+", _norm(words))
    total = cur = n = 0
    for t in toks:
        if t in WORD_VAL:
            cur += WORD_VAL[t]
            n += 1
            continue
        sc = next((m for rx, m in SCALE if rx.fullmatch(t)), None)
        if sc:
            total += (cur or 1) * sc
            cur = 0
            n += 1
            continue
        if FILLER.fullmatch(t):
            continue
        return None, 0
    return (total + cur if n else None), n


_SP = "[ \u00a0\u202f]"
_AMT_AFTER = re.compile(
    rf"(?<![\d:/.,\-])(?<!\d{_SP})(?<![.,]{_SP})(?P<num>\d+(?:{_SP}\d{{3}})*)(?P<frac>[.,]{_SP}?\d{{1,3}})?"
    r"(?:\s*(?P<unit>(?:[^\W\d_]{1,8}\.?){1,2}))?\s*\(\s*(?P<words>[^()]{3,200}?)\s*\)")
_AMT_BEFORE = re.compile(
    r"\(\s*(?P<num>\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?P<frac>[.,]\d+)?\s*(?:руб[а-я]*\.?|₽|р\.)?\s*\)")
_SCALE_UNIT = {"тыс": 10 ** 3, "млн": 10 ** 6, "млрд": 10 ** 9}


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def check_amounts(line: str):
    """[(фрагмент, проблема)] — число цифрами и прописью в одной строке."""
    out = []
    for m in _AMT_AFTER.finditer(line):
        words = m.group("words")
        if re.search(r"\d", re.sub(r"\d+\s*/\s*\d+", "", words)):
            continue   # в скобках что-то кроме числительных
        digits = int(re.sub(r"\D", "", m.group("num")))
        unit = _norm(m.group("unit") or "").rstrip(".")
        mult = _SCALE_UNIT.get(unit, 1)
        wl = _norm(words)
        frac = re.sub(r"\D", "", m.group("frac") or "")
        # варианты чтения цифр: «50.000» — тысячи; «2,10» — рубли и копейки; «565,7» — десятичная дробь
        cands = [digits * mult]
        if len(frac) == 3 and not unit:
            cands.append(int(str(digits) + frac))
        frac_w = None
        kop_w = None
        if "цел" in wl:   # «565,7 (пятьсот шестьдесят пять целых и 7/10)»
            head, tail = re.split(r"цел\w*", wl, maxsplit=1)
            fm = re.search(r"(\d+)\s*/\s*(\d+)", tail)
            frac_w = Fraction(int(fm.group(1)), int(fm.group(2))) if fm and int(fm.group(2)) else None
            val, n = words_to_int(head)
        elif re.search(r"руб\w*", wl) and re.search(r"коп", wl):   # «два рубля десять копеек»
            head, tail = re.split(r"руб\w*", wl, maxsplit=1)
            val, n = words_to_int(head)
            kop_w = words_to_int(tail)[0]
        else:
            val, n = words_to_int(wl)
        if val is None or n == 0:
            continue
        shown = line[m.start():m.end()].strip()
        if val not in cands:
            out.append((shown, f"цифрами {_fmt(cands[0])}, прописью {_fmt(val)}"))
        elif frac_w is not None and frac and val == cands[0]:
            dec = Fraction(int(frac), 10 ** len(frac))
            if dec != frac_w:
                out.append((shown, f"дробная часть цифрами {m.group('frac')}, прописью {frac_w}"))
        elif kop_w is not None and frac and val == cands[0] and int((frac + "0")[:2]) != kop_w:
            out.append((shown, f"копейки цифрами {frac}, прописью {kop_w}"))
    for m in _AMT_BEFORE.finditer(line):
        toks = [(t.group(), t.start()) for t in re.finditer(r"[а-яё]+", _norm(line[:m.start()]))][-14:]
        take, started, first = [], False, m.start()
        for t, pos in reversed(toks):
            if t in WORD_VAL or any(rx.fullmatch(t) for rx, _ in SCALE):
                take.append(t)
                started, first = True, pos
            elif not started and FILLER.fullmatch(t):
                continue          # «… тысяч рублей (75 000)»: слова после числительных не берём
            else:
                break
        val, n = words_to_int(" ".join(reversed(take))) if take else (None, 0)
        if val is None or n == 0:
            continue
        digits = int(re.sub(r"\D", "", m.group("num")))
        if digits != val:
            out.append((line[first:m.end()].strip(),
                        f"прописью {_fmt(val)}, цифрами {m.group('num').strip()}"))
    return out


# ---------- доля и процент ----------

VULGAR = {"¼": Fraction(1, 4), "½": Fraction(1, 2), "¾": Fraction(3, 4), "⅓": Fraction(1, 3),
          "⅔": Fraction(2, 3), "⅕": Fraction(1, 5), "⅖": Fraction(2, 5), "⅗": Fraction(3, 5),
          "⅘": Fraction(4, 5), "⅙": Fraction(1, 6), "⅚": Fraction(5, 6), "⅛": Fraction(1, 8),
          "⅜": Fraction(3, 8), "⅝": Fraction(5, 8), "⅞": Fraction(7, 8)}
FRAC_RE = re.compile(r"(?<![\d/.:\-])(?P<a>\d{1,3})\s?/\s?(?P<b>\d{1,3})(?![\d/])(?![.:\-]\d)|(?P<v>[¼½¾⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞])")
PCT_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[.,]\d+)?)\s?(?:%|процент\w*)")
ABBR = re.compile(r"(?:^|\s)(?:п|пп|ст|ч|г|гг|кв|м|им|ул|д|стр|рис|т|тел|руб|коп|т\.е|напр|см)$", re.I)


def _sentences(line: str):
    """Предложения строки: граница — «. » / «; » перед заглавной, кроме сокращений («п. 3», «кв. м»)."""
    parts, start = [], 0
    for m in re.finditer(r"[.;!?]\s+(?=[А-ЯЁA-Z«\"])", line):
        if ABBR.search(line[start:m.start()]) or re.search(r"\d$", line[start:m.start()]) and m.group().startswith("."):
            continue
        parts.append(line[start:m.end()])
        start = m.end()
    parts.append(line[start:])
    return parts


SHARE_WORD = r"(?:доли|доля|долю|долей|долях|долею|части|часть)"
NOT_SHARE = re.compile(r"голос|кворум|большинств|тираж|ставк", re.I)


def check_fractions(line: str):
    """Доля (¾, 1/4 доли) и процент рядом, в одном предложении: ни одна доля не равна ни одному
    проценту. Только доли, названные словом «доля/часть» вплотную к дроби; про голоса и кворум
    («2/3 голосов, более 50 %») — разные величины, их не сверяем."""
    out = []
    for sent in _sentences(line):
        if re.search(r"(?<![а-яё])(?:остав|остальн|прочи|разниц)", sent, re.I) or NOT_SHARE.search(sent):
            continue
        sn = _norm(sent)
        fr = []
        for m in FRAC_RE.finditer(sent):
            if m.group("v"):
                f = VULGAR[m.group("v")]
            else:
                a, b = int(m.group("a")), int(m.group("b"))
                if not (0 < a <= b <= 100 and b > 1):
                    continue
                f = Fraction(a, b)
            # «пятьсот шестьдесят пять целых и 8/10» — десятичная запись, не доля
            if re.search(r"цел\w*\s+(?:и\s+)?$", _norm(sent[max(0, m.start() - 20):m.start()])):
                continue
            # дробь вплотную к слову «доля/часть»: «1/4 доли», «доля в размере 1/4»
            after = sn[m.end():m.end() + 12]
            before = sn[max(0, m.start() - 30):m.start()]
            if not (re.match(rf"\s*(?:\(?\s*)?{SHARE_WORD}", after) or re.search(rf"{SHARE_WORD}[^.;]{{0,20}}$", before)):
                continue
            if f <= 1:
                fr.append((m.group().strip(), f, m.start()))
        pc = [(m.group().strip(), float(m.group(1).replace(",", ".")), m.start()) for m in PCT_RE.finditer(sent)]
        if not fr or not pc:
            continue
        # процент, относящийся к доле, стоит недалеко от неё (до 160 знаков)
        pc = [x for x in pc if any(abs(x[2] - y[2]) <= 160 for y in fr)]
        if not pc or any(abs(float(f) * 100 - p) <= 0.51 for _, f, _ in fr for _, p, _ in pc):
            continue
        fs = ", ".join(f"{t} = {float(f) * 100:g} %" for t, f, _ in fr[:3])
        out.append((" … ".join([fr[0][0], pc[0][0]]), f"доля {fs}, а в том же предложении {', '.join(t for t, _, _ in pc[:3])}"))
    return out


# ---------- разбор строк ----------

def _clean(line: str) -> str:
    line = re.sub(r"<[^>]+>", " | ", line)
    line = line.replace("\\|", "|").replace("**", "").replace("&nbsp;", " ")
    return line.replace("[?]", "")      # пометка чтения глазами «прочитано неуверенно» — не часть числа


def _ctx(line: str, a: int, b: int) -> str:
    s = line[max(0, a - 25):b + 25].strip()
    return re.sub(r"\s+", " ", s)[:110]


def _lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _near(a: str, b: str, kind: str) -> bool:
    if a == b or min(len(a), len(b)) < 6 or abs(len(a) - len(b)) > 1:
        return False
    d = _lev(a, b)
    if d > (2 if min(len(a), len(b)) >= 9 else 1):
        return False
    if kind in ("р/с", "к/с") and len(a) == len(b) == 20:
        diff = [i for i in range(20) if a[i] != b[i]]
        if diff and all(5 <= i <= 7 for i in diff):
            return False      # отличается только код валюты (810/840/978): это разные счета
    return True


def _entity(ents, page, kind, raw, digits, ctx, ok=True, problem=""):
    ents.append({"page": page, "kind": kind, "raw": raw, "digits": digits, "ctx": ctx,
                 "ok": ok, "problem": problem})


MARKS = ("так в оригинале", "обрезано", "?")


def check_md(text: str, include_marked: bool = False) -> list[dict]:
    """Найти в тексте `.md` подозрительные реквизиты и числа. Страница — из ближайшего выше
    заголовка «## Страница N». Числа, сразу за которыми стоит пометка сверки по картинке
    («[так в оригинале…]», «[обрезано…]», «[?]»), не возвращаются (`include_marked=True` —
    вернуть с полем `marked`)."""
    page = None
    ents: list[dict] = []
    findings: list[dict] = []
    biks: dict = {}

    def add(pg, kind, value, problem, ctx=""):
        findings.append({"page": pg, "kind": kind, "value": value, "problem": problem, "context": ctx})

    lines = text.splitlines()
    for idx, raw_line in enumerate(lines):
        pm = PAGE_RE.match(raw_line.strip())
        if pm:
            page = int(pm.group(1))
            continue
        if raw_line.lstrip().startswith(SERVICE):
            continue
        line = _clean(raw_line)
        if not line.strip():
            continue
        # метка в конце строки, число — в следующей (таблицы, списки)
        if re.search(LABEL_RE.pattern + r"[\s:|]*$", line, re.I) and idx + 1 < len(lines):
            nxt = next((_clean(l) for l in lines[idx + 1:idx + 4] if l.strip()), "")
            if nxt and nxt.lstrip()[:1].isdigit() and not nxt.lstrip().startswith(SERVICE):
                line = line.rstrip(" :|") + " " + nxt.lstrip(" |")

        for m in LABEL_RE.finditer(line):
            gi = next(i for i in range(len(LABELS)) if m.group(f"g{i}"))
            label = LABELS[gi][0]
            if label == "ИНН/КПП":
                _check_inn_kpp(line, m, page, ents, add, biks)
                continue
            if label == "КПП":
                kl = _kpp_letters(line, m.end())
                if kl:
                    _entity(ents, page, "КПП", kl[0], kl[0], _ctx(line, m.start(), kl[1]))
                    continue
            toks = _tokens_after(line, m.end())
            if not toks:
                continue
            lens = {"ИНН": (10, 12), "ОГРН": (13, 15), "ОГРНИП": (13, 15), "КПП": (9,), "СНИЛС": (11,),
                    "БИК": (9,), "р/с": (20,), "к/с": (20,)}[label]
            chosen = _pick(toks, lens)
            raw = _raw(chosen)
            d = _digits(raw)
            if len(d) < 4:
                continue
            if _UNREADABLE.match(line, chosen[-1][2]):
                continue      # «ОГРН 1207[неразборчиво]»: читавший сам сказал, что число неполное
            ctx = _ctx(line, m.start(), chosen[-1][2])
            _check_one(label, raw, d, page, ents, add, ctx, biks)

        for pm2 in PASSPORT_RE.finditer(line):
            d = re.sub(r"\D", "", pm2.group("ser") + pm2.group("num"))
            _entity(ents, page, "паспорт", d, d, _ctx(line, pm2.start(), pm2.end()))

        for val, prob in check_amounts(line):
            add(page, "сумма прописью", val, prob, re.sub(r"\s+", " ", val)[:110])
        for val, prob in check_fractions(line):
            add(page, "доля и процент", val, prob, "")

    # ключи счетов по БИК той же страницы
    for e in ents:
        if e["kind"] in ("р/с", "к/с") and e["ok"] and len(e["digits"]) == 20 \
                and not e["digits"].startswith(NO_KEY_PREFIXES):
            bl = biks.get(e["page"])
            e["keyed"] = bool(bl)
            if bl and not any(account_key_ok(b, e["digits"], e["kind"]) for b in bl):
                e["ok"] = False
                e["problem"] = ("ключ счёта не сходится с БИК " + ", ".join(sorted(bl))
                                + " (ошибка в счёте или в БИК)")

    # разные написания одного реквизита
    bad = {(e["kind"], e["digits"]) for e in ents if not e["ok"]}
    by_kind: dict = {}
    for e in ents:
        if e["digits"]:
            by_kind.setdefault(e["kind"], {}).setdefault(e["digits"], []).append(e["page"])
    keyed = {(e["kind"], e["digits"]) for e in ents if e.get("keyed") and e["ok"]}
    related: dict = {}
    for kind, vals in by_kind.items():
        if kind in ("БИК", "КПП", "СНИЛС"):
            continue
        keys = sorted(vals)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                near = _near(a, b, kind)
                # обрезанный/удлинённый счёт делит с настоящим начало: банк и владелец те же
                same_acc = kind in ("р/с", "к/с") and len(a) >= 14 and len(b) >= 14 and a[:14] == b[:14]
                if not (near or same_acc):
                    continue
                related.setdefault((kind, a), set()).add(b)
                related.setdefault((kind, b), set()).add(a)
                if (kind, a) in bad or (kind, b) in bad or not near:
                    continue      # у неверного варианта и так будет находка с подсказкой
                if kind in CHECKSUM_KINDS or ((kind, a) in keyed and (kind, b) in keyed):
                    continue      # оба варианта проходят проверку: это соседние законные номера (ИНН, ОГРН
                                  # и счета того же банка бывают в двух цифрах друг от друга)
                # оба варианта правдоподобны: «меньшинство» (реже встречается) — подозреваемый
                small, big = sorted((a, b), key=lambda v: (len(vals[v]), v))
                pg = min((p for p in vals[small] if p is not None), default=None)
                add(pg, kind, small, f"написан иначе: {small} ({_pages(vals[small])}) и {big} ({_pages(vals[big])}), "
                                      f"отличие в {_lev(a, b)} цифр(е)", "")

    # собственные находки сущностей; подсказка про близкий вариант
    valid_vals = {(e["kind"], e["digits"]) for e in ents if e["ok"]}
    for e in ents:
        if not e["ok"]:
            hint = ""
            rel = sorted(related.get((e["kind"], e["digits"]), ()))
            good = [v for v in rel if (e["kind"], v) in valid_vals]
            if not good:    # для подсказки допустимо и более далёкое сходство: до 3 правок у длинных чисел
                cands = [v for v in by_kind.get(e["kind"], {}) if (e["kind"], v) in valid_vals
                         and len(v) >= 12 and abs(len(v) - len(e["digits"])) <= 1 and _lev(v, e["digits"]) <= 3]
                good = sorted(cands, key=lambda v: (-len(by_kind[e["kind"]][v]), v))[:1]
            if good:
                hint = f"; в этом же документе есть {good[0]} ({_pages(by_kind[e['kind']][good[0]])}), проходящий проверку"
            elif rel:
                hint = f"; похоже на {rel[0]} ({_pages(by_kind[e['kind']][rel[0]])})"
            add(e["page"], e["kind"], e["raw"], e["problem"] + hint, e["ctx"])

    # Уже разобранное при сверке по картинке не повторять: число с пометкой сразу после него
    # («[так в оригинале…]», «[обрезано…]», «[?]») — человек его видел.
    segs: dict = {}
    cur = None
    for ln in lines:
        pm = PAGE_RE.match(ln.strip())
        if pm:
            cur = int(pm.group(1))
            continue
        if not ln.lstrip().startswith(SERVICE) and "✎" not in ln[:6]:
            segs.setdefault(cur, []).append(ln)

    def marked(f) -> bool:
        seg = "\n".join(segs.get(f["page"], lines))
        v = f["value"]
        pat = r"[\s\-]?".join(map(re.escape, v)) if v.isdigit() else re.escape(v)
        hits = list(re.finditer(r"(?<!\d)" + pat + r"(?!\d)\s*(\[[^\]]*\])?", seg))
        return bool(hits) and all(h.group(1) and h.group(1).lstrip("[ ").lower().startswith(MARKS) for h in hits)

    seen, out = set(), []
    for f in findings:
        k = (f["page"], f["kind"], f["value"], f["problem"])
        if k not in seen:
            seen.add(k)
            if not (include_marked or marked(f)):
                out.append(f)
            elif include_marked:
                f["marked"] = marked(f)
                out.append(f)
    out.sort(key=lambda f: (f["page"] if f["page"] is not None else 0))
    return out


def _pages(pages) -> str:
    ps = sorted({p for p in pages if p is not None})
    return "стр. " + ", ".join(map(str, ps[:6])) + (" …" if len(ps) > 6 else "") if ps else "без страницы"


def _check_inn_kpp(line, m, page, ents, add, biks):
    """«ИНН/КПП 7704123456/770401001»: две цифровые группы через косую черту."""
    rest = line[m.end():]
    k = re.search(r"[/\\]", rest)
    left, right = (rest[:k.start()], rest[k.end():]) if k else (rest, "")
    ctx = _ctx(line, m.start(), m.end() + (k.end() + 14 if k else 14))
    for label, part, lens in (("ИНН", left, (10, 12)), ("КПП", right, (9,))):
        if label == "КПП":
            kl = _kpp_letters(part, 0)
            if kl:
                _entity(ents, page, "КПП", kl[0], kl[0], ctx)
                continue
        toks = _tokens_after(part, 0) if part.strip() else []
        if toks:
            chosen = _pick(toks, lens)
            _check_one(label, _raw(chosen), _digits(_raw(chosen)), page, ents, add, ctx, biks)


def _check_one(label, raw, d, page, ents, add, ctx, biks):
    if _has_lookalike(raw):
        add(page, label, raw, "в номере буква, похожая на цифру (OCR)", ctx)
    if label == "ИНН":
        if len(d) in (19, 21):                 # ИНН и КПП подряд без метки между ними
            d = d[:-9]
        if len(d) not in (10, 12):
            _entity(ents, page, "ИНН", raw, d, ctx, False, f"{len(d)} цифр вместо 10 или 12")
            return
        ok, want = inn_check(d)
        _entity(ents, page, "ИНН", raw, d, ctx, ok, "" if ok else f"контрольные цифры не сходятся (должны быть {want})")
    elif label in ("ОГРН", "ОГРНИП"):
        # по метке и длине: «ОГРН» из 15 цифр — это ОГРНИП, «ОГРНИП» из 13 — ошибка метки или цифр
        kind = "ОГРНИП" if (label == "ОГРНИП" or len(d) == 15) else "ОГРН"
        need = 15 if kind == "ОГРНИП" else 13
        if len(d) != need:
            _entity(ents, page, kind, raw, d, ctx, False, f"{len(d)} цифр вместо {need}"
                    + (" (у ОГРНИП — 15)" if kind == "ОГРН" else ""))
            return
        ok, want = ogrn_check(d)
        msgs = []
        if not ok:
            msgs.append(f"контрольная цифра не сходится (должна быть {want})")
        if kind == "ОГРН" and d[0] not in "125":      # 2… встречается в реальных текстах (контрольная цифра сходится)
            msgs.append("ОГРН начинается с 1 или 5")
        if kind == "ОГРНИП" and d[0] not in "34":
            msgs.append("ОГРНИП начинается с 3 (у КФХ с 4)")
        _entity(ents, page, kind, raw, d, ctx, not msgs, "; ".join(msgs))
    elif label == "СНИЛС":
        if len(d) != 11:
            _entity(ents, page, "СНИЛС", raw, d, ctx, False, f"{len(d)} цифр вместо 11")
            return
        ok, want = snils_check(d)
        _entity(ents, page, "СНИЛС", raw, d, ctx, ok or int(d[:9]) <= 1001998,
                "" if ok or int(d[:9]) <= 1001998 else f"контрольное число не сходится (должно быть {want})")
    elif label == "БИК":
        # 04… — банки и подразделения Банка России, 01… — территориальные органы казначейства (ЕКС)
        ok = len(d) == 9 and d[:2] in ("04", "01")
        _entity(ents, page, "БИК", raw, d, ctx, ok, "" if ok else
                ("БИК: " + (f"{len(d)} цифр вместо 9" if len(d) != 9 else "начинается не с 04 (и не с 01 — казначейство)")))
        if ok:
            biks.setdefault(page, set()).add(d)
    elif label == "КПП":
        ok = bool(re.fullmatch(r"\d{4}[0-9A-Z]{2}\d{3}", d))
        _entity(ents, page, "КПП", raw, d, ctx, ok, "" if ok else f"КПП: {len(d)} знаков вместо 9")
    elif label in ("р/с", "к/с"):
        if len(d) != 20:
            # «на расчётном счёте 33 000 руб.»: короткое число после метки — сумма, а не счёт;
            # потерянную цифру ищем только у чисел, похожих на счёт по длине
            if 15 <= len(d) <= 25:
                _entity(ents, page, label, raw, d, ctx, False, f"{len(d)} цифр вместо 20")
            return
        _entity(ents, page, label, raw, d, ctx, True)


# ---------- командная строка ----------

def md_files(target: Path):
    if target.is_file():
        return [target]
    return sorted(p for p in target.rglob("*.md")
                  if not p.name.startswith("_") and not p.name.endswith(".markitdown.md"))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    target = Path(args[0]).expanduser()
    if not target.exists():
        print(f"нет такого пути: {target}", file=sys.stderr)
        return 2
    result, total = {}, 0
    for f in md_files(target):
        try:
            res = check_md(f.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:  # noqa: BLE001
            print(f"{f}: ошибка {type(e).__name__}: {e}", file=sys.stderr)
            continue
        result[str(f)] = res
        total += len(res)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    for fname, res in result.items():
        if not res:
            continue
        try:
            shown = str(Path(fname).relative_to(target)) if target.is_dir() else Path(fname).name
        except ValueError:
            shown = fname
        print(f"\n{shown}  ({len(res)})")
        for r in res:
            pg = f"стр. {r['page']}" if r["page"] is not None else "—"
            print(f"  {pg:8s} {r['kind']}: {r['value']} — {r['problem']}")
            if r.get("context"):
                print(f"           … {r['context']} …")
    print(f"\nфайлов проверено: {len(result)}, находок: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
