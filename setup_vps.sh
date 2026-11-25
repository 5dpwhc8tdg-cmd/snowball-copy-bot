#!/usr/bin/env bash
# Установка бота на чистый VPS (Ubuntu 22.04 / 24.04 или Debian 12).
#
# Запуск от root или через sudo:
#     bash setup_vps.sh
#
# Скрипт делает всё сам:
#   1) ставит python3, git, venv;
#   2) создаёт системного пользователя snowball (бот не работает под root);
#   3) клонирует репозиторий в /opt/snowball-copy-bot;
#   4) создаёт venv и ставит зависимости;
#   5) спрашивает токен Telegram и chat_id, пишет .env с правами 600;
#   6) ставит systemd-юнит, включает автозапуск, запускает;
#   7) показывает журнал и делает тестовую проверку портфеля.
#
# Идемпотентен: повторный запуск обновляет код и перезапускает сервис.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/5dpwhc8tdg-cmd/snowball-copy-bot.git}"
APP_DIR="${APP_DIR:-/opt/snowball-copy-bot}"
APP_USER="${APP_USER:-snowball}"
SERVICE="${SERVICE:-snowball-worker}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m!! %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "Запустите через sudo: sudo bash setup_vps.sh"
command -v python3 >/dev/null || die "Нет python3 — это не Debian/Ubuntu?"
command -v systemctl >/dev/null || die "Нет systemd (в контейнере служба не поставится)"

say "1/7 Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates curl

say "2/7 Пользователь $APP_USER"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
  echo "создан пользователь $APP_USER"
else
  echo "пользователь $APP_USER уже есть"
fi

say "3/7 Код в $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --quiet origin main
  git -C "$APP_DIR" reset --hard --quiet origin/main
  echo "код обновлён до $(git -C "$APP_DIR" log --oneline -1)"
else
  git clone --quiet "$REPO_URL" "$APP_DIR"
  echo "склонировано: $(git -C "$APP_DIR" log --oneline -1)"
fi

say "4/7 Виртуальное окружение"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
if [ -f "$APP_DIR/requirements.txt" ]; then
  "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt" || true
fi
mkdir -p "$APP_DIR/state"

say "5/7 Настройки (.env)"
ENV_FILE="$APP_DIR/.env"
if [ -f "$ENV_FILE" ]; then
  echo "файл $ENV_FILE уже существует — оставляю как есть"
else
  read -rp "Токен бота от @BotFather: " TG_TOKEN
  read -rp "Ваш chat_id (узнать: напишите боту, он покажет в /status): " TG_CHAT
  cat > "$ENV_FILE" <<EOF
# Токен бота Telegram
TELEGRAM_BOT_TOKEN=$TG_TOKEN
# Кому присылать (можно несколько через запятую)
TELEGRAM_CHAT_ID=$TG_CHAT

# Состояние — файл, чтобы ничего не терялось при перезапуске
STATE_BACKEND=json
STATE_PATH=$APP_DIR/state/state.json

# Как часто проверять портфель (секунды). 300 = каждые 5 минут
POLL_INTERVAL=300

# Ваш капитал для расчёта размера сделок
MY_CAPITAL=100000
MY_MAX_POSITION_PCT=10
MY_RESERVE_PCT=20

# Сигналы по падениям выключены (см. README)
DIP_ENABLED=0

TZ_NAME=Europe/Moscow
EOF
  chmod 600 "$ENV_FILE"
  echo "создан $ENV_FILE (права 600)"
fi

say "6/7 Служба systemd"
cat > "$UNIT_DIR/$SERVICE.service" <<EOF
[Unit]
Description=Snowball Copy Bot — сделки портфеля в Telegram
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=ENV_FILE=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python worker.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
if ! chown -R "$APP_USER":"$APP_USER" "$APP_DIR" 2>/dev/null; then
  echo "⚠️  не смог сменить владельца файлов на $APP_USER — проверьте права вручную"
fi
systemctl daemon-reload
systemctl enable --quiet "$SERVICE"
systemctl restart "$SERVICE"
sleep 4

say "7/7 Проверка"
sleep 3
systemctl --no-pager --lines=0 status "$SERVICE" || true
echo
journalctl -u "$SERVICE" -n 20 --no-pager || true

# Токен принят? Без этого «работает» ничего не значит: бот может крутиться,
# а сообщения не доходить.
TOKEN_OK=$(journalctl -u "$SERVICE" -n 60 --no-pager 2>/dev/null | grep -c "токен принят" || true)
if [ "${TOKEN_OK:-0}" = "0" ]; then
  cat <<'WARN'

⚠️  В журнале нет подтверждения, что Telegram принял токен.
    Проверьте TELEGRAM_BOT_TOKEN в /opt/snowball-copy-bot/.env
    и перезапустите: systemctl restart snowball-worker
WARN
else
  echo
  echo "✅ Токен принят Telegram — бот на связи."
fi
cat <<EOF

Готово. Что дальше:
  • статус:      systemctl status $SERVICE
  • логи:        journalctl -u $SERVICE -f
  • перезапуск:  systemctl restart $SERVICE
  • обновление:  sudo bash $APP_DIR/setup_vps.sh   (обновит код и перезапустит)

Проверьте в Telegram: /status — должно ответить сразу.
Первое сообщение о новых сделках придёт при следующей сделке автора
(или по команде /now).
EOF
