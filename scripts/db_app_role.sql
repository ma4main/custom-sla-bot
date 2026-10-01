-- Роль приложения botapp с урезанными правами:
--   * миграции (сервис migrate), бэкап и restore-test — владелец схемы
--     (POSTGRES_USER), ему можно DDL;
--   * bot и worker — роль botapp: только DML по таблицам схемы public
--     и последовательности. CREATE/DROP/ALTER ей недоступны.
--
-- Запуск (пароль в файл не попадает, только переменные psql):
--   psql -U $POSTGRES_USER -d $POSTGRES_DB \
--        -v app_password='...' -v dbname="$POSTGRES_DB" -f scripts/db_app_role.sql
--
-- ⚠️ Переменные psql (:'x') НЕ подставляются внутри DO $$ … $$, поэтому
-- условное создание/обновление роли идёт через \gexec, а не через DO.
-- Скрипт идемпотентен. Права на БУДУЩИЕ таблицы (следующие миграции)
-- выдаются через ALTER DEFAULT PRIVILEGES от имени владельца схемы —
-- того, кем запущен этот скрипт и кем идут миграции.

SELECT format('CREATE ROLE botapp LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'botapp')
\gexec

SELECT format('ALTER ROLE botapp WITH LOGIN PASSWORD %L', :'app_password')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'botapp')
\gexec

GRANT CONNECT ON DATABASE :"dbname" TO botapp;
GRANT USAGE ON SCHEMA public TO botapp;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO botapp;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO botapp;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO botapp;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO botapp;
