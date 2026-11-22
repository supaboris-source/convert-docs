#!/usr/bin/env python3
"""Обработка в служебной папке и выдача только итоговых Markdown.

prepare <цель> [--out <_md>] — новый запуск и route.py, без запуска OCR.
snapshot <запуск> — резервная копия Markdown/JSON перед правками.
publish <запуск> — postcheck и копирование только документов из плана.
cleanup-plan <запуск> — список удаления, ничего не удаляет.
cleanup <запуск> --manifest <план> --approved-path <запуск> — после разрешения пользователя.
Существующие результаты сохраняются; отличающийся текст выдаётся новой версией.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import uuid

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from common import BAD_SUFFIX, find_md, unread_pages
from finalize import insert_mark
from merge_eyes import EYE_RE, PAGE_RE, parse_eye

SERVICE = Path(os.environ.get("CONVERT_DOCS_HOME", "~/.local/share/convert-docs")).expanduser()
SKIPPED = {"готово", "пропуск", "дубль"}


def tag() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


def save_new(path: Path, data: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


def load_run(run: Path) -> tuple[dict, Path, Path]:
    meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
    work, output = Path(meta["work"]).resolve(), Path(meta["output"]).resolve()
    if not work.is_relative_to(run.resolve()) or output == work or output.is_relative_to(work):
        raise ValueError("небезопасные пути рабочей папки и результата")
    return meta, work, output


def snapshot(run: Path, work: Path) -> Path:
    dest = run / "backups" / tag()
    dest.mkdir(parents=True, exist_ok=False)
    for src in sorted(work.rglob("*")):
        if src.is_file() and src.suffix in {".md", ".json"}:
            dst = dest / src.relative_to(work)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    print(f"Резервная копия: {dest}", flush=True)
    return dest


def prepare(args: argparse.Namespace) -> int:
    target = Path(args.target).expanduser().resolve(strict=True)
    base = target if target.is_dir() else target.parent
    output = Path(args.out).expanduser().resolve() if args.out else base / "_md"
    service = Path(args.service_root).expanduser().resolve()
    if service == base or service.is_relative_to(base):
        raise ValueError("служебная папка должна находиться вне папки исходников")
    run = service / "runs" / tag()
    work = run / "documents"
    work.mkdir(parents=True, exist_ok=False)
    save_new(run / "run.json", {"format": 1, "id": run.name, "service_root": str(service),
                                "target": str(target), "output": str(output),
                                "work": str(work), "plan": str(run / "plan.json")})
    print(f"Запуск: {run}\nРабочая папка: {work}\nИтоговые Markdown: {output}", flush=True)
    cmd = [sys.executable, str(SCRIPTS / "route.py"), str(target), "--out", str(work),
           "--existing-out", str(output), "--json", str(run / "plan.json")]
    for value in args.exclude:
        cmd += ["--exclude", value]
    if args.force:
        cmd.append("--force")
    return subprocess.run(cmd, check=False).returncode


def deliver(src: Path, dst: Path, run: Path, content: bytes | None = None) -> Path:
    """Не перезаписывать ни существующий файл, ни ссылку; повторы идемпотентны."""
    payload = src.read_bytes() if content is None else content
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and not dst.is_symlink() and dst.read_bytes() == payload:
        return dst
    original = dst
    for version in sorted(dst.parent.iterdir()):
        if (version.name.startswith(original.stem + " (версия ")
                and version.suffix == original.suffix and version.is_file()
                and not version.is_symlink() and version.read_bytes() == payload):
            return version
    # Сохраняем прежний результат отдельно, хотя выдаём новую версию без замены.
    if original.is_file():
        backups = run / "backups" / ("published-" + tag())
        backups.mkdir(parents=True, exist_ok=False)
        shutil.copy2(original, backups / original.name)
        print(f"Резервная копия прежнего результата: {backups}", flush=True)
    while True:
        try:
            with dst.open("xb") as stream:
                stream.write(payload)
            shutil.copystat(src, dst)
            return dst
        except FileExistsError:
            dst = original.with_name(f"{original.stem} (версия {tag()}){original.suffix}")


def publish(run: Path, include_csv: bool = False) -> int:
    meta, work, output = load_run(run)
    data = json.loads(Path(meta["plan"]).read_text(encoding="utf-8"))
    pending = []
    for eye in sorted(work.rglob("_ЧТЕНИЕ-*.md")):
        match = EYE_RE.match(eye.name)
        md = find_md(eye.parent, match.group("stem")) if match else None
        if md is None:
            pending.append(eye)
            continue
        text = md.read_text(encoding="utf-8")
        _, body = parse_eye(eye.read_text(encoding="utf-8"))
        page = int(match.group("page"))
        heads = list(PAGE_RE.finditer(text))
        sections = {int(h.group(1)): text[h.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)]
                    for i, h in enumerate(heads)}
        section = sections.get(page, text if not heads and page == 1 else "")
        if not body or body not in section or "✎ Прочитано глазами" not in section:
            pending.append(eye)
    if pending:
        print("Сначала объединить ручное чтение (merge_eyes.py --keep): "
              + ", ".join(str(p.relative_to(work)) for p in pending))
        return 1
    snapshot(run, work)  # postcheck обновляет JSON и отчёт.
    log = run / ("postcheck-" + tag() + ".log")
    with log.open("x", encoding="utf-8") as stream:
        rc = subprocess.run([sys.executable, str(SCRIPTS / "postcheck.py"), str(work)],
                            stdout=stream, stderr=subprocess.STDOUT, check=False).returncode
    print(f"Проверка: {log}", flush=True)
    if rc:
        return rc
    delivered, errors = [], []
    for row in data.get("plan", []):
        if row.get("engine") in SKIPPED:
            if row.get("engine") == "готово" and row.get("published_out"):
                delivered.append(row["published_out"])
            continue
        if row.get("engine") == "ОШИБКА":
            errors.append(f"{row.get('rel')}: {row.get('why')}")
            continue
        try:
            planned = Path(row["out"]).resolve()
            rel = planned.relative_to(work)
            md = find_md(planned.parent, planned.stem)
            if md is None or not md.read_text(encoding="utf-8").strip():
                raise ValueError("нет непустого итогового Markdown")
            if not md.resolve().is_relative_to(work):
                raise ValueError("Markdown находится вне рабочей папки")
            stats_file = planned.with_name(planned.stem + "_ocr.json")
            stats = json.loads(stats_file.read_text(encoding="utf-8")) if stats_file.exists() else {}
            if row.get("engine") == "ocr":
                count = stats.get("pages", 0)
                pages = [p.get("page") for p in stats.get("page_stats", [])]
                # сканы внутри PDF с текстом: распознаются только страницы из only_pages
                expected = [int(x) for x in row["only_pages"]] if row.get("only_pages") else list(range(1, count + 1))
                if not count or pages != expected:
                    raise ValueError("OCR не завершён: нет полного списка распознанных страниц")
                if row.get("pages") and row["pages"] != count:
                    raise ValueError("число страниц OCR расходится с планом")
                unread = unread_pages(stats.get("page_stats", []), skip_eyes=True)
                if unread:
                    raise ValueError("не распознаны и не прочитаны глазами стр. "
                                     + ", ".join(map(str, unread)))
            text = md.read_text(encoding="utf-8")
            quality = stats.get("quality") or {}
            name = md.name
            if quality.get("bad"):
                text = insert_mark(text, quality.get("why") or "исходник плохого качества")
                name = md.stem.removesuffix(BAD_SUFFIX) + BAD_SUFFIX + ".md"
            dst = deliver(md, output / rel.parent / name, run, text.encode("utf-8"))
            delivered.append(str(dst))
            print(f"Готово: {dst}", flush=True)
            if include_csv:
                for csv in row.get("csv", []):
                    csv_path = Path(csv)
                    src = (csv_path if csv_path.is_absolute() else planned.parent / csv_path).resolve()
                    csv_rel = src.relative_to(work)
                    delivered.append(str(deliver(src, output / csv_rel, run)))
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f"{row.get('rel')}: {exc}")
    receipt = run / ("delivery-" + tag() + ".json")
    artifacts = [artifact(Path(p)) for p in delivered]
    save_new(receipt, {"files": delivered, "artifacts": artifacts,
                       "errors": errors, "media": data.get("media", [])})
    for message in errors:
        print("ОШИБКА: " + message, file=sys.stderr)
    return 1 if errors else 0


def artifact(path: Path) -> dict:
    """Отпечаток выданного файла, читаем по частям без загрузки в память."""
    path = path.resolve(strict=True)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def cleanup_context(run: Path) -> tuple[dict, list[dict]]:
    """Удалять можно отдельный зарегистрированный запуск с сохранённым результатом."""
    if run.is_symlink():
        raise ValueError("папка запуска не должна быть символической ссылкой")
    meta, _, output = load_run(run)
    if meta.get("format") != 1 or meta.get("id") != run.name:
        raise ValueError("нет регистрации запуска; автоматическая очистка запрещена")
    expected = Path(meta["service_root"]).resolve() / "runs" / meta["id"]
    if run.resolve() != expected or run.name in {".", "..", ""}:
        raise ValueError("очищать можно только конкретную папку запуска внутри service_root/runs")
    source = Path(meta["target"]).resolve(strict=True)
    if source.is_relative_to(run) or output.is_relative_to(run):
        raise ValueError("исходник или папка результата находится внутри удаляемой папки")
    receipts = sorted(run.glob("delivery-*.json"), key=lambda p: p.stat().st_mtime_ns)
    if not receipts:
        raise ValueError("нет записи о выдаче результата")
    receipt = json.loads(receipts[-1].read_text(encoding="utf-8"))
    artifacts = receipt.get("artifacts") or []
    if receipt.get("errors") or not artifacts or not any(Path(a["path"]).suffix == ".md" for a in artifacts):
        raise ValueError("выдача не завершена или нет проверяемого итогового Markdown")
    for item in artifacts:
        path = Path(item["path"]).resolve(strict=True)
        if path.is_relative_to(run) or not path.is_relative_to(output):
            raise ValueError("выданный результат находится вне ожидаемой папки")
        if artifact(path) != item:
            raise ValueError(f"результат изменился после выдачи: {path}; очистка остановлена")
    return meta, artifacts


def inventory(run: Path, exclude: Path) -> list[dict]:
    """Состав папки без обхода ссылок; изменения после согласования блокируют очистку."""
    entries = []
    for parent, dirs, files in os.walk(run, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(parent) / name
            if path == exclude:
                continue
            info = path.lstat()
            kind = ("symlink" if stat.S_ISLNK(info.st_mode) else "directory"
                    if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "special")
            if kind == "special":
                raise ValueError(f"необычный объект в рабочей папке: {path}")
            item = {"path": str(path.relative_to(run)), "kind": kind,
                    "bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
                    "inode": info.st_ino, "mode": info.st_mode}
            if kind == "symlink":
                item["link"] = os.readlink(path)
            entries.append(item)
    return sorted(entries, key=lambda item: item["path"])


def cleanup_plan(run: Path) -> int:
    meta, artifacts = cleanup_context(run)
    plan = run / ("cleanup-plan-" + tag() + ".json")
    entries = inventory(run, plan)
    # mtime корневой папки не входит в список и не меняет его при создании плана.
    save_new(plan, {"run": str(run), "action": "delete_run_recursively", "target": meta["target"],
                    "artifacts": artifacts, "entries": entries})
    count = sum(e["kind"] != "directory" for e in entries) + 1
    size = sum(e["bytes"] for e in entries if e["kind"] == "file")
    print(f"Предлагается удалить папку со всем содержимым: {run}\n"
          f"Объём: {count} файлов/ссылок, около {size} байт.\n"
          f"Полный список: {plan}\n"
          "Причина: результат выдан, рабочие данные больше не нужны после приёмки.\n"
          "Восстановление: повторная обработка сохранённого исходника; точные рабочие версии "
          "и резервные копии внутри этой папки после удаления не восстановить.\n"
          "Ничего не удалено. Требуется явное разрешение пользователя на указанную папку.")
    return 0


def cleanup(run: Path, manifest: Path, approved_path: str) -> int:
    approved = Path(approved_path).expanduser().absolute()
    # сравнение после раскрытия ссылок в родительских папках (/var → /private/var на macOS), но сама
    # подтверждённая папка ссылкой быть не может
    if approved.is_symlink() or approved.resolve() != run:
        raise ValueError("подтверждённый путь должен точно совпадать с указанной папкой запуска")
    _, artifacts = cleanup_context(run)
    if manifest.is_symlink() or manifest.resolve().parent != run or not manifest.name.startswith("cleanup-plan-"):
        raise ValueError("нужен план очистки внутри этой папки запуска")
    manifest = manifest.resolve()   # тот же вид пути, что у обхода папки запуска, иначе план «найдёт» сам себя
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("run") != str(run) or data.get("action") != "delete_run_recursively":
        raise ValueError("план удаления относится к другой папке или действию")
    if data.get("artifacts") != artifacts:
        raise ValueError("после составления плана изменился выданный результат")
    if data.get("entries") != inventory(run, manifest):
        raise ValueError("содержимое папки изменилось после составления списка; нужен новый список и разрешение")
    shutil.rmtree(run)
    print(f"Удалена согласованная служебная папка: {run}")
    print("Итоговые файлы проверены по SHA-256 и сохранены отдельно.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("target")
    prep.add_argument("--out")
    prep.add_argument("--service-root", default=str(SERVICE))
    prep.add_argument("--exclude", action="append", default=[])
    prep.add_argument("--force", action="store_true")
    snap = commands.add_parser("snapshot")
    snap.add_argument("run")
    pub = commands.add_parser("publish")
    pub.add_argument("run")
    pub.add_argument("--csv", action="store_true", help="только по явной просьбе пользователя")
    plan = commands.add_parser("cleanup-plan", help="только составить список удаления")
    plan.add_argument("run")
    clean = commands.add_parser("cleanup", help="выполнять только после явного разрешения на этот путь")
    clean.add_argument("run")
    clean.add_argument("--manifest", required=True)
    clean.add_argument("--approved-path", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            return prepare(args)
        raw_run = Path(args.run).expanduser().absolute()
        if args.command in {"cleanup", "cleanup-plan"} and raw_run.is_symlink():
            raise ValueError("удаление по символической ссылке запрещено")
        run = raw_run.resolve(strict=True)
        if args.command == "snapshot":
            _, work, _ = load_run(run)
            snapshot(run, work)
            return 0
        if args.command == "cleanup-plan":
            return cleanup_plan(run)
        if args.command == "cleanup":
            return cleanup(run, Path(args.manifest).expanduser().absolute(), args.approved_path)
        return publish(run, args.csv)
    except (OSError, ValueError, KeyError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
