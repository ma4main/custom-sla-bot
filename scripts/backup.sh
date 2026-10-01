#!/usr/bin/env bash
# Резервная копия базы (pg_dump + gzip). Запускается по cron на сервере:
#   <каталог проекта>/scripts/backup.sh
# Переменные окружения (необязательные):
#   PROJECT_DIR        каталог проекта (по умолчанию — родитель каталога скрипта)
#   BACKUP_DIR         куда класть дампы (по умолчанию $PROJECT_DIR/backups)
#   KEEP_DAYS          сколько дней хранить дампы (по умолчанию 14)
#   BACKUP_PASSPHRASE  если задана — дамп шифруется openssl AES-256
#
# ⚠️ Копия лежит на ТОМ ЖЕ сервере: это защита от потери контейнера или
# volume, но не от потери диска. Выносить копии за пределы сервера нужно
# отдельно. Восстановление проверяется scripts/restore_test.sh.

set -euo pipefail

# cron вызывает скрипт по полному пути, поэтому каталог проекта
# вычисляется от расположения самого скрипта.
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-$PROJECT_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

cd "$PROJECT_DIR"

# shellcheck disable=SC1091
set -a; source .env; set +a

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

STAMP="$(date -u +%Y%m%d_%H%M%S)"
TARGET="$BACKUP_DIR/chatstats_$STAMP.sql.gz"

docker compose exec -T postgres pg_dump \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --no-owner --no-privileges \
  | gzip -9 > "$TARGET"

# Пустой или подозрительно маленький дамп — это не бэкап, а ложное спокойствие.
SIZE=$(stat -c%s "$TARGET")
if [ "$SIZE" -lt 10240 ]; then
  echo "ОШИБКА: дамп подозрительно мал ($SIZE байт), не считаю его валидным" >&2
  rm -f "$TARGET"
  exit 1
fi

if [ -n "${BACKUP_PASSPHRASE:-}" ]; then
  # Шифруем только после проверки размера: незачем шифровать заведомо битый файл.
  openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
    -in "$TARGET" -out "$TARGET.enc" -pass env:BACKUP_PASSPHRASE
  rm -f "$TARGET"
  TARGET="$TARGET.enc"
  ENCRYPTED="да"
else
  ENCRYPTED="НЕТ (BACKUP_PASSPHRASE не задан)"
fi

chmod 600 "$TARGET"

# Ротация: старое удаляется, иначе диск однажды кончится молча.
find "$BACKUP_DIR" -name 'chatstats_*.sql.gz' -mtime "+$KEEP_DAYS" -delete
find "$BACKUP_DIR" -name 'chatstats_*.sql.gz.enc' -mtime "+$KEEP_DAYS" -delete

echo "OK $TARGET ($((SIZE / 1024)) КБ), шифрование: $ENCRYPTED, хранится дней: $KEEP_DAYS"
