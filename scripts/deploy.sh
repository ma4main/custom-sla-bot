#!/usr/bin/env bash
# Деплой на сервер одной командой:
#   штамп версии → артефакт из git → тесты + смоуки → сборка → рестарт
#   → ожидание healthy → проверка версии в контейнере и логов
#
# Штамп (app/BUILD_INFO: sha, дата коммита, время сборки, dirty) едет в образ
# и виден в «Состоянии системы».
# Артефакт — `git archive HEAD`: на сервер попадает ровно то, что в коммите;
# удалённые файлы удаляются (rsync --delete), незакоммиченное не уезжает
# без явного --allow-dirty.
#
# Запуск из Git Bash в корне проекта:
#   DEPLOY_HOST=my-server bash scripts/deploy.sh                 # полный цикл
#   DEPLOY_HOST=my-server bash scripts/deploy.sh --skip-checks   # без тестов и смоуков (правки текстов)
#   DEPLOY_HOST=my-server bash scripts/deploy.sh --allow-dirty   # с незакоммиченными правками (осознанно)
#
# Переменные окружения:
#   DEPLOY_HOST      обязательна: ssh-алиас из ~/.ssh/config или user@host сервера
#   PROJECT_DIR      каталог проекта на сервере (по умолчанию /opt/chat-sla-bot)
#   HEALTHY_TIMEOUT  сколько секунд ждать healthy у bot и worker (по умолчанию 120)
set -euo pipefail

if [ -z "${DEPLOY_HOST:-}" ]; then
  echo "❌ Не задан DEPLOY_HOST — ssh-алиас или user@host сервера." >&2
  echo "   Пример: DEPLOY_HOST=my-server bash scripts/deploy.sh" >&2
  exit 2
fi
HOST="$DEPLOY_HOST"
PROJECT_DIR="${PROJECT_DIR:-/opt/chat-sla-bot}"
HEALTHY_TIMEOUT="${HEALTHY_TIMEOUT:-120}"
# Каталог уходит в удалённые скрипты аргументом `bash -s` и разбирается
# удалённой оболочкой, поэтому экранируется заранее.
PROJECT_DIR_Q=$(printf '%q' "$PROJECT_DIR")
RUN_CHECKS=1
ALLOW_DIRTY=0
for arg in "$@"; do
  case "$arg" in
    --skip-checks) RUN_CHECKS=0 ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    *) echo "Неизвестный флаг: $arg" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."

# ── 1. Штамп версии ──────────────────────────────────────────────────
SHA=$(git rev-parse --short HEAD)
DIRTY=0
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  DIRTY=1
  if [ "$ALLOW_DIRTY" != 1 ]; then
    echo "❌ Незакоммиченные изменения. На прод едет только коммит: закоммитьте" >&2
    echo "   или запустите с --allow-dirty (сборка будет помечена как dirty)." >&2
    exit 1
  fi
  echo "⚠️  Незакоммиченные изменения — сборка будет помечена как dirty"
fi
printf 'sha=%s\ncommit_date=%s\nbuilt_at=%s\ndirty=%s\n' \
  "$SHA" "$(git log -1 --format=%cI)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$DIRTY" \
  > app/BUILD_INFO
echo "▶ версия $SHA (dirty=$DIRTY)"

# ── 2. Артефакт и доставка ───────────────────────────────────────────
# git archive: только закоммиченное. На сервере rsync --delete приводит
# каталоги кода к артефакту один в один — стёртые файлы не переживают деплой.
ARCHIVE=$(mktemp -t chat-sla-release-XXXXXX.tar)
if [ "$DIRTY" = 1 ]; then
  # Осознанный dirty-деплой: берём рабочее дерево отслеживаемых файлов.
  git ls-files -z app tests scripts alembic alembic.ini pytest.ini requirements.txt requirements-dev.txt \
    docker-compose.yml Dockerfile .dockerignore | tar --null -T - -cf "$ARCHIVE"
