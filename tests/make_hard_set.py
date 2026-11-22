#!/usr/bin/env python3
"""Сложный тестовый набор convert-docs: вымышленные документы с известным текстом и трудными сочетаниями.

    ~/Projects/markitdown-local/.venv/bin/python tests/make_hard_set.py <папка>

Каждая страница несёт свои контрольные значения (шифр, сумма, дата, счёт, фамилия и три числа таблицы),
поэтому проверку делает скрипт, а не человек: `tests/check_hard_set.py`. Ожидания по маршруту и
содержимому пишутся в `<папка>/../expected.json` (рядом с набором, не внутри него).

Сочетания: PDF с текстом и вшитыми сканами (один боком) и пустым листом; сканы вверх ногами и боком;
Word из сканов; Word с текстом и большой картинкой; TIFF из трёх кадров (один боком, один плохого
качества); фото таблицы с перспективой и размытием; плохой скан 100 dpi с шумом и наклоном; скан с
негодным и с хорошим вшитым OCR-слоем; побайтовый дубль; фото HEIC (iPhone); фото без текста; старый
.doc; Excel из двух листов; презентация из сканов; текстовый PDF, повёрнутый на 90°; аудио; архив.
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

import openpyxl
import pymupdf
from PIL import Image, ImageDraw, ImageFilter
from pptx import Presentation
from pptx.util import Inches

SURNAMES = ["Воронцова", "Белозёров", "Кириленко", "Шаповалова", "Ермолаев", "Цыганков", "Юдашкина",
            "Мельниченко", "Сафронова", "Тарасевич", "Логвиненко", "Пономарёва", "Гаврилюк", "Остапенко"]
ARIAL = "/System/Library/Fonts/Supplemental/Arial.ttf"


def values(d: int, p: int) -> dict:
    """Контрольные значения страницы p документа d — у каждой страницы свои."""
    r = random.Random(d * 100 + p)
    return {
        "шифр": f"{d:02d}{p}-{r.randint(1000, 9999)}",
        "сумма": f"{r.randint(100, 999)} {r.randint(100, 999):03d},{r.randint(10, 99)}",
        "дата": f"{r.randint(1, 28):02d}.{r.randint(1, 12):02d}.2025",
        "счёт": f"40702810{r.randint(10**11, 10**12 - 1)}",
        "фамилия": SURNAMES[(d * 7 + p) % len(SURNAMES)],
        "таблица": [str(r.randint(1000, 99999)) for _ in range(3)],
    }


def control(v: dict) -> list[str]:
    return [v["шифр"], v["сумма"], v["дата"], v["счёт"], v["фамилия"], *v["таблица"]]


def page_html(d: int, p: int) -> tuple[str, str]:
    """HTML страницы и её простой текст (для невидимого слоя)."""
    v = values(d, p)
    t = v["таблица"]
    html = f"""
