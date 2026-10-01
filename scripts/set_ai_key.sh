#!/usr/bin/env bash
# Замена AI_API_KEY в .env и .env.app с проверкой доступности /models.
# До запуска отдельно проверить генерацию: /models не доказывает валидность ключа и баланс.
# Ключ вводится с клавиатуры скрыто и нигде не печатается.
# Запуск на сервере: ssh -t <сервер> <каталог проекта>/scripts/set_ai_key.sh
# Каталог проекта по умолчанию — тот, в котором лежит сам скрипт;
# другой можно задать переменной PROJECT_DIR.
set -euo pipefail
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$PROJECT_DIR"
base_url=$(grep -E "^AI_BASE_URL=" .env.app | cut -d= -f2- | tr -d "\r")
read -r -s -p "Вставьте новый ключ и нажмите Enter (символы не отображаются): " key
echo
key=$(printf "%s" "$key" | tr -d "[:space:]")
if [ ${#key} -lt 20 ]; then echo "Ключ слишком короткий (${#key} символов) — вставка не сработала. Ничего не изменено."; exit 1; fi
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 -H "Authorization: Bearer $key" "${base_url%/}/models" || true)
if [ "$code" != "200" ]; then echo "Cloud.ru ответил HTTP $code на этот ключ — файлы не тронуты."; exit 1; fi
echo "GET /models: HTTP 200. Эта проверка не подтверждает баланс и право генерации."
echo "Ключ следует заменять только после отдельной успешной проверки генерации."
stamp=$(date +%Y%m%d-%H%M%S)
cp .env ".env.bak-$stamp-key"; cp .env.app ".env.app.bak-$stamp-key"
chmod 600 ".env.bak-$stamp-key" ".env.app.bak-$stamp-key"
for f in .env .env.app; do
  awk -v k="$key" 'BEGIN{done=0} /^AI_API_KEY=/{print "AI_API_KEY=" k; done=1; next} {print} END{if(!done) print "AI_API_KEY=" k}' "$f" > "$f.tmp" && chmod 600 "$f.tmp" && mv "$f.tmp" "$f"
done
echo "Файлы .env и .env.app обновлены, копии: .env.bak-$stamp-key, .env.app.bak-$stamp-key"
docker compose up -d --force-recreate bot worker
echo "Готово. Контейнеры пересозданы."
