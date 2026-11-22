#!/usr/bin/env python3
"""Делает безопасный тестовый набор для проверки установки: вымышленные документы,
никаких реальных данных. Запуск лёгким окружением:

    ~/Projects/markitdown-local/.venv/bin/python make_test_set.py ~/Desktop/тест-OCR

В папке появятся шесть файлов и ожидаемые результаты (см. инструкцию):

    01_договор_с_текстом.pdf   PDF с текстовым слоем и таблицей   -> pdfplumber/pymupdf4llm
    02_договор_скан.pdf        тот же договор картинкой, без текста -> OCR
    03_договор_фото.jpg        тот же договор «как фото с телефона» -> OCR
    04_расчёт.xlsx             таблица с числами и формулой        -> markitdown + CSV
    05_договор_tiff.tif        тот же договор двумя страницами TIFF -> OCR, обе страницы
    06_иск_со_сканом.pdf       текст, скан, текст в одном PDF      -> текстовый слой + OCR стр. 2 на её место

Шрифты берутся из самого PyMuPDF (кириллица есть), системные не нужны, поэтому
скрипт работает одинаково на macOS, Windows и Linux.
"""
from __future__ import annotations

import random
import sys
from io import BytesIO
from pathlib import Path

import pymupdf
from PIL import Image, ImageFilter
import openpyxl

HTML = """
<h2 style="text-align:center">ДОГОВОР ВОЗМЕЗДНОГО ОКАЗАНИЯ УСЛУГ № 17/2026</h2>
<p style="text-align:center">(вымышленный образец для проверки программы)</p>
<p>г. Москва, 15 января 2026 г.</p>
<p>Общество с ограниченной ответственностью «Ромашка» (ИНН 7700000001), в лице директора
Ивановой Анны Петровны, именуемое «Исполнитель», и гражданин Сидоров Пётр Николаевич,
именуемый «Заказчик», заключили настоящий договор о нижеследующем.</p>
<p><b>1. Предмет договора.</b> Исполнитель обязуется оказать Заказчику юридические услуги,
перечисленные в таблице, а Заказчик обязуется их оплатить.</p>
<table border="1" cellpadding="4" width="100%">
<tr><td><b>№</b></td><td><b>Услуга</b></td><td><b>Срок</b></td><td><b>Стоимость, руб.</b></td></tr>
<tr><td>1</td><td>Консультация</td><td>20.01.2026</td><td>15 000,00</td></tr>
<tr><td>2</td><td>Подготовка претензии</td><td>25.01.2026</td><td>40 000,00</td></tr>
<tr><td>3</td><td>Представительство в суде</td><td>10.02.2026</td><td>70 000,00</td></tr>
<tr><td colspan="3"><b>Итого</b></td><td><b>125 000,00</b></td></tr>
</table>
<p><b>2. Оплата.</b> Заказчик оплачивает услуги в течение 5 (пяти) рабочих дней
с даты подписания акта оказанных услуг.</p>
<p><b>3. Ответственность.</b> За просрочку оплаты Заказчик уплачивает неустойку в размере
0,1% от неоплаченной суммы за каждый день просрочки.</p>
<p><b>4. Реквизиты.</b> Исполнитель: р/с 40702810000000000017 в АО «Тестбанк», БИК 044525999.</p>
<p>Подписи сторон: ____________ / Иванова А. П.&nbsp;&nbsp;&nbsp;&nbsp;____________ / Сидоров П. Н.</p>
"""


def make_text_pdf(path: Path) -> None:
    story = pymupdf.Story(html=HTML, user_css="body{font-size:11pt} td{font-size:10.5pt}")
    writer = pymupdf.DocumentWriter(str(path))
    page_rect = pymupdf.paper_rect("a4")
    where = page_rect + (56, 56, -56, -56)
    more = 1
    while more:
        dev = writer.begin_page(page_rect)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()