<h3 style="text-align:center">ДОКУМЕНТ № {v['шифр']}</h3>
<p style="text-align:center">(вымышленный образец для проверки программы, лист {p})</p>
<p>г. Москва, {v['дата']}</p>
<p>Ответственное лицо: {v['фамилия']} Анна Сергеевна. Настоящим подтверждается, что по договору
поставки начислено {v['сумма']} руб., которые подлежат перечислению на расчётный счёт
{v['счёт']} в АО «Тестбанк» в течение 10 (десяти) банковских дней.</p>
<table border="1" cellpadding="4" width="100%">
<tr><td><b>№</b></td><td><b>Наименование</b></td><td><b>Количество</b></td></tr>
<tr><td>1</td><td>Позиция первая</td><td>{t[0]}</td></tr>
<tr><td>2</td><td>Позиция вторая</td><td>{t[1]}</td></tr>
<tr><td>3</td><td>Позиция третья</td><td>{t[2]}</td></tr>
</table>
<p>Документ составлен в двух экземплярах, имеющих равную юридическую силу.</p>
"""
    text = (f"ДОКУМЕНТ № {v['шифр']}\nг. Москва, {v['дата']}\nОтветственное лицо: {v['фамилия']} Анна Сергеевна. "
            f"Начислено {v['сумма']} руб. на расчётный счёт {v['счёт']} в АО «Тестбанк».\n"
            f"1 Позиция первая {t[0]}\n2 Позиция вторая {t[1]}\n3 Позиция третья {t[2]}\n"
            "Документ составлен в двух экземплярах, имеющих равную юридическую силу.")
    return html, text


def text_page_pdf(d: int, p: int) -> pymupdf.Document:
    html, _ = page_html(d, p)
    buf = BytesIO()
    story = pymupdf.Story(html=html, user_css="body{font-size:11pt} td{font-size:10.5pt}")
    writer = pymupdf.DocumentWriter(buf)
    rect = pymupdf.paper_rect("a4")
    dev = writer.begin_page(rect)
    story.place(rect + (56, 56, -56, -56))
    story.draw(dev)
    writer.end_page()
    writer.close()
    return pymupdf.open("pdf", buf.getvalue())


def render(d: int, p: int, dpi: int = 200) -> Image.Image:
    with text_page_pdf(d, p) as doc:
        pix = doc[0].get_pixmap(dpi=dpi)
    return Image.open(BytesIO(pix.tobytes("png"))).convert("RGB")


def png_bytes(img: Image.Image, fmt: str = "PNG", **kw) -> bytes:
    b = BytesIO()
    img.save(b, fmt, **kw)
    return b.getvalue()


def add_image_page(doc: pymupdf.Document, img: Image.Image, text_layer: str | None = None) -> None:
    w, h = img.size
    page = doc.new_page(width=595.28, height=595.28 * h / w)
    page.insert_image(page.rect, stream=png_bytes(img, "JPEG", quality=85))
    if text_layer is not None:   # невидимый слой, как у сканов из Adobe Scan / FineReader
        page.insert_textbox(page.rect + (40, 40, -40, -40), text_layer, fontsize=10,
                            fontfile=ARIAL, fontname="ar", render_mode=3)


def noisy(img: Image.Image, amount: int, seed: int) -> Image.Image:
    r = random.Random(seed)
    px = img.load()
    w, h = img.size
    for _ in range(w * h // 40):
        x, y = r.randrange(w), r.randrange(h)
        c = max(0, min(255, px[x, y][0] + r.randint(-amount, amount)))
        px[x, y] = (c, c, c)
    return img


def photo_like(img: Image.Image, seed: int) -> Image.Image:
    """Фото телефоном: перспектива, лёгкий наклон, размытие, неравномерный свет, фон стола."""
    w, h = img.size
    bg = Image.new("RGB", (int(w * 1.15), int(h * 1.12)), (120, 95, 70))
    bg.paste(img, (int(w * 0.07), int(h * 0.05)))
    W, H = bg.size
    k = 0.06
    coeffs = _persp((0, 0, W, 0, W, H, 0, H), (W * k, H * 0.02, W * (1 - k * 0.4), 0, W, H, 0, H * 0.98))
    out = bg.transform((W, H), Image.PERSPECTIVE, coeffs, Image.BICUBIC, fillcolor=(110, 90, 65))
    out = out.rotate(1.8, expand=False, fillcolor=(110, 90, 65), resample=Image.BICUBIC)
    out = out.filter(ImageFilter.GaussianBlur(1.0))
    px = out.load()
    for y in range(0, H):
        f = 0.75 + 0.25 * (y / H)
        for x in range(0, W, 1):
            r_, g, b = px[x, y]
            px[x, y] = (int(r_ * f), int(g * f * 0.97), int(b * f * 0.92))
    return out


def _persp(src, dst):
    """Коэффициенты Image.PERSPECTIVE: точки результата (dst) → точки исходника (src)."""
    import numpy as np
    a = []
    for (x, y), (u, v) in zip(zip(dst[0::2], dst[1::2]), zip(src[0::2], src[1::2])):
        a.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        a.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    A = np.array(a, dtype=float)
    B = np.array(src, dtype=float)
    return np.linalg.solve(A, B).tolist()


def make_docx(path: Path, blocks: list[tuple]) -> None:
    """Минимальный .docx без python-docx: ("p", текст) и ("img", png, ширина_дюймы, высота_дюймы)."""
    body, rels, media = [], [], []
    for b in blocks:
        if b[0] == "p":
            t = b[1].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            body.append(f'<w:p><w:r><w:t xml:space="preserve">{t}</w:t></w:r></w:p>')
        else:
            n = len(media) + 1
            media.append((f"image{n}.png", b[1]))
            rels.append(f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                        f'relationships/image" Target="media/image{n}.png"/>')
            cx, cy = int(b[2] * 914400), int(b[3] * 914400)
            body.append(
                f'<w:p><w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/>'
                f'<wp:docPr id="{n}" name="Picture {n}"/><a:graphic><a:graphicData '
                f'uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:nvPicPr>'
                f'<pic:cNvPr id="{n}" name="image{n}.png"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill>'
                f'<a:blip r:embed="rId{n}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr>'
                f'<a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect">'
                f'<a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline>'
                f'</w:drawing></w:r></w:p>')
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document '
           'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
           'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
           'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
           'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><w:body>'
           + "".join(body) + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="567" w:right="567" '
           'w:bottom="567" w:left="567"/></w:sectPr></w:body></w:document>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                   'wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                   'Target="word/document.xml"/></Relationships>')
        z.writestr("word/_rels/document.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(rels) + "</Relationships>")
        z.writestr("word/document.xml", doc)
        for name, data in media:
            z.writestr(f"word/media/{name}", data)


def main() -> int:
    dst = Path(sys.argv[1] if len(sys.argv) > 1 else "сложный-набор").expanduser()
    if dst.exists() and any(dst.iterdir()):
        print(f"папка не пуста: {dst}", file=sys.stderr)
        return 2
    (dst / "Приложения").mkdir(parents=True, exist_ok=True)
    exp: dict = {}

    def pages_exp(d, ps):
        return {str(i): control(values(d, p)) for i, p in ps}

    # 01. PDF: текст, текст, скан, пустой лист, скан боком, текст
    doc = pymupdf.open()
    for p in (1, 2):
        with text_page_pdf(1, p) as t:
            doc.insert_pdf(t)
    add_image_page(doc, render(1, 3))
    doc.new_page(width=595.28, height=841.89)
    add_image_page(doc, render(1, 5).rotate(90, expand=True))
    with text_page_pdf(1, 6) as t:
        doc.insert_pdf(t)
    doc.save(dst / "01_иск_с_приложениями.pdf")
    exp["01_иск_с_приложениями.pdf"] = {"engines": ["pdfplumber|pymupdf4llm", "ocr"], "only_pages": [3, 5],
                                         "pages": pages_exp(1, [(1, 1), (2, 2), (3, 3), (5, 5), (6, 6)]),
                                         "empty_pages_flag": [4], "sections": 6}
    # 02. сканы: прямо, вверх ногами, боком
    doc = pymupdf.open()
    add_image_page(doc, render(2, 1))
    add_image_page(doc, render(2, 2).rotate(180))
    add_image_page(doc, render(2, 3).rotate(270, expand=True))
    doc.save(dst / "02_сканы_вверх_ногами_и_боком.pdf")
    exp["02_сканы_вверх_ногами_и_боком.pdf"] = {"engines": ["ocr"], "pages": pages_exp(2, [(1, 1), (2, 2), (3, 3)])}
    # 03. Word из сканов (две страницы-картинки, текста нет)
    make_docx(dst / "Приложения/03_word_из_сканов.docx",
              [("img", png_bytes(render(3, 1, 150)), 7.4, 10.4), ("img", png_bytes(render(3, 2, 150)), 7.4, 10.4)])
    exp["Приложения/03_word_из_сканов.docx"] = {"engines": ["ocr"], "pages": pages_exp(3, [(1, 1), (2, 2)])}
    # 04. Word с текстом и большой картинкой-сканом (текст — markitdown, картинка — пометка)
    _, txt = page_html(4, 1)
    make_docx(dst / "Приложения/04_word_текст_и_скан.docx",
              [("p", line) for line in txt.split("\n")] + [("img", png_bytes(render(4, 2, 150)), 7.0, 9.9)])
    exp["Приложения/04_word_текст_и_скан.docx"] = {"engines": ["markitdown"], "note": "картин",
                                                    "pages": {"1": control(values(4, 1))}}
    # 05. TIFF: три кадра — прямо, боком, плохого качества
    f1, f2 = render(5, 1, 150), render(5, 2, 150).rotate(90, expand=True)
    f3 = noisy(render(5, 3, 110), 70, 5)
    f1.convert("L").save(dst / "05_tiff_3_кадра.tif", "TIFF", save_all=True,
                         append_images=[f2.convert("L"), f3.convert("L")], compression="tiff_deflate")
    exp["05_tiff_3_кадра.tif"] = {"engines": ["ocr"], "pages": pages_exp(5, [(1, 1), (2, 2), (3, 3)])}
    # 06. фото таблицы: перспектива, размытие, свет, JPEG q=60
    photo_like(render(6, 1, 170), 6).save(dst / "06_фото_таблицы.jpg", "JPEG", quality=60)
    exp["06_фото_таблицы.jpg"] = {"engines": ["ocr"], "pages": {"1": control(values(6, 1))}}
    # 07. плохой скан таблицы: 100 dpi, шум, наклон 1,5°
    noisy(render(7, 1, 100), 80, 7).rotate(1.5, expand=True, fillcolor=(255, 255, 255)).save(dst / "07_плохой_скан_таблицы.png")
    exp["07_плохой_скан_таблицы.png"] = {"engines": ["ocr"], "pages": {"1": control(values(7, 1))}}
    # 08. скан с негодным вшитым слоем (смесь алфавитов) — должен уйти в OCR
    doc = pymupdf.open()
    junk = " ".join(["ПОЛIIОСlt311MCll", "ДОГОВOРa", "Мoсквa", "pаcчeтный", "cчeт", "Тeстбaнк"] * 40)
    add_image_page(doc, render(8, 1), text_layer=junk)
    doc.save(dst / "08_скан_с_мусорным_слоем.pdf")
    exp["08_скан_с_мусорным_слоем.pdf"] = {"engines": ["ocr"], "why": "негоден", "pages": {"1": control(values(8, 1))}}
    # 09. скан с хорошим вшитым слоем — быстрая ветка, без OCR; 10 — его побайтовый дубль
    doc = pymupdf.open()
    _, txt9 = page_html(9, 1)
    add_image_page(doc, render(9, 1), text_layer=txt9)
    doc.save(dst / "09_скан_с_хорошим_слоем.pdf")
    shutil.copy2(dst / "09_скан_с_хорошим_слоем.pdf", dst / "10_копия_09.pdf")
    # оригиналом пары route.py считает файл с более коротким именем; второй — «дубль»
    for a, b in (("09_скан_с_хорошим_слоем.pdf", "10_копия_09.pdf"), ("10_копия_09.pdf", "09_скан_с_хорошим_слоем.pdf")):
        exp[a] = {"engines": ["pdfplumber|pymupdf4llm|дубль"], "dup_pair": b, "pages": {"1": control(values(9, 1))}}
    # 11. фото HEIC (iPhone)
    with tempfile.TemporaryDirectory() as td:
        jpg = Path(td) / "x.jpg"
        photo_like(render(11, 1, 170), 11).save(jpg, "JPEG", quality=90)
        subprocess.run(["sips", "-s", "format", "heic", str(jpg), "--out", str(dst / "11_фото_айфон.heic")],
                       check=True, capture_output=True)
    exp["11_фото_айфон.heic"] = {"engines": ["ocr"], "pages": {"1": control(values(11, 1))}}
    # 12. фото без текста — в OCR не идёт (mode empty)
    img = Image.new("RGB", (1600, 1200), (90, 120, 150))
    dr = ImageDraw.Draw(img)
    r = random.Random(12)
    for _ in range(40):
        x, y = r.randrange(1600), r.randrange(1200)
        dr.ellipse([x, y, x + r.randrange(40, 300), y + r.randrange(40, 300)],
                   fill=(r.randrange(256), r.randrange(256), r.randrange(256)))
    img.filter(ImageFilter.GaussianBlur(3)).save(dst / "12_фото_без_текста.jpg", "JPEG", quality=85)
    exp["12_фото_без_текста.jpg"] = {"engines": ["ocr"], "no_text": True}
    # 13. старый .doc (LibreOffice из .docx с текстом)
    _, txt13 = page_html(13, 1)
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "13_старый_формат.docx"
        make_docx(src, [("p", line) for line in txt13.split("\n")])
        so = shutil.which("soffice") or "/opt/homebrew/bin/soffice"
        subprocess.run([so, f"-env:UserInstallation=file://{td}/lo", "--headless", "--convert-to", "doc",
                        "--outdir", str(dst), str(src)], check=True, capture_output=True, timeout=180)
    exp["13_старый_формат.doc"] = {"engines": ["markitdown"], "pages": {"1": control(values(13, 1))}}
    # 14. Excel из двух листов
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Платежи"
    ws.append(["Дата", "Плательщик", "Сумма"])
    rows14 = []
    for i in range(1, 9):
        v = values(14, i)
        ws.append([v["дата"], v["фамилия"], int(v["таблица"][0])])
        rows14 += [v["дата"], v["фамилия"], v["таблица"][0]]
    ws2 = wb.create_sheet("Итоги")
    ws2.append(["Показатель", "Значение"])
    ws2.append(["Шифр расчёта", values(14, 9)["шифр"]])
    rows14.append(values(14, 9)["шифр"])
    wb.save(dst / "14_расчёт.xlsx")
    exp["14_расчёт.xlsx"] = {"engines": ["markitdown"], "pages": {"1": rows14}}
    # 15. презентация из сканов
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(8.27), Inches(11.69)
    for p in (1, 2):
        s = prs.slides.add_slide(prs.slide_layouts[6])
        s.shapes.add_picture(BytesIO(png_bytes(render(15, p, 150))), 0, 0, prs.slide_width, prs.slide_height)
    prs.save(dst / "Приложения/15_презентация_из_сканов.pptx")
    exp["Приложения/15_презентация_из_сканов.pptx"] = {"engines": ["ocr"], "pages": pages_exp(15, [(1, 1), (2, 2)])}
    # 16. текстовый PDF, страницы которого повёрнуты на 90°
    doc = pymupdf.open()
    for p in (1, 2):
        with text_page_pdf(16, p) as t:
            doc.insert_pdf(t)
    for pg in doc:
        pg.set_rotation(90)
    doc.save(dst / "16_текстовый_pdf_боком.pdf")
    exp["16_текстовый_pdf_боком.pdf"] = {"engines": ["pdfplumber|pymupdf4llm"], "pages": pages_exp(16, [(1, 1), (2, 2)])}
    # 17. аудио и 18. архив — не конвертируются, но должны быть названы
    (dst / "17_аудиозапись.m4a").write_bytes(b"\x00\x00\x00\x18ftypM4A " + bytes(200))
    with zipfile.ZipFile(dst / "18_архив_документов.zip", "w") as z:
        z.writestr("внутри.txt", "документ в архиве")
    exp["17_аудиозапись.m4a"] = {"engines": ["медиа"]}
    exp["18_архив_документов.zip"] = {"engines": ["пропуск"]}

    (dst.parent / "expected.json").write_text(json.dumps(exp, ensure_ascii=False, indent=1), encoding="utf-8")
    print("Готово:", *sorted(str(p.relative_to(dst)) for p in dst.rglob("*") if p.is_file()), sep="\n  ")
    print(f"ожидания: {dst.parent / 'expected.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