else
  git archive --format=tar HEAD app tests scripts alembic alembic.ini pytest.ini requirements.txt \
    requirements-dev.txt docker-compose.yml Dockerfile .dockerignore > "$ARCHIVE"
fi
scp -q "$ARCHIVE" "$HOST:/tmp/chat-sla-release.tar"
rm -f "$ARCHIVE"
ssh "$HOST" bash -s "$PROJECT_DIR_Q" <<'REMOTE_UNPACK'
set -euo pipefail
PROJECT_DIR="$1"
cd "$PROJECT_DIR"
rm -rf .release && mkdir .release
tar -xf /tmp/chat-sla-release.tar -C .release && rm -f /tmp/chat-sla-release.tar
for dir in app tests scripts alembic; do
  rsync -a --delete ".release/$dir/" "$dir/"
done
cp .release/pytest.ini .release/alembic.ini .release/requirements.txt .release/requirements-dev.txt \
   .release/docker-compose.yml .release/Dockerfile .release/.dockerignore .
chmod +x scripts/*.sh 2>/dev/null || true
# CRLF в шелл-скриптах молча ломает запуск из cron («bash^M: No such file»).
sed -i 's/\r$//' scripts/*.sh 2>/dev/null || true
# .env.app для bot/worker: всё из .env, кроме учётных данных владельца схемы
# и пароля бэкапов (он нужен только скриптам на хосте, они читают .env).
# Пересобирается при каждом выкате — новая переменная в .env доедет
# до контейнеров сама.
grep -vE '^(DATABASE_URL|POSTGRES_PASSWORD|BACKUP_PASSPHRASE)=' .env > .env.app && chmod 600 .env.app
rm -rf .release
REMOTE_UNPACK
# Штамп — после rsync --delete (он не в git и был бы стёрт).
scp -q app/BUILD_INFO "$HOST:$PROJECT_DIR/app/BUILD_INFO"
echo "▶ артефакт на сервере (rsync --delete)"

# ── 3. Тесты и смоуки ────────────────────────────────────────────────
# Тестовая база пересоздаётся: conftest делает create_all, и новая колонка
# в модели иначе роняет тесты на старой схеме.
# DATABASE_URL и APP_DATABASE_URL в контейнере тестов явно указывают
# на тестовую базу: env_file подставляет боевые DSN. Токен бота и ключ ИИ
# перебиваются фиктивными по той же причине: тест или смоук, не подменивший
# сеть, иначе пошёл бы в Telegram и платный ИИ боевыми ключами.
if [ "$RUN_CHECKS" = 1 ]; then
# ⚠️ Каждый docker compose exec/run здесь — с </dev/null: скрипт подан
# через stdin, и команда, читающая stdin, съедает его остаток — скрипт
# молча заканчивается с кодом 0. Поэтому же в конце — маркер CHECKS_OK:
# обрыв не должен выглядеть успехом.
  checks_log=$(mktemp -t chat-sla-checks-XXXXXX.log)
  ssh "$HOST" bash -s "$PROJECT_DIR_Q" <<'REMOTE_CHECKS' | tee "$checks_log"
set -euo pipefail
PROJECT_DIR="$1"
cd "$PROJECT_DIR"
set -a; source .env; set +a
TEST_DSN="postgresql+asyncpg://$POSTGRES_USER:$POSTGRES_PASSWORD@postgres:5432/chatstats_test"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d postgres -q \
  -c 'DROP DATABASE IF EXISTS chatstats_test' -c 'CREATE DATABASE chatstats_test' </dev/null
# Образ собирается ДО проверок, и тесты идут в нём: проверяется ровно то,
# что запустится. Каталог app поэтому намеренно не монтируется.
docker compose build -q bot worker migrate </dev/null
docker compose run --rm --no-deps \
  -v "$PROJECT_DIR/tests:/app/tests" \
  -v "$PROJECT_DIR/scripts:/app/scripts" -v "$PROJECT_DIR/pytest.ini:/app/pytest.ini" \
  -v "$PROJECT_DIR/requirements-dev.txt:/app/requirements-dev.txt" \
  -e PYTHONPATH=/app -e TEST_DATABASE_URL="$TEST_DSN" \
  -e DATABASE_URL="$TEST_DSN" -e APP_DATABASE_URL="$TEST_DSN" \
  -e BOT_TOKEN=1:test -e AI_API_KEY= -e AI_ENABLED=0 bot sh -c '
    pip install -q -r requirements-dev.txt 1>/dev/null 2>&1
    if python -m pytest tests/ -o addopts="" -q --tb=short -p no:cacheprovider > /tmp/pytest.out 2>&1; then
      grep -oE "[0-9]+ passed" /tmp/pytest.out | tail -1 || tail -1 /tmp/pytest.out
    else
      tail -25 /tmp/pytest.out; echo "❌ тесты не прошли"; exit 1
    fi
    for s in verify_screens verify_bot verify_no_group_reply verify_reports_access; do
      python scripts/$s.py > /dev/null && echo "  ok    $s" || { echo "❌ $s"; exit 1; }
    done
    echo CHECKS_OK' </dev/null
REMOTE_CHECKS
  grep -q '^CHECKS_OK$' "$checks_log" || { echo "❌ проверки не дошли до конца (обрыв скрипта?)"; rm -f "$checks_log"; exit 1; }
  rm -f "$checks_log"
  echo "▶ тесты и смоуки зелёные"
fi

# ── 4. Сборка, рестарт, ожидание healthy ─────────────────────────────
# Ждём healthy у bot и worker или падаем с логами: ложное «готово»
# при нездоровом контейнере хуже честного красного.
deploy_log=$(mktemp -t chat-sla-deploy-XXXXXX.log)
ssh "$HOST" bash -s "$HEALTHY_TIMEOUT" "$SHA" "$PROJECT_DIR_Q" <<'REMOTE_DEPLOY' | tee "$deploy_log"
set -euo pipefail
TIMEOUT="$1"; SHA="$2"; PROJECT_DIR="$3"
cd "$PROJECT_DIR"
docker compose logs --since 3h > "logs/pre-restart-$(date +%Y%m%d_%H%M%S).log" 2>&1 </dev/null || true
docker compose build -q bot worker migrate </dev/null
docker compose up -d </dev/null
waited=0
# Смотрим на имя СЕРВИСА, а не контейнера: имя контейнера зависит от имени
# проекта compose, а сервисы bot и worker называются так всегда.
until [ "$(docker compose ps --format '{{.Service}} {{.Health}}' </dev/null | grep -cE '^(bot|worker) healthy$')" = 2 ]; do
  sleep 5; waited=$((waited + 5))
  if [ "$waited" -ge "$TIMEOUT" ]; then
    echo "❌ bot/worker не стали healthy за ${TIMEOUT}с:"
    docker compose ps --format '{{.Name}} {{.Status}}' </dev/null
    docker compose logs bot worker --since 5m 2>&1 </dev/null | tail -40
    exit 1
  fi
done
docker compose ps --format '{{.Name}} {{.Status}}' </dev/null
stamp=$(docker compose exec -T bot cat /app/app/BUILD_INFO </dev/null | tr '\n' ' ')
echo "в контейнере: $stamp"
case "$stamp" in *"sha=$SHA"*) ;; *) echo "❌ в контейнере не та версия"; exit 1 ;; esac
if docker compose logs bot worker --since 90s 2>&1 </dev/null | grep -qi 'error\|traceback'; then
  echo "⚠️  в логах после старта есть error/traceback — посмотрите docker compose logs"
else
  echo "логи после старта чистые"
fi
echo DEPLOY_OK
REMOTE_DEPLOY
grep -q '^DEPLOY_OK$' "$deploy_log" || { echo "❌ рестарт не дошёл до конца (обрыв скрипта?)"; rm -f "$deploy_log"; exit 1; }
rm -f "$deploy_log"
echo "▶ готово: $SHA на сервере"
