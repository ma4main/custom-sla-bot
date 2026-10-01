#!/usr/bin/env bash
# Проверка восстановления: разворачивает свежий дамп во ВРЕМЕННУЮ базу
# и сверяет, что данные на месте. Боевую базу не трогает.
# Запуск на сервере (вручную или по cron): <каталог проекта>/scripts/restore_test.sh
# Переменные окружения (необязательные): PROJECT_DIR, BACKUP_DIR — как в backup.sh;
# TEST_DB — имя временной базы (restore_check*); BACKUP_PASSPHRASE — для .enc.

set -euo pipefail

# Каталог проекта по умолчанию — тот, в котором лежит сам скрипт.
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-$PROJECT_DIR/backups}"
TEST_DB="${TEST_DB:-restore_check}"

# Имя временной базы проверяется жёстко: оно подставляется в DROP DATABASE.
case "$TEST_DB" in
  restore_check|restore_check_*) ;;
  *) echo "ОШИБКА: TEST_DB должен начинаться с restore_check, получено «$TEST_DB»" >&2; exit 1 ;;
esac

cd "$PROJECT_DIR"

# shellcheck disable=SC1091
set -a; source .env; set +a

# Временная база удаляется при любом выходе, в том числе при ошибке.
cleanup() {
  docker compose exec -T postgres psql -U "$POSTGRES_USER" -d postgres \
    -c "DROP DATABASE IF EXISTS $TEST_DB;" >/dev/null 2>&1 || true
}
trap cleanup EXIT

LATEST="$(ls -1t "$BACKUP_DIR"/chatstats_*.sql.gz "$BACKUP_DIR"/chatstats_*.sql.gz.enc 2>/dev/null | head -1 || true)"
if [ -z "$LATEST" ]; then
  echo "ОШИБКА: дампов не найдено в $BACKUP_DIR" >&2
  exit 1
fi
echo "проверяю: $LATEST"

docker compose exec -T postgres psql -U "$POSTGRES_USER" -d postgres \
  -c "DROP DATABASE IF EXISTS $TEST_DB;" -c "CREATE DATABASE $TEST_DB;" >/dev/null

# Зашифрованный дамп расшифровывается на лету — на диск ничего не ложится.
if [ "${LATEST%.enc}" != "$LATEST" ]; then
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in "$LATEST" -pass env:BACKUP_PASSPHRASE
else
  cat "$LATEST"
fi | gunzip -c | docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$TEST_DB" -q -v ON_ERROR_STOP=1
# ON_ERROR_STOP: без него psql проглатывает ошибку внутри дампа и возвращает 0.
# Схема сверяется с боевой по версии миграций и числу таблиц: наличие
# сообщений полноту восстановления не доказывает.
LIVE_HEAD=$(docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -A -c "SELECT version_num FROM alembic_version;")
RESTORED_HEAD=$(docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$TEST_DB" -t -A -c "SELECT version_num FROM alembic_version;")
if [ -z "$RESTORED_HEAD" ] || [ "$RESTORED_HEAD" != "$LIVE_HEAD" ]; then
  echo "ОШИБКА: версия схемы в дампе «$RESTORED_HEAD», в боевой базе «$LIVE_HEAD»" >&2
  exit 1
fi
LIVE_TABLES=$(docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -A -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';")
RESTORED_TABLES=$(docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$TEST_DB" -t -A -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';")
if [ "$RESTORED_TABLES" != "$LIVE_TABLES" ]; then
  echo "ОШИБКА: таблиц в дампе $RESTORED_TABLES, в боевой базе $LIVE_TABLES" >&2
  exit 1
fi

echo "--- строки в восстановленной базе ---"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$TEST_DB" -t -A -F' ' -c "
  SELECT 'chat', count(*) FROM chat
  UNION ALL SELECT 'message', count(*) FROM message
  UNION ALL SELECT 'interaction', count(*) FROM interaction
  UNION ALL SELECT 'bot_user', count(*) FROM bot_user;"

MESSAGES=$(docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$TEST_DB" -t -A -c "SELECT count(*) FROM message;")

if [ "$MESSAGES" -lt 1 ]; then
  echo "ОШИБКА: в восстановленной базе нет сообщений" >&2
  exit 1
fi
echo "OK: восстановление прошло, схема $RESTORED_HEAD, таблиц $RESTORED_TABLES, сообщений $MESSAGES, временная база удалена"
