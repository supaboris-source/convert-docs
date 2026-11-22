"""Проверка всей цепочки одной командой — после любого обновления (brew, pip, macOS, модели).

Собирает тестовый набор установки во временной папке и проводит его тем же путём, что и агент:
workspace.py prepare → dots_batch.py → postcheck.py → workspace.py publish. Проверяет: 11 контрольных
значений в каждом из договоров (текстовый PDF, скан, фото, двухстраничный TIFF — на обеих страницах,
PDF «текст — скан — текст» — на странице-скане, распознанной OCR и вставленной на место),
таблицу Excel и CSV, сверку Vision, то, что служебные файлы не попали в выдачу, и наличие частей движка
(Apple Vision, орфофильтр, запасные движки, llama.cpp). Около четырёх минут работы GPU; другие модели
в это время не запускать. Служебная папка запуска создаётся внутри временной папки, а не в
~/.local/share/convert-docs.

    ~/ocr-env2/bin/python -I <папка навыка>/scripts/selftest.py [--keep]

Код выхода 0 — всё на месте; иначе печатается, что сломалось, и папка прогона остаётся для разбора.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
MD_PYTHON = Path(os.environ.get("CONVERT_DOCS_LIGHT_VENV", "~/Projects/markitdown-local/.venv")).expanduser() / "bin/python"
OCR_PYTHON = Path(os.environ.get("CONVERT_DOCS_OCR_VENV", "~/ocr-env2")).expanduser() / "bin/python"
CONTROL = ["17/2026", "125 000,00", "15 000,00", "40 000,00", "70 000,00", "7700000001",
           "40702810000000000017", "044525999", "0,1%", "Ромашка", "Сидоров"]
BUSY = "mlx_vlm|dots_batch|ocr_worker|llama-mtmd"   # два GPU-процесса сразу — Metal OOM и битый прогон


def squash(s: str) -> str:
    return re.sub(r"\s+", "", s.replace(" ", " ").replace(" ", " "))


def run(cmd: list, log: Path, timeout: int) -> int:
    with open(log, "a", encoding="utf-8") as f:
        f.write("\n$ " + " ".join(map(str, cmd)) + "\n")
        f.flush()
        try:
            return subprocess.run(list(map(str, cmd)), stdout=f, stderr=subprocess.STDOUT, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            f.write(f"ТАЙМАУТ {timeout} с\n")
            return -1


def main() -> int:
    keep = "--keep" in sys.argv
    probe = subprocess.run(["pgrep", "-fl", BUSY], capture_output=True, text=True)
    if probe.returncode not in (0, 1):
        print("Не удалось проверить занятость GPU: список процессов недоступен. "
              "Полный OCR selftest не запускался. Запустите его в локальном Terminal.\n"
              + probe.stderr.strip())
        return 2
    busy = probe.stdout.strip()
    if busy:
        print("Сейчас работает другой процесс OCR — дождаться его конца:\n" + busy)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="convert-docs-selftest-"))
    src, log, service = tmp / "набор", tmp / "selftest.log", tmp / "служебное"
    out = tmp / "результат"
    res: list[tuple[str, bool, str]] = []

    # части движка (без GPU)
    sys.path.insert(0, str(SCRIPTS))
    try:
        import dots_engine
        import coverage
        res.append(("модель dots.mocr в кэше", Path(dots_engine.model_path()).exists(), ""))
        res.append(("орфофильтр NSSpellChecker", bool(coverage._checker()), ""))
        res.append(("llama.cpp (llama-mtmd-cli)", bool(dots_engine.llama_bin()), dots_engine.llama_bin() or "не найден"))
        for k in ("xiaomi", "hunyuan15", "glm"):
            res.append((f"запасной движок {k}", dots_engine.second_available(k), ""))
    except Exception as e:  # noqa: BLE001
        res.append(("импорт движка", False, f"{type(e).__name__}: {e}"))
    try:
        import Vision  # noqa: F401
        res.append(("Apple Vision (pyobjc)", True, ""))
    except Exception as e:  # noqa: BLE001
        res.append(("Apple Vision (pyobjc)", False, str(e)))

    # цепочка — как у агента (SKILL.md)
    rc = run([MD_PYTHON, SCRIPTS / "make_test_set.py", src], log, 120)
    res.append(("тестовый набор", rc == 0, "" if rc == 0 else f"код {rc}, см. {log}"))
    run_dir = None
    if rc == 0:
        rc = run(["python3", SCRIPTS / "workspace.py", "prepare", src, "--out", out,
                  "--service-root", service], log, 300)
        runs = sorted((service / "runs").glob("*")) if (service / "runs").exists() else []
        run_dir = runs[-1] if len(runs) == 1 else None
        res.append(("workspace.py prepare (route.py)", rc == 0 and run_dir is not None,
                    "" if rc == 0 else f"код {rc}, см. {log}"))
    work = run_dir / "documents" if run_dir else tmp / "нет"
    if run_dir and rc == 0:
        plan_rows = json.loads((run_dir / "plan.json").read_text(encoding="utf-8")).get("plan", [])
        tif = next((r for r in plan_rows if r.get("ext") in (".tif", ".tiff")), {})
        res.append(("route.py: в TIFF 2 страницы", tif.get("pages") == 2, f"в плане: {tif.get('pages')}"))
        mix = next((r for r in plan_rows if r.get("only_pages")), {})
        res.append(("route.py: скан внутри PDF с текстом — в OCR только стр. 2", mix.get("only_pages") == [2],
                    f"в плане: {mix.get('only_pages')}"))
        steps = [("dots_batch.py", [OCR_PYTHON, SCRIPTS / "dots_batch.py", "--plan", run_dir / "plan.json"], 1800),
                 ("postcheck.py", ["python3", SCRIPTS / "postcheck.py", work], 300),
                 ("workspace.py publish", ["python3", SCRIPTS / "workspace.py", "publish", run_dir, "--csv"], 300)]
        for name, cmd, to in steps:
            rc = run(cmd, log, to)
            res.append((name, rc == 0, "" if rc == 0 else f"код {rc}, см. {log}"))
            if rc != 0:
                break
    mds = list(out.rglob("*.md")) if out.exists() else []
    for stem in ("01_договор_с_текстом", "02_договор_скан", "03_договор_фото", "05_договор_tiff"):
        md = next((p for p in mds if p.stem == stem), None)
        raw = md.read_text(encoding="utf-8") if md else ""
        parts = raw.split("## Страница 2") if stem == "05_договор_tiff" else [raw]
        if stem == "05_договор_tiff" and len(parts) != 2:
            res.append((f"{stem}: обе страницы", False, "нет раздела «Страница 2»" if md else "нет .md"))
            continue
        for k, part in enumerate(parts, 1):
            text = squash(part)
            miss = [v for v in CONTROL if squash(v) not in text]
            label = f"{stem}: контрольные значения" + (f", стр. {k}" if len(parts) > 1 else "")
            res.append((label, md is not None and not miss,
                        f"{len(CONTROL) - len(miss)}/{len(CONTROL)}" + (f", нет: {', '.join(miss)}" if miss else "") if md else "нет .md"))
    md6 = next((p for p in mds if p.stem == "06_иск_со_сканом"), None)
    sec = re.split(r"(?m)^## Страница \d+\s*$", md6.read_text(encoding="utf-8")) if md6 else []
    if len(sec) == 4:     # до первой страницы + три страницы
        miss6 = [v for v in CONTROL if squash(v) not in squash(sec[2])]
        res.append(("06_иск_со_сканом: стр. 2 (скан) распознана на своём месте", not miss6,
                    f"{len(CONTROL) - len(miss6)}/{len(CONTROL)}" + (f", нет: {', '.join(miss6)}" if miss6 else "")))
    else:
        res.append(("06_иск_со_сканом: стр. 2 (скан) распознана на своём месте", False,
                    "нет .md" if not md6 else f"разделов страниц: {max(0, len(sec) - 1)}"))
    res.append(("04_расчёт.xlsx: таблица и CSV", any(p.stem == "04_расчёт" for p in mds) and bool(list(out.rglob("*.csv"))), ""))
    leaked = [p.name for p in out.rglob("*") if p.is_file() and (p.name.endswith("_ocr.json") or p.name.startswith("_"))] \
        if out.exists() else []
    res.append(("в выдаче нет служебных файлов", out.exists() and not leaked, ", ".join(leaked[:5])))
    js = next(iter(work.rglob("02_договор_скан_ocr.json")), None) if work.exists() else None
    cc = (json.loads(js.read_text(encoding="utf-8"))["page_stats"][0].get("crosscheck") or {}) if js else {}
    res.append(("сверка Vision в _ocr.json", "names_rare" in cc, "" if cc else "нет _ocr.json или сверки"))

    ok = all(r[1] for r in res)
    for name, good, note in res:
        print(f"{'OK  ' if good else 'FAIL'} {name}" + (f" — {note}" if note else ""))
    print("ИТОГ:", "всё работает" if ok else "есть поломки", "|", tmp if (keep or not ok) else "временная папка удалена")
    if ok and not keep:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
