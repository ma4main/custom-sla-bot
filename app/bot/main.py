"""Точка входа процесса bot: long polling и приватные диалоги.

Классификация, алерты и рассылки — в процессе worker: медленный ИИ-провайдер
не должен тормозить приём апдейтов.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import structlog
from aiogram import Dispatcher, Router
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    MenuButtonCommands,
)

from app.bot.handlers import (
    alerts_ui,
    chat_lifecycle,
    chats,
    fallback,
    health,
    help_ui,
    ingest,
    menu,
    reports,
    reports_lab,
    sender_ui,
    settings_ui,
    staff_ui,
    start,
    users,
)
from app.health import beat, start_watchdog
from app.bot.guard import create_bot
from app.bot.middlewares import AuthMiddleware, RawUpdateJournalMiddleware
from app.config import get_settings
from app.db.base import session_scope
from app.services.access import Perm
from app.services.tracking import backfill

# Раздел меню → право. Проверяется на каждом событии: старые кнопки
# после понижения роли не работают.
_PROTECTED: tuple[tuple[Router, str | None], ...] = (
    (menu.router, None),
    (chats.router, Perm.CHAT_MANAGE),
    (staff_ui.router, Perm.STAFF_MANAGE),
    (users.router, Perm.USER_MANAGE),
    (settings_ui.router, Perm.SYSTEM_SETTINGS),
    (reports.router, Perm.REPORT_SELF),
    (reports_lab.router, Perm.REPORT_ALL_CHATS),
    (health.router, Perm.SYSTEM_HEALTH),
    (alerts_ui.router, Perm.REPORT_CHAT),
    # Менять разметку в карточке «Кто это?» может только ATTRIBUTION_ASSIGN,
    # без него карточка работает на чтение.
    (sender_ui.router, Perm.REPORT_CHAT),
    # Без отдельного права, но AuthMiddleware требует подтверждённую учётку.
    (help_ui.router, None),
)


def setup_logging(level: str) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            # Без него exc_info=True не печатает трейсбек.
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()

    # Журнал апдейтов — до обработчиков: сырьё сохраняется, даже если обработчик упадёт.
    dispatcher.update.outer_middleware(RawUpdateJournalMiddleware())

    for router, perm in _PROTECTED:
        auth = AuthMiddleware(required_perm=perm)
        router.message.middleware(auth)
        router.callback_query.middleware(auth)

    # Порядок важен: жизненный цикл чатов и /start идут до защищённых разделов,
    # приём групповых сообщений — последним, он самый широкий.
    dispatcher.include_router(chat_lifecycle.router)
    dispatcher.include_router(start.router)
    for router, _ in _PROTECTED:
        dispatcher.include_router(router)
    # Страховка для приватных сообщений вне сценариев: после разделов меню
    # (иначе перехватит ввод форм) и до приёма групповых.
    dispatcher.include_router(fallback.router)
    dispatcher.include_router(ingest.router)

    return dispatcher


async def setup_menu_button(bot, log) -> None:
    """Кнопка «Меню» и команды — только в приватных чатах. Для групп команды
    стираются: там бот невидимый читатель (docs/SCREENS.md, раздел 0)."""
    commands = [
        BotCommand(command="menu", description="📊 Главное меню"),
        BotCommand(command="start", description="Начать заново"),
    ]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
        await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        log.info("bot.menu_button_ready", commands=[c.command for c in commands])
    except Exception:  # noqa: BLE001 — без кнопки бот работает, падать незачем
        log.exception("bot.menu_button_failed")


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    log = structlog.get_logger("bot")

    bot = create_bot(settings.require_bot_token())
    dispatcher = build_dispatcher()

    me = await bot.get_me()
    log.info(
        "bot.starting",
        username=me.username,
        bot_id=me.id,
        environment=settings.environment,
        allowed_updates=settings.allowed_updates_list,
        integrator_bot_id=settings.integrator_bot_id,
        bootstrap_owner=settings.bootstrap_owner_tg_id,
    )

    if settings.integrator_bot_id is None:
        log.warning(
            "integrator_bot_id.missing",
            hint="Сообщения из Битрикса не будут отнесены к компании. "
            "Определите from.id бота-интегратора и впишите в .env (INTEGRATOR_BOT_ID)",
        )

    await setup_menu_button(bot, log)

    async with session_scope() as session:
        # Переезд группы уведомлений до рестарта: новый номер берётся из настроек.
        from app.services.notify_group import load_migration

        await load_migration(session)
        # Чатам без истории наблюдения — интервал задним числом, иначе они
        # выпадут из отчётов.
        restored = await backfill(session)
    if restored:
        log.info("tracking.backfilled", chats=restored)

    # Отметку живости ставят два источника: каждый входящий апдейт
    # (RawUpdateJournalMiddleware) и проба get_me раз в минуту — на случай
    # тишины в чатах ночью и в выходные.
    beat("bot")
    heartbeat = asyncio.create_task(_heartbeat_loop(bot))
    # Сторож в отдельном потоке: зависший цикл событий asyncio-задача не разбудит
    # (см. app/health.py).
    start_watchdog("bot")

    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=settings.allowed_updates_list,
            handle_signals=True,
        )
    finally:
        heartbeat.cancel()
        await bot.session.close()


PROBE_INTERVAL_SECONDS = 60
PROBE_TIMEOUT_SECONDS = 30


async def _heartbeat_loop(bot) -> None:
    """Проба get_me; отметка живости ставится только при успехе.

    Провал отметку не обновляет: она протухает, и сторож выходит из процесса
    (предел 600 с при пробе раз в 60 с — около десяти неудач подряд).
    """
    while True:
        await asyncio.sleep(PROBE_INTERVAL_SECONDS)
        try:
            await asyncio.wait_for(bot.get_me(), timeout=PROBE_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 — проба не должна ронять бота сама
            structlog.get_logger("bot").warning(
                "bot.probe_failed", error=f"{type(exc).__name__}: {exc}"[:200]
            )
            continue
        beat("bot")


if __name__ == "__main__":
    asyncio.run(main())
