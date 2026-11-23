#!/usr/bin/env bash
# Telegram MCP installer for a Linux VPS or macOS. Run it from this folder: ./install.sh
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# uv installs Python 3.12 and all packages into .venv, nothing is installed system-wide
if ! command -v uv >/dev/null 2>&1; then
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    say "Ставлю uv (менеджер Python-пакетов)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
fi

say "Ставлю зависимости..."
uv sync --frozen --no-dev

[ -f .env ] || cp .env.example .env
chmod 600 .env
mkdir -p sessions files models
chmod 700 sessions

say "Шаг 1 из 3. Ключи Telegram API"
uv run --no-sync telegram-mcp-login --keys-only

say "Шаг 2 из 3. Модель Whisper качается в фоне, пока вы входите (лог: models/download.log)"
uv run --no-sync telegram-mcp-model > models/download.log 2>&1 &
MODEL_PID=$!
trap 'kill $MODEL_PID 2>/dev/null || true' INT TERM

say "Шаг 3 из 3. Вход по QR-коду"
uv run --no-sync telegram-mcp-login

say "Жду, пока докачается модель Whisper..."
if wait "$MODEL_PID"; then
  tail -n 1 models/download.log
else
  echo "Модель не скачалась, подробности в models/download.log. Повторить: uv run telegram-mcp-model"
fi
trap - INT TERM

# Token for HTTP mode, generated once
uv run --no-sync python - <<'PY'
import secrets
from dotenv import dotenv_values, set_key
if not (dotenv_values(".env").get("MCP_BEARER_TOKEN") or "").strip():
    set_key(".env", "MCP_BEARER_TOKEN", secrets.token_urlsafe(32), quote_mode="never")
PY
TOKEN="$(grep '^MCP_BEARER_TOKEN=' .env | tail -n 1 | cut -d= -f2-)"
PORT="$(grep '^MCP_PORT=' .env | tail -n 1 | cut -d= -f2- || true)"
PORT="${PORT:-8765}"

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  read -r -p $'\nЗапустить HTTP-режим как службу systemd с автозапуском? [y/N] ' answer
  if [[ "${answer:-}" =~ ^[YyДд] ]]; then
    SUDO=""
    [ "$(id -u)" -eq 0 ] || SUDO="sudo"
    $SUDO tee /etc/systemd/system/telegram-mcp.service >/dev/null <<EOF
[Unit]
Description=Telegram MCP (HTTP)
After=network-online.target
Wants=network-online.target

[Service]
User=$(id -un)
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/telegram-mcp --transport http --env-file $DIR/.env
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now telegram-mcp
    echo "Служба запущена. Логи: journalctl -u telegram-mcp -f"
  fi
fi

say "Готово. Как подключить:"
cat <<EOF

stdio (клиент сам запускает процесс на этом сервере):
  команда: $DIR/.venv/bin/telegram-mcp
  аргументы: --transport stdio --env-file $DIR/.env

HTTP:
  запуск:    $DIR/.venv/bin/telegram-mcp --transport http
  адрес:     http://127.0.0.1:$PORT/mcp
  заголовок: Authorization: Bearer $TOKEN

Одновременно работает только один режим: два процесса на одной сессии мешают друг другу.
EOF
