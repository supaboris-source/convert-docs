#!/bin/bash
# Установка скилла convert-docs для Claude Code и/или ChatGPT/Codex.
# Копирует только файлы скилла; пакеты, модели и программы не ставит (см. УСТАНОВКА.md).
#
#   ./install.sh            для всех найденных программ (~/.claude — Claude Code; ~/.codex или ~/.agents — ChatGPT/Codex)
#   ./install.sh --claude   только Claude Code       -> ~/.claude/skills/convert-docs
#   ./install.sh --codex    только ChatGPT/Codex     -> ~/.agents/skills/convert-docs
#                           (если уже есть прежний ~/.codex/skills/convert-docs, обновляется он:
#                            два скилла с одним именем Codex не объединяет)
#   ./install.sh --dry-run  только показать, что будет сделано
#
# Прежняя установка не удаляется: она переносится в ~/.local/share/convert-docs/skill-backups/
# (вне папки скиллов, иначе агент увидел бы два скилла convert-docs).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
BACKUPS="${CONVERT_DOCS_HOME:-$HOME/.local/share/convert-docs}/skill-backups"
DRY=0
WANT_CLAUDE=0
WANT_CODEX=0

for arg in "$@"; do
  case "$arg" in
    --claude) WANT_CLAUDE=1 ;;
    --codex|--chatgpt) WANT_CODEX=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Неизвестный параметр: $arg" >&2; exit 2 ;;
  esac
done

if [ ! -f "$SRC/SKILL.md" ] || [ ! -d "$SRC/scripts" ]; then
  echo "Запускайте install.sh из папки репозитория convert-docs (рядом должны быть SKILL.md и scripts/)." >&2
  exit 1
fi

if [ "$WANT_CLAUDE" = 0 ] && [ "$WANT_CODEX" = 0 ]; then
  [ -d "$HOME/.claude" ] && WANT_CLAUDE=1
  { [ -d "$HOME/.codex" ] || [ -d "$HOME/.agents" ]; } && WANT_CODEX=1
  if [ "$WANT_CLAUDE" = 0 ] && [ "$WANT_CODEX" = 0 ]; then
    echo "Не найдены ни ~/.claude (Claude Code), ни ~/.codex / ~/.agents (ChatGPT/Codex)." >&2
    echo "Укажите явно: ./install.sh --claude или ./install.sh --codex" >&2
    exit 1
  fi
fi

run() { if [ "$DRY" = 1 ]; then echo "  [проверка] $*"; else "$@"; fi; }

install_to() {
  local dest="$1" label="$2"
  local real_dest
  real_dest="$(cd "$(dirname "$dest")" 2>/dev/null && pwd -P)/$(basename "$dest")" || real_dest="$dest"
  if [ "$real_dest" = "$SRC" ]; then
    echo "$label: установщик запущен из самой установленной папки ($dest) — пропускаю."
    return
  fi
  echo "$label: $dest"
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    local stamp owner backup
    stamp="$(date +%Y%m%d-%H%M%S)"
    owner="$(basename "$(dirname "$(dirname "$dest")")")"   # .claude / .agents / .codex
    backup="$BACKUPS/${owner#.}-convert-docs-$stamp"
    run mkdir -p "$BACKUPS"
    run mv "$dest" "$backup"
    echo "  прежняя установка перенесена: $backup"
  fi
  run mkdir -p "$dest"
  run rsync -a \
    --exclude '.git' --exclude '.github' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude '.DS_Store' --exclude 'install.sh' --exclude 'README.md' --exclude 'tests' \
    "$SRC/" "$dest/"
  [ "$DRY" = 1 ] || echo "  готово"
}

if [ "$WANT_CLAUDE" = 1 ]; then
  install_to "$HOME/.claude/skills/convert-docs" "Claude Code"
fi
if [ "$WANT_CODEX" = 1 ]; then
  if [ -e "$HOME/.codex/skills/convert-docs" ]; then
    install_to "$HOME/.codex/skills/convert-docs" "ChatGPT/Codex (прежнее место)"
  else
    install_to "$HOME/.agents/skills/convert-docs" "ChatGPT/Codex"
  fi
fi

cat <<'EOF'

Дальше:
  1. окружения и модель — разделы 1–3 в УСТАНОВКА.md (если ещё не сделаны);
  2. самопроверка: ~/ocr-env2/bin/python -I <папка скилла>/scripts/selftest.py
  3. вызов: Claude Code — /convert-docs <папка>; Codex — $convert-docs <папка>; ChatGPT — @convert-docs
     (если скилл не появился, перезапустите программу).
EOF