def render(path: Path, dpi: int) -> Image.Image:
    with pymupdf.open(path) as doc:
        pix = doc[0].get_pixmap(dpi=dpi)
    return Image.open(BytesIO(pix.tobytes("png"))).convert("RGB")


def make_scan(text_pdf: Path, out: Path) -> None:
    img = render(text_pdf, 200)
    doc = pymupdf.open()
    page = doc.new_page(width=595.28, height=841.89)
    buf = BytesIO()
    img.save(buf, "PNG")
    page.insert_image(page.rect, stream=buf.getvalue())
    doc.save(out)  # в файле только картинка, текстового слоя нет


def make_photo(text_pdf: Path, out: Path) -> None:
    random.seed(7)
    img = render(text_pdf, 220).rotate(2.5, expand=True, fillcolor=(205, 200, 190), resample=Image.BICUBIC)
    img = img.filter(ImageFilter.GaussianBlur(0.8))
    px = img.load()
    w, h = img.size
    for y in range(h):          # неравномерный свет, как у фото на столе
        k = 0.78 + 0.22 * (1 - y / h)
        for x in range(0, w):
            r, g, b = px[x, y]
            px[x, y] = (int(r * k), int(g * k), int(b * k))
    img.save(out, "JPEG", quality=68)


def make_tiff(text_pdf: Path, out: Path) -> None:
    """Многостраничный TIFF, как его пишет сканер «в один файл»: две страницы-кадра.
    Проверяет, что распознаются все кадры, а не только первый."""
    img = render(text_pdf, 150).convert("L")
    img.save(out, "TIFF", save_all=True, append_images=[img.copy()], compression="tiff_deflate")


def make_mixed(text_pdf: Path, scan_pdf: Path, out: Path) -> None:
    """Иск с приложением-сканом: страница с текстом, страница-скан, снова текст. Скан не должен
    потеряться — его распознаёт OCR и вставляет на место второй страницы."""
    doc = pymupdf.open()
    for f in (text_pdf, scan_pdf, text_pdf):
        with pymupdf.open(f) as part:
            doc.insert_pdf(part)
    doc.save(out)


def make_xlsx(out: Path) -> None:
    """Расчёт неустойки по месяцам. Значения записаны числами, не формулами:
    так файл читается одинаково везде (формулы без пересчёта в Excel не имеют значений)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Расчёт"
    ws.append(["Месяц", "Долг, руб.", "Ставка, % в день", "Дней", "Неустойка, руб."])
    months = [("Январь", 31), ("Февраль", 28), ("Март", 31), ("Апрель", 30), ("Май", 31), ("Июнь", 30),
              ("Июль", 31), ("Август", 31), ("Сентябрь", 30), ("Октябрь", 31), ("Ноябрь", 30), ("Декабрь", 31)]
    total = 0.0
    for name, days in months:
        penalty = round(125000 * 0.1 / 100 * days, 2)
        total += penalty
        ws.append([name, 125000, 0.1, days, penalty])
    ws.append(["Итого", None, None, sum(d for _, d in months), round(total, 2)])
    wb.save(out)


def main() -> None:
    dst = Path(sys.argv[1] if len(sys.argv) > 1 else "тест-OCR").expanduser()
    dst.mkdir(parents=True, exist_ok=True)
    text_pdf = dst / "01_договор_с_текстом.pdf"
    make_text_pdf(text_pdf)
    make_scan(text_pdf, dst / "02_договор_скан.pdf")
    make_photo(text_pdf, dst / "03_договор_фото.jpg")
    make_xlsx(dst / "04_расчёт.xlsx")
    make_tiff(text_pdf, dst / "05_договор_tiff.tif")
    make_mixed(text_pdf, dst / "02_договор_скан.pdf", dst / "06_иск_со_сканом.pdf")
    print("Готово:", *sorted(p.name for p in dst.iterdir()), sep="\n  ")


if __name__ == "__main__":
    main()
