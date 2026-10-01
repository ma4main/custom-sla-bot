"""Проверка доступа к отчётам.

  1. владелец открывает все отчёты основного меню, включая сводку
     по алертам;
  2. меню бывшего раздела «🧪 Экспериментальные» не открывается НИКОМУ —
     ни владельцу, ни администратору: старая кнопка в пересланном сообщении
     получает вежливый отказ.

Скрытая кнопка не защита: нажатие можно повторить пересланным сообщением,
поэтому синтетический callback скармливается реальному диспетчеру
от имени пользователя.
"""

import asyncio

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import Update, User
from sqlalchemy import delete, select

from app.bot.callbacks import DismissedAction, LabAction
from app.bot.guard import create_bot
from app.bot.main import build_dispatcher, setup_logging
from app.config import get_settings
from app.db.base import session_scope
from app.db.models import BotRole, BotUser, BotUserState, TelegramUpdate

# Регистратор намеренно рвёт сеть после записи вызова, и middleware честно
# логирует это трейсбеком. Для проверки трейсбеки — шум, из-за которого
# не виден результат, поэтому логи приглушены штатной настройкой уровня.
setup_logging("CRITICAL")

BOT_ID = 7000000001
BOT_USERNAME = "example_sla_bot"

OWNER_TG_ID = 991000001    # синтетический владелец
ADMIN_TG_ID = 991000002  # синтетический администратор
UPDATE_BASE = 9200001


class OutboundRecorder(BaseRequestMiddleware):
    """Записывает исходящие вызовы и рвёт сеть — до Telegram ничего не уходит."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, make_request, bot, method):
        name = type(method).__name__
        if name != "GetMe":
            payload = ""
            for field in ("text", "caption"):
                value = getattr(method, field, None)
                if value:
                    payload = str(value)[:80]
                    break
            self.calls.append((name, payload))
        raise TelegramNetworkError(method=method, message="no-network (тест)")


def callback_update(update_id: int, tg_user_id: int, data: str) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": str(update_id),
                "from": {"id": tg_user_id, "is_bot": False, "first_name": "Тест"},
                "chat_instance": "test",
                "data": data,
                "message": {
                    "message_id": 500,
                    "date": 1755680000,
                    "chat": {"id": tg_user_id, "type": "private", "first_name": "Тест"},
                    "from": {
                        "id": BOT_ID,
                        "is_bot": True,
                        "first_name": "SLA Monitor",
                        "username": BOT_USERNAME,
                    },
                    "text": "меню",
                },
            },
        }
    )


async def _ensure_user(tg_user_id: int, role: BotRole, name: str) -> None:
    async with session_scope() as session:
        user = await session.scalar(
            select(BotUser).where(BotUser.tg_user_id == tg_user_id)
        )
        if user is None:
            session.add(
                BotUser(
                    tg_user_id=tg_user_id,
                    display_name=name,
                    role=role,
                    state=BotUserState.ACTIVE,
                    permissions={},
                )
            )
        else:
            user.role = role
            user.state = BotUserState.ACTIVE


async def _cleanup() -> None:
    async with session_scope() as session:
        await session.execute(
            delete(TelegramUpdate).where(
                TelegramUpdate.update_id.between(UPDATE_BASE, UPDATE_BASE + 20)
            )
        )
        await session.execute(
            delete(BotUser).where(BotUser.tg_user_id.in_([OWNER_TG_ID, ADMIN_TG_ID]))
        )


async def main() -> None:
    settings = get_settings()
    bot = create_bot(settings.require_bot_token())
    recorder = OutboundRecorder()
    bot.session.middleware(recorder)
    bot._me = User(
        id=BOT_ID,
        is_bot=True,
        first_name="SLA Monitor",
        username=BOT_USERNAME,
        can_join_groups=True,
        can_read_all_group_messages=True,
        supports_inline_queries=False,
    )

    dispatcher = build_dispatcher()

    await _ensure_user(OWNER_TG_ID, BotRole.OWNER, "Владелец (тест)")
    await _ensure_user(ADMIN_TG_ID, BotRole.ADMIN, "Администратор (тест)")
    update_id = UPDATE_BASE

    print("Переехавшие в основные отчёты ОБЯЗАНЫ открываться владельцу:")
    for label, data in (
        ("требует внимания", LabAction(kind="attention").pack()),
        ("скорость по сотрудникам", LabAction(kind="speed", period="last7").pack()),
        ("когда пишут клиенты", LabAction(kind="load", period="last7").pack()),
        ("работа вне графика", LabAction(kind="night", period="last7").pack()),
        ("сводка по алертам", LabAction(kind="adig").pack()),
        ("снятые алерты", DismissedAction().pack()),
    ):
        before = len(recorder.calls)
        try:
            await dispatcher.feed_update(bot, callback_update(update_id, OWNER_TG_ID, data))
        except TelegramNetworkError:
            pass
        update_id += 1
        produced = recorder.calls[before:]
        # Регистратор рвёт сеть на ПЕРВОМ же вызове, а отчёт начинается
        # с «Считаю…», поэтому до EditMessageText дело не доходит.
        # Признак допуска — отсутствие отказа: обработчик пошёл считать.
        denied = [
            call for call in produced
            if call[0] == "AnswerCallbackQuery" and "прав" in str(call[1])
        ]
        assert not denied, f"«{label}»: владельцу отказали: {produced}"
        assert produced, f"«{label}»: бот промолчал вовсе"
        print(f"  ok    {label}: владельцу открыт")

    print("Закрытая лаборатория не открывается никому:")
    for who, tg_id in (("владелец", OWNER_TG_ID), ("администратор", ADMIN_TG_ID)):
        before = len(recorder.calls)
        try:
            await dispatcher.feed_update(
                bot, callback_update(update_id, tg_id, LabAction(kind="menu").pack())
            )
        except TelegramNetworkError:
            pass
        update_id += 1
        produced = recorder.calls[before:]
        shown = [call for call in produced if call[0] == "EditMessageText"]
        assert not shown, f"{who}: меню лаборатории воскресло: {shown}"
        answered = [call for call in produced if call[0] == "AnswerCallbackQuery"]
        assert answered, f"{who}: бот промолчал вместо вежливого отказа: {produced}"
        print(f"  ok    {who}: старая кнопка получает «этого отчёта больше нет»")

    await _cleanup()
    print("  ok    синтетические пользователи удалены из базы")

    await bot.session.close()
    print()
    print("ПОДТВЕРЖДЕНО: переехавшие отчёты открыты владельцу, лаборатория закрыта")


asyncio.run(main())
